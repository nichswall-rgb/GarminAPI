import logging
import time
import traceback
from datetime import datetime, timezone

from . import db
from .clock import local_today
from .config import settings
from .garmin_client import LoginRequired, get_client

log = logging.getLogger("poller")


def _today() -> str:
    return local_today()


def _safe(metric: str, day: str, fn) -> bool:
    """Run one metric fetch; never let a single failure abort the poll.

    Retried per settings.metric_attempts: Garmin's slower endpoints (get_stats
    especially) intermittently exceed garth's 10s read timeout, which used to
    leave that metric's snapshot stale until the next poll.
    """
    attempts = max(1, settings.metric_attempts)
    for attempt in range(1, attempts + 1):
        try:
            payload = fn()
            db.save_snapshot(metric, day, payload)
            if attempt > 1:
                log.info("metric %s recovered on attempt %s", metric, attempt)
            return True
        except Exception:  # noqa: BLE001 - log and continue with other metrics
            final = attempt == attempts
            log.warning(
                "metric %s failed (attempt %s/%s)%s:\n%s",
                metric, attempt, attempts,
                "" if final else " - retrying",
                traceback.format_exc(),
            )
            if not final:
                time.sleep(settings.retry_delay_seconds)
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
        # Overnight signals for BG-confounder analysis. Each is best-effort:
        # a watch that doesn't record one just logs and continues (_safe).
        "hrv": _safe("hrv", day, lambda: g.get_hrv_data(day)),
        "spo2": _safe("spo2", day, lambda: g.get_spo2_data(day)),
        "respiration": _safe(
            "respiration", day, lambda: g.get_respiration_data(day)
        ),
        "activities": _safe(
            "activities",
            day,
            lambda: g.get_activities(0, settings.activities_limit),
        ),
    }

    # Keep the rolling retention window trimmed.
    try:
        removed = db.prune(settings.retention_days)
        if removed:
            log.info("pruned %s snapshot rows past %s-day window",
                     removed, settings.retention_days)
    except Exception:  # noqa: BLE001 - pruning must never fail a poll
        log.warning("prune failed:\n%s", traceback.format_exc())

    ok = sum(1 for v in results.values() if v)
    total = len(results)
    status = f"ok {ok}/{total}"
    db.set_meta("last_status", status)
    db.set_meta("last_success", datetime.now(timezone.utc).isoformat())
    log.info("poll complete: %s", status)
    return {"ok": True, "results": results, "status": status}
