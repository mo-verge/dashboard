# dashboard

Card dashboard for the Raspberry Pi 5 (`monet-wifi-2`) driving a 2560×1440 HDMI monitor.
A small Python server (`server.py`) serves `web/` to Chromium in kiosk mode.

```
scripts/deploy.sh      # rsync to the Pi, (re)start server + kiosk
scripts/kiosk.sh       # on the Pi: start server if needed, open Chromium full screen
http://127.0.0.1:8080/?demo   # render cards with sample data
```

The server binds to loopback only. From a Mac: `ssh -L 8080:127.0.0.1:8080 monet-wifi-2`, then browse `http://127.0.0.1:8080/`.

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
