#!/usr/bin/env bash
# HDMI capture preview with local Whisper captions (see capture/hdmi_captions.py).
#
#   scripts/capture-captions.sh            # Spanish captions, picture delayed 50 s
#   CAPTION_LANG=auto CAPTION_DELAY=60 scripts/capture-captions.sh
#   q in the video window quits; the whisper server stops with it.
set -euo pipefail
cd "$(dirname "$0")/.."

WHISPER_DIR="${WHISPER_DIR:-$HOME/whisper/whisper.cpp}"
MODEL="${WHISPER_MODEL:-$WHISPER_DIR/models/ggml-small-q8_0.bin}"
VAD_MODEL="${WHISPER_VAD_MODEL:-$WHISPER_DIR/models/ggml-silero-v6.2.0.bin}"
PORT="${WHISPER_PORT:-8178}"   # 8080 is the dashboard

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export WHISPER_URL="http://127.0.0.1:$PORT/inference"
# Fixed Spanish by default: auto-detect mislabels Spanish as Galician/Catalan on
# some chunks, and skipping detection is faster.
export CAPTION_LANG="${CAPTION_LANG:-es}"

# 3 threads leaves a core for decoding the video.
"$WHISPER_DIR/build/bin/whisper-server" -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
  -t 3 -bs 1 -bo 1 --vad --vad-model "$VAD_MODEL" >/tmp/whisper-server.log 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 60); do
  curl -fs "http://127.0.0.1:$PORT/" >/dev/null && break
  sleep 0.5
done

python3 capture/hdmi_captions.py
