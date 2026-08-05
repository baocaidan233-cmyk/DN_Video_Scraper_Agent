"""
SQLite-backed execution history for the dashboard.
Uses stdlib sqlite3 wrapped in run_in_executor to stay async.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("data/history.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT UNIQUE NOT NULL,
    run_type   TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL DEFAULT 'running',
    error_msg  TEXT
);

CREATE TABLE IF NOT EXISTS step_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    step_name        TEXT NOT NULL,
    articles_in      INTEGER DEFAULT 0,
    articles_out     INTEGER DEFAULT 0,
    articles_dropped INTEGER DEFAULT 0,
    duration_ms      INTEGER DEFAULT 0,
    error_msg        TEXT,
    extra_json       TEXT
);
"""


def _init_sync() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(_CREATE_SQL)
    # Mark any runs still 'running' from a previous process as interrupted
    conn.execute(
        "UPDATE pipeline_runs SET status='failed', error_msg='Interrupted: process killed'"
        " WHERE status='running'"
    )
    conn.commit()
    conn.close()


async def init_db() -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_sync)


# ------------------------------------------------------------------ #
# Write helpers                                                        #
# ------------------------------------------------------------------ #

def _save_run_sync(run_id: str, run_type: str, started_at: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR IGNORE INTO pipeline_runs (run_id, run_type, started_at) VALUES (?,?,?)",
        (run_id, run_type, started_at),
    )
    conn.commit()
    conn.close()


async def save_run_start(run_id: str, run_type: str, started_at: str) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _save_run_sync, run_id, run_type, started_at)


def _update_run_sync(run_id: str, status: str, ended_at: str, error_msg: Optional[str]) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE pipeline_runs SET status=?, ended_at=?, error_msg=? WHERE run_id=?",
        (status, ended_at, error_msg, run_id),
    )
    conn.commit()
    conn.close()


async def update_run(run_id: str, status: str, ended_at: str, error_msg: Optional[str] = None) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_run_sync, run_id, status, ended_at, error_msg)


def _save_step_sync(
    run_id: str,
    step_name: str,
    articles_in: int,
    articles_out: int,
    articles_dropped: int,
    duration_ms: int,
    error_msg: Optional[str],
    extra_json: Optional[str],
) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """INSERT INTO step_results
           (run_id, step_name, articles_in, articles_out, articles_dropped, duration_ms, error_msg, extra_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, step_name, articles_in, articles_out, articles_dropped, duration_ms, error_msg, extra_json),
    )
    conn.commit()
    conn.close()


async def save_step(
    run_id: str,
    step_name: str,
    articles_in: int = 0,
    articles_out: int = 0,
    articles_dropped: int = 0,
    duration_ms: int = 0,
    error_msg: Optional[str] = None,
    extra_json: Optional[str] = None,
) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _save_step_sync,
        run_id, step_name, articles_in, articles_out, articles_dropped,
        duration_ms, error_msg, extra_json,
    )


# ------------------------------------------------------------------ #
# Read helpers                                                         #
# ------------------------------------------------------------------ #

def _get_history_sync(limit: int, offset: int = 0, run_types: list[str] | None = None) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if run_types:
        placeholders = ",".join("?" * len(run_types))
        total = conn.execute(
            f"SELECT COUNT(*) FROM pipeline_runs WHERE run_type IN ({placeholders})",
            run_types,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM pipeline_runs WHERE run_type IN ({placeholders})"
            f" ORDER BY id DESC LIMIT ? OFFSET ?",
            (*run_types, limit, offset),
        ).fetchall()
    else:
        total = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    result = []
    for row in rows:
        run = dict(row)
        steps = conn.execute(
            "SELECT * FROM step_results WHERE run_id=? ORDER BY id ASC",
            (run["run_id"],),
        ).fetchall()
        run["steps"] = [dict(s) for s in steps]
        result.append(run)
    conn.close()
    return {"runs": result, "total": total}


async def get_history(limit: int = 50, offset: int = 0, run_types: list[str] | None = None) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_history_sync, limit, offset, run_types)
