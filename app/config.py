from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Auth in front of this proxy. REQUIRED. Generate a long random string.
    proxy_api_key: str = ""

    # Garmin credentials. Only needed for the one-time /garmin/login bootstrap.
    # After tokens are persisted you can leave these unset.
    garmin_email: str | None = None
    garmin_password: str | None = None

    # Persisted to a Railway Volume so login survives redeploys (hands-off).
    garmin_token_dir: str = "/data/garmin_tokens"
    db_path: str = "/data/garmin.db"

    # Server-side poll cadence.
    poll_interval_minutes: int = 30
    # MUST match the Garmin account's own timezone — every day boundary here
    # (local_today, local_day_offset, prune, backfill ranges) is computed in it,
    # while Garmin attributes each day in the account's zone.
    #
    # Corrected New_York -> Los_Angeles on 2026-09-06: the account is UTC-7
    # (`wellnessStartTimeLocal` 00:00 against `wellnessStartTimeGmt` 07:00), so an
    # Eastern setting rolled the date over at 21:00 local. Between then and
    # midnight every poll asked Garmin for a day that had not started and stored
    # empty stubs under a future date. The `day <= today` guards in
    # get_all_snapshots/get_history do not save you, because they resolve "today"
    # in this same wrong zone.
    #
    # A TIMEZONE env var on Railway overrides this — check there too if the day
    # boundary ever looks off.
    timezone: str = "America/Los_Angeles"

    # How many recent activities to pull each poll.
    activities_limit: int = 5

    # Per-metric retry. Garmin's slower endpoints (get_stats especially) sometimes
    # exceed garth's 10s read timeout; one retry a few seconds later almost always
    # lands. Worst case stays far inside the poll interval.
    metric_attempts: int = 2
    retry_delay_seconds: int = 5

    # Activity detail. Runs are sparse and stay useful long after the daily
    # snapshots expire, so they get their own retention. detail_maxchart caps
    # the per-point series Garmin returns (its own charts use 2000).
    activity_retention_days: int = 90
    activity_detail_maxchart: int = 2000
    # Cap the heavy per-activity fetches done in a single poll, so one backfill
    # can't stall a poll or hammer Garmin.
    activity_details_per_poll: int = 3

    # How many days of overnight history to retain (rolling window).
    # Each metric keeps one row per day; older rows are pruned after each poll.
    #
    # Raised 3 -> 30 on 2026-09-06. Three days is enough to render last night, but
    # far too short to correlate wearable channels against insulin sensitivity —
    # the analysis the app actually wants needs weeks, not nights. A day of all
    # metrics is a few hundred KB of JSON, so 30 days is still small.
    retention_days: int = 30

    # ── Backfill ────────────────────────────────────────────────────────────
    # The poller only ever fetches TODAY, so a fresh deployment starts empty and
    # fills one day at a time. Backfill replays the same per-day fetchers across a
    # past date range so the window can be populated on demand from Garmin, which
    # retains years.
    #
    # Hard ceiling on one backfill request. Callers are ALSO clamped to
    # retention_days at the endpoint (see main.backfill): prune() runs after every
    # poll and deletes anything older than the window, so fetching deeper than
    # that would hammer Garmin for rows deleted within the half hour. Raise both
    # together if the window ever widens.
    backfill_max_days: int = 30
    # Pause between days. Backfill is the only place we issue a burst of calls
    # (14 metrics x N days); Garmin throttles aggressive clients, and there is no
    # deadline here worth risking the session over.
    backfill_day_pause_seconds: float = 2.0


settings = Settings()
