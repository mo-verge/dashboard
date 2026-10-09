#!/usr/bin/env bash
# Single owner of the HDMI capture adapter (MS2109). One ffmpeg reads the
# MJPEG video and the PipeWire audio once and feeds:
#
#   1) the local full-screen preview: untouched MJPEG + PCM in live Matroska -> mpv
#      (mpv plays the audio too, in sync; no pw-loopback)
#   2) /dev/shm/box.jpg: the adapter's raw 1920x1080 MJPEG frames, for capture/box.py shot()
#   3) the cast stream: H.264 + AAC -> MediaMTX rtsp://127.0.0.1:8554/tv -> HLS for Chromecast
#
# Replaces scripts/capture-preview.sh (do not run both: only one process can open /dev/video0).
#
#   ~/stream/capture-hub.sh                                  # cast branch 720p30
#   CAST_MODE=540p ~/stream/capture-hub.sh                   # cheapest cast (MJPEG decoded at half size)
#   CAST_MODE=1080p X264_THREADS=0 ~/stream/capture-hub.sh   # full res, x264 on all cores
#   CAST_MODE=off ~/stream/capture-hub.sh                    # preview + frames only (like capture-preview.sh)
#   PREVIEW=none ~/stream/capture-hub.sh                     # headless: frames + cast, no window
#   PREVIEW_DELAY=18 CAST_MODE=off ~/stream/capture-hub.sh   # preview 18 s behind live with Whisper subtitles
#
# q / Esc in the preview turn the preview off and c toggles subtitles (mpv-input.conf ->
# capture/tvctl.py; under monet-hub.service). Exit 42 from mpv still means "quit" when the hub
# is run by hand. Any other exit (ffmpeg error,
# MediaMTX restart, mpv crash) restarts the pipeline after a short pause.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------- knobs
CAST_MODE="${CAST_MODE:-720p}"           # 720p | 540p | 1080p | off
X264_THREADS="${X264_THREADS:-2}"        # libx264 threads; 0 = auto (all cores)
X264_PRESET="${X264_PRESET:-ultrafast}"  # ultrafast | superfast | veryfast ...
CAST_BITRATE="${CAST_BITRATE:-}"         # default by mode: 540p 2M, 720p 3M, 1080p 5M
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
PREVIEW="${PREVIEW:-mpv}"                # mpv | none
AUDIO_DELAY="${AUDIO_DELAY:-0}"          # preview only: mpv --audio-delay (s, + = audio later)
# >0: the preview plays this many seconds behind live and shows the subtitles from
# /dev/shm/captions.json (stream/captions.sh), like the TV app's CC mode. Whisper lines are
# ready ~17 s after the speech. The buffer is MJPEG: ~5.5 MB/s, ~100 MB at 18 s.
PREVIEW_DELAY="${PREVIEW_DELAY:-0}"
# The audio reaches ffmpeg through PipeWire's buffer, so with wall-clock
# timestamps it is stamped (and plays) later than the video. Shift it earlier.
# A/V offset left after the timestamp fix, measured with capture/avsync.py on a sync
# test (2026-10-02): -21/-23 ms with 0 here, +18 ms with -0.023 (the meter resolves one
# 30 fps frame, 33 ms), i.e. ~-10 ms real: imperceptible, so no correction needed.
# Positive = move audio earlier, negative = later.
AUDIO_ADVANCE="${AUDIO_ADVANCE:-0}"
SCREEN_W="${SCREEN_W:-2560}" SCREEN_H="${SCREEN_H:-1440}"
# By-id name: /dev/videoN numbering changes when another camera (a webcam) is plugged in.
DEV="${CAPTURE_DEVICE:-/dev/v4l/by-id/usb-MACROSILICON_2109-video-index0}"
SOURCE="${CAPTURE_AUDIO:-alsa_input.usb-MACROSILICON_2109-02.analog-stereo}"
RTSP_URL="${RTSP_URL:-rtsp://127.0.0.1:8554/tv}"
RTSP_HOST="${RTSP_HOST:-127.0.0.1}" RTSP_PORT="${RTSP_PORT:-8554}"
FRAME_FILE="${FRAME_FILE:-/dev/shm/box.jpg}"        # capture/box.py LATEST
PIDFILE="${HUB_PIDFILE:-/dev/shm/capture-hub.pid}"  # read by box.py; mtime = last (re)start
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-error}"
VIDEO_QUEUE="${VIDEO_QUEUE:-8}"   # capture frames ffmpeg may hold (8 = 0.27 s); see IN_V
STALL_SECS="${STALL_SECS:-10}"   # no new frame for this long = stuck pipeline, restart it
# Test hooks (e.g. synthetic sources on a machine without the adapter):
HUB_VIDEO_IN="${HUB_VIDEO_IN:-}"   # replaces the v4l2 input args, word-split
HUB_AUDIO_IN="${HUB_AUDIO_IN:-}"   # replaces the pulse input args, word-split
PREVIEW_CMD="${PREVIEW_CMD:-}"     # replaces the mpv command line (reads Matroska on stdin)

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

log() { echo "capture-hub: $*" >&2; }

case "$CAST_MODE" in
  720p)  VF="scale=1280:720:flags=fast_bilinear,format=yuv420p"; BR="${CAST_BITRATE:-3M}" ;;
  540p)  VF="format=yuv420p";                                    BR="${CAST_BITRATE:-2M}" ;;  # -lowres 1 -> 960x540
  1080p) VF="format=yuv420p";                                    BR="${CAST_BITRATE:-5M}" ;;
  off)   VF="" BR="" ;;
  *) log "CAST_MODE must be 720p, 540p, 1080p or off"; exit 2 ;;
esac
case "$PREVIEW" in mpv|none) ;; *) log "PREVIEW must be mpv or none"; exit 2 ;; esac

# ---------------------------------------------------------------- refuse to fight over the device
pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

if [ -f "$PIDFILE" ] && pid_alive "$(cat "$PIDFILE" 2>/dev/null)"; then
  log "another capture-hub is running (pid $(cat "$PIDFILE"))"; exit 1
fi
if [ -z "$HUB_VIDEO_IN" ]; then
  if pgrep -f "scripts/capture-preview.sh" >/dev/null 2>&1; then
    log "scripts/capture-preview.sh is running; stop it first (q in its window)"; exit 1
  fi
  if [ -f /dev/shm/box-tap.pid ] && pid_alive "$(cat /dev/shm/box-tap.pid 2>/dev/null)"; then
    log "box.py frame tap is running; stop it first: python3 ~/dashboard/capture/box.py tap stop"; exit 1
  fi
  if users="$(fuser "$DEV" 2>/dev/null)" && [ -n "${users// /}" ]; then
    log "$DEV is already open by pid(s):$users"; exit 1
  fi
fi

# ---------------------------------------------------------------- ffmpeg arguments
if [ -n "$HUB_VIDEO_IN" ]; then
  # shellcheck disable=SC2206
  IN_V=( $HUB_VIDEO_IN )
else
  # Timestamps: the kernel's capture time of each frame (uvcvideo, CLOCK_MONOTONIC)
  # converted to wall clock, not "when ffmpeg happened to read it".
  # Small input queue: when the encoder falls behind, frames are dropped at the device
  # instead of queueing. With 512 (17 s) a saturated 1080p encoder made EVERY output
  # (frames, previews, stream) lag ~10 s behind the box.
  IN_V=( -thread_queue_size "$VIDEO_QUEUE" -ts mono2abs )
  # 540p: the MJPEG decoder for the cast branch decodes at half size (cheap).
  # Stream-copied outputs (preview, box.jpg) are not affected.
  [ "$CAST_MODE" = 540p ] && IN_V+=( -lowres:v 1 )
  # Always capture 1920x1080@30: box.py / inventory.py coordinates assume 1080p frames.
  IN_V+=( -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 -i "$DEV" )
fi
[ -n "$HUB_VIDEO_IN" ] && [ "$CAST_MODE" = 540p ] && IN_V=( -lowres:v 1 "${IN_V[@]}" )
if [ -n "$HUB_AUDIO_IN" ]; then
  # shellcheck disable=SC2206
  IN_A=( $HUB_AUDIO_IN )
else
  # Pulse input stamps wall clock minus the stream's reported latency (its
  # default); -use_wallclock_as_timestamps used to override that with the
  # bursty arrival time. AUDIO_ADVANCE is the remaining constant offset,
  # measured with capture/avsync.py.
  IN_A=( -thread_queue_size 1024 -itsoffset "$(awk "BEGIN { print -($AUDIO_ADVANCE) }")"
         -f pulse -sample_rate 48000 -channels 2 -i "$SOURCE" )
fi

OUT_PREVIEW=( -map 0:v -map 1:a -c:v copy -c:a pcm_s16le -f matroska -live 1 pipe:1 )
OUT_FRAMES=( -map 0:v -c:v copy -f image2 -update 1 -atomic_writing 1 "$FRAME_FILE" )
OUT_CAST=( -map 0:v -map 1:a -vf "$VF"
           -c:v libx264 -preset "$X264_PRESET" -tune zerolatency -profile:v main -level:v 4.0
           -g 30 -keyint_min 30 -sc_threshold 0 -b:v "$BR" -maxrate "$BR" -bufsize "$BR"
           -threads "$X264_THREADS"
           -af aresample=async=1 -c:a aac -b:a "$AUDIO_BITRATE" -ac 2 -ar 48000
           -f rtsp -rtsp_transport tcp "$RTSP_URL" )

MPV=( mpv --no-terminal --force-window=yes
      --title="HDMI capture · hub · cast $CAST_MODE"   # labwc rc.xml fullscreens "HDMI capture*"
      --no-border --geometry="${SCREEN_W}x${SCREEN_H}+0+0"
      --audio-delay="$AUDIO_DELAY" --input-conf="$HERE/mpv-input.conf" )
if [ "$PREVIEW_DELAY" = 0 ]; then
  MPV+=( --profile=low-latency --cache=no --demuxer-readahead-secs=0 - )
else
  # mpv keeps reading the pipe while the script holds playback PREVIEW_DELAY s behind it.
  # A full buffer would block ffmpeg (and box.jpg), so leave plenty of room.
  MPV+=( --cache=yes --cache-secs=$((PREVIEW_DELAY * 4)) --demuxer-readahead-secs=$((PREVIEW_DELAY * 4))
         --demuxer-max-bytes=$((PREVIEW_DELAY * 4 * 6))MiB --demuxer-max-back-bytes=0
         --script="$HERE/mpv-captions.lua" --script-opts=captions-delay="$PREVIEW_DELAY" - )
fi

rtsp_up() { (exec 3<>"/dev/tcp/$RTSP_HOST/$RTSP_PORT") 2>/dev/null; }

# ---------------------------------------------------------------- lifecycle
RUN_PID="" WATCH_PID="" STALL_PID=""
stop_children() {
  for p in "$STALL_PID" "$WATCH_PID" "$RUN_PID"; do
    [ -n "$p" ] || continue
    pkill -TERM -P "$p" 2>/dev/null
    kill -TERM "$p" 2>/dev/null
  done
}
cleanup() { stop_children; rm -f "$PIDFILE" "$FRAME_FILE"; }
trap cleanup EXIT
trap 'exit 0' INT TERM HUP

FAILS=0
while true; do
  cast=1
  if [ "$CAST_MODE" = off ]; then
    cast=0
  elif ! rtsp_up; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do sleep 1; rtsp_up && break; done
    rtsp_up || { cast=0; log "MediaMTX not reachable at $RTSP_HOST:$RTSP_PORT: running without the cast branch"; }
  fi
  OUTS=()
  [ "$PREVIEW" = mpv ] && OUTS+=( "${OUT_PREVIEW[@]}" )
  OUTS+=( "${OUT_FRAMES[@]}" )
  [ "$cast" = 1 ] && OUTS+=( "${OUT_CAST[@]}" )
  FF=( ffmpeg -hide_banner -nostdin -y -loglevel "$FFMPEG_LOGLEVEL" "${IN_V[@]}" "${IN_A[@]}" "${OUTS[@]}" )

  echo $$ > "$PIDFILE"   # box.py: hub owns the device; frames older than this (+1 s) are stale
  log "start: cast=$CAST_MODE(on=$cast) x264 threads=$X264_THREADS preset=$X264_PRESET bitrate=${BR:-none} preview=$PREVIEW delay=${PREVIEW_DELAY}s audio_advance=${AUDIO_ADVANCE}s"
  if [ "$PREVIEW" = mpv ]; then
    if [ -n "$PREVIEW_CMD" ]; then
      ( "${FF[@]}" | bash -c "$PREVIEW_CMD"; s=("${PIPESTATUS[@]}"); log "ffmpeg=${s[0]} preview=${s[1]}"; exit "${s[1]}" ) &
    else
      ( "${FF[@]}" | "${MPV[@]}"; s=("${PIPESTATUS[@]}"); log "ffmpeg=${s[0]} mpv=${s[1]}"; exit "${s[1]}" ) &
    fi
  else
    ( "${FF[@]}"; s=$?; log "ffmpeg=$s"; exit 1 ) &
  fi
  RUN_PID=$!
  WATCH_PID=""
  if [ "$CAST_MODE" != off ] && [ "$cast" = 0 ]; then
    # MediaMTX came back: restart ffmpeg so the cast branch is added (preview blinks once).
    ( until rtsp_up; do sleep 5; done; log "MediaMTX is back: restarting to add the cast branch"
      pkill -TERM -P "$RUN_PID" -x ffmpeg ) &
    WATCH_PID=$!
  fi
  # Stall watchdog: ffmpeg can hang without exiting (seen: the PipeWire audio input stopped
  # after an HDMI-audio driver warning at boot; ffmpeg spun at ~160% CPU writing nothing and
  # ignored SIGTERM). No new frame for STALL_SECS -> SIGKILL it so the loop restarts.
  ( sleep 20
    while sleep 5; do
      age=$(( $(date +%s) - $(stat -c %Y "$FRAME_FILE" 2>/dev/null || echo 0) ))
      if [ "$age" -gt "$STALL_SECS" ]; then
        log "no new frame for ${age}s: killing the stuck pipeline"
        pkill -KILL -P "$RUN_PID" -x ffmpeg
        break
      fi
    done ) &
  STALL_PID=$!
  started=$SECONDS
  wait "$RUN_PID"; rc=$?
  for p in "$WATCH_PID" "$STALL_PID"; do [ -n "$p" ] && { kill "$p" 2>/dev/null; wait "$p" 2>/dev/null; }; done
  RUN_PID="" WATCH_PID="" STALL_PID=""
  if [ "$rc" = 42 ]; then log "quit from the preview window"; break; fi
  # back off when ffmpeg dies right away (device unplugged, source missing)
  if [ $((SECONDS - started)) -lt 10 ]; then FAILS=$((FAILS + 1)); else FAILS=0; fi
  pause=$(( FAILS < 10 ? FAILS + 1 : 10 ))
  log "pipeline ended (rc=$rc); restarting in ${pause}s"
  sleep "$pause" & wait $!   # interruptible: SIGTERM is handled at once, not after the sleep
done
