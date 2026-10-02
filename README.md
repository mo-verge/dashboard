# dashboard

Card dashboard for the Raspberry Pi 5 (`monet-wifi-2`) driving a 2560×1440 HDMI monitor.
A small Python server (`server.py`) serves `web/` to Chromium in kiosk mode.

```
scripts/deploy.sh      # rsync to the Pi, (re)start server + kiosk
scripts/kiosk.sh       # on the Pi: start server if needed, open Chromium full screen
http://127.0.0.1:8080/?demo   # render cards with sample data
```

The server binds to loopback only. From a Mac: `ssh -L 8080:127.0.0.1:8080 monet-wifi-2`, then browse `http://127.0.0.1:8080/`.

## BTC / CAD card

Kraken public API (no key): last trade from `Ticker`, 30 days of hourly candles from `OHLC` (720, Kraken's max per request). Change %, low/high and the dashed reference line are over the same 30 days (`markets.py`, cached 60s, served at `/api/btc`).

## Google Health (steps card)

Data comes from the [Google Health API](https://developers.google.com/health) (the Fitbit Web API is being shut down).
Credentials live on the Pi in `~/.config/dashboard/`, never in this repo.

1. [Google Cloud console](https://console.cloud.google.com/) → create a project (e.g. `monet-dashboard`).
2. APIs & Services → Library → enable **Google Health API**.
3. Google Auth Platform → Branding / Audience: user type **External**, publishing status **Testing**, add your Google account under **Test users**.
4. Data Access → add scope `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`.
5. Clients → Create client → **Desktop app** → download the JSON.
6. Copy it to the Pi: `scp client_secret_*.json monet-wifi-2:.config/dashboard/google-client.json`
7. Sign in: `ssh -L 8080:127.0.0.1:8080 monet-wifi-2`, open `http://127.0.0.1:8080/auth`, approve.

Your Google account must be linked in the Google Health phone app, otherwise the API returns `FAILED_PRECONDITION`.
Debug the raw API responses on the Pi with `python3 ~/dashboard/health.py`.

## HDMI capture, captions and the TVIP box

The TVIP IPTV box's HDMI goes into the Pi through a USB capture adapter (MS2109, `/dev/video0`).

```
scripts/capture-preview.sh [1080p|720p] [audio-delay]   # live full-screen preview with audio
scripts/capture-captions.sh                             # delayed preview + local Whisper Spanish captions
capture/box.py press <key> | shot <file>                 # drive the box / grab a frame
```

Captions use whisper.cpp (`~/whisper` on the Pi, small model, 8-bit, VAD) on 20–28 s chunks;
the picture is held back 50 s so each caption lands on its line.

The Pi controls the box as a fake Bluetooth remote — see [docs/tvip-remote.md](docs/tvip-remote.md).
Boot setup (dashboard server, kiosk, fake remote, Bluetooth identity): `deploy/install.sh`.

## Streaming to the TV (Chromecast with Google TV)

One ffmpeg (`stream/capture-hub.sh`) owns the capture adapter and feeds the local preview, the
raw frames for `capture/box.py`, and an H.264 + AAC encode published to MediaMTX
(`stream/mediamtx.yml`): RTSP on `:8554` (low latency, used by the TV app) and HLS on `:8888`
(for casting). Setup, test order and rollback: [stream/README.md](stream/README.md).

```
CAST_MODE=1080p X264_THREADS=0 ~/stream/capture-hub.sh     # on the Pi (720p while an inventory runs)
~/stream/venv/bin/python ~/stream/cast.py cast|stop          # cast HLS to "Living Room"
```

**Monet TV** (`tvapp/`) is a sideloaded Android TV app: one tile, opens straight into the RTSP
stream (~1 s lag, HLS fallback), forwards the Chromecast remote's D-pad / OK / Back to the TVIP box
via `capture/key_relay.py` (LAN :8180, token in `~/.config/dashboard/relay-token`), long-press OK =
channel picker from the inventory shortlists, long-press Back = exit.

**Subtitles.** `stream/captions.sh` (systemd user unit `captions`) runs whisper.cpp tuned for short
chunks (`-ac 512`: ~3 s per 10 s of audio) and `capture/live_captions.py`, which reads the box's audio
from PipeWire and writes wall-clock-timestamped Spanish/English subtitles to `/dev/shm/captions.json`
(served by the relay at `/captions`). Monet TV plays **live** (RTSP, ~1 s) while remote keys are being
pressed, and after 10 s idle switches to **subtitle mode**: HLS played 10 s behind live, each subtitle
matched to the frame via `EXT-X-PROGRAM-DATE-TIME`. Any key goes straight back to live. The subtitle
language follows the channel picked in the app (`POST /caption-lang/<es|en|auto|off>`).

**Key-word glosses.** Each subtitle line's text goes to Gemini (`gemini-flash-lite-latest`, ~1 s;
key in `~/.config/dashboard/gemini-key`, paid tier with prepaid AI Studio credit, the free tier
allows only 20 requests/day). It picks up to 6 words a beginner wouldn't know, skipping common
words, names and cognates, with their meaning in that sentence. The app shows them in yellow
with the English in small teal above. Only runs while the app is polling subtitles; backs off on 429.

`deploy/tv-install.sh <apk>` (run on the Pi) installs app updates, finding the Chromecast's
wireless-debugging port again when it changes.

Run the hub at 720p with `PREVIEW=none` while subtitles run (Whisper needs ~2 cores);
`AUDIO_ADVANCE` (default 0.12 s) fixes lip-sync on the TV.

```
cd tvapp && ./gradlew assembleRelease                         # needs JDK 17 + Android SDK 35; monet.properties holds the relay token
adb -s 192.168.50.79:<port> install -r app/build/outputs/apk/release/app-release.apk   # wireless debugging (run adb on the Pi)
python3 tvapp/art/make_art.py <dir with Geist TTFs>           # regenerate launcher art
```

## Channel inventory

`capture/inventory.py` walks every TVIP category with the fake remote and OCR (tesseract for names,
`capture/digits.py` templates for channel numbers), tuning Spanish/English/sports channels to read
the now/next EPG panel and check video/audio. `capture/enrich.py` merges it with the iptv-org
database. Results in `data/tvip/`: `channels.json` (all), `soccer.json`, `spanish.json`,
`english.json`, plus raw per-category captures in `data/tvip/channels/`.
