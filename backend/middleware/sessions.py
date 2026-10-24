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

A worker recycle (``--max-requests``) or a container restart still empties the
process — so every mutation is also journaled to SQLite
(:mod:`backend.datastore.session_log`). On a miss the store rebuilds the
session with one ``create_session`` over the logged bars and ticking continues
O(1) after that single re-evaluation. The journal is best-effort: when the db
is unreachable the store degrades to memory-only instead of failing requests.

Eviction is TTL + capacity, because a leaked session holds a whole parsed
script. Both are deliberately conservative: a session that is being ticked
every few hundred milliseconds must never expire mid-stream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid

from typing import Any

from backend.datastore import live as _live
from backend.datastore import session_log as _session_log
from backend.datastore.series import SeriesKey
from pynescript import stats as _stats
from pynescript.runtime import Runtime


logger = logging.getLogger(__name__)

# Sessions idle for longer than this are evicted on the next sweep.
DEFAULT_TTL_SECONDS = 900.0  # 15 min

# Hard cap on concurrent sessions. Each holds a parsed script + series, so the
# bound is about memory safety, not throughput. Callers get a 503-style error
# (SESSIONS_FULL) rather than an OOM kill when this is hit.
DEFAULT_MAX_SESSIONS = 64

# Sweep cadence — cheap, and only walks a dict of at most max_sessions.
_SWEEP_INTERVAL_SECONDS = 30.0


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


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
        db_path: str | None = None,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_sessions)
        # ``None`` resolves lazily via the env/volume default; an explicit
        # path (tests, ``PYNE_SESSION_DB``) pins the journal. ``None`` means
        # memory-only — every journal call below is a no-op then.
        self._db_path = db_path if db_path is not None else _session_log.session_db_path()
        self._sessions: dict[str, Any] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()
        # Shared-session pooling (F-29): content fingerprint → session id, plus
        # a refcount per pooled id. Tabs attach/detach instead of
        # create/release; the session itself still expires by idle TTL.
        self._shared: dict[str, str] = {}
        self._shared_refs: dict[str, int] = {}

    # ── journal (best-effort; never raises) ──────────────────────────────

    def _log(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a :mod:`session_log` call, degrading to memory-only on failure."""
        if self._db_path is None:
            return None
        try:
            return fn(self._db_path, *args, **kwargs)
        except Exception as exc:
            # Persistence must never fail requests; memory stays authoritative.
            logger.warning("session store: journal write failed (%s); memory-only", exc)
            return None

    def track(  # one explicit kwarg per journaled field
        self,
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
        """Journal a newly created session so a restart can rebuild it."""
        self._log(
            _session_log.record_create,
            session_id,
            script=script,
            symbol=symbol,
            inputs=inputs,
            libraries=libraries,
            profiler=profiler,
            bars=bars,
            binding=binding,
        )

    def note_tick(self, session_id: str, bar: dict[str, Any]) -> None:
        """Journal a forming-bar re-tick (replaces the last logged bar)."""
        self._log(_session_log.record_tick, session_id, bar)

    def note_append(self, session_id: str, bars: list[dict[str, Any]]) -> None:
        """Journal newly closed bars (extends the log)."""
        self._log(_session_log.record_append, session_id, bars)

    def note_binding(self, session_id: str, binding: dict[str, Any] | None) -> None:
        """Journal the data-session binding (series key + tail bar)."""
        self._log(_session_log.record_binding, session_id, binding)

    def _rebuild(self, record: dict[str, Any]) -> Any | None:
        """Re-create the live object from a journal record, or ``None``."""
        try:
            runtime = Runtime(symbol=str(record.get("symbol") or "CHART"))
            session = runtime.create_session(
                record["script"],
                list(record.get("bars") or []),
                inputs=record.get("inputs") or None,
                libraries=record.get("libraries") or None,
                profiler=bool(record.get("profiler")),
                mode="interpret",
            )
        except Exception as exc:
            # Corrupt record (script no longer parses, bars invalid): drop it.
            logger.warning("session store: rebuild failed, dropping record (%s)", exc)
            return None
        binding = record.get("binding")
        if isinstance(binding, dict) and binding.get("key"):
            try:
                parts = str(binding["key"]).split("|")
                key = SeriesKey(exchange=parts[0], market=parts[1], symbol=parts[2], interval=parts[3])
                last_bar = dict(binding.get("last_bar") or {})
                _live.bind_session(session, key, last_bar)
            except Exception as exc:
                # Malformed binding: restore unbound rather than failing.
                logger.warning("session store: bad binding, restoring unbound (%s)", exc)
        return session

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
            _stats.count("sessions.evicted", len(expired), dim="idle")
        # Journal rows use wall-clock expiry under the same TTL.
        self._log(_session_log.sweep_expired, self._ttl)

    def _discard_locked(self, session_id: str, reason: str) -> None:
        # Journal first: an id that is only persisted (never loaded into this
        # process) must still be gone after an explicit drop.
        self._log(_session_log.delete_record, session_id)
        session = self._sessions.pop(session_id, None)
        self._last_seen.pop(session_id, None)
        self._shared_refs.pop(session_id, None)
        for fingerprint, pooled in list(self._shared.items()):
            if pooled == session_id:
                del self._shared[fingerprint]
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
        _stats.count("sessions.dropped", dim=reason)
        _stats.set_gauge("sessions.active", len(self._sessions))

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
            _stats.count("sessions.created")
            _stats.set_gauge("sessions.active", len(self._sessions))
            _stats.max_gauge("sessions.active_max", len(self._sessions))
            _stats.set_gauge("sessions.capacity", self._max)
            return session_id

    # ── shared pooling (F-29) ────────────────────────────────────────────

    @staticmethod
    def shared_fingerprint(
        script: str = "",
        symbol: str = "CHART",
        inputs: Any = None,
        libraries: Any = None,
        profiler: bool = False,
    ) -> str:
        """Stable content id for a session so identical tabs share one."""
        canonical = json.dumps(
            {
                "script": script or "",
                "symbol": symbol or "CHART",
                "inputs": inputs if inputs is not None else {},
                "libraries": libraries if libraries is not None else [],
                "profiler": bool(profiler),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def acquire_shared(self, fingerprint: str, factory: Any) -> tuple[str, bool]:
        """Return ``(session_id, created)`` for a pooled session.

        ``factory`` builds the session object on a miss (called under no lock;
        a raced duplicate is discarded in favor of the winner). Hits bump the
        refcount instead of creating. Raises ``RuntimeError`` at capacity,
        like :meth:`put`.
        """
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            pooled = self._shared.get(fingerprint)
            if pooled is not None and pooled in self._sessions:
                self._shared_refs[pooled] = self._shared_refs.get(pooled, 0) + 1
                self._last_seen[pooled] = now
                _stats.count("sessions.shared_hits")
                return pooled, False
            if len(self._sessions) >= self._max:
                msg = f"session store is full ({self._max}); retry after an existing session goes idle"
                raise RuntimeError(msg)
        session = factory()
        with self._lock:
            winner = self._shared.get(fingerprint)
            if winner is not None and winner in self._sessions:
                self._shared_refs[winner] = self._shared_refs.get(winner, 0) + 1
                self._last_seen[winner] = time.monotonic()
                _stats.count("sessions.shared_hits")
                return winner, False
            if len(self._sessions) >= self._max:
                msg = f"session store is full ({self._max}); retry after an existing session goes idle"
                raise RuntimeError(msg)
            session_id = uuid.uuid4().hex
            self._sessions[session_id] = session
            self._last_seen[session_id] = time.monotonic()
            self._shared[fingerprint] = session_id
            self._shared_refs[session_id] = 1
            _stats.count("sessions.created")
            _stats.count("sessions.shared_misses")
            _stats.set_gauge("sessions.active", len(self._sessions))
            _stats.max_gauge("sessions.active_max", len(self._sessions))
            _stats.set_gauge("sessions.shared", len(self._shared_refs))
            return session_id, True

    def release_shared(self, session_id: str) -> bool:
        """Drop one pooled reference. The session itself idles out by TTL."""
        with self._lock:
            refs = self._shared_refs.get(session_id, 0)
            if refs <= 0:
                return False
            if refs == 1:
                del self._shared_refs[session_id]
                for fingerprint, pooled in list(self._shared.items()):
                    if pooled == session_id:
                        del self._shared[fingerprint]
            else:
                self._shared_refs[session_id] = refs - 1
            _stats.set_gauge("sessions.shared", len(self._shared_refs))
            return True

    def get(self, session_id: str) -> Any | None:
        """Return a live session and mark it used, or ``None`` if unknown.

        On a miss the journal is consulted: a session that was journaled
        before a restart is rebuilt with one ``create_session`` over the
        logged bars and registered under its original id.
        """
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            session = self._sessions.get(session_id)
            if session is not None:
                self._last_seen[session_id] = now
                self._log(_session_log.touch_record, session_id)
                return session
        rebuilt = self._restore_from_journal(session_id)
        return rebuilt

    def _restore_from_journal(self, session_id: str) -> Any | None:
        """Rebuild a journaled session outside the store lock (slow)."""
        record = self._journal_record(session_id)
        session = self._rebuild(record) if record is not None else None
        if session is None:
            if record is not None:
                self._log(_session_log.delete_record, session_id)
            return None
        now = time.monotonic()
        with self._lock:
            if session_id in self._sessions:
                # Raced with another thread restoring the same id.
                return self._sessions[session_id]
            if len(self._sessions) >= self._max:
                return None
            self._sessions[session_id] = session
            self._last_seen[session_id] = now
            _stats.count("sessions.restored")
            _stats.set_gauge("sessions.active", len(self._sessions))
            _stats.max_gauge("sessions.active_max", len(self._sessions))
        self._log(_session_log.touch_record, session_id)
        return session

    def _journal_record(self, session_id: str) -> dict[str, Any] | None:
        """Journal row for ``session_id``, honouring TTL (no rebuild)."""
        if self._db_path is None:
            return None
        try:
            record = _session_log.load_record(self._db_path, session_id)
        except Exception as exc:
            # Journal is best-effort; a read failure keeps 404 semantics.
            logger.warning("session store: journal read failed (%s)", exc)
            return None
        if record is None:
            return None
        if time.time() - float(record.get("updated_at") or 0) > self._ttl:
            self._log(_session_log.delete_record, session_id)
            return None
        return record

    def drop(self, session_id: str) -> bool:
        """Explicit delete. Returns ``False`` when the id was already gone."""
        with self._lock:
            existed = session_id in self._sessions
            if not existed and self._db_path is not None:
                # The id may live only in the journal (created before a
                # restart, never loaded into this process) — releasing it
                # still counts as existing.
                try:
                    existed = _session_log.load_meta(self._db_path, session_id) is not None
                except Exception as exc:
                    # Journal is best-effort; a read failure keeps 404 semantics.
                    logger.warning("session store: journal read failed (%s)", exc)
            self._discard_locked(session_id, reason="explicit")
            return existed

    def drop_by_key(self, key_text: str) -> int:
        """Drop every session bound to one series key. Returns the count.

        Purge cascade: ``DELETE /data/series`` replaces the stored window out
        from under pooled sessions; a fingerprint hit afterwards would resume
        the pre-purge window indefinitely (fingerprints never re-validate),
        so the purge drops the bound sessions together with the bars.
        Unbound sessions (plain ``/session`` runs) are never touched — only
        pooled live sessions carry a binding.
        """
        if not key_text:
            return 0
        with self._lock:
            victims = []
            for sid, session in self._sessions.items():
                try:
                    binding = _live.binding_of(session)
                except Exception:
                    continue
                if binding is not None and binding.key.text == key_text:
                    victims.append(sid)
        for sid in victims:
            self.drop(sid)
        return len(victims)

    def _journal_meta(self, session_id: str) -> dict[str, Any] | None:
        """Row for a journaled-but-unloaded id, honouring TTL (no rebuild)."""
        if self._db_path is None:
            return None
        try:
            meta = _session_log.load_meta(self._db_path, session_id)
        except Exception as exc:
            # Journal is best-effort; a read failure keeps 404 semantics.
            logger.warning("session store: journal read failed (%s)", exc)
            return None
        if meta is None:
            return None
        if time.time() - float(meta.get("updated_at") or 0) > self._ttl:
            self._log(_session_log.delete_record, session_id)
            return None
        return meta

    def describe(self, session_id: str) -> dict[str, Any] | None:
        """Small status blob for ``GET /session/<id>`` — never the full state.

        A journaled-but-unloaded session is described from its row (no
        rebuild), so status stays cheap after a restart.
        """
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            session = self._sessions.get(session_id)
            if session is not None:
                seen = self._last_seen.get(session_id, now)
                bars = getattr(session, "_n_bars", None)
                return {
                    "session_id": session_id,
                    "bars": bars if isinstance(bars, int) else None,
                    "idle_seconds": round(now - seen, 3),
                    "ttl_seconds": self._ttl,
                }
        meta = self._journal_meta(session_id)
        if meta is None:
            return None
        idle = time.time() - float(meta.get("updated_at") or 0)
        return {
            "session_id": session_id,
            "bars": int(meta.get("bar_count") or 0),
            "idle_seconds": round(idle, 3),
            "ttl_seconds": self._ttl,
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._sweep_locked(time.monotonic())
            _stats.set_gauge("sessions.active", len(self._sessions))
            _stats.max_gauge("sessions.active_max", len(self._sessions))
            _stats.set_gauge("sessions.capacity", self._max)
            out: dict[str, Any] = {
                "sessions": len(self._sessions),
                "max_sessions": self._max,
                "ttl_seconds": self._ttl,
                "shared": len(self._shared_refs),
            }
        if self._db_path is not None:
            try:
                out["persisted"] = _session_log.count_records(self._db_path)
            except Exception as exc:
                # Journal is best-effort; stats must never fail.
                logger.warning("session store: journal read failed (%s)", exc)
                out["persisted"] = None
        else:
            out["persisted"] = None
        return out

    def clear(self) -> int:
        """Drop everything. Returns how many were removed."""
        with self._lock:
            ids = list(self._sessions)
            for sid in ids:
                self._discard_locked(sid, reason="clear")
            count = len(ids)
            self._shared.clear()
            self._shared_refs.clear()
        # Rows for ids never loaded into this process (pre-restart creates).
        self._log(_session_log.delete_all_records)
        return count

    def describe_all(self) -> list[dict[str, Any]]:
        """Read-only snapshot of every live session (pool inspector).

        Never raises: a session whose binding cannot be read is reported
        with ``key: None`` instead of failing the whole snapshot. Lock is
        held only for the in-memory read; callers join store windows
        afterwards.
        """
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            pooled = {sid: fp for fp, sid in self._shared.items()}
            rows: list[dict[str, Any]] = []
            for sid, session in self._sessions.items():
                try:
                    binding = _live.binding_of(session)
                except Exception:
                    binding = None
                rows.append(
                    {
                        "session_id": sid,
                        "pooled": sid in pooled,
                        "fingerprint": pooled.get(sid),
                        "refcount": int(self._shared_refs.get(sid, 0)),
                        "key": binding.key.text if binding is not None else None,
                        "anchor_time": int(binding.last_time) if binding is not None else None,
                        "idle_seconds": round(max(0.0, now - self._last_seen.get(sid, now)), 3),
                    }
                )
        return rows


# Process-wide singleton. Module-level because the store must be shared by
# every request handler in this worker — and deliberately NOT shared across
# workers (each process rebuilds its own memory view from the journal, so two
# writers would diverge; the single-worker constraint stands).
SESSIONS = SessionStore(
    ttl_seconds=_env_float("SESSION_TTL_SECONDS", DEFAULT_TTL_SECONDS),
    max_sessions=_env_int("SESSION_MAX_SESSIONS", DEFAULT_MAX_SESSIONS),
)


def _reset_sessions_for_tests() -> None:
    """@internal test helper — empty the singleton."""
    SESSIONS.clear()
