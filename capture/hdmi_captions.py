#!/usr/bin/env python3
"""HDMI capture preview with local Whisper captions (desktop prototype).

  ffmpeg ──► matroska (MJPEG copy + PCM) ──► delay buffer (D s) ──► mpv
     └────► 16 kHz mono PCM ──► chunker ──► whisper-server ──► caption scheduler ──► mpv OSD

The picture and sound are held back by DELAY seconds so each caption can be
shown exactly when its line is spoken. Audio is cut into 20-28 s chunks at
quiet points so words are not split and Whisper's 30 s window is used fully, transcribed by a local
whisper.cpp server, and each segment is shown via mpv's IPC at
capture time + DELAY + OFFSET.

Run with scripts/capture-captions.sh; all knobs are environment variables.
"""
import array
import collections
import heapq
import io
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave

ENV = os.environ.get
DEVICE = ENV("CAPTURE_DEVICE", "/dev/v4l/by-id/usb-MACROSILICON_2109-video-index0")   # by-id: stable when a webcam is plugged in
AUDIO_SOURCE = ENV("CAPTURE_AUDIO", "alsa_input.usb-MACROSILICON_2109-02.analog-stereo")
SIZE, FPS = ENV("CAPTURE_SIZE", "1920x1080"), ENV("CAPTURE_FPS", "30")
DELAY = float(ENV("CAPTION_DELAY", "50"))       # seconds the picture is held back
OFFSET = float(ENV("CAPTION_OFFSET", "0.3"))    # extra shift for player latency
LANG = ENV("CAPTION_LANG", "auto")               # "auto" or e.g. "es"
# whisper-server 500s when VAD finds no speech and it then tries to auto-detect
# the language of nothing; a retry with a fixed language returns an empty result.
FALLBACK_LANG = ENV("CAPTION_FALLBACK_LANG", "es")
# Whisper always encodes a 30 s window, so shorter chunks cost the same: fill it.
CHUNK_MIN, CHUNK_MAX = 20.0, 28.0
WHISPER_URL = ENV("WHISPER_URL", "http://127.0.0.1:8178/inference")
MPV_SOCK = ENV("MPV_SOCK", "/tmp/hdmi-captions-mpv.sock")
RATE = 16000
LINE_CHARS = 42

# Whisper's well-known hallucinations on music/silence, mostly from subtitle credits.
JUNK = re.compile(r"amara\.org|subtítulos (realizados|por)|suscríbete|gracias por ver|^\W*$", re.I)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- capture

def start_ffmpeg():
    asr_r, asr_w = os.pipe()
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1",
        "-f", "v4l2", "-input_format", "mjpeg", "-video_size", SIZE, "-framerate", FPS, "-i", DEVICE,
        "-thread_queue_size", "1024", "-f", "pulse", "-i", AUDIO_SOURCE,
        # to the player: untouched MJPEG + PCM in a live Matroska stream
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "pcm_s16le",
        "-f", "matroska", "-live", "1", "pipe:1",
        # to the recogniser: 16 kHz mono PCM
        "-map", "1:a", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", "-f", "s16le", f"pipe:{asr_w}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, pass_fds=(asr_w,))
    os.close(asr_w)
    return proc, os.fdopen(asr_r, "rb", buffering=0)


def start_mpv():
    try:
        os.unlink(MPV_SOCK)
    except FileNotFoundError:
        pass
    cmd = [
        "mpv", "--title=HDMI capture · captions", "--no-terminal", "--force-window=yes",
        f"--input-ipc-server={MPV_SOCK}", "--cache=no", "--demuxer-readahead-secs=0",
        "--osd-align-x=center", "--osd-align-y=bottom", "--osd-margin-y=70",
        "--osd-font=DejaVu Sans", "--osd-font-size=46", "--osd-bold=yes",
        "--osd-color=#FFFFFFFF", "--osd-border-size=2.5", "--osd-border-color=#E0000000",
        "--osd-back-color=#99000000", "--osd-shadow-offset=0", "--osd-bar=no",
        "--osd-level=1", "-",
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def delay_pump(src, dst):
    """Forward bytes from ffmpeg to mpv, each chunk exactly DELAY s after it arrived."""
    q = collections.deque()
    cond = threading.Condition()

    def reader():
        while chunk := src.read(65536):
            with cond:
                q.append((time.monotonic(), chunk))
                cond.notify()
        with cond:
            q.append((None, b""))
            cond.notify()

    threading.Thread(target=reader, daemon=True).start()
    while True:
        with cond:
            while not q:
                cond.wait()
            t, chunk = q[0]
        if t is None:
            break
        wait = t + DELAY - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        with cond:
            q.popleft()
        try:
            dst.write(chunk)
            dst.flush()
        except BrokenPipeError:
            break


# ---------------------------------------------------------------- recognition

def rms(samples):
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def wav_bytes(samples):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def transcribe(samples, lang=LANG):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("response_format", "verbose_json"), ("language", lang), ("temperature", "0")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode() + wav_bytes(samples) + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(WHISPER_URL, data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def chunker(asr, jobs):
    """Cut the 16 kHz stream at quiet points into CHUNK_MIN..CHUNK_MAX second chunks."""
    buf = array.array("h")
    t0 = None            # monotonic capture time of buf[0]
    win = int(0.4 * RATE)
    while data := asr.read(3200):  # 0.1 s
        now = time.monotonic()
        a = array.array("h", data)
        if t0 is None:
            t0 = now - len(a) / RATE
        buf.extend(a)
        dur = len(buf) / RATE
        if dur < CHUNK_MIN:
            continue
        quiet = rms(buf[-win:]) < 0.35 * max(rms(buf), 1.0)
        if quiet or dur >= CHUNK_MAX:
            cut = len(buf)
            if not quiet:  # no pause yet: cut at the quietest 0.4 s in the last 3 s
                lo = max(win, len(buf) - 3 * RATE)
                cut = min(range(lo, len(buf) - win + 1, win // 2), key=lambda i: rms(buf[i:i + win])) + win // 2
            jobs.append((t0, buf[:cut]))
            t0 += cut / RATE
            buf = buf[cut:]


def wrap(text):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > LINE_CHARS:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    # at most two lines on screen; split longer segments into consecutive cards
    return ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]


def recogniser(jobs, schedule):
    busy, audio = 0.0, 0.0
    while True:
        if not jobs:
            time.sleep(0.05)
            continue
        t0, samples = jobs.popleft()
        started = time.monotonic()
        try:
            try:
                res = transcribe(samples)
            except Exception:  # no speech in the chunk (see FALLBACK_LANG), or still warming up
                time.sleep(0.5)
                res = transcribe(samples, FALLBACK_LANG)
        except Exception as e:
            log("transcribe failed, chunk dropped:", e)
            continue
        busy += time.monotonic() - started
        audio += len(samples) / RATE
        for seg in res.get("segments", []):
            text = seg.get("text", "").strip()
            if JUNK.search(text) or re.fullmatch(r"[\[\(].*[\]\)]", text):
                continue
            start, end = float(seg["start"]), float(seg["end"])
            cards = wrap(text)
            step = (end - start) / len(cards)
            for k, card in enumerate(cards):
                show = t0 + start + k * step + DELAY + OFFSET
                dur = max(1.5, min(step + 0.4, 7.0))
                schedule(show, card, dur)
        lag = time.monotonic() - (t0 + len(samples) / RATE)
        log(f"chunk {len(samples)/RATE:4.1f}s lang={res.get('language','?')} "
            f"ready {lag:4.1f}s after speech (budget {DELAY:.0f}s) · queue {len(jobs)} · "
            f"speed {audio/max(busy,1e-6):.2f}x · {res.get('text','').strip()[:60]!r}")


# ---------------------------------------------------------------- display

class Captions:
    def __init__(self):
        self.heap, self.cond = [], threading.Condition()
        self.sock = None

    def schedule(self, at, text, dur):
        with self.cond:
            heapq.heappush(self.heap, (at, text, dur))
            self.cond.notify()

    def _send(self, text, dur):
        for _ in range(2):
            try:
                if self.sock is None:
                    self.sock = socket.socket(socket.AF_UNIX)
                    self.sock.connect(MPV_SOCK)
                msg = {"command": ["show-text", text, int(dur * 1000)]}
                self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode())
                return
            except OSError:
                self.sock = None
                time.sleep(0.2)

    def run(self):
        while True:
            with self.cond:
                while not self.heap:
                    self.cond.wait()
                at, text, dur = self.heap[0]
                wait = at - time.monotonic()
                if wait > 0:
                    self.cond.wait(min(wait, 0.5))
                    continue
                heapq.heappop(self.heap)
            late = -wait
            if late < dur:  # still inside its window: show for the remainder
                self._send(text, dur - late)
            else:
                log(f"caption {late:.1f}s late, dropped: {text[:40]!r}")


def main():
    ff, asr = start_ffmpeg()
    mpv = start_mpv()
    caps = Captions()
    jobs = collections.deque()
    for target, args in ((chunker, (asr, jobs)), (recogniser, (jobs, caps.schedule)), (caps.run, ())):
        threading.Thread(target=target, args=args, daemon=True).start()
    log(f"capturing {SIZE}@{FPS}, picture delayed {DELAY:.0f}s, language={LANG}")
    try:
        delay_pump(ff.stdout, mpv.stdin)
    finally:
        for p in (ff, mpv):
            p.terminate()


if __name__ == "__main__":
    main()
