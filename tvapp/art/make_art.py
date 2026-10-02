#!/usr/bin/env python3
"""Draw the Monet TV launcher art (Android TV banner 320x180 @xhdpi and icons).

Same language as the dashboard: near-black ground, blurred violet / teal / pink
glows, a gradient ring (the steps dial) around a soft play glyph, Geist type.

    python3 tvapp/art/make_art.py FONT_DIR     # needs Pillow and Geist-500/600 TTFs
"""
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

BG = (6, 6, 10)
VIOLET, TEAL, PINK, INK = (124, 92, 255), (46, 230, 214), (255, 92, 147), (244, 243, 248)
SS = 4  # supersample, then downscale for clean edges


def glows(w, h, spots):
    layer = Image.new("RGB", (w, h), BG)
    for (cx, cy, r, color, alpha) in spots:
        blob = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(blob)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=tuple(int(c * alpha) for c in color))
        blob = blob.filter(ImageFilter.GaussianBlur(r * 0.55))
        layer = ImageChops.add(layer, blob)
    return layer


def gradient_ring(size, width):
    """Ring stroked with a violet -> pink -> teal sweep, like the steps dial."""
    import math
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    ImageDraw.Draw(mask).ellipse((width, width, size - 1 - width, size - 1 - width), fill=0)
    stops = [(0.0, VIOLET), (0.5, PINK), (1.0, TEAL)]
    px = ring.load()
    c = (size - 1) / 2
    for y in range(size):
        for x in range(size):
            t = (math.atan2(y - c, x - c) / math.pi + 1) / 2          # 0..1 around the ring
            t = (t + 0.25) % 1.0
            for (a, ca), (b, cb) in zip(stops, stops[1:]):
                if a <= t <= b:
                    k = (t - a) / (b - a)
                    px[x, y] = tuple(int(ca[i] + (cb[i] - ca[i]) * k) for i in range(3)) + (255,)
                    break
    ring.putalpha(mask)
    return ring


def play_glyph(size):
    """Soft, rounded play triangle."""
    g = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(g)
    pad = size * 0.06
    d.polygon([(pad + size * 0.08, pad), (size - pad, size / 2), (pad + size * 0.08, size - pad)], fill=255)
    g = g.filter(ImageFilter.GaussianBlur(size * 0.045)).point(lambda v: 255 if v > 128 else 0)
    return g


def emblem(size):
    """Ring + play with a soft halo. The canvas is padded so the halo's blur
    fades out instead of being clipped to a visible square."""
    pad = int(size * 0.3)
    e = Image.new("RGBA", (size + 2 * pad, size + 2 * pad), (0, 0, 0, 0))
    halo = Image.new("RGBA", e.size, (0, 0, 0, 0))
    halo.alpha_composite(gradient_ring(size, int(size * 0.16)), (pad, pad))
    halo = halo.filter(ImageFilter.GaussianBlur(size * 0.09))
    e.alpha_composite(halo)
    e.alpha_composite(gradient_ring(size, int(size * 0.075)), (pad, pad))
    p = int(size * 0.36)
    white = Image.new("RGBA", (p, p), INK + (255,))
    white.putalpha(play_glyph(p))
    e.alpha_composite(white, (pad + (size - p) // 2 + int(p * 0.06), pad + (size - p) // 2))
    return e, pad


def banner(font_dir, out):
    w, h = 320 * SS, 180 * SS
    img = glows(w, h, [(w * 0.12, h * 0.05, w * 0.42, VIOLET, 0.55),
                       (w * 0.95, h * 1.05, w * 0.45, TEAL, 0.45),
                       (w * 0.62, h * 0.30, w * 0.22, PINK, 0.22)]).convert("RGBA")
    ring = int(h * 0.50)
    em, pad = emblem(ring)
    ex = int(w * 0.07)
    img.alpha_composite(em, (ex - pad, (h - ring) // 2 - pad))
    d = ImageDraw.Draw(img)
    bold = ImageFont.truetype(os.path.join(font_dir, "Geist-600.ttf"), int(h * 0.20))
    thin = ImageFont.truetype(os.path.join(font_dir, "Geist-300.ttf"), int(h * 0.20))
    small = ImageFont.truetype(os.path.join(font_dir, "Geist-600.ttf"), int(h * 0.062))
    tx = ex + ring + int(w * 0.045)
    # wordmark baseline slightly above centre, chip below it
    _, t, _, b = d.textbbox((0, 0), "Monet", font=bold, anchor="ls")
    cap = -t                                  # cap height above the baseline
    base = h / 2 + cap * 0.18
    d.text((tx, base), "Monet", font=bold, fill=INK, anchor="ls")
    mw = d.textlength("Monet ", font=bold)
    d.text((tx + mw, base), "TV", font=thin, fill=(244, 243, 248, 170), anchor="ls")
    chip_h = h * 0.12
    cy0 = base + h * 0.07
    label_w = d.textlength("LIVE", font=small)
    cx1 = tx + chip_h * 0.95 + label_w + chip_h * 0.45
    d.rounded_rectangle((tx, cy0, cx1, cy0 + chip_h), radius=chip_h / 2,
                        fill=(255, 92, 147, 70), outline=(255, 92, 147, 230), width=SS)
    dot = chip_h * 0.32
    d.ellipse((tx + chip_h * 0.38, cy0 + (chip_h - dot) / 2, tx + chip_h * 0.38 + dot, cy0 + (chip_h + dot) / 2),
              fill=PINK + (255,))
    d.text((tx + chip_h * 0.95, cy0 + chip_h / 2), "LIVE", font=small, fill=INK, anchor="lm")
    img.convert("RGB").resize((320, 180), Image.LANCZOS).save(out)


def icon(out, px):
    s = px * SS
    img = glows(s, s, [(s * 0.2, s * 0.15, s * 0.6, VIOLET, 0.6),
                       (s * 0.9, s * 0.95, s * 0.6, TEAL, 0.5)]).convert("RGBA")
    ring = int(s * 0.66)
    em, pad = emblem(ring)
    img.alpha_composite(em, ((s - ring) // 2 - pad, (s - ring) // 2 - pad))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=255)
    out_img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out_img.paste(img, (0, 0), mask)
    out_img.resize((px, px), Image.LANCZOS).save(out)


if __name__ == "__main__":
    fonts = sys.argv[1]
    res = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "src", "main", "res")
    banner(fonts, os.path.join(res, "drawable", "banner.png"))
    for density, px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        os.makedirs(os.path.join(res, f"mipmap-{density}"), exist_ok=True)
        icon(os.path.join(res, f"mipmap-{density}", "ic_launcher.png"), px)
    print("art written")
