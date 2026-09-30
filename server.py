#!/usr/bin/env python3
"""Serves web/ plus the dashboard's JSON endpoints and Google sign-in."""
import html
import json
import os
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import health

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
# Loopback only: health data never leaves the Pi. Reach it from another
# machine with an SSH tunnel (ssh -L 8080:127.0.0.1:8080 <pi>).
BIND = os.environ.get("DASHBOARD_BIND", "127.0.0.1")

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

        if url.path == "/api/system":
            return self._json(system())
        if url.path == "/api/steps":
            return self._json(health.steps_status())

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
