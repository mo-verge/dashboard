#!/usr/bin/env bash
# Install / update Monet TV on the Chromecast with Google TV from the Pi
# (adb on the Mac is blocked by macOS Local Network privacy).
#
#   deploy/tv-install.sh /tmp/monet-tv.apk
#
# Wireless debugging must be on (Settings > System > Developer options). Its
# connect port changes whenever the TV restarts or toggles it, so reuse the
# last one and rescan 30000-49999 if it no longer answers. If the pairing was
# lost: adb pair 192.168.50.79:<pair-port> <code> first.
set -euo pipefail
APK="${1:?apk path}"
HOST="${TV_HOST:-192.168.50.79}"
PORT_FILE="$HOME/.config/dashboard/chromecast-adb-port"

online() { adb devices | grep -q "^$HOST:$1[[:space:]]*device"; }

port="$(cat "$PORT_FILE" 2>/dev/null || true)"
if [ -n "$port" ]; then timeout 8 adb connect "$HOST:$port" >/dev/null 2>&1 || true; fi
if [ -z "$port" ] || ! online "$port"; then
  adb disconnect >/dev/null 2>&1 || true
  for p in $(python3 - "$HOST" <<'EOF'
import socket, sys, concurrent.futures as cf
host = sys.argv[1]
def probe(p):
    s = socket.socket(); s.settimeout(0.4)
    try:
        s.connect((host, p)); return p
    except OSError:
        return None
    finally:
        s.close()
with cf.ThreadPoolExecutor(400) as ex:
    print(" ".join(str(p) for p in sorted(p for p in ex.map(probe, range(30000, 50000)) if p)))
EOF
  ); do
    timeout 8 adb connect "$HOST:$p" >/dev/null 2>&1 || true
    sleep 1
    if online "$p"; then port=$p; echo "$p" > "$PORT_FILE"; break; fi
    adb disconnect "$HOST:$p" >/dev/null 2>&1 || true
  done
fi
online "${port:-0}" || { echo "Chromecast not reachable over adb (wireless debugging off or pairing lost)" >&2; exit 1; }

D="$HOST:$port"
adb -s "$D" install -r "$APK"
adb -s "$D" shell am force-stop tv.monet.app
adb -s "$D" shell am start -n tv.monet.app/.MainActivity >/dev/null
echo "installed $(adb -s "$D" shell dumpsys package tv.monet.app | grep -m1 versionName | xargs) on $D"
