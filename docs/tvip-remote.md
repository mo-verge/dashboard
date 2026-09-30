# Controlling the TVIP box from the Pi (fake Bluetooth remote)

The IPTV box is a **TVIP S-Box v.705** (Android 11, TVIP launcher, BT name `s7xx`,
address `22:22:C9:E6:2B:51`). Its HDMI output goes into the Pi through a MacroSilicon
MS2109 USB capture adapter (`/dev/video0` + ALSA card `MS2109`), so the Pi can both see
the box and — via this remote — drive it.

## Why Bluetooth

| Route | Result |
|---|---|
| HDMI-CEC | Not possible: the capture adapter does not carry CEC, and the Pi's HDMI ports are outputs. |
| `adb` over the network | Developer options / Android settings are locked by the TVIP launcher. |
| IR blaster | Would need hardware; kept as a fallback. |
| **Pi as a BLE HID remote** | **Works.** `capture/bt_remote.py` |

## What the box accepts

The launcher's Bluetooth-remote pairing only accepts its own remote. It ignored the Pi
until *all* of these matched the real remote, captured with `btmon` while the remote was
in pairing mode (`capture/ble_scan.py`, `capture/ble_inspect.py`):

| Property | Real remote | Pi |
|---|---|---|
| Address | `00:E0:4C:78:73:29` (public, Realtek OUI) | `00:E0:4C:78:73:30` (public) — **the last blocker** |
| Controller | LE only | LE only (`ControllerMode = le`) |
| ADV_IND | flags `0x05` (LE limited discoverable) + manufacturer `0x005D` (Realtek) `0300010a000000000000` | identical, raw via `btmgmt add-adv -c -l` |
| SCAN_RSP | appearance `0x0180` (remote control) + name `TVIP Bluetooth RC` | identical |
| Adv interval | ~50–56 ms | 50–55 ms (debugfs `adv_min/max_interval` = 80/88) |

The real remote's GATT table (read without pairing): Battery `180F`, Device Information
`180A` (all reads need pairing), HID `1812` (hidden by BlueZ's own HoG plugin), `1920`,
and two Realtek vendor services `D0FF` (`FFD1–FFF2`, OTA/config) and `6287`. The box did
not require any of those to pair; the Pi only exposes DIS, Battery and HID.

The Pi's BlueZ D-Bus advertising API cannot produce that exact layout (limited
discoverable flags, manufacturer data in ADV_IND, name in scan response), hence the raw
`btmgmt add-adv`.

## HID reports

`capture/bt_remote.py` registers a HID-over-GATT service with two input reports:
report 1 = boot keyboard, report 2 = one 16-bit consumer usage.

## Keys (tested 2026-09-30 against the launcher and the IPTV portal)

Send with `curl -X POST http://127.0.0.1:8179/key/<name>` or `capture/box.py press <name>`.

| Key | HID usage | Result |
|---|---|---|
| `up` `down` `left` `right` | kbd 0x52 0x51 0x50 0x4F | ✅ navigation everywhere |
| `ok` / `enter` | kbd 0x28 | ✅ select / play channel |
| `back` | consumer 0x0224 (AC Back) | ✅ back (keyboard Esc `esc` 0x29 is **ignored**) |
| `0`–`9` | kbd 0x1E–0x27 | ✅ direct channel number entry during playback |
| `ch_up` / `ch_down` | consumer 0x009C / 0x009D | ✅ in the channel list: move highlight down / up one channel |
| `guide` | consumer 0x008D | ✅ opens the full TV Guide (multi-day EPG with descriptions) |
| `play_pause` | consumer 0x00CD | ✅ on live TV: shows/hides the info banner (channel + now/next) |
| `vol_up` / `vol_down` / `mute` | consumer 0x00E9 / 0x00EA / 0x00E2 | ✅ box volume OSD / mute toggle |
| `home` | consumer 0x0223 | ⚠️ no visible effect (in YouTube or on the launcher itself) |
| `menu` | kbd 0x76 | ❌ no visible effect |
| `next` `prev` `ff` `rew` `stop` | consumer 0x00B5 0x00B6 0x00B3 0x00B4 0x00B7 | no effect on live TV (likely for VOD/recordings) |
| `power` | consumer 0x0030 | not tested (could put the box in standby) |

Launcher layout: top row **TVIP** (Watch TV, DVR, Mediaplayer, CCTV, Browser), then
Applications (YouTube, YouTube Kids, Plex, app store, Puffin TV Browser), then Settings.
**Watch TV** opens the IPTV portal (Media Browser / TV / Video Club / Radio; TV categories
list; channel list "TV / ALL / BY NUMBER", 11 219 channels on 802 pages).

## Boot persistence (`deploy/`)

`deploy/install.sh` installs:

* `bt-identity.service` → `deploy/bt-identity.sh`: after bluetoothd is up, set the public
  address `00:E0:4C:78:73:30` and LE-only through mgmt. The kernel re-registers hci0 and
  bluetoothd loads the box's bond from `/var/lib/bluetooth/00:E0:4C:78:73:30/`.
* `bt-remote.service`: the fake remote (restarts on failure).
* `/etc/bluetooth/main.conf` `ControllerMode = le`.

Traps hit on the way:

* Setting the address *before* bluetoothd, while the BCM4345C0 firmware patch was still
  loading, wedged mgmt (`Index list with 0 items`) until the next reboot.
* `btmgmt` exits on stdin EOF before the controller replies, so under systemd it prints
  nothing — pipe `sleep 1 |` into it.
* Setting an address that is already set is rejected; the script is idempotent.
* bluetoothd turns BR/EDR back on when the adapter re-registers unless `ControllerMode = le`.
* `pkill -f pattern` over SSH also matches the SSH shell's own command line.
* The Pi desktop's panel (wf-panel-pi) pops up "Connection successful"; killing it clears
  it (lwrespawn restarts the panel).

## Open issue: key subscription is lost when bluetoothd restarts

The box enables notifications (CCCD) on the two input reports **only when it pairs**. A
bonded host assumes the CCCD persists, and on reconnect it never writes it again — not
after a disconnect, not after Service Changed indications (it re-discovers, but does not
re-subscribe). BlueZ 5.66 keeps server-side CCCD state only in memory (it persists just
the Service Changed CCCD), so after a Pi reboot or a bluetoothd restart the box is
connected but every key notification is silently dropped.

Workaround today: remove the bond on the Pi (`bluetoothctl remove 22:22:C9:E6:2B:51`) and
re-pair from the box's Bluetooth-remote screen.

Planned fix: run the fake remote on Google's Bumble stack (HCI user channel) instead of
BlueZ, notify the bonded connection directly, and import the existing LTK from
`/var/lib/bluetooth/…/info` so no re-pair is needed.
