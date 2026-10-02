#!/usr/bin/env bash
# Live Whisper subtitles for the Monet TV app: a whisper.cpp server tuned for
# short chunks (-ac 512 ~ 10 s audio window: ~3 s per 10 s chunk on the Pi 5
# next to the 1080p encode) plus capture/live_captions.py, which reads the
# box's audio from PipeWire (shared with the capture hub) and writes
# /dev/shm/captions.json.
set -euo pipefail
WHISPER_DIR="${WHISPER_DIR:-$HOME/whisper/whisper.cpp}"
MODEL="${WHISPER_MODEL:-$WHISPER_DIR/models/ggml-small-q8_0.bin}"
VAD_MODEL="${WHISPER_VAD_MODEL:-$WHISPER_DIR/models/ggml-silero-v6.2.0.bin}"
PORT="${WHISPER_PORT:-8178}"
THREADS="${WHISPER_THREADS:-3}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WHISPER_URL="http://127.0.0.1:$PORT/inference"

nice -n 5 "$WHISPER_DIR/build/bin/whisper-server" -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
  -t "$THREADS" -bs 1 -bo 1 -ac 512 -l es --vad --vad-model "$VAD_MODEL" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -fs "http://127.0.0.1:$PORT/" >/dev/null && break; sleep 0.5; done

python3 "$HOME/dashboard/capture/live_captions.py"
