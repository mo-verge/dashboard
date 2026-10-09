#!/usr/bin/env python3
"""Drive the TVIP box: press keys through the fake BLE remote, grab frames
from the HDMI capture adapter.

    python3 capture/box.py tap start            # background frame tap -> /dev/shm/box.jpg
    python3 capture/box.py press down [n]       # press a key n times
    python3 capture/box.py shot /tmp/x.jpg      # copy the latest frame
    python3 capture/box.py press ok --shot /tmp/x.jpg --wait 1.5

The tap copies the adapter's own MJPEG frames (no decoding) into a RAM file,
so a screenshot is just a file copy. When the capture hub (~/stream/capture-hub.sh)
is running, it owns the device and writes that same file, so shots read its frames
and no tap is started.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import remote_keys
# By-id name of the MS2109 HDMI capture adapter: /dev/videoN numbering changes when
# another camera (a webcam) is plugged in.
DEVICE = os.environ.get("CAPTURE_DEVICE", "/dev/v4l/by-id/usb-MACROSILICON_2109-video-index0")

REMOTE = "http://127.0.0.1:8179"
LATEST = "/dev/shm/box.jpg"
PIDFILE = "/dev/shm/box-tap.pid"
HUB_PIDFILE = "/dev/shm/capture-hub.pid"   # ~/stream/capture-hub.sh; mtime = its last (re)start


def press(key, times=1, gap=0.35):
    """USB keyboard in the box if it's there, else the Bluetooth remote (remote_keys.py)."""
    for i in range(times):
        remote_keys.press(key)
        if i < times - 1:
            time.sleep(gap)


def tap_running():
    try:
        os.kill(int(open(PIDFILE).read()), 0)
        return True
    except (OSError, ValueError):
        return False


def hub_running():
    """The capture hub owns the capture device and keeps LATEST up to date."""
    try:
        os.kill(int(open(HUB_PIDFILE).read()), 0)
        return True
    except (OSError, ValueError):
        return False


def hub_wait_frame(not_before, timeout=20.0):
    """Wait until the hub has written a frame captured at or after `not_before`
    and at least 1 s after its last (re)start (the adapter's first frames are
    corrupt). False if the hub goes away meanwhile; raises if it stalls."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            started = os.stat(HUB_PIDFILE).st_mtime
            if os.stat(LATEST).st_mtime >= max(not_before, started + 1.0):
                return True
        except FileNotFoundError:
            pass
        if not hub_running():
            return False
        time.sleep(0.05)
    raise RuntimeError(f"capture hub is running but wrote no new frame to {LATEST} in {timeout:.0f}s")


def tap_start(size="1920x1080"):
    if tap_running() or hub_running():   # the hub already writes LATEST
        return
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg",
         "-video_size", size, "-framerate", "30", "-i", DEVICE,
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
    for p in (PIDFILE,) if hub_running() else (PIDFILE, LATEST):   # LATEST is the hub's
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def screen_running():
    return subprocess.run(["pgrep", "-x", "ffplay"], capture_output=True).returncode == 0


PREVIEW_WANTED = "/dev/shm/preview-wanted"   # set by scripts/capture-preview.sh while it runs


def shot(dest, settle=0.0):
    """Latest box frame as 1920x1080. While the capture hub owns the device, copy
    its newest raw frame. While the full-screen preview (ffplay) owns it, grab the
    Pi's display instead and scale it back down, so the preview stays visible and
    coordinates stay the same. Only when nothing owns the device, start the tap."""
    time.sleep(settle)
    if hub_running() and hub_wait_frame(not_before=time.time()):
        shutil.copyfile(LATEST, dest)
        return dest
    if os.path.exists(PREVIEW_WANTED):
        # preview should be up (its wrapper restarts ffplay if it crashes):
        # wait for it rather than grabbing the device and blocking the restart
        for _ in range(40):
            if screen_running():
                break
            time.sleep(0.5)
    if screen_running():
        from PIL import Image
        env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}", WAYLAND_DISPLAY="wayland-0")
        # the preview fills the 2560x1440 screen; grim scales straight to 1920x1080
        subprocess.run(["grim", "-s", "0.75", "-t", "ppm", "/dev/shm/screen.ppm"], env=env, check=True)
        Image.open("/dev/shm/screen.ppm").save(dest, quality=95)
        return dest
    if not tap_running():
        tap_start()
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
