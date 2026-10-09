#!/usr/bin/env python3
"""Make the Pi a Bluetooth LE remote (HID over GATT) for the TVIP box.

The TVIP box only pairs with its own remote, so the Pi advertises exactly like
it: name "TVIP Bluetooth RC", appearance 0x0180 (remote control), HID service.
Pair once from the box's Bluetooth-remote screen, then send
keys locally:

    curl -X POST http://127.0.0.1:8179/key/up
    curl -X POST http://127.0.0.1:8179/key/ok
    curl http://127.0.0.1:8179/status

Two input reports: a boot-style keyboard (arrows, Enter, Esc=Back, digits)
and a consumer control (Home, volume, channel, media keys). Android maps
both to remote-control key events.
"""
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

NAME = "TVIP Bluetooth RC"   # what the real remote advertises

# Byte-for-byte copy of the TVIP remote's pairing-mode advertisement (captured
# with btmon). The box keys on the Realtek manufacturer data, not just the name:
#   ADV_IND  flags 0x05 (LE limited discoverable) + manufacturer 0x005D 0300010a000000000000
#   SCAN_RSP appearance 0x0180 (remote control) + complete name
ADV_DATA = "0dff5d000300010a000000000000"
SCAN_RSP = "03198001" + f"{len(NAME) + 1:02x}09" + NAME.encode().hex()
# The remote advertises every ~50-56 ms; the kernel default is 1.28 s, which the
# box's short scan windows mostly miss. Units of 0.625 ms.
ADV_INTERVAL = (80, 88)
DEBUGFS = "/sys/kernel/debug/bluetooth/hci0"
PORT = 8179
BLUEZ = "org.bluez"
GATT_MANAGER = "org.bluez.GattManager1"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
GATT_DESC = "org.bluez.GattDescriptor1"
AGENT_PATH = "/monet/agent"
APP_PATH = "/monet/hid"

# Report 1: keyboard (modifiers, reserved, 6 keys). Report 2: consumer (one 16-bit usage).
REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
])

from remote_keys import CONSUMER, KEYBOARD  # noqa: E402  (one key table for USB + Bluetooth)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- GATT plumbing

class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = APP_PATH
        self.services = []
        super().__init__(bus, self.path)

    @dbus.service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        out = {}
        for s in self.services:
            out[s.get_path()] = s.get_properties()
            for c in s.characteristics:
                out[c.get_path()] = c.get_properties()
                for d in c.descriptors:
                    out[d.get_path()] = d.get_properties()
        return out


class Service(dbus.service.Object):
    def __init__(self, bus, index, uuid):
        self.path = f"{APP_PATH}/service{index}"
        self.uuid, self.characteristics = uuid, []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {GATT_SERVICE: {"UUID": self.uuid, "Primary": True,
                               "Characteristics": dbus.Array([c.get_path() for c in self.characteristics], "o")}}


class Characteristic(dbus.service.Object):
    def __init__(self, bus, service, index, uuid, flags, value=b""):
        self.path = f"{service.path}/char{index}"
        self.uuid, self.flags, self.service = uuid, flags, service
        self.value, self.descriptors, self.notifying = bytes(value), [], False
        super().__init__(bus, self.path)
        service.characteristics.append(self)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {GATT_CHRC: {"Service": self.service.get_path(), "UUID": self.uuid, "Flags": self.flags,
                            "Descriptors": dbus.Array([d.get_path() for d in self.descriptors], "o")}}

    @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return dbus.Array(self.value, "y")

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        self.value = bytes(value)

    @dbus.service.method(GATT_CHRC)
    def StartNotify(self):
        self.notifying = True
        log(f"host subscribed to {self.uuid[4:8]} {self.path[-12:]}")

    @dbus.service.method(GATT_CHRC)
    def StopNotify(self):
        self.notifying = False

    @dbus.service.signal(DBUS_PROP, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def notify(self, value):
        # Always signal, even without StartNotify: a bonded host that reconnects
        # (e.g. after a Pi reboot) doesn't re-subscribe; BlueZ keeps its stored
        # CCCD and forwards the notification if it is enabled.
        self.value = bytes(value)
        self.PropertiesChanged(GATT_CHRC, {"Value": dbus.Array(self.value, "y")}, [])


class Descriptor(dbus.service.Object):
    def __init__(self, bus, chrc, index, uuid, flags, value):
        self.path = f"{chrc.path}/desc{index}"
        self.uuid, self.flags, self.chrc, self.value = uuid, flags, chrc, bytes(value)
        super().__init__(bus, self.path)
        chrc.descriptors.append(self)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {GATT_DESC: {"Characteristic": self.chrc.get_path(), "UUID": self.uuid, "Flags": self.flags}}

    @dbus.service.method(GATT_DESC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return dbus.Array(self.value, "y")


class Agent(dbus.service.Object):
    """NoInputNoOutput agent: accept 'just works' pairing from the box."""

    @dbus.service.method("org.bluez.Agent1", in_signature="os")
    def AuthorizeService(self, device, uuid):
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="ou")
    def RequestConfirmation(self, device, passkey):
        log("pairing confirmed with", device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o")
    def RequestAuthorization(self, device):
        return

    @dbus.service.method("org.bluez.Agent1")
    def Cancel(self):
        pass

    @dbus.service.method("org.bluez.Agent1")
    def Release(self):
        pass


# ---------------------------------------------------------------- the remote

class Remote:
    def __init__(self, bus):
        self.bus = bus
        self.app = Application(bus)
        enc = ["read", "encrypt-read"]

        dis = Service(bus, 0, "180a")
        Characteristic(bus, dis, 0, "2a29", ["read"], b"Monet")
        # PnP ID: source USB-IF, vendor, product, version
        Characteristic(bus, dis, 1, "2a50", ["read"], bytes([0x02, 0x6B, 0x1D, 0x46, 0x02, 0x00, 0x01]))

        bat = Service(bus, 1, "180f")
        Characteristic(bus, bat, 0, "2a19", ["read", "notify"], bytes([100]))

        hid = Service(bus, 2, "1812")
        Characteristic(bus, hid, 0, "2a4a", ["read"], bytes([0x11, 0x01, 0x00, 0x02]))   # HID info
        Characteristic(bus, hid, 1, "2a4b", enc, REPORT_MAP)                              # report map
        Characteristic(bus, hid, 2, "2a4c", ["write-without-response"], b"\x00")          # control point
        Characteristic(bus, hid, 3, "2a4e", ["read", "write-without-response"], b"\x01")  # report mode
        self.kbd = Characteristic(bus, hid, 4, "2a4d", enc + ["notify"], bytes(8))
        Descriptor(bus, self.kbd, 0, "2908", ["read"], bytes([0x01, 0x01]))              # report id 1, input
        self.cons = Characteristic(bus, hid, 5, "2a4d", enc + ["notify"], bytes(2))
        Descriptor(bus, self.cons, 0, "2908", ["read"], bytes([0x02, 0x01]))             # report id 2, input

        self.app.services = [dis, bat, hid]
        self.agent = Agent(bus, AGENT_PATH)
        self.connected = False

    def start(self):
        adapter_path = "/org/bluez/hci0"
        adapter = self.bus.get_object(BLUEZ, adapter_path)
        props = dbus.Interface(adapter, DBUS_PROP)
        props.Set("org.bluez.Adapter1", "Alias", NAME)
        props.Set("org.bluez.Adapter1", "Pairable", True)
        props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))

        mgr = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
        mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        mgr.RequestDefaultAgent(AGENT_PATH)

        ok = lambda what: lambda *a: log(what, "registered")
        err = lambda what: lambda e: log(what, "FAILED:", e)
        dbus.Interface(adapter, GATT_MANAGER).RegisterApplication(
            APP_PATH, {}, reply_handler=ok("GATT app"), error_handler=err("GATT app"))
        # BlueZ's D-Bus advertising API can't produce limited-discoverable flags with
        # this exact layout, so set the raw advertisement through the mgmt API.
        subprocess.run(["sudo", "-n", "btmgmt", "clr-adv"], capture_output=True)
        for name, units in zip(("adv_min_interval", "adv_max_interval"), ADV_INTERVAL):
            subprocess.run(["sudo", "-n", "tee", f"{DEBUGFS}/{name}"], input=str(units), text=True,
                           capture_output=True)
        r = subprocess.run(["sudo", "-n", "btmgmt", "add-adv", "-c", "-l",
                            "-d", ADV_DATA, "-s", SCAN_RSP, "1"], capture_output=True, text=True)
        log("advertisement", "registered" if r.returncode == 0 else f"FAILED: {r.stdout}{r.stderr}".strip())

        self.bus.add_signal_receiver(self._on_props, dbus_interface=DBUS_PROP, signal_name="PropertiesChanged",
                                     arg0="org.bluez.Device1", path_keyword="path")

    def _on_props(self, interface, changed, invalidated, path=None):
        if "Connected" in changed:
            self.connected = bool(changed["Connected"])
            log("host", "connected" if self.connected else "disconnected", path)
        if "Paired" in changed:
            log("paired" if changed["Paired"] else "unpaired", path)

    def press(self, name, hold=0.06):
        """Called on the GLib thread: key down, then key up after `hold` seconds."""
        if name in KEYBOARD:
            self.kbd.notify(bytes([0, 0, KEYBOARD[name], 0, 0, 0, 0, 0]))
            GLib.timeout_add(int(hold * 1000), lambda: self.kbd.notify(bytes(8)) and False)
        elif name in CONSUMER:
            self.cons.notify(CONSUMER[name].to_bytes(2, "little"))
            GLib.timeout_add(int(hold * 1000), lambda: self.cons.notify(bytes(2)) and False)
        return False


def serve_http(remote):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/status":
                return self._reply(200, {"connected": remote.connected,
                                         "subscribed": remote.kbd.notifying or remote.cons.notifying,
                                         "keys": sorted(KEYBOARD) + sorted(CONSUMER)})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/key/"):
                return self._reply(404, {"error": "not found"})
            name = self.path[len("/key/"):]
            if name not in KEYBOARD and name not in CONSUMER:
                return self._reply(400, {"error": f"unknown key {name}"})
            GLib.idle_add(remote.press, name)
            self._reply(200, {"sent": name, "connected": remote.connected})

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    remote = Remote(bus)
    remote.start()
    threading.Thread(target=serve_http, args=(remote,), daemon=True).start()
    log(f"advertising as {NAME!r}; keys on http://127.0.0.1:{PORT}/key/<name>")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
