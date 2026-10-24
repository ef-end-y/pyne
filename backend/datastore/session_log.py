# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite event log for live evaluation sessions.

The in-memory :class:`~backend.middleware.sessions.SessionStore` is the source
of truth while the process lives, but gunicorn recycles workers
(``--max-requests``) and containers restart — both silently drop every live
session today. This module persists the *inputs* a session was built from so a
fresh process can rebuild it with one ``create_session`` over identical bars:

- create → full bar log (+ script, symbol, inputs, libraries, binding)
- tick   → replace the last bar (mirrors ``update_last_bar`` overwriting
  ``InterpretSession._ohlcv_data[-1]`` in place)
- append → extend the log (mirrors ``append_bars`` extending ``_ohlcv_data``)

The rebuilt session is a rebase, not a byte-identical resume: indicators
evaluate identically over the same bars, but stateful constructs that never
roll back (strategy fills) may differ exactly as if the client had created a
new session over the same window. Ticks after the rebuild are O(1) again after
the one re-evaluation.

Writes are best-effort: every caller wraps them so a full disk or a locked db
degrades to today's memory-only behaviour instead of failing the request.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from typing import Any


logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    script     TEXT NOT NULL,
    symbol     TEXT NOT NULL DEFAULT 'CHART',
    inputs     TEXT NOT NULL DEFAULT '{}',
    libraries  TEXT NOT NULL DEFAULT '[]',
    profiler   INTEGER NOT NULL DEFAULT 0,
    binding    TEXT,
    bar_count  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_bars (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    bar        TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""


class _ConnectionPool:
    """Single lazy SQLite connection (reopened when the path changes)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.path: str | None = None


_POOL = _ConnectionPool()


def _close_pooled() -> None:
    """Close the pooled connection, logging instead of raising. Caller holds the lock."""
    if _POOL.conn is not None:
        try:
            _POOL.conn.close()
        except Exception as exc:
            logger.debug("session log: pooled close failed (%s)", exc)
    _POOL.conn = None
    _POOL.path = None


def reset_session_log() -> None:
    """Drop the process connection (tests)."""
    with _POOL.lock:
        _close_pooled()


def session_db_path() -> str | None:
    """Where the session log lives, or ``None`` when persistence is disabled.

    Explicit ``PYNE_SESSION_DB`` wins; otherwise the shared ``/data`` volume
    (present in Docker, absent in plain dev checkouts — which stay memory-only).
    """
    explicit = (os.environ.get("PYNE_SESSION_DB") or "").strip()
    if explicit:
        return explicit
    if os.path.isdir("/data"):
        return "/data/sessions.db"
    return None


def _connect(path: str) -> sqlite3.Connection:
    with _POOL.lock:
        if _POOL.conn is not None and _POOL.path == path:
            return _POOL.conn
        _close_pooled()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            logger.debug("session log: WAL unavailable (%s)", exc)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            logger.debug("session log: synchronous=NORMAL unavailable (%s)", exc)
        conn.executescript(_SCHEMA)
        _POOL.conn = conn
        _POOL.path = path
        return conn


def _encode(value: Any) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session log value is not JSON-serializable: {exc}") from exc


def record_create(  # one explicit kwarg per journaled field
    path: str,
    session_id: str,
    *,
    script: str,
    symbol: str = "CHART",
    inputs: Any = None,
    libraries: Any = None,
    profiler: bool = False,
    bars: list[dict[str, Any]] | None = None,
    binding: dict[str, Any] | None = None,
) -> None:
    """(Re)write the full record for a new session."""
    conn = _connect(path)
    now = time.time()
    rows = [(session_id, i, _encode(bar)) for i, bar in enumerate(bars or [])]
    with _POOL.lock:
        conn.execute("DELETE FROM session_bars WHERE session_id = ?", (session_id,))
        if rows:
            conn.executemany(
                "INSERT INTO session_bars (session_id, seq, bar) VALUES (?, ?, ?)",
                rows,
            )
        conn.execute(
            "INSERT OR REPLACE INTO sessions"
            " (id, script, symbol, inputs, libraries, profiler, binding, bar_count, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                script,
                symbol,
                _encode(inputs if inputs is not None else {}),
                _encode(libraries if libraries is not None else []),
                1 if profiler else 0,
                _encode(binding) if binding is not None else None,
                len(rows),
                now,
            ),
        )


def record_tick(path: str, session_id: str, bar: dict[str, Any]) -> None:
    """Replace the last logged bar (forming-bar re-tick)."""
    conn = _connect(path)
    now = time.time()
    payload = _encode(bar)
    with _POOL.lock:
        row = conn.execute("SELECT MAX(seq) FROM session_bars WHERE session_id = ?", (session_id,)).fetchone()
        if row is None or row[0] is None:
            conn.execute(
                "INSERT INTO session_bars (session_id, seq, bar) VALUES (?, 0, ?)",
                (session_id, payload),
            )
            conn.execute(
                "UPDATE sessions SET bar_count = 1, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            return
        conn.execute(
            "UPDATE session_bars SET bar = ? WHERE session_id = ? AND seq = ?",
            (payload, session_id, int(row[0])),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))


def record_append(path: str, session_id: str, bars: list[dict[str, Any]]) -> None:
    """Extend the log with newly closed bars (no-op + touch for ``[]``)."""
    conn = _connect(path)
    now = time.time()
    with _POOL.lock:
        if bars:
            row = conn.execute("SELECT MAX(seq) FROM session_bars WHERE session_id = ?", (session_id,)).fetchone()
            start = int(row[0]) + 1 if row is not None and row[0] is not None else 0
            conn.executemany(
                "INSERT OR REPLACE INTO session_bars (session_id, seq, bar) VALUES (?, ?, ?)",
                [(session_id, start + i, _encode(bar)) for i, bar in enumerate(bars)],
            )
            conn.execute(
                "UPDATE sessions SET bar_count = bar_count + ?, updated_at = ? WHERE id = ?",
                (len(bars), now, session_id),
            )
        else:
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))


def record_binding(path: str, session_id: str, binding: dict[str, Any] | None) -> None:
    """Persist the data-session binding (series key + tail bar)."""
    conn = _connect(path)
    with _POOL.lock:
        conn.execute(
            "UPDATE sessions SET binding = ?, updated_at = ? WHERE id = ?",
            (_encode(binding) if binding is not None else None, time.time(), session_id),
        )


def touch_record(path: str, session_id: str) -> None:
    """Refresh the idle clock without changing content (read path)."""
    conn = _connect(path)
    with _POOL.lock:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id))


def _row_to_record(row: sqlite3.Row | tuple, bars: list[dict[str, Any]]) -> dict[str, Any]:
    sid, script, symbol, inputs, libraries, profiler, binding, bar_count, updated_at = row
    return {
        "id": sid,
        "script": script,
        "symbol": symbol,
        "inputs": json.loads(inputs) if inputs else {},
        "libraries": json.loads(libraries) if libraries else [],
        "profiler": bool(profiler),
        "binding": json.loads(binding) if binding else None,
        "bars": bars,
        "bar_count": int(bar_count),
        "updated_at": float(updated_at),
    }


def load_record(path: str, session_id: str) -> dict[str, Any] | None:
    """Full record (row + ordered bars), or ``None`` when unknown."""
    conn = _connect(path)
    with _POOL.lock:
        row = conn.execute(
            "SELECT id, script, symbol, inputs, libraries, profiler, binding, bar_count, updated_at"
            " FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        bar_rows = conn.execute(
            "SELECT bar FROM session_bars WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    try:
        bars = [json.loads(r[0]) for r in bar_rows]
        return _row_to_record(row, bars)
    except (ValueError, TypeError, IndexError) as exc:
        logger.warning("session log: dropping corrupt record %s (%s)", session_id, exc)
        delete_record(path, session_id)
        return None


def load_meta(path: str, session_id: str) -> dict[str, Any] | None:
    """Row without bars (cheap status reads)."""
    conn = _connect(path)
    with _POOL.lock:
        row = conn.execute(
            "SELECT id, script, symbol, inputs, libraries, profiler, binding, bar_count, updated_at"
            " FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        return _row_to_record(row, [])
    except (ValueError, TypeError, IndexError):
        return None


def delete_record(path: str, session_id: str) -> None:
    conn = _connect(path)
    with _POOL.lock:
        conn.execute("DELETE FROM session_bars WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def delete_all_records(path: str) -> None:
    conn = _connect(path)
    with _POOL.lock:
        conn.execute("DELETE FROM session_bars")
        conn.execute("DELETE FROM sessions")


def sweep_expired(path: str, ttl_seconds: float) -> int:
    """Delete rows idle longer than the TTL. Returns the removed count."""
    conn = _connect(path)
    cutoff = time.time() - float(ttl_seconds)
    with _POOL.lock:
        stale = conn.execute("SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)).fetchall()
        for (sid,) in stale:
            conn.execute("DELETE FROM session_bars WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
    if stale:
        logger.info("session log: evicted %d idle session(s)", len(stale))
    return len(stale)


def count_records(path: str) -> int:
    conn = _connect(path)
    with _POOL.lock:
        row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    return int(row[0]) if row else 0


def list_meta(path: str) -> list[dict[str, Any]]:
    """Every journaled session row (id, binding, counts) — no bar payloads.

    Survives the owning process: this is how an out-of-process inspector
    (MCP, CLI) sees sessions journaled by a backend worker whose memory it
    cannot read. Best-effort; a missing/unreadable file reads as empty.
    """
    try:
        conn = _connect(path)
        with _POOL.lock:
            rows = conn.execute("SELECT id, binding, bar_count, updated_at FROM sessions").fetchall()
    except Exception as exc:
        logger.warning("session log: list failed (%s)", exc)
        return []
    out: list[dict[str, Any]] = []
    for sid, binding, bar_count, updated_at in rows:
        try:
            parsed = json.loads(binding) if binding else None
        except Exception:
            parsed = None
        out.append(
            {
                "id": sid,
                "binding": parsed,
                "bar_count": bar_count,
                "updated_at": updated_at,
            }
        )
    return out
