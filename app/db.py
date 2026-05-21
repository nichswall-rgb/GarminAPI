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
                metric     TEXT PRIMARY KEY,
                day        TEXT,
                payload    TEXT,
                fetched_at TEXT
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_snapshot(metric: str, day: str, payload) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "REPLACE INTO snapshots (metric, day, payload, fetched_at) VALUES (?, ?, ?, ?)",
            (metric, day, json.dumps(payload), _now()),
        )


def get_all_snapshots() -> dict:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT metric, day, payload, fetched_at FROM snapshots").fetchall()
    return {
        r["metric"]: {
            "day": r["day"],
            "fetched_at": r["fetched_at"],
            "data": json.loads(r["payload"]),
        }
        for r in rows
    }


def set_meta(key: str, value: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(key: str) -> str | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
