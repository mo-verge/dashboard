"""Current weather for the dashboard's city cards, from Open-Meteo (free, no key).

One request covers every city. Values: temperature, feels-like (apparent temperature),
relative humidity, WMO weather code (turned into a short description + icon name),
day/night, and the city's local time.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.open-meteo.com/v1/forecast"
CITIES = {   # id: (name, latitude, longitude)
    "tehran": ("Tehran", 35.6892, 51.3890),
    "toronto": ("Toronto", 43.6532, -79.3832),
    "madrid": ("Madrid", 40.4168, -3.7038),
}
CACHE_SECONDS = 600      # Open-Meteo updates current conditions every 15 min
_cache = {"at": 0.0, "data": None}

# WMO weather interpretation codes -> (description, icon)
WMO = {
    0: ("Clear", "clear"), 1: ("Mostly clear", "clear"), 2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm, hail", "storm"), 99: ("Thunderstorm, hail", "storm"),
}


def fetch_weather():
    ids = list(CITIES)
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(CITIES[i][1]) for i in ids),
        "longitude": ",".join(str(CITIES[i][2]) for i in ids),
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,is_day",
        "timezone": "auto",
    })
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "monet-dashboard"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.load(r)
    rows = body if isinstance(body, list) else [body]     # one city -> object, several -> list
    out = {}
    for cid, row in zip(ids, rows):
        cur = row["current"]
        desc, icon = WMO.get(cur["weather_code"], ("—", "cloud"))
        out[cid] = {
            "name": CITIES[cid][0],
            "temp": cur["temperature_2m"],
            "feels": cur["apparent_temperature"],
            "humidity": cur["relative_humidity_2m"],
            "desc": desc,
            "icon": icon,
            "day": bool(cur["is_day"]),
            "utc_offset_s": row["utc_offset_seconds"],
        }
    return out


def weather_status():
    """Cached weather for the cards, never raising."""
    if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        data = {"state": "ok", "cities": fetch_weather(), "fetched_at": time.time()}
    except (OSError, ValueError, KeyError, urllib.error.URLError) as e:
        # keep showing the last good reading; cache the failure too (no retry per request)
        stale = _cache["data"] if _cache["data"] and _cache["data"].get("cities") else {}
        data = {**stale, "state": "stale" if stale else "error", "reason": str(e)}
    _cache.update(at=time.time(), data=data)
    return data
