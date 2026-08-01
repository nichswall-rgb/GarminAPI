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


def _fetch_activity_details(g) -> int:
    """Fetch detail for activities we haven't stored yet. Returns how many.

    `get_activities` gives a summary per activity; the per-point series behind
    Garmin's own charts comes from `get_activity_details`, the lap structure
    from `get_activity_splits`, and time-in-zone from
    `get_activity_hr_in_timezones`. Each is best-effort per activity.
    """
    listing = g.get_activities(0, settings.activities_limit) or []
    known = db.activity_ids_present()
    fetched = 0
    for a in listing:
        if fetched >= settings.activity_details_per_poll:
            break
        aid = a.get("activityId")
        if aid is None or str(aid) in known:
            continue
        start_local = a.get("startTimeLocal") or ""
        try:
            details = g.get_activity_details(
                aid, maxchart=settings.activity_detail_maxchart
            )
        except Exception:  # noqa: BLE001 - store the summary even if detail fails
            log.warning("activity %s details failed:\n%s", aid, traceback.format_exc())
            details = None
        splits = _try(lambda: g.get_activity_splits(aid), f"activity {aid} splits")
        zones = _try(lambda: g.get_activity_hr_in_timezones(aid), f"activity {aid} zones")
        db.save_activity(
            activity_id=aid,
            day=start_local[:10],
            start_local=start_local,
            activity_type=((a.get("activityType") or {}).get("typeKey") or ""),
            name=a.get("activityName") or "",
            summary=a,
            details=details,
            splits=splits,
            hr_zones=zones,
        )
        fetched += 1
        log.info("stored activity %s (%s) details=%s", aid, start_local, details is not None)

    removed = db.prune_activities(settings.activity_retention_days)
    if removed:
        log.info("pruned %s activities past %s-day window",
                 removed, settings.activity_retention_days)
    return fetched


def _try(fn, label: str):
    """Best-effort sub-fetch: returns None instead of raising."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log.warning("%s failed:\n%s", label, traceback.format_exc())
        return None


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

    # Per-activity detail: the summary list carries distance and calories only,
    # so the intraday series (HR, cadence, pace, running dynamics), the lap
    # splits and the HR-zone breakdown are fetched per activity. Only new ids
    # are fetched, and at most activity_details_per_poll of them, since each
    # call is far heavier than a daily snapshot.
    try:
        results["activity_details"] = _fetch_activity_details(g)
    except Exception:  # noqa: BLE001 - detail is a bonus, never fail the poll
        log.warning("activity detail pass failed:\n%s", traceback.format_exc())

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
