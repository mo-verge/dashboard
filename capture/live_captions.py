#!/usr/bin/env python3
"""Continuous Whisper subtitles for the box's audio, timestamped in wall-clock
time so the Monet TV app can line them up with the delayed HLS stream (whose
segments carry EXT-X-PROGRAM-DATE-TIME).

    audio (PipeWire, shared with the capture hub) -> chunks of 4-10 s cut at pauses
      -> whisper-server (small q8, VAD, short audio_ctx) -> /dev/shm/captions.json

/dev/shm/captions.json: {"lang": "es", "ready_until": <epoch ms of last processed
audio>, "captions": [{"start": ms, "end": ms, "text": "..."}]} — last 15 minutes.
Language comes from /dev/shm/caption-lang (es | en | auto | off), set by the app
through capture/key_relay.py; "off" stops transcribing.

Key-word glosses: each new subtitle line is sent (text only) to Gemini, which
picks up to 6 words a beginner wouldn't know and gives their English meaning in
that sentence. Entries get "gloss": [{"w": "orgulloso", "en": "proud"}, ...];
lines are published first and glossed a moment later. Needs the API key in
~/.config/dashboard/gemini-key; without it, subtitles are plain.

Run via stream/captions.sh (starts the whisper server too).
"""
import array
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque

RATE = 16000
SOURCE = os.environ.get("CAPTURE_AUDIO", "alsa_input.usb-MACROSILICON_2109-02.analog-stereo")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8178/inference")
OUT = "/dev/shm/captions.json"
LANG_FILE = "/dev/shm/caption-lang"
CHUNK_MIN, CHUNK_MAX = 4.0, 10.0       # matches whisper-server -ac 512 (~10 s window)
KEEP_MS = 15 * 60 * 1000
GEMINI_KEY_FILE = os.path.expanduser("~/.config/dashboard/gemini-key")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")   # ~1 s, good glosses, cheapest
GLOSS_LEVEL = os.environ.get("GLOSS_LEVEL", "beginner")
VIEWER_FILE = "/dev/shm/captions-viewer"   # touched by key_relay.py on every /captions poll
VIEWER_TIMEOUT = 30                        # s: only call Gemini while the app is fetching subtitles
CAPTURE_LATENCY = float(os.environ.get("CAPTION_AUDIO_LATENCY", "0.15"))  # s, PipeWire->us

JUNK = re.compile(r"amara\.org|subt[ií]tulos (realizados|por)|suscr[ií]bete|gracias por ver|^\W*$", re.I)
BRACKETED = re.compile(r"^\s*[\[\(].*[\]\)]\s*$")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def lang():
    try:
        v = open(LANG_FILE).read().strip()
        return v if v in ("es", "en", "auto", "off") else "es"
    except FileNotFoundError:
        return "es"


def rms(a):
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def wav_bytes(samples):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def transcribe(samples, language):
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in (("response_format", "verbose_json"), ("language", language), ("temperature", "0"))]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="c.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode() + wav_bytes(samples) + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(WHISPER_URL, data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


class Store:
    def __init__(self):
        self.caps = deque()
        self.ready_until = 0
        self.lock = threading.Lock()

    def add(self, items, ready_until):
        with self.lock:
            self.caps.extend(items)
            self.ready_until = max(self.ready_until, ready_until)
            cutoff = ready_until - KEEP_MS
            while self.caps and self.caps[0]["end"] < cutoff:
                self.caps.popleft()
        self.write()

    def set_gloss(self, items, glosses):
        with self.lock:
            for it, g in zip(items, glosses):
                it["gloss"] = g
        self.write()

    def write(self):
        with self.lock:
            data = {"lang": lang(), "ready_until": self.ready_until, "captions": list(self.caps)}
            tmp = OUT + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, OUT)


# ---------------------------------------------------------------- key-word glosses (Gemini)

GLOSS_PROMPT = """You help a {level} learner of {language} watch TV with subtitles.
For each subtitle line below, pick at most 6 key words that a {level} would probably NOT know
and that matter for understanding the line. Skip:
- very common words (articles, pronouns, prepositions, ser/estar/tener/ir/hacer, top-300 words)
- names of people, places, teams, companies, brands, channels, and numbers
- cognates: words that look like their English meaning (director, sistema, televisión,
  presentar, momento, favorito, competencia) - an English speaker can guess those.
It is fine to pick fewer words, or none.
For each picked word give its English meaning IN THIS SENTENCE in 1-3 words (for verbs, the
meaning of this form, e.g. "metido" -> "put in"). Copy the word exactly as it appears.
Return JSON: one list per line, in the same order.

Lines:
{lines}"""

GLOSS_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"w": {"type": "STRING"}, "en": {"type": "STRING"}},
        "required": ["w", "en"]}},
}


def gemini_key():
    try:
        return open(GEMINI_KEY_FILE).read().strip() or None
    except FileNotFoundError:
        return None


def gloss_lines(lines, language):
    """[[{"w","en"}...] per line] from Gemini, or None if unavailable."""
    key = gemini_key()
    if not key or not lines:
        return None
    body = {
        "contents": [{"parts": [{"text": GLOSS_PROMPT.format(
            level=GLOSS_LEVEL, language={"es": "Spanish", "en": "English"}.get(language, "Spanish"),
            lines="\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines)))}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": GLOSS_SCHEMA,
                             "temperature": 0.2},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.load(r)
    text = resp["candidates"][0]["content"]["parts"][0]["text"]
    out = json.loads(text)
    out = (out + [[]] * len(lines))[:len(lines)]
    # keep only real, non-obvious words that occur in their line, max 6
    clean = []
    for line, gl in zip(lines, out):
        low = line.lower()
        clean.append([g for g in gl if g.get("w") and g["w"].lower() in low
                      and not obvious(g["w"], g.get("en", ""), line)][:6])
    return clean


def _fold(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def obvious(word, en, line):
    """Safety net for the prompt: cognates (director=director, favorito=favorite)
    and capitalised words in mid-sentence (names)."""
    import difflib
    w, e = _fold(word), _fold(en)
    if any(difflib.SequenceMatcher(None, w, part).ratio() >= 0.8 for part in e.split() or [e]):
        return True
    i = line.find(word)
    return word[:1].isupper() and i > 0 and line[:i].rstrip()[-1:] not in ".!?¡¿-—\"«"


def viewer_active():
    try:
        return time.time() - os.stat(VIEWER_FILE).st_mtime < VIEWER_TIMEOUT
    except FileNotFoundError:
        return False


def retry_after(err):
    """Seconds Gemini asks us to wait (429 RetryInfo), default 60."""
    try:
        body = json.loads(err.read())
        for d in body["error"].get("details", []):
            if "retryDelay" in d:
                return float(d["retryDelay"].rstrip("s")) + 1
    except Exception:
        pass
    return 60.0


def glosser(store, jobs):
    """Background: gloss each published chunk without holding up the subtitles.
    Only while someone is watching (the app polls /captions), and backs off
    when Gemini rate-limits instead of retrying every chunk."""
    paused_until = 0.0
    while True:
        items, language = jobs.get()
        if language not in ("es", "en") or not viewer_active() or time.time() < paused_until:
            continue
        t = time.time()
        try:
            gl = gloss_lines([i["text"].replace("\n", " ") for i in items], language)
        except urllib.error.HTTPError as e:
            wait = retry_after(e) if e.code == 429 else 30.0
            paused_until = time.time() + wait
            log(f"gloss failed: HTTP {e.code}; pausing glosses for {wait:.0f}s")
            continue
        except Exception as e:
            log("gloss failed:", str(e)[:200])
            continue
        if gl is not None:
            store.set_gloss(items, gl)
            log(f"glossed {sum(map(len, gl))} words in {time.time() - t:.1f}s: "
                f"{', '.join(g['w'] + '=' + g['en'] for line in gl for g in line)[:100]}")


def audio():
    """16 kHz mono PCM from the shared PipeWire source, with the wall-clock time
    of the first sample of each block."""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "pulse", "-i", SOURCE,
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    t0, n = None, 0
    while block := proc.stdout.read(3200):          # 0.1 s
        a = array.array("h", block)
        if t0 is None:
            t0 = time.time() - len(a) / RATE - CAPTURE_LATENCY
        yield t0 + n / RATE, a
        n += len(a)
    proc.wait()


def chunks():
    buf, start, win = array.array("h"), None, int(0.4 * RATE)
    for t, a in audio():
        if start is None:
            start = t
        buf.extend(a)
        dur = len(buf) / RATE
        if dur < CHUNK_MIN:
            continue
        quiet = rms(buf[-win:]) < 0.35 * max(rms(buf), 1.0)
        if quiet or dur >= CHUNK_MAX:
            cut = len(buf)
            if not quiet:   # no pause yet: cut at the quietest 0.4 s in the last 3 s
                lo = max(win, len(buf) - 3 * RATE)
                cut = min(range(lo, len(buf) - win + 1, win // 2), key=lambda i: rms(buf[i:i + win])) + win // 2
            yield start, buf[:cut]
            start += cut / RATE
            buf = buf[cut:]


def wrap(text, width=42):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]


def main():
    import queue
    store = Store()
    store.add([], int(time.time() * 1000))
    jobs = queue.Queue()
    threading.Thread(target=glosser, args=(store, jobs), daemon=True).start()
    # Read audio on its own thread. If the reader waits for Whisper, the audio
    # backs up in PipeWire and the sample-count timestamps drift behind real time.
    pending = queue.Queue()

    def reader():
        for c in chunks():
            pending.put(c)
    threading.Thread(target=reader, daemon=True).start()

    while True:
        start, samples = pending.get()
        if pending.qsize() > 3:      # Whisper can't keep up: drop old audio, stay near live
            log(f"behind: dropping {pending.qsize()} queued chunks")
            while pending.qsize() > 1:
                pending.get()
            continue
        end_ms = int((start + len(samples) / RATE) * 1000)
        language = lang()
        if language == "off":
            store.add([], end_ms)
            continue
        t = time.time()
        try:
            try:
                res = transcribe(samples, language)
            except Exception:   # 500 when VAD finds no speech and language=auto
                res = transcribe(samples, "es" if language == "auto" else language)
        except Exception as e:
            log("transcribe failed:", e)
            store.add([], end_ms)
            continue
        items = []
        for seg in res.get("segments", []):
            text = seg.get("text", "").strip()
            if not text or JUNK.search(text) or BRACKETED.match(text):
                continue
            s0, s1 = start + float(seg["start"]), start + float(seg["end"])
            cards = wrap(text)
            step = (s1 - s0) / len(cards)
            for k, card in enumerate(cards):
                items.append({"start": int((s0 + k * step) * 1000),
                              "end": int((s0 + (k + 1) * step) * 1000) + 400,
                              "text": card})
        store.add(items, end_ms)
        if items:
            detected = {"spanish": "es", "english": "en"}.get(str(res.get("language", "")).lower(), "es")
            jobs.put((items, language if language != "auto" else detected))
        lag = time.time() - (start + len(samples) / RATE)
        log(f"{len(samples)/RATE:4.1f}s {language} in {time.time()-t:4.1f}s, ready {lag:4.1f}s after speech: "
            f"{' / '.join(i['text'].replace(chr(10), ' ') for i in items)[:80]!r}")


if __name__ == "__main__":
    main()
