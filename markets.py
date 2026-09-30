"""Crypto prices in CAD from public APIs (no keys).

BTC: Kraken last price plus 30 days of hourly candles for the chart.
Small coin tiles: CoinGecko markets, with 30-day change to match the BTC card
(Kraken has no ADA/CAD pair).
"""
import json
import time
import urllib.error
import urllib.request

KRAKEN = "https://api.kraken.com/0/public"
PAIR = "XBTCAD"
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
COINS = {"eth": "ethereum", "ada": "cardano"}
CACHE_SECONDS = 60
_cache = {"at": 0.0, "data": None}
_coins_cache = {"at": 0.0, "data": None}


def _get(path):
    req = urllib.request.Request(f"{KRAKEN}/{path}", headers={"User-Agent": "monet-dashboard"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.load(r)
    if body.get("error"):
        raise ValueError("; ".join(body["error"]))
    result = body["result"]
    return next(v for k, v in result.items() if k != "last")


def fetch_btc(days=30):
    ticker = _get(f"Ticker?pair={PAIR}")
    price = float(ticker["c"][0])

    # Hourly candles: [time, open, high, low, close, vwap, volume, count].
    # Kraken returns at most 720 of them, which is exactly 30 days.
    candles = _get(f"OHLC?pair={PAIR}&interval=60")
    since = time.time() - days * 86400
    period = [c for c in candles if c[0] >= since]
    ref = float(period[0][1])

    return {
        "price": price,
        "days": days,
        "ref": ref,
        "change_pct": (price - ref) / ref * 100,
        "high": max([price] + [float(c[2]) for c in period]),
        "low": min([price] + [float(c[3]) for c in period]),
        "series": [[c[0], float(c[4])] for c in period] + [[int(time.time()), price]],
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


def fetch_coins():
    q = f"{COINGECKO}?vs_currency=cad&ids={','.join(COINS.values())}&price_change_percentage=30d"
    req = urllib.request.Request(q, headers={"User-Agent": "monet-dashboard"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.load(r)
    by_id = {c["id"]: c for c in body}
    return {sym: {"price": by_id[cg]["current_price"],
                  "change_pct": by_id[cg].get("price_change_percentage_30d_in_currency") or 0.0,
                  "days": 30}
            for sym, cg in COINS.items()}


def coins_status():
    """Cached {symbol: {price, change_pct}} for the small coin tiles, never raising."""
    if _coins_cache["data"] and time.time() - _coins_cache["at"] < CACHE_SECONDS:
        return _coins_cache["data"]
    try:
        data = {"state": "ok", "coins": fetch_coins(), "fetched_at": time.time()}
    except (OSError, ValueError, KeyError, urllib.error.URLError) as e:
        stale = _coins_cache["data"] if _coins_cache["data"] and _coins_cache["data"].get("state") == "ok" else {}
        data = {**stale, "state": "stale" if stale else "error", "reason": str(e)}
    _coins_cache.update(at=time.time(), data=data)
    return data


if __name__ == "__main__":
    print(fetch_coins())
