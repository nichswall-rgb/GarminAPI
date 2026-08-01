import json
import os
import sqlite3
from datetime import datetime, timezone
from threading import Lock

from .clock import local_day_offset, local_today
from .config import settings

_lock = Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                metric     TEXT,
                day        TEXT,
                payload    TEXT,
                fetched_at TEXT,
                PRIMARY KEY (metric, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # Activities live outside the rolling snapshot window: they are sparse,
        # each one is far bigger than a daily snapshot, and a run stays worth
        # analysing long after three days.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                activity_id   TEXT PRIMARY KEY,
                day           TEXT,
                start_local   TEXT,
                activity_type TEXT,
                name          TEXT,
                summary       TEXT,
                details       TEXT,
                splits        TEXT,
                hr_zones      TEXT,
                fetched_at    TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_day ON activities (day DESC)")
        _migrate_snapshots_pk(conn)


def _migrate_snapshots_pk(conn: sqlite3.Connection) -> None:
    """Upgrade the legacy single-column PK (metric) to composite (metric, day).

    Older deployments created `snapshots` with `metric` as the sole primary
    key, so only the latest day per metric was ever retained. CREATE TABLE
    IF NOT EXISTS can't alter that, so rebuild the table once. The snapshot
    data is a disposable cache — the next poll refills it — so we simply drop
    and recreate rather than copy rows across.
    """
    cols = conn.execute("PRAGMA table_info(snapshots)").fetchall()
    day_is_pk = any(c["name"] == "day" and c["pk"] for c in cols)
    if day_is_pk:
        return  # already on the composite-key schema
    conn.execute("DROP TABLE IF EXISTS snapshots")
    conn.execute(
        """
        CREATE TABLE snapshots (
            metric     TEXT,
            day        TEXT,
            payload    TEXT,
            fetched_at TEXT,
            PRIMARY KEY (metric, day)
        )
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_snapshot(metric: str, day: str, payload) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "REPLACE INTO snapshots (metric, day, payload, fetched_at) VALUES (?, ?, ?, ?)",
            (metric, day, json.dumps(payload), _now()),
        )


def get_all_snapshots() -> dict:
    """Latest day per metric — the shape the app's /metrics/latest expects.

    With history retained there are now several rows per metric, so pick the
    most recent day for each so existing callers keep seeing one snapshot.

    Rows dated past today are ignored. A poll that ran while the clock was
    ahead of local time stored empty stubs under tomorrow's date, and MAX(day)
    let those shadow real data until the date caught up.
    """
    today = local_today()
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT metric, day, payload, fetched_at FROM snapshots
            WHERE day <= ? AND (metric, day) IN (
                SELECT metric, MAX(day) FROM snapshots WHERE day <= ? GROUP BY metric
            )
            """,
            (today, today),
        ).fetchall()
    return {
        r["metric"]: {
            "day": r["day"],
            "fetched_at": r["fetched_at"],
            "data": json.loads(r["payload"]),
        }
        for r in rows
    }


def get_history(days: int) -> list:
    """All retained days, newest first — for overnight time-series analysis.

    Returns a flat list of {metric, day, fetched_at, data} rows spanning the
    last `days` calendar days (inclusive of today). Future-dated stubs are
    excluded for the same reason as get_all_snapshots — the app sorts nights
    newest-first, so an empty tomorrow would be the one it analyses.
    """
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT metric, day, payload, fetched_at FROM snapshots
            WHERE day >= ? AND day <= ?
            ORDER BY day DESC, metric ASC
            """,
            (local_day_offset(days - 1), local_today()),
        ).fetchall()
    return [
        {
            "metric": r["metric"],
            "day": r["day"],
            "fetched_at": r["fetched_at"],
            "data": json.loads(r["payload"]),
        }
        for r in rows
    ]


def prune(days: int) -> int:
    """Drop snapshot rows older than the retention window. Returns rows deleted."""
    with _lock, _conn() as conn:
        cur = conn.execute(
            "DELETE FROM snapshots WHERE day < ?",
            (local_day_offset(days - 1),),
        )
        return cur.rowcount


# ── activities ──────────────────────────────────────────────────────────────


def activity_ids_present() -> set:
    """Ids already stored WITH details — used to skip re-fetching heavy payloads."""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT activity_id FROM activities WHERE details IS NOT NULL AND details != ''"
        ).fetchall()
    return {r["activity_id"] for r in rows}


def save_activity(
    activity_id: str,
    day: str,
    start_local: str,
    activity_type: str,
    name: str,
    summary,
    details=None,
    splits=None,
    hr_zones=None,
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """
            REPLACE INTO activities
                (activity_id, day, start_local, activity_type, name,
                 summary, details, splits, hr_zones, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(activity_id), day, start_local, activity_type, name,
                json.dumps(summary),
                json.dumps(details) if details is not None else None,
                json.dumps(splits) if splits is not None else None,
                json.dumps(hr_zones) if hr_zones is not None else None,
                _now(),
            ),
        )


def _activity_row(r, with_details: bool) -> dict:
    out = {
        "activity_id": r["activity_id"],
        "day": r["day"],
        "start_local": r["start_local"],
        "activity_type": r["activity_type"],
        "name": r["name"],
        "fetched_at": r["fetched_at"],
        "summary": json.loads(r["summary"]) if r["summary"] else None,
        "splits": json.loads(r["splits"]) if r["splits"] else None,
        "hr_zones": json.loads(r["hr_zones"]) if r["hr_zones"] else None,
        "has_details": bool(r["details"]),
    }
    if with_details:
        out["details"] = json.loads(r["details"]) if r["details"] else None
    return out


def get_activities(days: int) -> list:
    """Recent activities, newest first, WITHOUT the heavy per-point series."""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activities WHERE day >= ? ORDER BY start_local DESC",
            (local_day_offset(max(0, days - 1)),),
        ).fetchall()
    return [_activity_row(r, with_details=False) for r in rows]


def get_activity(activity_id: str) -> dict | None:
    """One activity including its per-point series."""
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (str(activity_id),)
        ).fetchone()
    return _activity_row(r, with_details=True) if r else None


def prune_activities(days: int) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute(
            "DELETE FROM activities WHERE day < ?", (local_day_offset(days - 1),)
        )
        return cur.rowcount


def set_meta(key: str, value: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(key: str) -> str | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
