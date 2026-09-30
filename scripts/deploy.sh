#!/usr/bin/env bash
# Copy the repo to the Pi and restart the kiosk. Usage: scripts/deploy.sh [ssh-host]
set -euo pipefail
HOST="${1:-monet-wifi-2}"
cd "$(dirname "$0")/.."

rsync -az --delete --exclude .git --exclude web/media ./ "$HOST:dashboard/"
ssh -T "$HOST" '
  cd ~/dashboard
  [ -f web/media/flow.mp4 ] || scripts/make-media.sh
  sudo -n systemctl restart dashboard.service
  scripts/kiosk.sh
'
