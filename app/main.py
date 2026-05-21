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
        next_run_time=None,  # don't run immediately on boot
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler started: every %s min", settings.poll_interval_minutes)
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
