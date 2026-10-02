#!/usr/bin/env python3
"""Inventory the TVIP IPTV portal by driving the box (fake BLE remote) and
reading the HDMI capture with tesseract.

    python3 capture/inventory.py categories out.json
        From the portal home (TV tile selected), step down the category list and
        OCR the highlighted entry until the list repeats.

Frames come from capture/box.py's tap (native 1920x1080 MJPEG).
"""
import json
import os
import re
import subprocess
import sys
import time

from PIL import Image, ImageOps

sys.path.insert(0, sys.path[0])
import box  # noqa: E402
import digits  # noqa: E402

# Portal home: the highlighted category is the 3rd row of the list box.
CAT_HIGHLIGHT = (690, 330, 1240, 380)

# Channel list (TV / <category> / BY NUMBER), 1920x1080: 14 rows of ~57 px.
ROW_X0, ROW_X1, NAME_X0 = 104, 892, 245
ROW_Y0, ROW_PITCH, ROWS = 116, 57.2, 14
FOOTER = (100, 930, 900, 960)            # "Page 1 OF 3. Found 41 RECORDINGS."
EPG_PANEL = (930, 620, 1810, 945)        # now/next list under the preview
HEADER = (300, 60, 1000, 110)            # "TV / SPORTS | SOCCER /BY NUMBER /"
PREVIEW = (932, 132, 1800, 628)          # live preview of the tuned channel


def ocr(img, psm=7, lang="eng+spa", digits=False, scale=2):
    g = ImageOps.grayscale(img)
    if digits:  # number boxes: dark-on-light or light-on-dark; normalise to dark text
        if sum(g.getdata()) / (g.width * g.height) < 128:
            g = ImageOps.invert(g)
    g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    g.save("/dev/shm/ocr.png")
    cmd = ["tesseract", "/dev/shm/ocr.png", "-", "--psm", str(psm), "-l", lang]
    if digits:
        cmd += ["-c", "tessedit_char_whitelist=0123456789"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return out.strip()


def clean(text):
    text = re.sub(r"\s*[|lI\]]\s*$", "", text.strip())   # box border read as | l I ]
    return re.sub(r"\s+", " ", text).strip(" -_.")


def frame(settle=0.6):
    time.sleep(settle)
    return Image.open(box.shot("/dev/shm/inv.jpg"))


def norm(name):
    """Canonical category key: drop OCR border junk, collapse separators."""
    name = re.sub(r"^[\W_]+", "", name)
    name = re.sub(r"\s+(ff|\[J?|—|Tv)$", "", name)
    name = re.sub(r"\s*\|\s*", " | ", name)
    return re.sub(r"\s+", " ", name).strip().upper()


KNOWN_CATEGORIES = []   # filled by run() from categories.json


def on_home():
    """Portal home: the category highlight reads like a known category name."""
    text = clean(ocr(frame(1.5).crop(CAT_HIGHLIGHT)))
    if "|" in text or norm(text) in ("ALL", "FIFA WORLDCUP REPLAYS"):
        return True
    return any(similar(text, c) >= 0.85 for c in KNOWN_CATEGORIES)


def ensure_home(tries=3):
    for _ in range(tries):
        if on_home():
            return True
        box.press("back")
    return on_home()


def similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def wait_change(region, before, timeout=10.0):
    """Wait until `region` of the screen differs from `before` (the box's UI can
    lag seconds behind the keys); returns the new frame."""
    from PIL import ImageChops, ImageStat
    deadline = time.time() + timeout
    img = frame(0.15)
    while time.time() < deadline:
        diff = ImageStat.Stat(ImageChops.difference(img.convert("L").crop(region), before)).mean[0]
        if diff > 2:
            return frame(0.35)          # let the animation settle
        img = frame(0.15)
    return img


def highlighted_category(img):
    return clean(ocr(img.crop(CAT_HIGHLIGHT)))


def goto_category(target, limit=300):
    """From the portal home, press down until the highlighted category is `target`
    (fuzzy: OCR of the highlighted row varies a little between passes). Waits for
    the box to actually move after each key, and confirms the match twice."""
    img = frame(0.5)
    for _ in range(limit):
        if similar(highlighted_category(img), target) >= 0.88:
            again = frame(0.6)
            if similar(highlighted_category(again), target) >= 0.88:
                return True
            img = again
            continue
        before = img.convert("L").crop(CAT_HIGHLIGHT)
        box.press("down")
        img = wait_change(CAT_HIGHLIGHT, before)
        # never queue a second key while the box hasn't shown the first one
        if similar(highlighted_category(img), highlighted_category_from(before)) >= 0.99:
            img = wait_change(CAT_HIGHLIGHT, before, timeout=20)
    return False


def highlighted_category_from(gray_crop):
    return clean(ocr(gray_crop.convert("RGB")))


def list_title(img):
    t = ocr(img.crop(HEADER), psm=7)
    m = re.search(r"TV\s*/\s*(.+?)\s*/\s*BY", t, re.I)
    return m.group(1) if m else t


def row_box(i):
    y = round(ROW_Y0 + i * ROW_PITCH)
    return (ROW_X0, y, ROW_X1, y + 52)


def highlighted_row(img):
    """The highlighted channel row is the brightest band in the name column."""
    g = ImageOps.grayscale(img)
    means = []
    for i in range(ROWS):
        b = row_box(i)
        band = g.crop((NAME_X0, b[1] + 8, b[2], b[3] - 8))
        means.append(sum(band.getdata()) / (band.width * band.height))
    return max(range(ROWS), key=means.__getitem__)


def read_number(img, i, prev=None):
    """Channel number via digit templates (capture/digits.py). Lists are sorted by
    number, so a reading that doesn't exceed the previous one is flagged."""
    b = row_box(i)
    n = digits.read(img.crop((digits.BOX_X0, b[1] + 6, digits.BOX_X1, b[3] - 4)))
    if not n:
        return None, True
    return int(n), prev is not None and int(n) <= prev


def read_row(img, i, prev=None):
    num, fixed = read_number(img, i, prev)
    b = row_box(i)
    # text starts after logo + clock icon (and after the ▶ on the tuned channel)
    x0 = 352 if is_playing(img, i) else 298
    name = clean(ocr(img.crop((x0, b[1], b[2], b[3])), psm=7))
    name = re.sub(r"^[^\w(]+", "", name)   # play / clock / star icons read as junk
    return num, name, fixed


EPG_LINE = re.compile(r"(\d{1,2}[:.]\d{2}\s*[AP]M)\s*[-–]\s*(.+)", re.I)


def read_epg(img):
    text = ocr(img.crop(EPG_PANEL), psm=6)
    return [{"time": m.group(1).replace(".", ":").upper(), "title": m.group(2).strip()}
            for m in map(EPG_LINE.search, text.splitlines()) if m]


def read_total(img):
    m = re.search(r"Found\s+(\d+)", ocr(img.crop(FOOTER), psm=7, lang="eng"))
    return int(m.group(1)) if m else None


def in_list(img):
    return "TV" in ocr(img.crop(HEADER), psm=7, lang="eng")


def is_playing(img, row):
    """The tuned channel's row shows a blue ▶ square just right of its logo."""
    y = row_box(row)[1] + 26
    rgb = img.convert("RGB")

    def avg(x0, x1):
        px = list(rgb.crop((x0, y - 3, x1, y + 3)).getdata())
        return [sum(c[i] for c in px) / len(px) for i in range(3)]
    left, mid, right = avg(262, 268), avg(274, 280), avg(290, 296)
    blue = lambda c: c[2] > 140 and c[0] < 70
    return blue(left) and blue(right) and min(mid) > 170


def tune(settle, max_wait=8.0):
    """OK on the highlighted channel tunes it into the preview and fills the
    now/next panel. OK on the channel that is *already* playing goes full screen,
    so skip it; if we do end up full screen, OK brings the list back — then give
    the list time to settle before the next key."""
    img = frame(0.4)
    if not is_playing(img, highlighted_row(img)):
        box.press("ok")
        img = frame(settle)
    if not in_list(img):
        box.press("ok")
        img = frame(3.0)
    # streams take anywhere from ~1 s to several seconds to appear in the preview:
    # keep looking until there is a picture (or give up and report no video)
    deadline = time.time() + max_wait
    while not stream_health(img)["video_ok"] and time.time() < deadline:
        img = frame(0.7)
    return img


def stream_health(img):
    """Black/flat preview = no video (broken or not yet started)."""
    g = ImageOps.grayscale(img.crop(PREVIEW)).resize((64, 36))
    px = list(g.getdata())
    mean = sum(px) / len(px)
    var = sum((v - mean) ** 2 for v in px) / len(px)
    return {"video_ok": not (mean < 18 or var < 25), "luma": round(mean, 1)}


def audio_level(seconds=0.6):
    """RMS of the capture adapter's audio right now (0 = silent)."""
    import array, math
    import wave
    env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")
    wav = "/dev/shm/level.wav"
    subprocess.run(["timeout", str(seconds), "pw-record", "--target",
                    "alsa_input.usb-MACROSILICON_2109-02.analog-stereo", "--rate", "16000",
                    "--channels", "1", "--format", "s16", wav], capture_output=True, env=env)
    try:
        with wave.open(wav) as w:
            a = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, EOFError, FileNotFoundError):
        return 0.0
    return round(math.sqrt(sum(x * x for x in a) / len(a)), 1) if a else 0.0


SPORT_WORDS = re.compile(
    r"SPORT|SPOR\b|BEIN|BE IN|SSC|ALKASS|AL KASS|DAZN|ESPN|TSN|EUROSPORT|ARENA|MATCH|LIGA|LALIGA|FUTBOL|"
    r"FÚTBOL|FUTEBOL|GOAL|GOL\b|TUDN|UEFA|FIFA|PREMIER|SOCCER|FOOTBALL|KORA|AD SPORT|DUBAI SPORT|ON TIME|"
    r"S SPORT|TIVIBU|EXXEN|NBA|NFL|NHL|MLB|RACE|TENNIS|GOLF|F1|FORMULA", re.I)


def channels(category, out, epg=True, settle=2.5, thumbs=None, tune_if=None):
    """Open `category` from the portal home and walk its list one channel at a
    time. epg=True tunes every channel (OK) to read its now/next panel;
    tune_if=regex tunes only channels whose name matches (quick-scan tiers)."""
    if not goto_category(category):
        sys.exit(f"category not found: {category}")
    for attempt in (1, 2):
        box.press("ok")
        title, deadline = "", time.time() + 15     # the list can take many seconds to open
        while time.time() < deadline:
            img = frame(1.0)
            title = list_title(img)
            if similar(title, category) >= 0.8:
                break
        if similar(title, category) >= 0.8:
            break
        box.press("back")
        ensure_home()
        if attempt == 2 or not goto_category(category):
            sys.exit(f"opened '{title}' instead of {category}")
    total = read_total(img)
    print(f"{category}: {total} channels", flush=True)
    rows, seen = [], set()
    misses = stuck = 0
    if thumbs:
        os.makedirs(thumbs, exist_ok=True)
    while total is None or len(rows) < total:
        img = tune(1.2) if epg else frame(0.5)
        prev = rows[-1]["number"] if rows else None
        num, name, fixed = read_row(img, highlighted_row(img), prev)
        if rows and num is not None and num == prev:
            # Highlight hasn't moved: the box is lagging (keep waiting, a queued
            # key would overshoot) or the key was lost (then resend just one).
            stuck += 1
            if stuck >= 4:
                print(f"   stuck at {num}; leaving category", flush=True)
                break
            region = (ROW_X0, ROW_Y0, ROW_X1, int(ROW_Y0 + ROWS * ROW_PITCH))
            before = img.convert("L").crop(region)
            if not in_list(img):            # an error popup on a broken channel
                box.press("back")
                time.sleep(1.5)
            moved = wait_change(region, before, timeout=10)
            if moved.convert("L").crop(region).tobytes() == before.tobytes():
                box.press("down")
                wait_change(region, before, timeout=10)
            continue
        stuck = 0
        tuned = epg
        if not epg and tune_if is not None and tune_if.search(name or ""):
            img = tune(1.2)
            tuned = True
        if rows and num == rows[0]["number"] and len(rows) > 2:   # wrapped to the top: done
            break
        if num:
            seen.add(num)
            entry = {"number": num, "name": name}
            if fixed:
                entry["number_uncertain"] = True
            if tuned:
                entry["epg"] = read_epg(img)
                entry.update(stream_health(img))
                entry["audio_rms"] = audio_level()
                entry["audio_ok"] = entry["audio_rms"] > 30
                if thumbs:
                    img.crop(PREVIEW).resize((320, 180)).save(f"{thumbs}/{num}.jpg", quality=70)
            rows.append(entry)
            print(f"  {num:>6} {name[:40]:40} {entry.get('epg', [{}])[0].get('title', '') if entry.get('epg') else ''}",
                  flush=True)
            misses = 0
        else:
            misses += 1
            if misses > 5:
                break
        before = img.convert("L").crop((ROW_X0, ROW_Y0, ROW_X1, int(ROW_Y0 + ROWS * ROW_PITCH)))
        box.press("down")
        wait_change((ROW_X0, ROW_Y0, ROW_X1, int(ROW_Y0 + ROWS * ROW_PITCH)), before, timeout=10)
    box.press("back")
    ensure_home()
    json.dump({"category": category, "captured": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "mode": "full" if epg else ("sports-only" if tune_if is not None else "names"),
               "reported_total": total, "channels": rows}, open(out, "w"), indent=2, ensure_ascii=False)
    print(f"{len(rows)}/{total} channels -> {out}", flush=True)


def tier(cat):
    """A = tune every channel; B = names, tune sport-looking ones; None = skip."""
    c = norm(cat)
    if c in ("ALL",) or c.startswith("ADULT"):
        return None
    if re.match(r"(SPANISH|LATINO|ENGLISH|LOCAL|KIDS|SPORTS|MLB|PPV|ESPN|TSN|BEIN|TRUE 4K|FIFA)", c):
        return "A"
    if SPORT_WORDS.search(c) or "STARZ PLAY AD" in c:
        return "A"
    return "B"


def slug(cat):
    return re.sub(r"[^a-z0-9]+", "-", norm(cat).lower()).strip("-")


def run(cats_file, outdir, only=None):
    """Walk every category by tier; resumable (skips categories already saved)."""
    cats = [norm(c) for c in json.load(open(cats_file))["categories"]]
    cats = list(dict.fromkeys(cats))
    KNOWN_CATEGORIES[:] = cats
    order = [c for c in cats if tier(c) == "A"] + [c for c in cats if tier(c) == "B"]
    # Spanish first, then soccer/sports, then English, then the rest of tier A
    rank = lambda c: (0 if re.match(r"(SPANISH|LATINO)", c) else 1 if re.search(r"SOCCER|EPL|UEFA|BEIN|DAZN|FIFA|SSC|ALKASS|LALIGA|MLS", c)
                      else 2 if tier(c) == "A" and not c.startswith("ENGLISH") else 3 if c.startswith("ENGLISH") else 4)
    order.sort(key=rank)
    os.makedirs(outdir, exist_ok=True)
    thumbs = os.path.expanduser("~/tvip-thumbs")
    for c in order:
        if only and not re.search(only, c):
            continue
        out = os.path.join(outdir, slug(c) + ".json")
        if os.path.exists(out):
            continue
        t = tier(c)
        print(f"== [{t}] {c}", flush=True)
        try:
            if not ensure_home():
                print("   not on portal home; stopping", flush=True)
                return
            channels(c, out, epg=(t == "A"), thumbs=thumbs if t == "A" else None,
                     tune_if=SPORT_WORDS if t == "B" else None)
        except SystemExit as e:
            print(f"   skipped: {e}", flush=True)


def categories(out):
    seen = []
    first = clean(ocr(frame(0.2).crop(CAT_HIGHLIGHT)))
    seen.append(first)
    print(f"  1. {first}", flush=True)
    repeats = 0
    while True:
        box.press("down")
        name = clean(ocr(frame().crop(CAT_HIGHLIGHT)))
        if name == seen[-1]:           # didn't move: end of list, or slow redraw
            repeats += 1
            if repeats >= 3:
                break
            continue
        repeats = 0
        if name == first and len(seen) > 2:   # wrapped around
            break
        seen.append(name)
        print(f"{len(seen):3d}. {name}", flush=True)
        if len(seen) > 400:
            break
    json.dump({"source": "TVIP portal > TV categories", "captured": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "categories": seen}, open(out, "w"), indent=2, ensure_ascii=False)
    print(f"{len(seen)} categories -> {out}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "categories":
        categories(sys.argv[2])
    elif cmd == "channels":
        channels(sys.argv[2], sys.argv[3], epg="--no-epg" not in sys.argv,
                 thumbs=os.path.expanduser("~/tvip-thumbs") if "--thumbs" in sys.argv else None)
    elif cmd == "run":
        run(sys.argv[2], sys.argv[3], only=sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "plan":
        cats = list(dict.fromkeys(norm(c) for c in json.load(open(sys.argv[2]))["categories"]))
        for c in cats:
            print(tier(c) or "-", c)
    elif cmd == "goto":
        print("found" if goto_category(sys.argv[2]) else "not found")
