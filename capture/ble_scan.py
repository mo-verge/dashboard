#!/usr/bin/env python3
"""Passive BLE scan: log everything nearby devices advertise (no connecting).

Used to see how the TVIP remote presents itself so the Pi can mimic it.
    python3 capture/ble_scan.py [seconds]
"""
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
KEYS = ("Address", "AddressType", "Name", "Alias", "Appearance", "Class", "Icon", "RSSI", "TxPower",
        "UUIDs", "ManufacturerData", "ServiceData", "AdvertisingFlags", "Paired", "Bonded")


def plain(v):
    if isinstance(v, dbus.Array) and v.signature == "y":
        return bytes(v).hex()
    if isinstance(v, (dbus.Array, list)):
        return [plain(x) for x in v]
    if isinstance(v, dbus.Dictionary):
        return {str(plain(k)): plain(x) for k, x in v.items()}
    if isinstance(v, (dbus.UInt16, dbus.UInt32, dbus.Int16, dbus.Byte)):
        return int(v)
    return str(v) if isinstance(v, dbus.String) else v


seen = {}


def record(path, props):
    dev = seen.setdefault(str(path), {})
    new = {k: plain(props[k]) for k in KEYS if k in props}
    changed = {k: v for k, v in new.items() if dev.get(k) != v and k != "RSSI"}
    dev.update(new)
    if changed:
        print(time.strftime("%H:%M:%S"), dev.get("Address", path), changed, flush=True)


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
    for path, ifaces in om.GetManagedObjects().items():
        if "org.bluez.Device1" in ifaces:
            record(path, ifaces["org.bluez.Device1"])
    bus.add_signal_receiver(lambda p, i: "org.bluez.Device1" in i and record(p, i["org.bluez.Device1"]),
                            dbus_interface="org.freedesktop.DBus.ObjectManager", signal_name="InterfacesAdded")
    bus.add_signal_receiver(lambda iface, ch, inv, path=None: iface == "org.bluez.Device1" and record(path, ch),
                            dbus_interface="org.freedesktop.DBus.Properties", signal_name="PropertiesChanged",
                            path_keyword="path")
    adapter = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.Adapter1")
    adapter.SetDiscoveryFilter({"Transport": "le", "DuplicateData": True})
    adapter.StartDiscovery()
    print(f"scanning LE for {SECONDS}s…", flush=True)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(SECONDS, loop.quit)
    loop.run()
    adapter.StopDiscovery()
    print("\n== summary (named devices first)")
    for d in sorted(seen.values(), key=lambda d: ("Name" not in d, d.get("Address", ""))):
        print({k: d[k] for k in KEYS if k in d})


if __name__ == "__main__":
    main()
