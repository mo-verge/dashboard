#!/usr/bin/env python3
"""Serves web/ plus the dashboard's JSON endpoints and Google sign-in.

The same page is the Monet TV app's home screen (Chromecast), the Pi's kiosk and a
page for laptops / phones on the home network. The Pi itself (loopback) needs no
token; anyone else opens /?t=<token> once (the relay token, ~/.config/dashboard/relay-token)
and gets a cookie. /api/frame.jpg is the live preview: the capture hub's latest frame,
scaled down.
"""
import html
import io
import json
import os
import secrets
import subprocess
import threading
import sys
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import health
import markets
import weather

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture"))
import tvctl  # noqa: E402  (TV switches: subtitles / Pi preview / cast)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
# All interfaces: the TV app and browsers on the LAN load it, behind the token.
BIND = os.environ.get("DASHBOARD_BIND", "0.0.0.0")
TOKEN_FILE = os.path.expanduser("~/.config/dashboard/relay-token")
COOKIE = "monet"
FRAME = "/dev/shm/box.jpg"      # capture hub's latest 1920x1080 frame (stream/capture-hub.sh)
FRAME_MAX_AGE = 5               # s: older means the hub is down
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def token():
    try:
        return open(TOKEN_FILE).read().strip()
    except FileNotFoundError:
        return None             # no token yet: LAN access stays closed


_frames = {}                    # width -> (source mtime_ns, jpeg bytes); shared by all viewers
_frames_lock = threading.Lock()


def frame_jpeg(width):
    """Latest capture frame scaled to `width` (JPEG draft mode decodes at 1/2-1/8 size: cheap)."""
    from PIL import Image
    st = os.stat(FRAME)
    if time.time() - st.st_mtime > FRAME_MAX_AGE:
        return None
    with _frames_lock:
        hit = _frames.get(width)
        if hit and hit[0] == st.st_mtime_ns:
            return hit[1]
        with Image.open(FRAME) as im:
            h = width * im.height // im.width
            im.draft("RGB", (width, h))
            im = im.convert("RGB")
            if im.width != width:          # draft sizes (1/2, 1/4 of 1920) need no resize
                im = im.resize((width, h), Image.BILINEAR)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=72)
        _frames[width] = (st.st_mtime_ns, buf.getvalue())
        return buf.getvalue()

_last_cpu = None


def cpu_pct():
    global _last_cpu
    with open("/proc/stat") as f:
        vals = list(map(int, f.readline().split()[1:]))
    idle, total = vals[3] + vals[4], sum(vals)
    prev, _last_cpu = _last_cpu, (idle, total)
    if not prev or total == prev[1]:
        return 0.0
    return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))


def system():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        temp = int(f.read()) / 1000
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.split()[0])
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    return {
        "temp_c": temp,
        "cpu_pct": cpu_pct(),
        "mem_pct": 100 * (1 - mem["MemAvailable"] / mem["MemTotal"]),
        "uptime_s": uptime,
        "ts": time.time(),
    }


PAGE = """<!doctype html><meta charset=utf-8><title>Dashboard</title>
<body style="font:18px system-ui;background:#06060a;color:#eee;display:grid;place-items:center;height:100vh;margin:0">
<div style="max-width:36rem;text-align:center"><h2>{title}</h2><p>{body}</p></div>"""


class Handler(SimpleHTTPRequestHandler):
    def _allowed(self, url, q):
        """Loopback is trusted; others need the token (cookie, X-Token header, or ?t= once)."""
        if self.client_address[0] in LOOPBACK:
            return True
        tok = token()
        if not tok:
            self._page("Not set up", "No access token on the Pi yet.", 403)
            return False
        given = q.get("t", [""])[0]
        if given and secrets.compare_digest(given, tok):
            # remember this device, then drop the token from the address bar
            rest = urllib.parse.urlencode({k: v for k, v in q.items() if k != "t"}, doseq=True)
            self.send_response(302)
            self.send_header("Set-Cookie", f"{COOKIE}={tok}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", url.path + ("?" + rest if rest else ""))
            self.end_headers()
            return False
        cookies = dict(c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c)
        for cand in (cookies.get(COOKIE, ""), self.headers.get("X-Token", "")):
            if cand and secrets.compare_digest(cand, tok):
                return True
        if url.path.startswith("/api/"):
            self._json({"error": "token required"}, 401)
        else:
            self._page("Monet", "Open the dashboard with its full link (the QR code) once on this device.", 401)
        return False

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _page(self, title, body, code=200):
        data = PAGE.format(title=html.escape(title), body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if not self._allowed(url, q):
            return

        if url.path == "/api/frame.mjpg":
            return self._mjpeg(q)
        if url.path == "/api/frame.jpg":
            try:
                width = max(160, min(1920, int(q.get("w", ["640"])[0])))
                data = frame_jpeg(width)
            except (OSError, ValueError):
                data = None
            if data is None:
                return self._json({"error": "no frame"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if url.path == "/api/system":
            return self._json(system())
        if url.path == "/api/steps":
            return self._json(health.steps_status())
        if url.path == "/api/btc":
            return self._json(markets.btc_status())
        if url.path == "/api/coins":
            return self._json(markets.coins_status())
        if url.path == "/api/version":   # newest web/ file: open pages reload when it changes
            newest = max(os.stat(os.path.join(ROOT, f)).st_mtime_ns for f in os.listdir(ROOT)
                         if os.path.isfile(os.path.join(ROOT, f)))
            return self._json({"version": str(newest)})
        if url.path == "/api/weather":
            return self._json(weather.weather_status())
        if url.path == "/api/tv":
            return self._json(tvctl.status())

        if url.path == "/auth":
            # Redirect back to whatever loopback host:port the browser used,
            # so it works both on the Pi and through an SSH tunnel.
            host = self.headers.get("Host", f"127.0.0.1:{PORT}")
            try:
                target = health.auth_url(f"http://{host}/oauth/callback")
            except health.NotConnected as e:
                return self._page("Not set up", html.escape(str(e)), 500)
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if url.path == "/oauth/callback":
            if "error" in q:
                return self._page("Sign-in cancelled", html.escape(q["error"][0]), 400)
            try:
                health.finish_auth(q.get("state", [""])[0], q.get("code", [""])[0])
            except (health.ApiError, health.NotConnected) as e:
                return self._page("Sign-in failed", html.escape(str(e)), 400)
            return self._page("Connected", "Google Health is linked. The dashboard will pick it up within a minute — you can close this tab.")

        super().do_GET()

    def _mjpeg(self, q):
        """Live preview as motion JPEG (multipart/x-mixed-replace): <img> plays it as video.
        Frames come from frame_jpeg's cache, so several viewers share the scaling work.
        Ends when the hub stops sending frames (the page then shows NO SIGNAL and retries)."""
        try:
            # 960 / 480 = what the JPEG decoder produces directly at 1/2 or 1/4 size: cheap
            width = 960 if int(q.get("w", ["960"])[0]) > 480 else 480
            fps = max(1, min(25, int(q.get("fps", ["15"])[0])))
        except ValueError:
            return self._json({"error": "bad w / fps"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last, stale_since = None, None
        try:
            while True:
                t0 = time.monotonic()
                try:
                    mtime = os.stat(FRAME).st_mtime_ns
                    data = frame_jpeg(width) if mtime != last else b""
                except OSError:
                    data = None
                if data is None:                                  # no fresh frame
                    stale_since = stale_since or t0
                    if t0 - stale_since > 3:
                        return
                else:
                    stale_since = None
                    if data:
                        last = mtime
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                        self.wfile.flush()
                time.sleep(max(0.0, 1 / fps - (time.monotonic() - t0)))
        except (BrokenPipeError, ConnectionResetError):
            return                                                # viewer left

    def do_POST(self):
        # POST /api/tv/<switch>/<on|off>
        url = urllib.parse.urlparse(self.path)
        if not self._allowed(url, {}):
            return
        parts = url.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tv"] and parts[2] in tvctl.SWITCHES and parts[3] in ("on", "off"):
            try:
                return self._json(tvctl.apply({parts[2]: parts[3] == "on"}))
            except (OSError, subprocess.SubprocessError) as e:
                return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)

    def end_headers(self):
        if not self.path.startswith("/media/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND, PORT), partial(Handler, directory=ROOT))
    print(f"dashboard on http://{BIND}:{PORT}")
    server.serve_forever()
