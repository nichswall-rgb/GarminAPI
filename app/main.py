import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import db, garmin_client, poller
from .config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

scheduler = BackgroundScheduler(timezone=settings.timezone)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not settings.proxy_api_key:
        raise HTTPException(500, "PROXY_API_KEY is not configured on the server.")
    if x_api_key != settings.proxy_api_key:
        raise HTTPException(401, "Invalid or missing X-API-Key.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.add_job(
        poller.poll_once,
        "interval",
        minutes=settings.poll_interval_minutes,
        id="garmin_poll",
        # No next_run_time override: APScheduler 3.x treats next_run_time=None as
        # "add the job paused", so the interval never fired and data only landed
        # on a manual POST /refresh.
        max_instances=1,
        coalesce=True,
    )
    # IntervalTrigger defaults start_date to now + interval, so the job above
    # first fires one interval after boot. This one-shot runs immediately (in the
    # scheduler's own thread, off the request path) so a redeploy or restart
    # refills the cache right away instead of leaving it stale for an interval.
    scheduler.add_job(poller.poll_once, "date", id="garmin_poll_boot")
    scheduler.start()
    log.info("scheduler started: boot poll now, then every %s min",
             settings.poll_interval_minutes)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Garmin Railway Proxy", lifespan=lifespan)


class LoginBody(BaseModel):
    email: str | None = None
    password: str | None = None


class MFABody(BaseModel):
    mfa_code: str


@app.get("/health")
def health():
    return {
        "status": "up",
        "has_tokens": garmin_client.has_tokens(),
        "last_status": db.get_meta("last_status"),
        "last_success": db.get_meta("last_success"),
        "last_attempt": db.get_meta("last_attempt"),
        "poll_interval_minutes": settings.poll_interval_minutes,
    }


@app.post("/garmin/login", dependencies=[Depends(require_api_key)])
def garmin_login(body: LoginBody):
    email = body.email or settings.garmin_email
    password = body.password or settings.garmin_password
    if not email or not password:
        raise HTTPException(400, "email and password required (body or env).")
    try:
        return garmin_client.start_login(email, password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"login failed: {exc}")


@app.post("/garmin/login/mfa", dependencies=[Depends(require_api_key)])
def garmin_login_mfa(body: MFABody):
    try:
        return garmin_client.resume_login(body.mfa_code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"mfa failed: {exc}")


@app.post("/refresh", dependencies=[Depends(require_api_key)])
def refresh():
    """Forced check-in: poll Garmin now and return the result."""
    return poller.poll_once()


@app.get("/metrics/latest", dependencies=[Depends(require_api_key)])
def metrics_latest():
    """Cached read for the app's 3-hour check-in. Instant, never hits Garmin."""
    return {
        "last_success": db.get_meta("last_success"),
        "last_status": db.get_meta("last_status"),
        "metrics": db.get_all_snapshots(),
    }


@app.get("/activities", dependencies=[Depends(require_api_key)])
def activities(days: int = 30):
    """Recent activities WITHOUT the per-point series — a light index.

    Use `/activities/{id}` for the intraday HR/cadence/pace series.
    """
    days = max(1, min(days, settings.activity_retention_days))
    rows = db.get_activities(days)
    return {"days": days, "count": len(rows), "activities": rows}


@app.get("/activities/{activity_id}", dependencies=[Depends(require_api_key)])
def activity(activity_id: str):
    """One activity including its per-point series, splits and HR zones."""
    row = db.get_activity(activity_id)
    if row is None:
        raise HTTPException(404, f"activity {activity_id} not stored")
    return row


@app.get("/metrics/history", dependencies=[Depends(require_api_key)])
def metrics_history(days: int = settings.retention_days):
    """Retained overnight history for time-series analysis.

    Returns a flat list of {metric, day, fetched_at, data} rows for the last
    `days` days (clamped to the server's retention window). Instant, cached.
    """
    days = max(1, min(days, settings.retention_days))
    return {
        "last_success": db.get_meta("last_success"),
        "last_status": db.get_meta("last_status"),
        "days": days,
        "snapshots": db.get_history(days),
    }
