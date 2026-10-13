# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-process store for live evaluation sessions.

A :class:`~pynescript.runtime.host.InterpretSession` keeps a parsed script, its
series, and its TA state alive so that re-ticking the forming bar costs O(1)
instead of re-evaluating every bar. Measured on 20k bars: a full run is ~1,081
ms, ``update_last_bar`` is ~0.3 ms.

That state lives in the Python process, which is why the Pro API is configured
for a **single** gunicorn worker (``-w 1`` in
``deploy/pynescript-api.service`` and ``docker/entrypoint-api.sh``). With two
workers a session created on one would be invisible on the other and roughly
half of all follow-up calls would 404. This module is the in-process half of
that contract; moving it to an external store is what would allow ``-w`` above 1.

Eviction is TTL + capacity, because a leaked session holds a whole parsed
script. Both are deliberately conservative: a session that is being ticked
every few hundred milliseconds must never expire mid-stream.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from typing import Any


logger = logging.getLogger(__name__)

# Sessions idle for longer than this are evicted on the next sweep.
DEFAULT_TTL_SECONDS = 900.0  # 15 min

# Hard cap on concurrent sessions. Each holds a parsed script + series, so the
# bound is about memory safety, not throughput. Callers get a 503-style error
# (SESSIONS_FULL) rather than an OOM kill when this is hit.
DEFAULT_MAX_SESSIONS = 64

# Sweep cadence — cheap, and only walks a dict of at most max_sessions.
_SWEEP_INTERVAL_SECONDS = 30.0


class SessionStore:
    """Thread-safe, TTL + capacity bounded registry of live sessions.

    gevent workers are single-threaded per process, but the Pro API can run
    threaded (``app.run(threaded=True)`` in development and some deploys), so
    access is guarded rather than assumed.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_sessions)
        self._sessions: dict[str, Any] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    # ── internals ──────────────────────────────────────────────────────────

    def _sweep_locked(self, now: float) -> None:
        """Drop idle sessions. Caller holds the lock."""
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        expired = [sid for sid, seen in self._last_seen.items() if now - seen > self._ttl]
        for sid in expired:
            self._discard_locked(sid, reason="idle")
        if expired:
            logger.info("session store: evicted %d idle session(s)", len(expired))

    def _discard_locked(self, session_id: str, reason: str) -> None:
        session = self._sessions.pop(session_id, None)
        self._last_seen.pop(session_id, None)
        if session is None:
            return
        # InterpretSession holds no OS resources today; this is here so adding
        # any later does not silently leak.
        closer = getattr(session, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.warning("session store: close() failed for %s", session_id)
        logger.debug("session store: dropped %s (%s)", session_id, reason)

    # ── public API ─────────────────────────────────────────────────────────

    @property
    def max_sessions(self) -> int:
        return self._max

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def full(self) -> bool:
        with self._lock:
            self._sweep_locked(time.monotonic())
            return len(self._sessions) >= self._max

    def put(self, session: Any) -> str:
        """Register ``session`` and return its id.

        Raises ``RuntimeError`` when the store is at capacity — callers turn
        this into a 503 so a client can back off instead of retrying into an
        ever-growing pile.
        """
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            if len(self._sessions) >= self._max:
                msg = f"session store is full ({self._max}); retry after an existing session goes idle"
                raise RuntimeError(msg)
            session_id = uuid.uuid4().hex
            self._sessions[session_id] = session
            self._last_seen[session_id] = now
            return session_id

    def get(self, session_id: str) -> Any | None:
        """Return a live session and mark it used, or ``None`` if unknown."""
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                return None
            self._last_seen[session_id] = now
            return session

    def drop(self, session_id: str) -> bool:
        """Explicit delete. Returns ``False`` when the id was already gone."""
        with self._lock:
            existed = session_id in self._sessions
            self._discard_locked(session_id, reason="explicit")
            return existed

    def describe(self, session_id: str) -> dict[str, Any] | None:
        """Small status blob for ``GET /session/<id>`` — never the full state."""
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                return None
            seen = self._last_seen.get(session_id, now)
            bars = getattr(session, "_n_bars", None)
            return {
                "session_id": session_id,
                "bars": bars if isinstance(bars, int) else None,
                "idle_seconds": round(now - seen, 3),
                "ttl_seconds": self._ttl,
            }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._sweep_locked(time.monotonic())
            return {
                "sessions": len(self._sessions),
                "max_sessions": self._max,
                "ttl_seconds": self._ttl,
            }

    def clear(self) -> int:
        """Drop everything. Returns how many were removed."""
        with self._lock:
            ids = list(self._sessions)
            for sid in ids:
                self._discard_locked(sid, reason="clear")
            return len(ids)


# Process-wide singleton. Module-level because the store must be shared by
# every request handler in this worker — and deliberately NOT shared across
# workers, which is the constraint documented above.
SESSIONS = SessionStore()


def _reset_sessions_for_tests() -> None:
    """@internal test helper — empty the singleton."""
    SESSIONS.clear()
