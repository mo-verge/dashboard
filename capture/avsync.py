#!/usr/bin/env python3
"""Measure audio/video sync of the stream the TV receives.

Play an A/V sync test on the box (YouTube: "audio video sync test" — a white
flash and a beep once per second), then:

    python3 capture/avsync.py [seconds] [url]

Reads the stream (default rtsp://127.0.0.1:8554/tv) with ffmpeg, logging per
frame mean brightness and per 10 ms audio RMS with their presentation
timestamps, finds flash onsets and beep onsets, pairs them and prints the
offset. Positive = audio LATER than video (raise the hub's AUDIO_ADVANCE by
that amount); negative = audio early.
"""
import os
import re
import statistics
import subprocess
import sys

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 30
URL = sys.argv[2] if len(sys.argv) > 2 else "rtsp://127.0.0.1:8554/tv"
# Visual event: abrupt change inside a region (fractions of the frame). The
# default fits "Adrian's Synctest" (YouTube), whose pie sweep resets on each
# beep; "full" = whole-frame flash tests.
ROI = os.environ.get("AVSYNC_ROI", "0.38,0.15,0.24,0.42")
V, A = "/dev/shm/avsync-v.txt", "/dev/shm/avsync-a.txt"


def read_meta(path, key):
    """ffmpeg metadata=print output -> [(pts_time, value)]"""
    out, t = [], None
    for line in open(path):
        m = re.search(r"pts_time:([-\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        if key in line and t is not None:
            v = line.split("=", 1)[1].strip()
            out.append((t, float(v) if v not in ("-inf", "inf", "nan") else -120.0))
    return out


def onsets(series, min_gap):
    """Rising edges through the midpoint between the quiet and loud levels."""
    vals = sorted(v for _, v in series)
    lo, hi = vals[len(vals) // 10], vals[-max(1, len(vals) // 50)]
    if hi - lo < 4:           # no clear events in this window
        return [], lo, hi
    thr = lo + (hi - lo) / 2
    ev, prev, last = [], None, -1e9
    for t, v in series:
        if prev is not None and prev < thr <= v and t - last >= min_gap:
            ev.append(t)
            last = t
        prev = v
    return ev, lo, hi


def main():
    for p in (V, A):
        if os.path.exists(p):
            os.unlink(p)
    if ROI == "full":
        crop = ""
    else:
        x, y, w, h = (float(v) for v in ROI.split(","))
        crop = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
    # per-frame mean of |frame - previous frame| in the region: spikes on resets/flashes
    fc = (f"[0:v]{crop}scale=96:96,format=gray,tblend=all_mode=difference,"
          f"signalstats,metadata=print:key=lavfi.signalstats.YAVG:file={V}[vo];"
          f"[0:a]aresample=48000,asetnsamples=n=480:p=0,astats=metadata=1:reset=1,"
          f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={A}[ao]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if URL.startswith("rtsp"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-t", str(SECONDS), "-i", URL, "-filter_complex", fc,   # -t on the input: ends both outputs
            "-map", "[vo]", "-f", "null", "-", "-map", "[ao]", "-f", "null", "-"]
    subprocess.run(cmd, check=True, timeout=SECONDS + 60)

    video = read_meta(V, "YAVG")
    audio = read_meta(A, "RMS_level")
    flashes, vlo, vhi = onsets(video, 0.4)
    beeps, alo, ahi = onsets(audio, 0.4)
    print(f"{len(video)} frames, {len(audio)} audio windows over {SECONDS:.0f}s")
    print(f"flashes: {len(flashes)} (brightness {vlo:.0f}..{vhi:.0f})   beeps: {len(beeps)} "
          f"(level {alo:.0f}..{ahi:.0f} dB)")
    pairs = []
    for f in flashes:
        near = min(beeps, key=lambda b: abs(b - f), default=None)
        if near is not None and abs(near - f) < 0.45:
            pairs.append(near - f)
    if len(pairs) < 3:
        print("not enough flash/beep pairs: is a sync test playing on the box?")
        return 1
    med = statistics.median(pairs)
    sd = statistics.pstdev(pairs)
    print(f"pairs: {len(pairs)}  offset median {med * 1000:+.0f} ms  spread ±{sd * 1000:.0f} ms  "
          f"(range {min(pairs) * 1000:+.0f}..{max(pairs) * 1000:+.0f} ms)")
    print("audio is LATER than video: raise AUDIO_ADVANCE by that much" if med > 0.02 else
          "audio is EARLIER than video: lower AUDIO_ADVANCE by that much" if med < -0.02 else
          "in sync (within 20 ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
