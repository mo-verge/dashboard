#!/usr/bin/env bash
# Desktop preview of the HDMI capture adapter (MS2109), to judge picture
# quality, latency and A/V sync before building the web card.
#
# Same pipeline the card will use: the adapter's own MJPEG frames are shown
# without re-encoding, and its audio is looped straight to the default output
# through PipeWire.
#
#   scripts/capture-preview.sh [1080p|720p] [audio-delay-seconds]
#   q or Esc in the video window quits; the audio loop stops with it.
set -euo pipefail

MODE="${1:-1080p}"
DELAY="${2:-0}"
DEV="${CAPTURE_DEVICE:-/dev/video0}"
SOURCE="${CAPTURE_AUDIO:-alsa_input.usb-MACROSILICON_2109-02.analog-stereo}"

case "$MODE" in
  1080p) SIZE=1920x1080 FPS=30 ;;
  720p)  SIZE=1280x720  FPS=60 ;;
  *) echo "mode must be 1080p or 720p" >&2; exit 2 ;;
esac

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

pw-loopback --name capture-preview-audio -C "$SOURCE" --latency 20 --delay "$DELAY" &
LOOP=$!
trap 'kill $LOOP 2>/dev/null || true' EXIT

# nobuffer/low_delay + framedrop keep the picture as close to live as ffplay allows.
# labwc refuses SDL fullscreen (and then -x/-y are ignored), so open a
# borderless window the size of the screen instead.
SDL_VIDEODRIVER=wayland ffplay -hide_banner -loglevel error \
  -window_title "HDMI capture · $MODE · audio delay ${DELAY}s" -noborder -x "${SCREEN_W:-2560}" -y "${SCREEN_H:-1440}" \
  -fflags nobuffer -flags low_delay -framedrop -sync ext \
  -f v4l2 -input_format mjpeg -video_size "$SIZE" -framerate "$FPS" "$DEV"
