# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Feed → session → socket push loop (closes the F-13/F-19 gap).

A WS ``subscribe`` ensures history and answers ``full``; without this module
nothing would ever push again. :func:`attach_feed` binds the engine session to
its series feed: each candle batch advances the session walk-free
(:func:`apply_feed_candles`) and emits ``delta`` + ``close`` frames with
per-subscription ``seq`` through the :class:`PushHub` sender the socket loop
registers.

Unchanged ticks emit nothing (the client already holds the tip). Sessions
released or TTL-evicted detach themselves on the next batch.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class PushHub:
    """Routes frames to live sockets by subscription id (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._senders: dict[str, Callable[[dict[str, Any]], None]] = {}

    def register(self, client_id: str, send: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._senders[client_id] = send

    def unregister(self, client_id: str) -> None:
        with self._lock:
            self._senders.pop(client_id, None)

    def send(self, client_id: str, frame: dict[str, Any]) -> bool:
        with self._lock:
            send = self._senders.get(client_id)
        if send is None:
            return False
        try:
            send(frame)
        except Exception:  # noqa: BLE001 — a dead socket must not kill the feed
            return False
        return True


HUB = PushHub()


def _journal_recorder(session_id: str) -> Callable[[str, Any], None]:
    from backend.app import _make_session_journal_recorder

    return _make_session_journal_recorder(session_id)


def emit_push_frames(
    client_id: str,
    registry: Any,
    session: Any,
    engine_session_id: str,
    binding: Any,
    outcome: Any,
    candles: list[dict[str, Any]],
    now_s: int,
) -> list[dict[str, Any]]:
    """Advance frames for one script-session feed batch. Returns emitted frames."""
    from backend.api.live_ws import closed_upto, make_close, make_delta

    if getattr(outcome, "unchanged", True):
        return []
    try:
        result = session.result()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(result, dict) or "error" in result:
        return []
    tail_len = int(getattr(outcome, "grown", 0) or 0) + int(getattr(outcome, "reticked", 0) or 0)
    if tail_len <= 0:
        return []
    from backend.app import _delta_envelope

    envelope = _delta_envelope(result, engine_session_id, {}, tail_len)
    if not isinstance(envelope, dict) or "delta" not in envelope:
        return []
    delta_block = envelope["delta"]
    interval_s = int(binding.key.interval_seconds)
    watermark = closed_upto(candles, interval_s, now_s)
    emitted: list[dict[str, Any]] = []
    seq = registry.next_seq(client_id)
    emitted.append(
        make_delta(
            engine_session_id,
            seq,
            int(delta_block["base_index"]),
            int(delta_block["count"]),
            int(delta_block["tail_len"]),
            watermark,
            client_id,
            forming=bool(getattr(outcome, "forming", False)),
            plots=envelope.get("plots"),
            series=envelope.get("series") if isinstance(envelope.get("series"), dict) else None,
        )
    )
    count = int(delta_block["count"])
    if int(getattr(outcome, "appended", 0) or 0) > 0 and watermark > 0:
        final = next((c for c in candles if int(c["time"]) == watermark), None)
        if final is not None:
            index = count - 1 - (1 if getattr(outcome, "forming", False) else 0)
            seq = registry.next_seq(client_id)
            emitted.append(
                make_close(
                    engine_session_id,
                    seq,
                    max(0, index),
                    watermark,
                    {k: final[k] for k in ("open", "high", "low", "close", "volume")},
                    client_id,
                )
            )
    return emitted


def emit_series_push(
    client_id: str,
    registry: Any,
    record: dict[str, Any],
    candles: list[dict[str, Any]],
    now_s: int,
) -> list[dict[str, Any]]:
    """Series-kind push: candle bars with positional cursor, no session needed."""
    from backend.api.live_ensure import parse_live_key
    from backend.api.live_ws import closed_upto, make_close, make_delta

    batch = [c for c in candles if isinstance(c, dict) and "time" in c]
    if not batch:
        return []
    key = parse_live_key(str(record.get("series_key") or ""))
    interval_s = int(key.interval_seconds) if key else 60
    before = int(record.get("count") or 0)
    after = before + len(batch)
    record["count"] = after
    watermark = closed_upto(batch, interval_s, now_s)
    session_id = str(record.get("engine_session_id") or f"series-{client_id}")
    seq = registry.next_seq(client_id)
    emitted = [
        make_delta(
            session_id,
            seq,
            before,
            after,
            len(batch),
            watermark,
            client_id,
            bars=batch,
        )
    ]
    if watermark > 0:
        final = next((c for c in batch if int(c["time"]) == watermark), None)
        if final is not None:
            emitted.append(
                make_close(
                    session_id,
                    registry.next_seq(client_id),
                    after - 1,
                    watermark,
                    {k: final[k] for k in ("open", "high", "low", "close", "volume")},
                    client_id,
                )
            )
    return emitted


def attach_feed(
    key_text: str,
    engine_session_id: str,
    client_id: str,
    registry: Any,
    hub: PushHub | None = None,
) -> bool:
    """Bind an engine session to its series feed with WS emission."""
    from backend.api.feed_manager import apply_feed_candles, get_feed_manager, now_seconds
    from backend.datastore import live as data_live
    from backend.middleware.sessions import SESSIONS

    hub = hub or HUB
    manager = get_feed_manager()

    def on_candles(candles: list[dict[str, Any]]) -> None:
        record = registry.get(client_id)
        if record is not None and str(record.get("kind") or "") == "series":
            for frame in emit_series_push(client_id, registry, record, candles, now_seconds()):
                hub.send(client_id, frame)
            return
        session = SESSIONS.get(engine_session_id)
        if session is None:
            manager.unsubscribe(key_text, engine_session_id)
            hub.unregister(client_id)
            return
        binding = data_live.binding_of(session)
        if binding is None:
            return
        now_s = now_seconds()
        try:
            outcome = apply_feed_candles(
                session, binding, candles, now_s, on_apply=_journal_recorder(engine_session_id)
            )
        except Exception:  # noqa: BLE001
            return
        for frame in emit_push_frames(
            client_id, registry, session, engine_session_id, binding, outcome, candles, now_s
        ):
            hub.send(client_id, frame)

    subscribed = manager.subscribe(key_text, engine_session_id, on_candles)
    return subscribed is not None
