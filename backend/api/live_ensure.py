# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Autonomous history ensure + session create/resume for ``WS /ws/live`` (F-13).

A WS ``subscribe`` never brokers a fetch from the client: this module checks
the series store, walks history to the last closed bar on a miss
(:func:`fetch_history`), creates the interpret session over the stored window
and binds + journal-tracks it — the same shape as ``POST /data/session``.
``resume`` re-enters through the session store so journaled sessions rebuild
after a restart.
"""

from __future__ import annotations

from typing import Any


def parse_live_key(key_text: str) -> Any | None:
    """``exchange|market|SYMBOL|interval`` → SeriesKey, or ``None``."""
    from backend.datastore import series as series_store

    parts = (key_text or "").split("|")
    if len(parts) != 4:
        return None
    exchange, market, symbol, interval = parts
    try:
        return series_store.make_key(exchange, symbol, interval, market)
    except Exception:
        return None


def key_text_symbol(series_key_text: str) -> str:
    """Symbol part of a canonical key text (display name for the runtime)."""
    parts = (series_key_text or "").split("|")
    return parts[2] if len(parts) == 4 else ""


def ensure_live_session(
    kind: str,
    series_key_text: str,
    history_bars: Any,
    *,
    script: str | None = None,
    inputs: dict[str, Any] | None = None,
    libraries: list[dict[str, Any]] | None = None,
    profiler: bool = False,
    symbol: str = "",
) -> dict[str, Any]:
    """Ensure history + session for a subscribe. Never raises.

    Outcomes: ``ready`` (``session_id`` may be ``None`` for ``series`` kind —
    bars serve straight from the store), ``short`` (``have``/``need`` — the
    caller keeps the client at ``datafeed-loading``), ``error`` (``code``).
    """
    from backend.api import data_series
    from backend.datastore import live as data_live
    from backend.datastore import series as series_store
    from backend.middleware.sessions import SESSIONS

    key = parse_live_key(series_key_text)
    if key is None:
        return {"outcome": "error", "code": "BAD_KEY", "message": "Unparseable series key."}
    try:
        history = series_store.check_history_bars(history_bars)
    except Exception:
        return {"outcome": "error", "code": "BAD_HISTORY", "message": "Unusable history_bars."}

    if kind == "script" and not script:
        return {"outcome": "error", "code": "NO_SCRIPT", "message": "Script subscriptions need a script."}

    store = series_store.get_store()
    have = int(store.status(key)["count"])
    if have < history:
        try:
            data_series.fetch_history(key, history)
        except Exception as exc:  # upstream/CCXT failure is data, not a 500
            from backend.api.data_series import DataError

            if isinstance(exc, DataError):
                return {"outcome": "error", "code": exc.code, "message": str(exc)}
            return {"outcome": "error", "code": "UPSTREAM", "message": f"History fetch failed: {exc}"}
        have = int(store.status(key)["count"])
    if have < history:
        return {"outcome": "short", "have": have, "need": history}

    bars = store.window(key, history)
    if not bars:
        return {"outcome": "short", "have": 0, "need": history}
    if kind == "series":
        newest = int(bars[-1]["time"])
        return {"outcome": "ready", "session_id": None, "count": len(bars), "newest": newest}

    from src.pynescript.runtime.host import Runtime

    ticker = symbol or str(key_text_symbol(series_key_text))
    fingerprint = SESSIONS.shared_fingerprint(
        script=script or "",
        symbol=f"{series_key_text}|{ticker}",
        inputs=inputs or {},
        libraries=libraries or [],
        profiler=bool(profiler),
    )

    def _factory() -> Any:
        runtime = Runtime(symbol=ticker)
        return runtime.create_session(
            script or "",
            bars,
            inputs=inputs or None,
            libraries=libraries or None,
            profiler=bool(profiler),
            mode="interpret",
        )

    try:
        session_id, created = SESSIONS.acquire_shared(fingerprint, _factory)
    except RuntimeError:
        return {"outcome": "error", "code": "SESSIONS_FULL", "message": "Session capacity reached."}
    except Exception as exc:
        return {"outcome": "error", "code": "SESSION_CREATE_FAILED", "message": str(exc)[:500]}

    session = SESSIONS.get(session_id)
    if session is None:
        return {"outcome": "error", "code": "SESSION_CREATE_FAILED", "message": "Shared session vanished."}
    if created:
        data_live.bind_session(session, key, bars[-1])
        binding = data_live.binding_of(session)
        SESSIONS.track(
            session_id,
            script=script or "",
            symbol=symbol,
            inputs=inputs or {},
            libraries=libraries or [],
            profiler=bool(profiler),
            bars=list(bars),
            binding=(
                {"key": binding.key.text, "last_time": binding.last_time, "last_bar": dict(binding.last_bar)}
                if binding is not None
                else None
            ),
        )
    return {"outcome": "ready", "session_id": session_id, "count": len(bars), "newest": int(bars[-1]["time"])}


def resume_live_session(session_id: str) -> dict[str, Any]:
    """Re-enter a session (journal rebuild included). Never raises."""
    from backend.middleware.sessions import SESSIONS

    try:
        session = SESSIONS.get(session_id)
    except Exception:
        session = None
    if session is None:
        return {"outcome": "gone"}
    bars = getattr(session, "_ohlcv_data", None) or []
    return {"outcome": "ready", "session": session, "count": len(bars)}
