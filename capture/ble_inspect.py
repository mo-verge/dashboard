#!/usr/bin/env python3
"""Connect to a BLE device and dump its GATT table plus readable public values.

Reads only the GAP / Device Information / Battery / HID-information values that
are normally open, so it does not trigger pairing (which would steal a remote
from the box it is bonded to).
    python3 capture/ble_inspect.py 00:E0:4C:78:73:29
"""
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

ADDR = sys.argv[1]
# Characteristics safe to read without encryption.
SAFE = {"2a00": "device name", "2a01": "appearance", "2a04": "conn params", "2a29": "manufacturer",
        "2a24": "model", "2a25": "serial", "2a26": "firmware", "2a27": "hardware", "2a28": "software",
        "2a50": "PnP ID", "2a19": "battery", "2a4a": "HID information"}

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
path = "/org/bluez/hci0/dev_" + ADDR.replace(":", "_")
# BlueZ forgets unpaired devices when discovery stops: scan until it shows up.
adapter = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.Adapter1")
om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
if path in om.GetManagedObjects():  # drop a stale cached entry so we wait for a live advert
    adapter.RemoveDevice(path)
adapter.SetDiscoveryFilter({"Transport": "le"})
adapter.StartDiscovery()
print("waiting for", ADDR, "(put it in pairing mode)…", flush=True)
for _ in range(720):
    if path in om.GetManagedObjects():
        break
    time.sleep(0.25)
else:
    sys.exit("not seen")
adapter.StopDiscovery()
dev = bus.get_object("org.bluez", path)
props = dbus.Interface(dev, "org.freedesktop.DBus.Properties")

print("connecting…", flush=True)
dbus.Interface(dev, "org.bluez.Device1").Connect()
for _ in range(60):
    if props.Get("org.bluez.Device1", "ServicesResolved"):
        break
    time.sleep(0.25)

objs = om.GetManagedObjects()
for p in sorted(k for k in objs if k.startswith(path + "/")):
    ifs = objs[p]
    if "org.bluez.GattService1" in ifs:
        print(f"\nservice {str(ifs['org.bluez.GattService1']['UUID'])[4:8]}  {p[len(path):]}")
    elif "org.bluez.GattCharacteristic1" in ifs:
        c = ifs["org.bluez.GattCharacteristic1"]
        u = str(c["UUID"])[4:8]
        line = f"  char {u} {[str(f) for f in c['Flags']]}"
        if u in SAFE and "read" in c["Flags"]:
            try:
                v = bytes(dbus.Interface(bus.get_object("org.bluez", p), "org.bluez.GattCharacteristic1")
                          .ReadValue({}))
                txt = v.decode("utf-8", "replace") if u in ("2a00", "2a29", "2a24", "2a25", "2a26", "2a27", "2a28") else ""
                line += f"  {SAFE[u]} = {v.hex()} {txt!r}"
            except dbus.exceptions.DBusException as e:
                line += f"  ({e.get_dbus_name().split('.')[-1]})"
        print(line)
    elif "org.bluez.GattDescriptor1" in ifs:
        print(f"    desc {str(ifs['org.bluez.GattDescriptor1']['UUID'])[4:8]}")

dbus.Interface(dev, "org.bluez.Device1").Disconnect()
print("\ndisconnected")
