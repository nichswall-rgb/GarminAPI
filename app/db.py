import json
import os
import sqlite3
from datetime import datetime, timezone
from threading import Lock

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
    """
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT metric, day, payload, fetched_at FROM snapshots
            WHERE (metric, day) IN (
                SELECT metric, MAX(day) FROM snapshots GROUP BY metric
            )
            """
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
    last `days` calendar days (inclusive of today).
    """
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT metric, day, payload, fetched_at FROM snapshots
            WHERE day >= date('now', 'localtime', ?)
            ORDER BY day DESC, metric ASC
            """,
            (f"-{days - 1} days",),
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
            "DELETE FROM snapshots WHERE day < date('now', 'localtime', ?)",
            (f"-{days - 1} days",),
        )
        return cur.rowcount


def set_meta(key: str, value: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(key: str) -> str | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
