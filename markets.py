"""BTC/CAD from Kraken's public API (no key): last price plus 24h of candles."""
import json
import time
import urllib.error
import urllib.request

KRAKEN = "https://api.kraken.com/0/public"
PAIR = "XBTCAD"
CACHE_SECONDS = 60
_cache = {"at": 0.0, "data": None}


def _get(path):
    req = urllib.request.Request(f"{KRAKEN}/{path}", headers={"User-Agent": "monet-dashboard"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.load(r)
    if body.get("error"):
        raise ValueError("; ".join(body["error"]))
    result = body["result"]
    return next(v for k, v in result.items() if k != "last")


def fetch_btc():
    ticker = _get(f"Ticker?pair={PAIR}")
    price = float(ticker["c"][0])

    # 15-minute candles: [time, open, high, low, close, vwap, volume, count]
    candles = _get(f"OHLC?pair={PAIR}&interval=15")
    since = time.time() - 24 * 3600
    day = [c for c in candles if c[0] >= since]
    ref = next((float(c[4]) for c in reversed(candles) if c[0] < since), float(day[0][1]))

    # Hourly candles for the candlestick view (4 x 15 min each).
    hourly = []
    for i in range(0, len(day), 4):
        chunk = day[i:i + 4]
        hourly.append({
            "t": chunk[0][0],
            "o": float(chunk[0][1]),
            "h": max(float(c[2]) for c in chunk),
            "l": min(float(c[3]) for c in chunk),
            "c": float(chunk[-1][4]),
        })

    return {
        "price": price,
        "ref_24h": ref,
        "change_pct": (price - ref) / ref * 100,
        "high_24h": max([price] + [float(c[2]) for c in day]),
        "low_24h": min([price] + [float(c[3]) for c in day]),
        "series": [[c[0], float(c[4])] for c in day] + [[int(time.time()), price]],
        "hourly": hourly,
        "fetched_at": time.time(),
    }


def btc_status():
    """Cached BTC payload for the cards, never raising."""
    if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        data = {"state": "ok", **fetch_btc()}
    except (OSError, ValueError, KeyError, StopIteration, urllib.error.URLError) as e:
        stale = _cache["data"] if _cache["data"] and _cache["data"].get("state") == "ok" else {}
        data = {**stale, "state": "stale" if stale else "error", "reason": str(e)}
    _cache.update(at=time.time(), data=data)
    return data


if __name__ == "__main__":
    d = fetch_btc()
    print({k: v for k, v in d.items() if k not in ("series", "hourly")}, len(d["series"]), len(d["hourly"]))
