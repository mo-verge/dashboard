#!/usr/bin/env python3
"""Briefly register and remove a dummy GATT service so BlueZ sends a
Service Changed indication to connected hosts (makes them re-discover)."""
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

sys.path.insert(0, sys.path[0])
from bt_remote import Application, Characteristic, Service  # noqa: E402

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()


class DummyApp(Application):
    def __init__(self, bus):
        dbus.service.Object.__init__(self, bus, "/monet/poke")
        self.path, self.services = "/monet/poke", []


app = DummyApp(bus)
svc = Service(bus, 0, "12345678-1234-5678-1234-56789abcdef0")
svc.path = "/monet/poke/service0"
dbus.service.Object.remove_from_connection(svc)
dbus.service.Object.__init__(svc, bus, svc.path)
Characteristic(bus, svc, 0, "12345678-1234-5678-1234-56789abcdef1", ["read"], b"\x00")
app.services = [svc]

mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.GattManager1")
loop = GLib.MainLoop()
mgr.RegisterApplication("/monet/poke", {}, reply_handler=lambda: print("dummy added"),
                        error_handler=lambda e: (print("add failed", e), loop.quit()))
GLib.timeout_add(3000, lambda: (mgr.UnregisterApplication("/monet/poke"), print("dummy removed"), loop.quit()))
loop.run()
