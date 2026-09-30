"""Google Health API access: OAuth (PKCE, loopback redirect) and step queries.

Credentials live outside the repo in ~/.config/dashboard/:
  google-client.json  OAuth client downloaded from Google Cloud (Desktop app)
  google-token.json   refresh/access token written after sign-in (mode 0600)
"""
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.path.expanduser(os.environ.get("DASHBOARD_CONFIG", "~/.config/dashboard"))
CLIENT_FILE = os.path.join(CONFIG_DIR, "google-client.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "google-token.json")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://health.googleapis.com/v4/users/me/dataTypes"
SCOPES = ["https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"]

CACHE_SECONDS = 60
_pending = {}  # state -> (code_verifier, redirect_uri)
_cache = {"at": 0.0, "data": None}


class NotConnected(Exception):
    pass


class ApiError(Exception):
    pass


# ---------- OAuth ----------

def _client():
    try:
        with open(CLIENT_FILE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise NotConnected(f"missing OAuth client file {CLIENT_FILE}")
    return raw.get("installed") or raw.get("web") or raw


def auth_url(redirect_uri):
    c = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _pending[state] = (verifier, redirect_uri)
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ApiError(f"{url} -> {e.code}: {e.read().decode(errors='replace')[:400]}")


def _save_token(tok):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def finish_auth(state, code):
    if state not in _pending:
        raise ApiError("unknown or expired sign-in attempt; start again at /auth")
    verifier, redirect_uri = _pending.pop(state)
    c = _client()
    tok = _post_form(TOKEN_URL, {
        "client_id": c["client_id"],
        "client_secret": c.get("client_secret", ""),
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    tok["obtained_at"] = time.time()
    _save_token(tok)
    _cache["at"] = 0


def _access_token():
    try:
        with open(TOKEN_FILE) as f:
            tok = json.load(f)
    except FileNotFoundError:
        raise NotConnected("not signed in")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if "refresh_token" not in tok:
        raise NotConnected("no refresh token; sign in again")
    c = _client()
    try:
        new = _post_form(TOKEN_URL, {
            "client_id": c["client_id"],
            "client_secret": c.get("client_secret", ""),
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        })
    except ApiError as e:
        if "invalid_grant" in str(e):
            raise NotConnected("sign-in expired; reconnect")
        raise
    tok.update(new)
    tok["expires_at"] = time.time() + new.get("expires_in", 3600) - 60
    _save_token(tok)
    return tok["access_token"]


def _call(path, body=None, query=None):
    """POST `body` as JSON, or GET with `query` params when there is no body."""
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ApiError(f"{path} -> {e.code}: {e.read().decode(errors='replace')[:600]}")


# ---------- steps ----------

def _count(value):
    """Pull the step count out of a StepsRollupValue, whatever its exact field name."""
    if not isinstance(value, dict):
        return 0
    for key in ("countSum", "count_sum", "count"):
        if key in value:
            return int(float(value[key]))
    nums = [v for v in value.values() if isinstance(v, (int, float, str)) and str(v).replace(".", "", 1).isdigit()]
    return int(float(nums[0])) if nums else 0


def _civil(d):
    # dailyRollUp ranges take CivilDateTime objects, not bare dates
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


def fetch_steps(days=7):
    now = dt.datetime.now().astimezone()
    today = now.date()
    first = today - dt.timedelta(days=days - 1)

    daily = _call("steps/dataPoints:dailyRollUp", {
        "range": {"start": _civil(first), "end": _civil(today + dt.timedelta(days=1))},
        "windowSizeDays": 1,
    })
    by_day = {}
    for p in daily.get("rollupDataPoints", []):
        s = p.get("civilStartTime", {})
        s = s.get("date", s)
        by_day[dt.date(s["year"], s["month"], s["day"])] = _count(p.get("steps"))
    week = [{"date": (first + dt.timedelta(days=i)).isoformat(),
             "steps": by_day.get(first + dt.timedelta(days=i), 0)} for i in range(days)]

    # Today at minute resolution: reconcile merges the tracker and phone streams
    # the same way the daily rollup does, and tells us how fresh the data is.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    query = {"filter": f'steps.interval.start_time >= "{midnight:%Y-%m-%dT%H:%M:%SZ}"', "pageSize": 10000}
    hours, latest = [0] * 24, None
    while True:
        page = _call("steps/dataPoints:reconcile", query=query)
        for p in page.get("dataPoints", []):
            iv = p["steps"]["interval"]
            start = _utc(iv["startTime"]).astimezone()
            if start.date() == today:
                hours[start.hour] += _count(p["steps"])
                latest = max(latest or 0, _utc(iv["endTime"]).timestamp())
        if not page.get("nextPageToken"):
            break
        query["pageToken"] = page["nextPageToken"]

    total = sum(hours)
    week[-1]["steps"] = total
    return {
        "today": total,
        "hours": hours,
        "week": week,
        "data_through": latest,
        "fetched_at": time.time(),
    }


def _utc(stamp):
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def steps_status():
    """Cached steps payload for the card, never raising."""
    if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        data = {"state": "ok", **fetch_steps()}
    except NotConnected as e:
        data = {"state": "disconnected", "reason": str(e)}
    except (ApiError, OSError, KeyError, ValueError) as e:
        stale = _cache["data"] if _cache["data"] and _cache["data"].get("state") == "ok" else {}
        data = {**stale, "state": "error" if not stale else "stale", "reason": str(e)}
    _cache.update(at=time.time(), data=data)
    return data


if __name__ == "__main__":
    # Debug: print raw API responses and the parsed payload.
    today = dt.date.today()
    print(json.dumps(_call("steps/dataPoints:dailyRollUp", {
        "range": {"start": _civil(today - dt.timedelta(days=2)), "end": _civil(today + dt.timedelta(days=1))},
        "windowSizeDays": 1,
    }), indent=2)[:3000])
    print(json.dumps(fetch_steps(), indent=2))
