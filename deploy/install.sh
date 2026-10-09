#!/usr/bin/env bash
# One-time (idempotent) boot setup on the Pi. Run from ~/dashboard:
#   deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")"

# Box keys go through the USB keyboard (firmware/usb_keys), reached over BLE by usb-keys.
# The fake Bluetooth remote (bt-remote, bt-identity) is retired: masked so nothing can
# start it, even as a dependency. Its code and units stay in the repo for reference.
sudo install -m 644 dashboard.service key-relay.service usb-keys.service /etc/systemd/system/
sudo rm -f /etc/systemd/system/bt-remote.service /etc/systemd/system/bt-identity.service
sudo systemctl daemon-reload
sudo systemctl mask bt-remote.service bt-identity.service
sudo systemctl enable dashboard.service key-relay.service usb-keys.service

# Kiosk at desktop login. A user autostart replaces the system one in labwc,
# so keep the stock Raspberry Pi OS entries and add ours.
mkdir -p ~/.config/labwc
{
  cat /etc/xdg/labwc/autostart
  echo "$HOME/dashboard/scripts/kiosk.sh &"
} > ~/.config/labwc/autostart
echo "installed; reboot to apply"
