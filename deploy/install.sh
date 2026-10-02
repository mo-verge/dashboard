#!/usr/bin/env bash
# One-time (idempotent) boot setup on the Pi. Run from ~/dashboard:
#   deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")"

# LE-only like the real remote. bluetoothd re-enables BR/EDR whenever the
# adapter re-registers (e.g. after bt-identity changes its address) unless told
# not to. Side effect: no classic Bluetooth (speakers/headsets) on this Pi.
sudo sed -i -E 's/^#?ControllerMode *=.*/ControllerMode = le/' /etc/bluetooth/main.conf

sudo install -m 644 bt-identity.service bt-remote.service dashboard.service key-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bt-identity.service bt-remote.service dashboard.service key-relay.service

# Kiosk at desktop login. A user autostart replaces the system one in labwc,
# so keep the stock Raspberry Pi OS entries and add ours.
mkdir -p ~/.config/labwc
{
  cat /etc/xdg/labwc/autostart
  echo "$HOME/dashboard/scripts/kiosk.sh &"
} > ~/.config/labwc/autostart
echo "installed; reboot to apply"
