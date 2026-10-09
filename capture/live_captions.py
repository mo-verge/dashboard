#!/usr/bin/env python3
"""Continuous subtitles for the box's audio, timestamped in wall-clock time so the
Monet TV app can line them up with the delayed HLS stream (whose segments carry
EXT-X-PROGRAM-DATE-TIME).

    audio (PipeWire, shared with the capture hub) -> chunks of 3-7 s cut at pauses
      -> Gemini (audio in: transcript + key-word glosses in one call) -> /dev/shm/captions.json

Transcription moved from Whisper to Gemini (2026-10-08): whisper.cpp on the Pi took 1.1-1.6
cores next to the 1080p encoder and fell 20-29 s behind; Gemini answers a short chunk in
~1-2 s and the Pi does no speech work. The Whisper code below (transcribe(), the
whisper-server in stream/captions.sh) is kept but not in the functional path.
Gemini is only called while someone watches (see viewer_active()).

/dev/shm/captions.json: {"lang": "es", "ready_until": <epoch ms of last processed
audio>, "captions": [{"start": ms, "end": ms, "text": "..."}]} — last 15 minutes.
Language comes from /dev/shm/caption-lang (es | en | auto | off), set by the app
through capture/key_relay.py; "off" stops transcribing.

Key-word glosses come back in the same Gemini call: up to 6 words a beginner wouldn't
know, with their English meaning in that sentence. Entries get
"gloss": [{"w": "orgulloso", "en": "proud"}, ...]. Needs the API key in
~/.config/dashboard/gemini-key; without it there are no subtitles.

Run via stream/captions.sh.
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
CHUNK_MIN, CHUNK_MAX = 3.0, 7.0        # short chunks: lower delay; cut at pauses
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
    """Whisper (whisper-server). NOT IN THE FUNCTIONAL PATH since 2026-10-08: kept for
    reference / a later fallback. See gemini_transcribe()."""
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


TRANSCRIBE_PROMPT = """This is a short clip of {language} TV audio, part of a live stream.
1. "text": transcribe the speech exactly as spoken, in {language} (no translation, no
   speaker names, no descriptions of sounds or music). If nobody speaks, return "".
   The clip may start or end mid-word: transcribe only words you clearly hear.
2. "gloss": you help a {level} learner of {language} follow it. Pick at most 6 key words
   from your transcript that a {level} would probably NOT know and that matter for
   understanding. Skip very common words (articles, pronouns, prepositions,
   ser/estar/tener/ir/hacer, top-300 words), names of people, places, teams, companies,
   brands, channels, numbers, and cognates an English speaker can guess (director, sistema,
   televisión, presentar, momento). Fewer or none is fine. For each give its English
   meaning IN THIS SENTENCE in 1-3 words, and copy the word exactly as in the transcript."""

TRANSCRIBE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "text": {"type": "STRING"},
        "gloss": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"w": {"type": "STRING"}, "en": {"type": "STRING"}},
            "required": ["w", "en"]}},
    },
    "required": ["text", "gloss"],
}


def gemini_transcribe(samples, language):
    """{"text": ..., "gloss": [...]} for one audio chunk, from Gemini (audio in)."""
    import base64
    key = gemini_key()
    if not key:
        raise RuntimeError("no Gemini key")
    name = {"es": "Spanish", "en": "English"}.get(language, "Spanish or English")
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav_bytes(samples)).decode()}},
            {"text": TRANSCRIBE_PROMPT.format(language=name, level=GLOSS_LEVEL)},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": TRANSCRIBE_SCHEMA,
                             "temperature": 0},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.load(r)
    parts = resp["candidates"][0].get("content", {}).get("parts", [])
    return json.loads(parts[0]["text"]) if parts else {"text": "", "gloss": []}


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
    # same long stem, different ending: absolutamente/absolutely, perfectamente/perfectly
    if any(len(os.path.commonprefix([w, part])) >= max(5, 0.6 * min(len(w), len(part))) for part in e.split()):
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
    """NOT IN THE FUNCTIONAL PATH since 2026-10-08 (Gemini glosses inside the transcription
    call now). Was: background glossing of Whisper lines.
    Background: gloss each published chunk without holding up the subtitles.
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
        now = time.time() - CAPTURE_LATENCY        # wall clock of this block's last sample
        # Re-anchor on a clock step: the Pi has no RTC, so at boot the clock jumps when
        # NTP syncs (seen: 8 min) and sample-count times would stay that far off.
        if t0 is None or abs(now - (t0 + (n + len(a)) / RATE)) > 2:
            if t0 is not None:
                log(f"clock step {now - (t0 + (n + len(a)) / RATE):+.1f}s: re-anchoring audio time")
            t0 = now - (n + len(a)) / RATE
        yield t0 + n / RATE, a
        n += len(a)
    proc.wait()


def chunks():
    buf, win = array.array("h"), int(0.4 * RATE)
    for t, a in audio():
        buf.extend(a)
        start = t + len(a) / RATE - len(buf) / RATE   # follows audio()'s re-anchoring
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


def chunk_items(start, samples, res):
    """Subtitle cards for one transcribed chunk: wrapped to two lines, the chunk's time span
    shared out by text length (Gemini gives no word timestamps), glosses attached to the
    card that contains the word."""
    text = (res.get("text") or "").strip()
    if not text or JUNK.search(text) or BRACKETED.match(text):
        return []
    cards = wrap(text)
    total = sum(len(c) for c in cards)
    t, end, items = start, start + len(samples) / RATE, []
    gloss = [g for g in res.get("gloss", []) if g.get("w") and g["w"].lower() in text.lower()
             and not obvious(g["w"], g.get("en", ""), text)][:6]
    for card in cards:
        t1 = t + (end - t) * len(card) / max(1, total)
        items.append({"start": int(t * 1000), "end": int(t1 * 1000) + 400, "text": card,
                      "gloss": [g for g in gloss if g["w"].lower() in card.lower()]})
        total -= len(card)
        t = t1
    return items


def main():
    import queue
    from concurrent.futures import ThreadPoolExecutor
    store = Store()
    store.add([], int(time.time() * 1000))
    # Read audio on its own thread. If the reader waits for the transcriber, the audio
    # backs up in PipeWire and the sample-count timestamps drift behind real time.
    pending = queue.Queue()

    def reader():
        for c in chunks():
            pending.put(c)
    threading.Thread(target=reader, daemon=True).start()

    paused_until = [0.0]       # after a 429: wait for Gemini's retryDelay

    def work(start, samples, language):
        end_ms = int((start + len(samples) / RATE) * 1000)
        t = time.time()
        try:
            res = gemini_transcribe(samples, language)
        except urllib.error.HTTPError as e:
            wait = retry_after(e) if e.code == 429 else 20.0
            paused_until[0] = time.time() + wait
            log(f"Gemini HTTP {e.code}: pausing subtitles for {wait:.0f}s")
            store.add([], end_ms)
            return
        except Exception as e:
            log("transcribe failed:", str(e)[:200])
            store.add([], end_ms)
            return
        items = chunk_items(start, samples, res)
        store.add(items, end_ms)
        lag = time.time() - (start + len(samples) / RATE)
        log(f"{len(samples)/RATE:4.1f}s {language} in {time.time()-t:4.1f}s, ready {lag:4.1f}s after speech: "
            f"{' / '.join(i['text'].replace(chr(10), ' ') for i in items)[:80]!r} "
            f"[{', '.join(g['w'] + '=' + g['en'] for i in items for g in i['gloss'])[:60]}]")

    # A few chunks in flight: one slow answer doesn't hold up the next.
    pool = ThreadPoolExecutor(max_workers=3)
    while True:
        start, samples = pending.get()
        end_ms = int((start + len(samples) / RATE) * 1000)
        language = lang()
        # Gemini only while someone watches (TV CC mode, Pi preview with subtitles)
        if language == "off" or not viewer_active() or time.time() < paused_until[0]:
            store.add([], end_ms)
            continue
        if rms(samples) < 60:      # silence: nothing to transcribe
            store.add([], end_ms)
            continue
        pool.submit(work, start, samples, language)


if __name__ == "__main__":
    main()
