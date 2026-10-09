"""Box key presses: the USB keyboard (ESP32-S3 in the box's USB port) first, the fake
Bluetooth remote as fallback. Used by box.py (inventory tools) and key_relay.py.

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
    **{str(d): 0x1E + d - 1 for d in range(1, 10)}, "0": 0x27,
}
CONSUMER = {  # HID consumer usages
    "home": 0x0223, "vol_up": 0x00E9, "vol_down": 0x00EA, "mute": 0x00E2,
    "ch_up": 0x009C, "ch_down": 0x009D, "play_pause": 0x00CD, "stop": 0x00B7,
    "next": 0x00B5, "prev": 0x00B6, "ff": 0x00B3, "rew": 0x00B4, "guide": 0x008D,
    "back": 0x0224, "power": 0x0030,  # TVIP launcher ignores Esc; AC Back works
}

BLUETOOTH = "http://127.0.0.1:8179"          # capture/bt_remote.py (fake remote, fallback)
USB_KEYS = "http://127.0.0.1:8176"           # capture/usb_keys_ble.py (BLE link to the board)


def _usb(kind, code, hold_ms):
    req = urllib.request.Request(f"{USB_KEYS}/press?kind={kind}&code={code}&hold={hold_ms}", method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def press(name, hold_ms=60):
    """Press one named key. Returns {"sent": name, "via": "usb" | "bluetooth"}."""
    if name in KEYBOARD:
        kind, code = "k", KEYBOARD[name]
    elif name in CONSUMER:
        kind, code = "c", CONSUMER[name]
    else:
        raise ValueError(f"unknown key {name!r}")
    try:
        _usb(kind, code, hold_ms)
        return {"sent": name, "via": "usb"}
    except OSError:                          # board off / not in the box / link down
        pass
    req = urllib.request.Request(f"{BLUETOOTH}/key/{name}", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        json.load(r)
    return {"sent": name, "via": "bluetooth"}


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
