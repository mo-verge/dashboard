#!/usr/bin/env python3
"""LAN-facing relay so the "Monet TV" Android TV app can drive the TVIP box.

The fake BLE remote (capture/bt_remote.py) only listens on 127.0.0.1:8179.
This relay listens on the LAN (default :8180), checks a shared token, and
forwards to it:

    POST /key/<name>        one remote key (up, down, left, right, ok, back, guide, ...)
    POST /tune/<number>     type a channel number (digits) on the box
    GET  /channels          shortlists from the inventory for the app's channel picker
    GET  /captions?since=ms live Whisper subtitles (capture/live_captions.py), wall-clock ms
    POST /caption-lang/<es|en|auto|off>   subtitle language (off = no delayed subtitle mode)
    GET  /health            no token needed

Token: ~/.config/dashboard/relay-token (created on first run); send it as the
X-Token header. Shortlists: data/tvip/{soccer,spanish,english}.json.
"""
import json
import os
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RELAY_PORT", "8180"))
REMOTE = "http://127.0.0.1:8179"
TOKEN_FILE = os.path.expanduser("~/.config/dashboard/relay-token")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tvip")
DIGIT_GAP = 0.45            # box needs a short pause between digits
CAPTIONS = "/dev/shm/captions.json"
CAPTION_LANG = "/dev/shm/caption-lang"

KEYS = {"up", "down", "left", "right", "ok", "enter", "back", "guide", "menu", "home", "play_pause",
        "ch_up", "ch_down", "vol_up", "vol_down", "mute", "next", "prev", "ff", "rew", "stop",
        *map(str, range(10))}
_lock = threading.Lock()     # one key sequence at a time


def token():
    if not os.path.exists(TOKEN_FILE):
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    return open(TOKEN_FILE).read().strip()


def press(key):
    req = urllib.request.Request(f"{REMOTE}/key/{key}", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def shortlists(limit=80):
    """Working channels only, best first, trimmed for a TV picker."""
    out = {}
    for name, title in (("soccer", "Soccer"), ("spanish", "Spanish"), ("english", "English")):
        try:
            chans = json.load(open(os.path.join(DATA, f"{name}.json")))["channels"]
        except (FileNotFoundError, ValueError):
            continue
        rows = []
        for c in chans:
            if c.get("video_ok") is False:
                continue
            now = next((e["title"] for e in c.get("epg") or [] if "No information" not in e["title"]), "")
            rows.append({"number": c["number"], "name": c["name"], "now": now, "lang": c.get("language"),
                         "type": c.get("content_type"), "score": (c.get("language_learning") or {}).get("score")
                         if name != "soccer" else c.get("soccer")})
            if len(rows) >= limit:
                break
        out[name] = {"title": title, "channels": rows}
    return out


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return True
        self._reply(401, {"error": "bad token"})
        return False

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"ok": True})
        if not self._authed():
            return
        if self.path == "/channels":
            return self._reply(200, shortlists())
        if self.path.startswith("/captions"):
            from urllib.parse import parse_qs, urlparse
            with open("/dev/shm/captions-viewer", "w"):   # live_captions.py glosses only while watched
                pass
            since = int((parse_qs(urlparse(self.path).query).get("since") or ["0"])[0])
            try:
                d = json.load(open(CAPTIONS))
            except (FileNotFoundError, ValueError):
                return self._reply(200, {"lang": "off", "ready_until": 0, "captions": []})
            d["captions"] = [c for c in d["captions"] if c["end"] >= since]
            d["now"] = int(time.time() * 1000)
            return self._reply(200, d)
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        parts = self.path.strip("/").split("/")
        try:
            if len(parts) == 2 and parts[0] == "key" and parts[1] in KEYS:
                with _lock:
                    press(parts[1])
                return self._reply(200, {"sent": parts[1]})
            if len(parts) == 2 and parts[0] == "caption-lang" and parts[1] in ("es", "en", "auto", "off"):
                with open(CAPTION_LANG, "w") as f:
                    f.write(parts[1])
                return self._reply(200, {"caption_lang": parts[1]})
            if len(parts) == 2 and parts[0] == "tune" and parts[1].isdigit() and len(parts[1]) <= 6:
                with _lock:
                    for d in parts[1]:
                        press(d)
                        time.sleep(DIGIT_GAP)
                return self._reply(200, {"tuned": int(parts[1])})
        except OSError as e:          # fake remote down / box not connected
            return self._reply(502, {"error": f"remote unavailable: {e}"})
        self._reply(404, {"error": "not found"})

    def log_message(self, *a):
        pass


TOKEN = token()

if __name__ == "__main__":
    print(f"key relay on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
