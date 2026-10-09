#!/usr/bin/env python3
"""On/off switches for the TV setup: subtitles, the Pi preview, casting.

One place that turns the three settings into running services, used by the phone
page (capture/key_relay.py), the dashboard card (server.py) and the preview's keys
(stream/mpv-input.conf):

  subtitles  Whisper + Gemini (captions.service). With the preview on, the preview
             plays PREVIEW_DELAY s behind live with the subtitles drawn on it.
  preview    full-screen mpv on the Pi's display (off = the dashboard shows); paused
             automatically while the TV stream runs
  cast       H.264 stream to MediaMTX for the Chromecast app. Not a user switch any more:
             on when someone goes full screen (Monet TV, the preview card), off after
             capture/key_relay.py sees no viewers for a few minutes.

The capture hub always runs (box.py / inventory.py need its frames); a change that
affects it rewrites its environment file and restarts monet-hub.service (~3 s).

    tvctl.py status
    tvctl.py set subtitles on|off      (also preview, cast)
    tvctl.py toggle subtitles
"""
import fcntl
import json
import os
import subprocess
import sys
import urllib.request

CONF = os.path.expanduser("~/.config/dashboard")
STATE = os.path.join(CONF, "tv.json")
HUB_ENV = os.path.join(CONF, "hub.env")         # EnvironmentFile of monet-hub.service
HUB, CAPTIONS = "monet-hub.service", "captions.service"
SUB_DELAY = 12          # s; Gemini lines are ready <= ~10 s after the speech (same as the TV app)
# Stream quality, measured on the Pi 5 (share of one core, 30 fps held):
#   720p ultrafast 3M 87% (blocky) | 720p veryfast 5M 117% | 1080p superfast 6M 177%
#   1080p veryfast 6M can't hold 30 fps.
CAST_SHARP = {"CAST_MODE": "1080p", "X264_PRESET": "superfast", "CAST_BITRATE": "6M", "X264_THREADS": "4"}
# Navigation on the TV uses the capture frames (motion JPEG, no encoder delay), so the H.264
# stream is only watched once settled: always 1080p. With subtitles, Whisper needs CPU:
# superfast (1.6 cores) left it ~1x real time and the subtitles fell 22-29 s behind (past
# the TV's CC delay); ultrafast at a higher bitrate (1.4 cores) holds ~29 fps and keeps the
# subtitles 4-15 s behind.
# (Whisper is out of the path since subtitles moved to Gemini: back to the sharp setting.)
CAST_SUBS = {"CAST_MODE": "1080p", "X264_PRESET": "ultrafast", "CAST_BITRATE": "8M", "X264_THREADS": "3"}  # unused
DEFAULTS = {"subtitles": True, "preview": True, "cast": False}
SWITCHES = tuple(DEFAULTS)


def _user_env():
    # The relay and the dashboard run as system services: point systemctl --user at
    # this user's manager.
    run = f"/run/user/{os.getuid()}"
    return {**os.environ, "XDG_RUNTIME_DIR": run, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus"}


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], env=_user_env(),
                          capture_output=True, text=True, timeout=30)


def _active(unit):
    return _systemctl("is-active", unit).stdout.strip() == "active"


def settings():
    try:
        with open(STATE) as f:
            return {**DEFAULTS, **{k: bool(v) for k, v in json.load(f).items() if k in DEFAULTS}}
    except (FileNotFoundError, ValueError):
        return dict(DEFAULTS)


def preview_active(s):
    """The Pi's own preview pauses while the TV stream runs (someone is watching on a TV):
    it costs ~0.5 core that the 1080p encoder needs. The "preview" setting is kept."""
    return s["preview"] and not s["cast"]


def hub_env(s):
    cast = CAST_SHARP if s["cast"] else {"CAST_MODE": "off"}
    pv = preview_active(s)
    return (f"PREVIEW={'mpv' if pv else 'none'}\n"
            f"PREVIEW_DELAY={SUB_DELAY if pv and s['subtitles'] else 0}\n"
            + "".join(f"{k}={v}\n" for k, v in cast.items()))


MEDIAMTX_API = "http://127.0.0.1:9997/v3/paths/get/tv"


def viewers():
    """Readers of the TV stream (Monet TV over RTSP/HLS, browsers), or None if unknown."""
    try:
        with urllib.request.urlopen(MEDIAMTX_API, timeout=3) as r:
            return len(json.load(r).get("readers", []))
    except (OSError, ValueError):
        return None                 # stream off / MediaMTX restarting


def status():
    s = settings()
    hub, cap = _active(HUB), _active(CAPTIONS)
    return {**s, "hub": hub, "captions": cap, "viewers": viewers() if s["cast"] else 0,
            "preview_active": preview_active(s),
            "delay": SUB_DELAY if preview_active(s) and s["subtitles"] else 0}


def apply(changes):
    """Set some switches ({"cast": True, ...}) and bring the services in line."""
    bad = set(changes) - set(SWITCHES)
    if bad:
        raise ValueError(f"unknown switch: {', '.join(sorted(bad))}")
    os.makedirs(CONF, exist_ok=True)
    with open(os.path.join(CONF, "tv.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)          # one change at a time
        s = {**settings(), **{k: bool(v) for k, v in changes.items()}}
        with open(STATE + ".tmp", "w") as f:
            json.dump(s, f)
        os.replace(STATE + ".tmp", STATE)
        env = hub_env(s)
        try:
            old = open(HUB_ENV).read()
        except FileNotFoundError:
            old = None
        if env != old:
            with open(HUB_ENV, "w") as f:
                f.write(env)
        # enable/disable so the choice survives a reboot
        _systemctl("enable" if s["subtitles"] else "disable", "--now", CAPTIONS)
        if env != old or not _active(HUB):
            _systemctl("restart", HUB)
    return status()


def main(argv):
    if argv[:1] == ["status"] or not argv:
        print(json.dumps(status()))
    elif len(argv) == 3 and argv[0] == "set" and argv[1] in SWITCHES and argv[2] in ("on", "off"):
        print(json.dumps(apply({argv[1]: argv[2] == "on"})))
    elif len(argv) == 2 and argv[0] == "toggle" and argv[1] in SWITCHES:
        print(json.dumps(apply({argv[1]: not settings()[argv[1]]})))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
