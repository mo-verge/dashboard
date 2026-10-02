#!/usr/bin/env python3
"""Read the TVIP channel-list number boxes by template matching.

The numbers are always the same font and size, which tesseract reads badly at
this scale (the box edge becomes a leading 1/9). Instead: binarise the box,
split it into glyphs by column projection, and match each glyph against
templates learned from screens with known numbers.

    python3 capture/digits.py train templates.json shot.jpg:5689,5693,... [...]
"""
import json
import os
import sys

from PIL import Image, ImageOps

GW, GH = 14, 22          # normalised glyph size
BOX_X0, BOX_X1 = 106, 205
TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digit_templates.json")


def binarise(crop):
    """Dark-on-light digits as a 0/1 matrix (inverts the highlighted white-on-blue box)."""
    g = ImageOps.grayscale(crop)
    px = list(g.getdata())
    mean = sum(px) / len(px)
    if mean < 128:
        g = ImageOps.invert(g)
        px = list(g.getdata())
    lo, hi = min(px), max(px)
    cut = lo + (hi - lo) * 0.55
    return [[1 if g.getpixel((x, y)) < cut else 0 for x in range(g.width)] for y in range(g.height)], g


def components(m):
    """4-connected ink blobs as (x0, y0, x1, y1, pixels)."""
    h, w = len(m), len(m[0])
    seen = [[0] * w for _ in range(h)]
    out = []
    for y in range(h):
        for x in range(w):
            if m[y][x] and not seen[y][x]:
                stack, pts = [(x, y)], []
                seen[y][x] = 1
                while stack:
                    cx, cy = stack.pop()
                    pts.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h and m[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = 1
                            stack.append((nx, ny))
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                out.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1, pts))
    return out


def glyphs(crop):
    m, _ = binarise(crop)
    h, w = len(m), len(m[0])
    blobs = []
    for x0, y0, x1, y1, pts in components(m):
        bw, bh = x1 - x0, y1 - y0
        if bh >= 0.9 * h or bw >= 0.45 * w:               # box border / outside area
            continue
        if bh < 0.4 * h or len(pts) < 20:                 # specks, icon fragments
            continue
        blobs.append((x0, y0, x1, y1, pts))
    blobs.sort()
    out = []
    for j, (x0, y0, x1, y1, pts) in enumerate(blobs):
        # touching digits (e.g. "44") form one blob ~2x as wide as any single digit
        others = [b[2] - b[0] for k, b in enumerate(blobs) if k != j]
        typical = max(others) if others else 0
        img = Image.new("L", (x1 - x0, y1 - y0), 0)
        for px, py in pts:
            img.putpixel((px - x0, py - y0), 255)
        # touching digits (e.g. "44") come out as one blob about n digits wide
        n = round((x1 - x0) / typical) if typical and (x1 - x0) > 1.7 * typical else 1
        for k in range(n):
            part = img.crop((round(k * img.width / n), 0, round((k + 1) * img.width / n), img.height))
            out.append(list(part.resize((GW, GH)).getdata()))
    return out


def dist(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


_templates = None


def load():
    global _templates
    if _templates is None:
        _templates = json.load(open(TEMPLATES))
    return _templates


def read(crop):
    t = load()
    digits = []
    for g in glyphs(crop):
        best = min(((dist(g, v), d) for d, vs in t.items() for v in vs))
        digits.append(best[1])
    return "".join(digits)


def train(out, specs, row_box):
    t = {str(d): [] for d in range(10)}
    for spec in specs:
        path, nums = spec.split(":")
        img = Image.open(path)
        for i, n in enumerate(nums.split(",")):
            b = row_box(i)
            gs = glyphs(img.crop((BOX_X0, b[1] + 6, BOX_X1, b[3] - 4)))
            if len(gs) != len(n):
                print(f"skip {path} row {i}: {len(gs)} glyphs for {n}")
                continue
            for d, g in zip(n, gs):
                t[d].append(g)
    json.dump(t, open(out, "w"))
    print({d: len(v) for d, v in t.items()})


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import inventory
    if sys.argv[1] == "train":
        train(sys.argv[2], sys.argv[3:], inventory.row_box)
