#!/usr/bin/env python3
"""Keeps a Bluetooth LE connection to the USB-keys board (firmware/usb_keys: an ESP32-S3
plugged into the TVIP box's USB port as a keyboard) and hands it key presses.

Both ends are ours, so a dropped link just reconnects; the box only sees a USB keyboard.
Local HTTP (127.0.0.1:8176), used by capture/remote_keys.py:

    POST /press?kind=k|c&code=<HID usage>&hold=<ms>   200, or 503 when the board isn't connected
    GET  /status                                       board status + link state

The board is found by its service UUID (address cached in ~/.config/dashboard/usb-keys-addr)
and only accepts presses carrying the relay token (~/.config/dashboard/relay-token).
Runs in ~/keys/venv (bleak).
"""
import asyncio
import json
import os
import time
from urllib.parse import parse_qs, urlparse

from bleak import BleakClient, BleakScanner

SVC = "6d6f6e65-7400-4b65-7973-000000000001"
PRESS = "6d6f6e65-7400-4b65-7973-000000000002"
STATUS = "6d6f6e65-7400-4b65-7973-000000000003"
PORT = int(os.environ.get("USB_KEYS_PORT", "8176"))
CONF = os.path.expanduser("~/.config/dashboard")
ADDR_FILE = os.path.join(CONF, "usb-keys-addr")
TOKEN = open(os.path.join(CONF, "relay-token")).read().strip()

client = None
connected_at = 0.0
write_lock = asyncio.Lock()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def board_address():
    try:
        return open(ADDR_FILE).read().strip()
    except FileNotFoundError:
        pass
    d = await BleakScanner.find_device_by_filter(lambda d, adv: SVC in adv.service_uuids, timeout=20)
    if d:
        with open(ADDR_FILE, "w") as f:
            f.write(d.address)
        log("found board", d.address)
        return d.address
    return None


async def keep_connected():
    global client, connected_at
    while True:
        try:
            addr = await board_address()
            if not addr:
                log("board not found; retrying")
                await asyncio.sleep(10)
                continue
            c = BleakClient(addr)
            await c.connect(timeout=20)
            client, connected_at = c, time.time()
            log("connected", addr)
            while c.is_connected:
                await asyncio.sleep(1)
            log("disconnected")
        except Exception as e:      # board off / out of range / BlueZ busy
            log("connect failed:", type(e).__name__, e)
        client = None
        await asyncio.sleep(3)


async def press(kind, code, hold):
    if not client or not client.is_connected:
        raise ConnectionError("board not connected")
    payload = kind.encode() + code.to_bytes(2, "little") + hold.to_bytes(2, "little") + TOKEN.encode()
    async with write_lock:          # keys go out in order
        await client.write_gatt_char(PRESS, payload, response=True)


async def handle(reader, writer):
    try:
        head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode(errors="replace")
        method, target = head.split(" ", 2)[:2]
        url = urlparse(target)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        code, body = 404, {"error": "not found"}
        if method == "POST" and url.path == "/press":
            try:
                kind = q["kind"]
                if kind not in ("k", "c"):
                    raise ValueError
                await press(kind, int(q["code"], 0), int(q.get("hold", "60")))
                code, body = 200, {"sent": True}
            except (KeyError, ValueError):
                code, body = 400, {"error": "kind=k|c&code=<usage>[&hold=ms]"}
            except Exception as e:
                code, body = 503, {"error": str(e)}
        elif method == "GET" and url.path == "/status":
            body = {"connected": bool(client and client.is_connected),
                    "since_s": int(time.time() - connected_at) if client else None}
            if body["connected"]:
                try:
                    body["board"] = json.loads(await client.read_gatt_char(STATUS))
                except Exception as e:
                    body["board_error"] = str(e)
            code = 200
        data = json.dumps(body).encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def main():
    asyncio.create_task(keep_connected())
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    log(f"usb keys bridge on 127.0.0.1:{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
