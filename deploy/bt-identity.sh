#!/bin/sh
# Give the Pi's Bluetooth controller the fake TVIP remote's identity once
# bluetoothd is up: a Realtek-prefixed public address (the box only pairs with
# 00:E0:4C:…) and LE-only, like the real remote. The kernel re-registers hci0
# with the new address and bluetoothd loads the box's bond stored under it.
#
# LE-only is enforced by ControllerMode = le in /etc/bluetooth/main.conf (see
# install.sh); bredr off here is only a fallback.
#
# Idempotent: each pass only does what is still missing (setting an address
# that is already set is rejected), always ends powered on, and retries for
# ~30 s in case the controller is still settling.
ADDR="${1:-00:E0:4C:78:73:30}"

# btmgmt exits as soon as stdin hits EOF, before the controller replies, so
# under systemd (stdin=/dev/null) it prints nothing. Keep stdin open briefly.
bm() { sleep 1 | btmgmt -i hci0 "$@" 2>/dev/null; }
settings() { bm info | grep "current settings"; }

for _ in $(seq 30); do
  if bm info | grep -q "addr $ADDR"; then has_addr=1; else has_addr=0; fi
  if settings | grep -q "br/edr"; then has_bredr=1; else has_bredr=0; fi

  if [ $has_addr = 1 ] && [ $has_bredr = 0 ] && settings | grep -q "powered"; then
    echo "bluetooth identity set: $ADDR, LE only"
    exit 0
  fi
  if [ $has_addr = 0 ] || [ $has_bredr = 1 ]; then
    bm power off >/dev/null
    [ $has_addr = 0 ] && bm public-addr "$ADDR" >/dev/null
    [ $has_bredr = 1 ] && bm bredr off >/dev/null
  fi
  bm power on >/dev/null
  sleep 1
done
echo "failed to set bluetooth identity" >&2
bm info >&2
exit 1
