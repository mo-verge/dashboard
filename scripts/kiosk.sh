#!/usr/bin/env bash
# Start the dashboard server (if needed) and open it full screen in Chromium.
# Run on the Pi, from SSH or the desktop session.
set -euo pipefail
cd "$(dirname "$0")/.."

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
PORT="${DASHBOARD_PORT:-8080}"

if ! curl -fs "http://127.0.0.1:$PORT/api/system" >/dev/null; then
  nohup python3 server.py >/tmp/dashboard-server.log 2>&1 &
  for _ in $(seq 20); do curl -fs "http://127.0.0.1:$PORT/api/system" >/dev/null && break; sleep 0.25; done
fi

pkill -f -- "--user-data-dir=$HOME/.config/dashboard-kiosk" || true
sleep 1

# Separate profile so the kiosk never shows restore bubbles or the normal
# browser's tabs; basic password store avoids the keyring unlock prompt.
nohup chromium \
  --ozone-platform=wayland \
  --kiosk \
  --user-data-dir="$HOME/.config/dashboard-kiosk" \
  --autoplay-policy=no-user-gesture-required \
  --password-store=basic \
  --noerrdialogs \
  --disable-session-crashed-bubble \
  --no-first-run \
  --disable-features=Translate \
  "http://127.0.0.1:$PORT/" >/tmp/dashboard-kiosk.log 2>&1 &
echo "kiosk started on http://127.0.0.1:$PORT/"
