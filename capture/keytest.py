#!/usr/bin/env python3
"""Press a sequence of remote keys and make a labelled contact sheet of the
screen after each one (reads the Pi's own display, where the capture preview
shows the box full screen).

    python3 capture/keytest.py /tmp/sheet.jpg right left down up
    python3 capture/keytest.py /tmp/sheet.jpg ok:2.5 back   # key:wait-seconds
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

ENV = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}", WAYLAND_DISPLAY="wayland-0")
W, H = 640, 360


def grab(path):
    subprocess.run(["grim", "-s", "0.25", path], env=ENV, check=True)


def press(key):
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:8179/key/{key}", method="POST"), timeout=5)


def main():
    out, steps = sys.argv[1], sys.argv[2:]
    tmp = tempfile.mkdtemp()
    shots = [("start", os.path.join(tmp, "0.png"))]
    grab(shots[0][1])
    for i, step in enumerate(steps, 1):
        key, _, wait = step.partition(":")
        press(key)
        time.sleep(float(wait or 1.5))
        path = os.path.join(tmp, f"{i}.png")
        grab(path)
        shots.append((key, path))

    cols = 3
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * W, rows * (H + 28)), "black")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    for n, (label, path) in enumerate(shots):
        x, y = (n % cols) * W, (n // cols) * (H + 28)
        sheet.paste(Image.open(path).convert("RGB").resize((W, H)), (x, y + 28))
        draw.text((x + 8, y + 2), f"{n}. {label}", fill="yellow", font=font)
    sheet.save(out, quality=80)


if __name__ == "__main__":
    main()
