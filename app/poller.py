import json
import logging
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from . import db
from .clock import local_today
from .config import settings
from .garmin_client import LoginRequired, get_client

log = logging.getLogger("poller")

# One backfill at a time. A second request while one is running is dropped rather
# than queued — they would fetch the same days and double the load on Garmin.
_backfill_lock = threading.Lock()


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


def backfill_range(start: str, end: str, force: bool = False) -> dict:
    """Replay the per-day fetchers across [start, end] so history can be populated
    on demand instead of accruing one day at a time.

    The poller only ever asks Garmin for TODAY, so a fresh deployment — or a
    widened retention window — starts almost empty and takes as many days to fill
    as the window is long. Garmin itself retains years, so there is no reason to
    wait: this walks the range and stores each day through the same code path.

    Oldest day first, so a run that dies partway still leaves a contiguous block
    ending at the newest day it reached, which is what the app reads.

    `activities` is excluded from the per-day loop on purpose: that fetcher returns
    the N most recent activities regardless of `day`, so calling it once per day
    would issue N identical requests. Historical activities are fetched once, by
    date range, at the end.

    Runs in the scheduler's thread (never on the request path). Progress is written
    to `meta` so GET /backfill/status can report it.
    """
    if not _backfill_lock.acquire(blocking=False):
        log.info("backfill already running - ignoring duplicate request")
        return {"ok": False, "error": "already_running"}
    try:
        return _backfill_range_locked(start, end, force)
    finally:
        _backfill_lock.release()


def _backfill_range_locked(start: str, end: str, force: bool) -> dict:
    try:
        g = get_client()
    except LoginRequired as exc:
        _set_backfill_status("failed", {"error": f"login_required: {exc}"})
        return {"ok": False, "error": "login_required", "detail": str(exc)}

    days = _date_range(start, end)
    present = set() if force else db.days_with_data()
    todo = [d for d in days if force or d not in present]

    state = {
        "start": start, "end": end,
        "days_total": len(days), "days_skipped": len(days) - len(todo),
        "days_done": 0, "metrics_ok": 0, "metrics_failed": 0,
        "activities": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _set_backfill_status("running", state)
    log.info("backfill %s..%s: %s days to fetch (%s already present)",
             start, end, len(todo), state["days_skipped"])

    for day in todo:
        fetchers = daily_fetchers(g, day)
        fetchers.pop("activities", None)
        for name, fn in fetchers.items():
            if _safe(name, day, fn):
                state["metrics_ok"] += 1
            else:
                state["metrics_failed"] += 1
        state["days_done"] += 1
        _set_backfill_status("running", state)
        # Throttle: this is the only burst of calls we make, and a throttled
        # session would cost far more than the seconds saved.
        time.sleep(settings.backfill_day_pause_seconds)

    try:
        state["activities"] = _backfill_activities(g, start, end)
    except Exception:  # noqa: BLE001 - activities are a bonus, never fail the run
        log.warning("backfill activity pass failed:\n%s", traceback.format_exc())

    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    _set_backfill_status("done", state)
    log.info("backfill complete: %s", state)
    return {"ok": True, **state}


def _backfill_activities(g, start: str, end: str) -> int:
    """Store activities in the range that we don't already hold with details.

    Uses get_activities_by_date rather than the poller's get_activities(0, N):
    the latter walks back from newest, so reaching an activity from weeks ago
    would mean requesting a large page of recent ones to find it.
    """
    listing = g.get_activities_by_date(start, end) or []
    known = db.activity_ids_present()
    stored = 0
    for a in listing:
        aid = a.get("activityId")
        if aid is None or str(aid) in known:
            continue
        start_local = a.get("startTimeLocal") or ""
        details = _try(
            lambda: g.get_activity_details(aid, maxchart=settings.activity_detail_maxchart),
            f"activity {aid} details",
        )
        db.save_activity(
            activity_id=aid,
            day=start_local[:10],
            start_local=start_local,
            activity_type=((a.get("activityType") or {}).get("typeKey") or ""),
            name=a.get("activityName") or "",
            summary=a,
            details=details,
            splits=_try(lambda: g.get_activity_splits(aid), f"activity {aid} splits"),
            hr_zones=_try(lambda: g.get_activity_hr_in_timezones(aid), f"activity {aid} zones"),
        )
        stored += 1
        time.sleep(settings.backfill_day_pause_seconds)
    return stored


def _date_range(start: str, end: str) -> list:
    """Inclusive YYYY-MM-DD range, oldest first."""
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _set_backfill_status(status: str, state: dict) -> None:
    db.set_meta("backfill_status", status)
    db.set_meta("backfill_progress", json.dumps(state))


def daily_fetchers(g, day: str) -> dict:
    """The per-day metric map: name -> zero-arg callable returning that day's payload.

    Both the live poll and the backfill run this exact map, so a metric can never
    exist in one path and be missing from the other.

    Deliberately NOT included, because `stats` already carries them and the app
    should not gather the same measurement twice:
      get_intensity_minutes_data  -> stats.moderateIntensityMinutes / vigorousIntensityMinutes
      get_floors                  -> stats.floorsAscended / floorsDescended
      get_all_day_stress          -> stress + body_battery already cover it
    """
    return {
        "stats": lambda: g.get_stats(day),
        "heart_rate": lambda: g.get_heart_rates(day),
        "steps": lambda: g.get_steps_data(day),
        "sleep": lambda: g.get_sleep_data(day),
        "stress": lambda: g.get_stress_data(day),
        "body_battery": lambda: g.get_body_battery(day, day),
        # Overnight signals for BG-confounder analysis. Each is best-effort:
        # a watch that doesn't record one just logs and continues (_safe).
        "hrv": lambda: g.get_hrv_data(day),
        "spo2": lambda: g.get_spo2_data(day),
        "respiration": lambda: g.get_respiration_data(day),
        # Garmin's own composite recovery/fitness models (added 2026-09-06).
        # These are the strongest single candidate variables for regressing
        # against insulin sensitivity, because Garmin has already folded sleep,
        # HRV, stress and training load into each one.
        "training_readiness": lambda: g.get_training_readiness(day),
        "training_status": lambda: g.get_training_status(day),
        "max_metrics": lambda: g.get_max_metrics(day),
        "body_composition": lambda: g.get_body_composition(day, day),
        "activities": lambda: g.get_activities(0, settings.activities_limit),
    }


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
        name: _safe(name, day, fn) for name, fn in daily_fetchers(g, day).items()
    }

    # Per-activity detail: the summary list carries distance and calories only,
    # so the intraday series (HR, cadence, pace, running dynamics), the lap
    # splits and the HR-zone breakdown are fetched per activity. Only new ids
    # are fetched, and at most activity_details_per_poll of them, since each
    # call is far heavier than a daily snapshot.
    # Kept OUT of `results`: that map is metric -> succeeded, and the steady
    # state here is 0 new activities, which as a boolean would read as a failed
    # metric and pin the status at "ok 10/11" forever.
    new_activities = 0
    try:
        new_activities = _fetch_activity_details(g)
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
    if new_activities:
        status += f" +{new_activities} activities"
    db.set_meta("last_status", status)
    db.set_meta("last_success", datetime.now(timezone.utc).isoformat())
    log.info("poll complete: %s", status)
    return {
        "ok": True,
        "results": results,
        "new_activities": new_activities,
        "status": status,
    }
