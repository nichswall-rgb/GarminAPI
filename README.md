# Garmin Railway Proxy

A small FastAPI service that logs into Garmin Connect **once**, polls your metrics
every 30 minutes, caches them, and serves them to the T1D-Wins Android + iOS apps
over a simple API. Built to run unattended on [Railway](https://railway.com).

```
Garmin Connect  ──(every 30 min, server-side)──►  this proxy (cache)
                                                        ▲
T1D-Wins app  ──(every 3h + forced refresh)── GET /metrics/latest
```

## Why it's built this way

- **Server polls, phone reads.** Mobile background limits (Android Doze / iOS) make
  on-device scheduling unreliable. The server owns the 30-min cadence; the app just
  reads the cache instantly. Same backend serves both clients.
- **Login once, then hands-off.** Garmin OAuth tokens are persisted to a Railway
  **Volume** and auto-refreshed by `garth`. After the one-time login the service
  re-authenticates itself across restarts and redeploys with no input from you.
- **MFA-resilient.** If your Garmin account has 2FA, login is a two-step exchange
  (`/garmin/login` then `/garmin/login/mfa`). After that, tokens carry you.

## Endpoints

All except `/health` require header `X-API-Key: <PROXY_API_KEY>`.

| Method | Path                 | Purpose                                            |
|--------|----------------------|----------------------------------------------------|
| GET    | `/health`            | Status, token presence, last poll success.         |
| POST   | `/garmin/login`      | One-time bootstrap. Body or env: email/password.   |
| POST   | `/garmin/login/mfa`  | Submit the 2FA code if login asked for one.         |
| POST   | `/refresh`           | **Forced check-in** — poll Garmin now, return data. |
| GET    | `/metrics/latest`    | Cached metrics for the app's 3-hour read.          |

Metrics collected: daily stats, heart rate, steps, sleep, stress, body battery,
and recent activities (exercise time, HR, distance, cadence, elevation, pace,
calories).

## Deploy to Railway

1. Push this folder to a GitHub repo.
2. Railway → **New Project → Deploy from GitHub repo** → pick it. Nixpacks builds
   it automatically (Python detected via `requirements.txt`).
3. **Add a Volume** (Service → Settings → Volumes) mounted at **`/data`**.
   This is what makes login survive redeploys — don't skip it.
4. **Variables** (Service → Variables):
   - `PROXY_API_KEY` = a long random string (the app will send this).
   - `TIMEZONE` = e.g. `America/New_York`.
   - Leave `GARMIN_TOKEN_DIR=/data/garmin_tokens` and `DB_PATH=/data/garmin.db`.
   - `GARMIN_EMAIL` / `GARMIN_PASSWORD` are optional (you can pass them in the
     login request instead so they're never stored).
5. Deploy. Grab the public URL (Settings → Networking → Generate Domain).

## One-time login

Replace `URL` and `KEY` with your Railway domain and `PROXY_API_KEY`.

```bash
# Start login
curl -X POST https://URL/garmin/login \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpass"}'
```

- Response `{"mfa_required": false}` → done, you're logged in.
- Response `{"mfa_required": true}` → check your email/authenticator and submit it:

```bash
curl -X POST https://URL/garmin/login/mfa \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d '{"mfa_code":"123456"}'
```

Verify with `curl https://URL/health` — `has_tokens` should be `true`. From here
the service is hands-off.

> Note: the MFA step keeps the in-progress login in memory, so submit the code
> before the service restarts. If it restarts mid-login, just call `/garmin/login`
> again.

## App integration (T1D-Wins)

- **Every 3 hours / on app open:** `GET /metrics/latest` with the API key. Instant,
  reads cache only.
- **Force check-in button:** `POST /refresh` with the API key. Triggers a live poll
  and returns fresh data.
- Store `PROXY_API_KEY` in the app's secure storage, not in source.

## Local test

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then set PROXY_API_KEY and GARMIN_TOKEN_DIR=./data/tokens, DB_PATH=./data/garmin.db
uvicorn app.main:app --reload
```

## Staying stable (hands-off notes)

- **Pinned versions.** `garminconnect` and friends are pinned in `requirements.txt`.
  Garmin's unofficial endpoints change; pinning means a redeploy never silently
  pulls a breaking update. Upgrade deliberately, then re-test login.
- **Be gentle.** 30-min polling (~48 calls/day for one account) is well within safe
  bounds. Don't lower the interval much further.
- **Watch `/health`.** `last_success` tells you the last good poll. If it goes stale,
  Garmin likely changed something or the token expired — re-run the login.
- **ToS.** This uses Garmin's unofficial/private API. Fine for personal use; not
  appropriate to publish broadly. The official Garmin Health API is the long-term
  path if you ever ship this widely.
```
