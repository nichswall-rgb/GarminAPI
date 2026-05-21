import logging
import traceback
from datetime import date, datetime, timezone

from . import db
from .config import settings
from .garmin_client import LoginRequired, get_client

log = logging.getLogger("poller")


def _today() -> str:
    return date.today().isoformat()


def _safe(metric: str, day: str, fn) -> bool:
    """Run one metric fetch; never let a single failure abort the poll."""
    try:
        payload = fn()
        db.save_snapshot(metric, day, payload)
        return True
    except Exception:  # noqa: BLE001 - log and continue with other metrics
        log.warning("metric %s failed:\n%s", metric, traceback.format_exc())
        return False


def poll_once() -> dict:
    """Pull all configured metrics from Garmin into the DB. Returns a summary."""
    day = _today()
    try:
        g = get_client()
    except LoginRequired as exc:
        db.set_meta("last_status", f"login_required: {exc}")
        db.set_meta("last_attempt", datetime.now(timezone.utc).isoformat())
        return {"ok": False, "error": "login_required", "detail": str(exc)}

    results = {
        "stats": _safe("stats", day, lambda: g.get_stats(day)),
        "heart_rate": _safe("heart_rate", day, lambda: g.get_heart_rates(day)),
        "steps": _safe("steps", day, lambda: g.get_steps_data(day)),
        "sleep": _safe("sleep", day, lambda: g.get_sleep_data(day)),
        "stress": _safe("stress", day, lambda: g.get_stress_data(day)),
        "body_battery": _safe(
            "body_battery", day, lambda: g.get_body_battery(day, day)
        ),
        "activities": _safe(
            "activities",
            day,
            lambda: g.get_activities(0, settings.activities_limit),
        ),
    }

    ok = sum(1 for v in results.values() if v)
    total = len(results)
    status = f"ok {ok}/{total}"
    db.set_meta("last_status", status)
    db.set_meta("last_success", datetime.now(timezone.utc).isoformat())
    log.info("poll complete: %s", status)
    return {"ok": True, "results": results, "status": status}
