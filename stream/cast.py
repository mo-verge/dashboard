#!/usr/bin/env python3
"""Cast the capture hub's HLS stream to Chromecasts (Default Media Receiver).

    cast.py list                              # Cast devices on the LAN (mDNS only, no connection)
    cast.py cast                              # cast to "Living Room", then exit
    cast.py cast "Living Room" "Bedroom TV"   # one session per device (Google groups can't do video)
    cast.py cast --watch                      # cast and keep it playing (watchdog, runs until Ctrl+C)
    cast.py stop                              # stop casting (only quits the Default Media Receiver)
    cast.py cast --dry-run                    # check the stream + find devices, send nothing

The stream URL must use the Pi's LAN address (never localhost): the Chromecast
fetches it itself. Default: http://<this host's LAN IP>:8888/tv/index.m3u8.

Watchdog policy (--watch): re-cast when the receiver reports an error or the
live stream "finished" (an HLS stall), or when buffering doesn't recover within
--stall-timeout s. Stop watching a device when someone else takes it over (another
app, another cast, or a stop from the TV remote), so the TV is never hijacked back.
"""
import argparse
import logging
import os
import socket
import sys
import time
import urllib.request

import pychromecast

DMR_APP_ID = "CC1AD845"   # Google's Default Media Receiver
DEFAULT_NAMES = [n for n in os.environ.get("CAST_DEVICES", "Living Room").split(",") if n]
DEFAULT_HOSTS = [h for h in os.environ.get("CAST_HOSTS", "192.168.50.79").split(",") if h]

log = logging.getLogger("cast")


def lan_ip(probe="192.168.50.1"):
    """This host's address on the LAN (the route towards the router); no packet is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((probe, 9))
        return s.getsockname()[0]


def default_url():
    return os.environ.get("CAST_URL") or f"http://{lan_ip()}:8888/tv/index.m3u8"


def stream_ready(url, timeout=60.0):
    """Wait until MediaMTX serves the playlist (it follows MediaMTX's cookieCheck 302)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                body = r.read(4096).decode("utf-8", "replace")
                if r.status == 200 and body.startswith("#EXTM3U"):
                    return True
                last = f"HTTP {r.status}"
        except Exception as e:  # noqa: BLE001 - any failure means "not yet"
            last = str(e)
        time.sleep(1)
    log.error("stream not ready at %s (%s)", url, last)
    return False


def media_info(args):
    if args.no_hls_hints:
        return {}
    # Tell the receiver the segments are MPEG-TS (H.264 + AAC-LC), not fMP4.
    return {"hlsSegmentFormat": "ts", "hlsVideoSegmentFormat": "mpeg2_ts"}


def find(names, hosts, timeout=10):
    casts, browser = pychromecast.get_listed_chromecasts(
        friendly_names=names, known_hosts=hosts or None, discovery_timeout=timeout)
    found = {c.cast_info.friendly_name for c in casts}
    for n in names:
        if n not in found:
            log.error("Chromecast %r not found (discovered: %s)", n, ", ".join(sorted(found)) or "none")
    return casts, browser


def load(cc, url, args):
    cc.wait(timeout=20)
    mc = cc.media_controller
    mc.play_media(url, "application/x-mpegURL", title=args.title,
                  stream_type="LIVE", autoplay=True, media_info=media_info(args))
    mc.block_until_active(timeout=30)
    log.info("%s: casting %s", cc.cast_info.friendly_name, url)


# ---------------------------------------------------------------- commands

def cmd_list(args):
    import zeroconf
    zconf = zeroconf.Zeroconf()
    browser = pychromecast.discovery.CastBrowser(
        pychromecast.discovery.SimpleCastListener(), zconf, args.hosts or None)
    browser.start_discovery()
    time.sleep(args.timeout)
    devices = list(browser.devices.values())
    browser.stop_discovery()
    zconf.close()
    for d in sorted(devices, key=lambda d: d.friendly_name or ""):
        print(f"{d.friendly_name}\t{d.model_name}\t{d.cast_type}\t{d.host}:{d.port}")
    return 0 if devices else 1


def cmd_cast(args):
    url = args.url or default_url()
    if "://localhost" in url or "://127." in url:
        log.error("the Chromecast can't reach %s: use the Pi's LAN IP", url)
        return 2
    if not stream_ready(url, timeout=args.ready_timeout):
        return 1
    casts, browser = find(args.names, args.hosts)
    if not casts:
        browser.stop_discovery()
        return 1
    if args.dry_run:
        for cc in casts:
            i = cc.cast_info
            print(f"would cast to {i.friendly_name} ({i.model_name}, {i.host}): LOAD {url} "
                  f"application/x-mpegURL LIVE {media_info(args)}")
        browser.stop_discovery()
        return 0
    rc = 0
    for cc in casts:
        try:
            load(cc, url, args)
        except Exception as e:  # noqa: BLE001
            log.error("%s: cast failed: %s", cc.cast_info.friendly_name, e)
            rc = 1
    if args.watch:
        try:
            watch(casts, url, args)
        except KeyboardInterrupt:
            pass
    browser.stop_discovery()
    return rc


def watch(casts, url, args):
    state = {cc.cast_info.friendly_name: {"stall_since": None, "backoff": 5} for cc in casts}
    active = list(casts)
    log.info("watching %s (Ctrl+C to stop watching; casting continues)",
             ", ".join(cc.cast_info.friendly_name for cc in active))
    while active:
        time.sleep(args.interval)
        for cc in list(active):
            name = cc.cast_info.friendly_name
            st = state[name]
            app = cc.app_id
            ms = cc.media_controller.status
            if not cc.socket_client.is_connected:
                continue                                  # pychromecast reconnects by itself
            if app != DMR_APP_ID:
                log.info("%s: now showing %s; stop watching", name, app or "the home screen")
                active.remove(cc)
                continue
            if ms.content_id and ms.content_id != url:
                log.info("%s: someone cast %s; stop watching", name, ms.content_id)
                active.remove(cc)
                continue
            reason = None
            if ms.player_state == "IDLE":
                if ms.idle_reason in ("ERROR", "FINISHED"):
                    reason = f"idle ({ms.idle_reason})"
                elif ms.idle_reason in ("CANCELLED", "INTERRUPTED"):
                    log.info("%s: stopped (%s); stop watching", name, ms.idle_reason)
                    active.remove(cc)
                    continue
            if ms.player_state == "BUFFERING":
                st["stall_since"] = st["stall_since"] or time.time()
                if time.time() - st["stall_since"] > args.stall_timeout:
                    reason = f"buffering > {args.stall_timeout:.0f}s"
            else:
                st["stall_since"] = None
            if ms.player_state == "PLAYING":
                st["backoff"] = 5
            if not reason:
                continue
            log.warning("%s: %s; re-casting in %ds", name, reason, st["backoff"])
            time.sleep(st["backoff"])
            st["backoff"] = min(st["backoff"] * 2, 60)
            st["stall_since"] = None
            if not stream_ready(url, timeout=60):
                continue
            try:
                load(cc, url, args)
            except Exception as e:  # noqa: BLE001
                log.error("%s: re-cast failed: %s", name, e)
    log.info("nothing left to watch")


def cmd_stop(args):
    casts, browser = find(args.names, args.hosts)
    rc = 0 if casts else 1
    for cc in casts:
        name = cc.cast_info.friendly_name
        cc.wait(timeout=20)
        if cc.app_id == DMR_APP_ID or args.force:
            if args.dry_run:
                print(f"would quit {cc.app_id} on {name}")
            else:
                cc.quit_app()
                log.info("%s: stopped", name)
        else:
            log.info("%s: not casting our stream (app %s); left alone (--force to quit it)",
                     name, cc.app_id or "none")
    browser.stop_discovery()
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hosts", nargs="*", default=DEFAULT_HOSTS,
                    help="known Chromecast IPs (used in addition to mDNS); env CAST_HOSTS")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list Cast devices (discovery only)")
    p.add_argument("--timeout", type=float, default=8, help="discovery time, s")

    p = sub.add_parser("cast", help="cast the stream")
    p.add_argument("names", nargs="*", default=DEFAULT_NAMES, help="device names; env CAST_DEVICES")
    p.add_argument("--url", help="stream URL; env CAST_URL; default http://<LAN IP>:8888/tv/index.m3u8")
    p.add_argument("--title", default="HDMI capture")
    p.add_argument("--watch", action="store_true", help="keep it playing (watchdog)")
    p.add_argument("--interval", type=float, default=5, help="watchdog poll interval, s")
    p.add_argument("--stall-timeout", type=float, default=30, help="re-cast after buffering this long, s")
    p.add_argument("--ready-timeout", type=float, default=60, help="wait this long for the HLS playlist, s")
    p.add_argument("--no-hls-hints", action="store_true", help="don't send hlsSegmentFormat hints")
    p.add_argument("--dry-run", action="store_true", help="check stream + devices, send nothing")

    p = sub.add_parser("stop", help="stop casting")
    p.add_argument("names", nargs="*", default=DEFAULT_NAMES)
    p.add_argument("--force", action="store_true", help="quit whatever app is running")
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("pychromecast").setLevel(logging.WARNING)
        logging.getLogger("zeroconf").setLevel(logging.ERROR)   # per-interface send errors are noise
    return {"list": cmd_list, "cast": cmd_cast, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
