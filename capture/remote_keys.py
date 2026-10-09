"""Box key presses through the USB keyboard (ESP32-S3 in the box's USB port).
Used by box.py (inventory tools) and key_relay.py.

The fake Bluetooth remote (bt_remote.py) is retired: its services are masked on the Pi
and nothing here falls back to it (2026-10-08). The code stays for reference.

The ESP32 (firmware/usb_keys) only presses raw HID codes; the names live here and
are the same for both paths:
  KEYBOARD  HID keyboard usages (arrows, OK, digits ...)
  CONSUMER  HID consumer usages (Back, Guide, channel +/- ...)

The Pi reaches the board over Bluetooth LE through capture/usb_keys_ble.py
(127.0.0.1:8176); the board checks the relay token, set on it by capture/usb_keys_setup.py.
"""
import json
import os
import urllib.error
import urllib.request

KEYBOARD = {  # HID keyboard usages
    "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F, "ok": 0x28, "enter": 0x28,
    "esc": 0x29, "menu": 0x76, "tab": 0x2B, "space": 0x2C,
    # colour buttons: what the original TVIP remote sends (captured with btmon). The box only
    # treats them as colours when the board uses the remote's USB identity (ident "remote").
    "red": 0x3A, "green": 0x3B, "yellow": 0x3C, "blue": 0x3D,
    **{str(d): 0x1E + d - 1 for d in range(1, 10)}, "0": 0x27,
}
CONSUMER = {  # HID consumer usages
    "home": 0x0223, "vol_up": 0x00E9, "vol_down": 0x00EA, "mute": 0x00E2,
    "ch_up": 0x009C, "ch_down": 0x009D, "play_pause": 0x00CD, "stop": 0x00B7,
    "next": 0x00B5, "prev": 0x00B6, "ff": 0x00B3, "rew": 0x00B4, "guide": 0x008D,
    "back": 0x0224, "power": 0x0030,  # TVIP launcher ignores Esc; AC Back works
}

USB_KEYS = "http://127.0.0.1:8176"           # capture/usb_keys_ble.py (BLE link to the board)


def _usb(kind, code, hold_ms):
    req = urllib.request.Request(f"{USB_KEYS}/press?kind={kind}&code={code}&hold={hold_ms}", method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def press(name, hold_ms=60):
    """Press one named key. Returns {"sent": name, "via": "usb"}; raises OSError if the
    USB keyboard can't be reached (no fallback)."""
    if name in KEYBOARD:
        kind, code = "k", KEYBOARD[name]
    elif name in CONSUMER:
        kind, code = "c", CONSUMER[name]
    else:
        raise ValueError(f"unknown key {name!r}")
    _usb(kind, code, hold_ms)
    return {"sent": name, "via": "usb"}


def ident(mode):
    """USB identity of the board: "remote" (colour buttons) or "keyboard" (letters).
    The board restarts to apply it; this waits until it's back (up to ~20 s)."""
    import time
    if usb_status().get("board", {}).get("ident") == mode:
        return mode
    req = urllib.request.Request(f"{USB_KEYS}/config?cmd=ident%20{mode}", method="POST")
    urllib.request.urlopen(req, timeout=5).close()
    for _ in range(40):
        time.sleep(0.5)
        try:
            st = usb_status()
            if st.get("connected") and st.get("board", {}).get("ident") == mode and st["board"].get("usb"):
                return mode
        except OSError:
            pass
    raise OSError(f"board didn't come back as {mode}")


def usb_status():
    with urllib.request.urlopen(f"{USB_KEYS}/status", timeout=3) as r:
        return json.load(r)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["status"]:
        print(usb_status())
    else:
        for k in sys.argv[1:]:
            print(press(k))
