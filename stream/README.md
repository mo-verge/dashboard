# HDMI capture → Chromecast kit

One ffmpeg process (`capture-hub.sh`) owns the MS2109 capture adapter and its
audio. It feeds three things at once:

```
/dev/video0 MJPEG 1080p30 ─┐                                  ┌─► mpv full-screen preview (MJPEG copy + PCM, A/V synced)
PipeWire MS2109 audio ─────┴─► capture-hub.sh (ffmpeg 5.1) ───┼─► /dev/shm/box.jpg  (raw frames for box.py / inventory.py)
                                                              └─► H.264 + AAC ─RTSP─► MediaMTX ─HLS(TS)─► Chromecast(s)
cast.py (pychromecast) ── tells each Chromecast to play http://192.168.50.160:8888/tv/index.m3u8
```

| File | Goes to | What it is |
|---|---|---|
| `mediamtx.yml` | `~/stream/` | MediaMTX v1.21.1 config: RTSP publish from localhost only, HLS (MPEG-TS, 1 s segments, CORS) on :8888 |
| `mediamtx.service` | `~/.config/systemd/user/` | systemd user unit for MediaMTX |
| `capture-hub.sh` | `~/stream/` | the capture hub; replaces `scripts/capture-preview.sh` |
| `mpv-input.conf` | `~/stream/` | makes q / Esc in the preview quit the hub (exit 42) instead of restarting it |
| `box.py.patch` | apply in `~/projects/dashboard` (Mac) | `shot()` reads the hub's frames; the tap is only used when nothing owns the device |
| `cast.py`, `requirements.txt` | `~/stream/` | `list` / `cast` / `cast --watch` / `stop` |

`~/stream` is outside `~/dashboard` on purpose: `scripts/deploy.sh` rsyncs with `--delete`.

Hub knobs (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `CAST_MODE` | `720p` | `720p` (1280x720 fast_bilinear, 3 Mb/s), `540p` (MJPEG decoded at half size via `-lowres 1`, 2 Mb/s, cheapest), `1080p` (5 Mb/s), `off` |
| `X264_THREADS` | `2` | libx264 threads; `0` = all cores |
| `X264_PRESET` | `ultrafast` | |
| `CAST_BITRATE` | per mode | e.g. `4M` |
| `PREVIEW` | `mpv` | `none` = headless (frames + cast only) |
| `AUDIO_DELAY` | `0` | preview only, seconds (mpv `--audio-delay`) |

The capture itself is always 1920x1080@30, because box.py and inventory.py coordinates assume 1080p frames.

Measured CPU on this Pi (synthetic 1080p MJPEG, nice 19, OCR running): 720p ≈ 0.75 core, 540p ≈ 0.45, 1080p ≈ 1.2 (no headroom). The preview costs what ffplay costs today.

---

## 0. Before you start

The channel inventory must be finished. It uses the capture (through box.py) and the remote.

```sh
ssh monet-wifi-2 'pgrep -af "inventory.py|capture-preview.sh|box-tap" ; fuser -v /dev/video0'
```

Expect only `capture-preview.sh` / `ffplay` (the preview) holding the device. `inventory.py` must be gone.

## 1. Install MediaMTX and the kit on the Pi

From the Mac:

```sh
KIT=~/projects/dashboard/stream
ssh monet-wifi-2 'mkdir -p ~/stream ~/.config/systemd/user'
scp "$KIT"/{mediamtx.yml,capture-hub.sh,mpv-input.conf,cast.py,requirements.txt} monet-wifi-2:stream/
scp "$KIT"/mediamtx.service monet-wifi-2:.config/systemd/user/
ssh monet-wifi-2 'cd ~/stream && chmod +x capture-hub.sh cast.py &&
  curl -fsSL -o /tmp/mediamtx.tgz https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_linux_arm64.tar.gz &&
  tar -xzf /tmp/mediamtx.tgz -C ~/stream mediamtx && rm /tmp/mediamtx.tgz && ./mediamtx --version'
ssh monet-wifi-2 'systemctl --user daemon-reload && systemctl --user enable --now mediamtx && sleep 1 &&
  systemctl --user --no-pager status mediamtx | head -5 && ss -ltn | grep -E ":(8554|8888) "'
```

Expect `127.0.0.1:8554` and `*:8888` listening. MediaMTX alone does not touch the capture, so this step is safe while the old preview is still running.

User units run while `verge`'s desktop session is up (Linger is off). To start MediaMTX at boot without a login, run `sudo loginctl enable-linger verge` (optional).

## 2. Patch box.py (Mac repo, then copy to the Pi)

```sh
cd ~/projects/dashboard
patch -p1 --dry-run < "$KIT/box.py.patch" && patch -p1 < "$KIT/box.py.patch"
scp capture/box.py monet-wifi-2:dashboard/capture/box.py   # just this file; deploy.sh would also restart the kiosk
```

The patch is safe to deploy before the hub. Without `/dev/shm/capture-hub.pid`, box.py behaves exactly as before (grim while ffplay runs, else its own tap).

Note: `capture/box.py` already has uncommitted changes in the Mac repo. The patch was made against that working-tree version, which is identical to the Pi's copy (md5 c787a300…). Commit after testing.

## 3. Switch the preview to the hub

On the Pi's desktop, press `q` in the "HDMI capture" window. Or, over ssh:

```sh
ssh monet-wifi-2 'pkill -f scripts/capture-preview.sh; pkill -x ffplay; sleep 1; rm -f /dev/shm/preview-wanted; fuser -v /dev/video0 || echo "device free"'
```

Then start the hub. Like the old preview, it runs detached in the desktop session:

```sh
ssh monet-wifi-2 'cd ~/stream && CAST_MODE=720p setsid nohup ./capture-hub.sh > ~/stream/hub.log 2>&1 < /dev/null & sleep 4; cat ~/stream/hub.log'
```

The hub refuses to start while `capture-preview.sh`, a box.py tap, or anything else holds `/dev/video0`. If MediaMTX is down, it runs preview + frames only, and adds the cast branch by itself once MediaMTX is back (the preview blinks once).

## 4. Test, in this order

1. **Preview:** the "HDMI capture · hub · …" window opens full screen (labwc rule `HDMI capture*`), with sound. Judge lip-sync, and adjust with `AUDIO_DELAY=0.1` etc.
2. **Frames for OCR:**
   ```sh
   ssh monet-wifi-2 'python3 ~/dashboard/capture/box.py shot /tmp/t.jpg && python3 -c "from PIL import Image; print(Image.open(\"/tmp/t.jpg\").size)"'
   ```
   Expect `(1920, 1080)`. These are now the adapter's raw frames instead of grim screenshots, so re-check a few OCR results (`inventory.py` thresholds were tuned on screenshots) before the next long run.
3. **CPU / heat:**
   ```sh
   ssh monet-wifi-2 'top -bn1 | head -15; vcgencmd measure_temp; vcgencmd get_throttled'
   ```
   If it's too hot or busy: restart the hub with `CAST_MODE=540p`. If CPU is free: `X264_THREADS=0` or `CAST_MODE=1080p`.
4. **The stream from the Mac** (no Chromecast involved):
   ```sh
   curl -sL "http://192.168.50.160:8888/tv/index.m3u8" | head    # expect #EXTM3U and CODECS="avc1...,mp4a.40.2"
   open -a VLC "http://192.168.50.160:8888/tv/index.m3u8"      # or Safari
   ```
5. **cast.py on the Pi:**
   ```sh
   ssh monet-wifi-2 'python3 -m venv ~/stream/venv && ~/stream/venv/bin/pip install -r ~/stream/requirements.txt'
   ssh monet-wifi-2 '~/stream/venv/bin/python ~/stream/cast.py list'              # expect: Living Room  Chromecast ... 192.168.50.79:8009
   ssh monet-wifi-2 '~/stream/venv/bin/python ~/stream/cast.py cast --dry-run'    # checks the stream + device, sends nothing
   ssh monet-wifi-2 '~/stream/venv/bin/python ~/stream/cast.py cast'              # the TV starts playing
   ssh monet-wifi-2 '~/stream/venv/bin/python ~/stream/cast.py stop'
   ```
   Measure the lag: put a clock on screen (or watch a news ticker) and compare the TV with the Pi's preview. Expect 4–8 s.
6. **Watchdog** (keeps it playing; re-casts on receiver error / stalled HLS; lets go if someone uses the TV for something else):
   ```sh
   ssh monet-wifi-2 'setsid nohup ~/stream/venv/bin/python ~/stream/cast.py cast --watch > ~/stream/cast.log 2>&1 < /dev/null &'
   ```
   Several TVs: `cast.py cast "Living Room" "Bedroom"`. That is one session per TV, and they won't be frame-synced. Google speaker groups can't play video.

**If the TV won't play** (see `journalctl --user -u mediamtx -f` on the Pi), try in order:
- `cast.py cast --url 'http://192.168.50.160:8888/tv/index.m3u8?cookieCheck=1'` (skips MediaMTX's cookie-check redirect);
- `cast.py cast --no-hls-hints`;
- check the Pi still has 192.168.50.160 (reserve it in the ASUS DHCP list).

## Rollback (back to scripts/capture-preview.sh)

```sh
ssh monet-wifi-2 '~/stream/venv/bin/python ~/stream/cast.py stop; pkill -f "cast.py cast --watch"'
ssh monet-wifi-2 'kill -TERM "$(cat /dev/shm/capture-hub.pid)"; sleep 2; fuser -v /dev/video0 || echo "device free"; ls /dev/shm/capture-hub.pid 2>/dev/null'
ssh monet-wifi-2 'cd ~/dashboard && setsid nohup scripts/capture-preview.sh 1080p 0 > /dev/null 2>&1 < /dev/null &'
ssh monet-wifi-2 'systemctl --user disable --now mediamtx'          # optional; it is idle without the hub
```

Pressing `q` in the hub window also stops the hub. The hub's exit trap stops ffmpeg and mpv and removes `/dev/shm/capture-hub.pid` and `/dev/shm/box.jpg`. With the pidfile gone, the patched box.py falls back to its old behaviour (grim while ffplay runs). It can stay; to revert it anyway:

```sh
cd ~/projects/dashboard && patch -R -p1 < "$KIT/box.py.patch" && scp capture/box.py monet-wifi-2:dashboard/capture/box.py
```

If the hub was killed with `-9` and left `/dev/shm/capture-hub.pid` behind, box.py ignores it (the pid is dead). Delete it anyway: `rm -f /dev/shm/capture-hub.pid`.
