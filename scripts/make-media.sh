#!/usr/bin/env bash
# Generate the sample media for the prototype cards into web/media/.
# Run on the Pi (uses ffmpeg and the Raspberry Pi OS wallpapers).
set -euo pipefail
cd "$(dirname "$0")/../web"
mkdir -p media && cd media

cp /usr/share/rpd-wallpaper/{aurora,fjord,sand}.jpg .

# Slow abstract gradient loop; 720p H.264 is cheap to software-decode on a Pi 5.
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "gradients=s=1280x720:r=30:d=20:speed=0.015:nb_colors=4:c0=0x5b3cff:c1=0x00d1c1:c2=0xff4d8d:c3=0x0b0b1a:seed=7" \
  -vf "gblur=sigma=30,format=yuv420p" -c:v libx264 -preset slow -crf 26 -movflags +faststart flow.mp4

# A-major drone with slow tremolo, for the audio card.
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "sine=f=220:d=30" -f lavfi -i "sine=f=277.18:d=30" -f lavfi -i "sine=f=329.63:d=30" \
  -filter_complex "amix=inputs=3,volume=0.5,tremolo=f=0.25:d=0.6,afade=t=in:d=3,afade=t=out:st=26:d=4,aecho=0.8:0.8:600:0.4" \
  -c:a libopus -b:a 64k ambient.ogg

ls -la
