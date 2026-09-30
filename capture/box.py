#!/usr/bin/env python3
"""Drive the TVIP box: press keys through the fake BLE remote, grab frames
from the HDMI capture adapter.

    python3 capture/box.py tap start            # background frame tap -> /dev/shm/box.jpg
    python3 capture/box.py press down [n]       # press a key n times
    python3 capture/box.py shot /tmp/x.jpg      # copy the latest frame
    python3 capture/box.py press ok --shot /tmp/x.jpg --wait 1.5

The tap copies the adapter's own MJPEG frames (no decoding) into a RAM file,
so a screenshot is just a file copy.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

REMOTE = "http://127.0.0.1:8179"
LATEST = "/dev/shm/box.jpg"
PIDFILE = "/dev/shm/box-tap.pid"


def press(key, times=1, gap=0.35):
    for i in range(times):
        req = urllib.request.Request(f"{REMOTE}/key/{key}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            json.load(r)
        if i < times - 1:
            time.sleep(gap)


def tap_running():
    try:
        os.kill(int(open(PIDFILE).read()), 0)
        return True
    except (OSError, ValueError):
        return False


def tap_start(size="1920x1080"):
    if tap_running():
        return
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg",
         "-video_size", size, "-framerate", "30", "-i", "/dev/video0",
         "-c:v", "copy", "-f", "image2", "-update", "1", "-atomic_writing", "1", LATEST],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # detached: must not hold the caller's (ssh) pipes open
    open(PIDFILE, "w").write(str(proc.pid))
    for _ in range(50):
        if os.path.exists(LATEST):
            break
        time.sleep(0.1)
    time.sleep(1.0)  # let the adapter's first corrupt frames pass


def tap_stop():
    if tap_running():
        os.kill(int(open(PIDFILE).read()), 15)
    for p in (PIDFILE, LATEST):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def shot(dest, settle=0.0):
    if not tap_running():
        tap_start()
    time.sleep(settle)
    shutil.copyfile(LATEST, dest)
    return dest


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tap")
    t.add_argument("action", choices=["start", "stop"])
    p = sub.add_parser("press")
    p.add_argument("key")
    p.add_argument("times", nargs="?", type=int, default=1)
    p.add_argument("--shot")
    p.add_argument("--wait", type=float, default=1.2)
    s = sub.add_parser("shot")
    s.add_argument("dest")
    a = ap.parse_args()

    if a.cmd == "tap":
        tap_start() if a.action == "start" else tap_stop()
    elif a.cmd == "press":
        press(a.key, a.times)
        if a.shot:
            shot(a.shot, a.wait)
    elif a.cmd == "shot":
        shot(a.dest)


if __name__ == "__main__":
    sys.exit(main())
