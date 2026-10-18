# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``WS /ws/live`` — engine-owned subscription plane (F-10).

One socket per client carries N multiplexed subscriptions::

    → { "type": "auth", "payload": { "token": "…" } }
    ← { "type": "auth.ok" }

    → { "type": "subscribe", "id": "…", "payload": {
            "kind": "series" | "script",
            "meta": { "exchange", "market", "symbol", "interval", "history_bars"? },
            "script"?, "inputs"?, "libraries"?, "mode"? } }
    ← { "type": "status", "id": "…", "sessionId": "…",
         "payload": { "code": "datafeed-loading", "have": 0, "need": 5000 } }

    → { "type": "ping" }  ← { "type": "pong" }

Auth mirrors the REST API-key middleware: a ``Bearer``/``ApiKey`` handshake
header or ``?api_key=`` pre-authenticates the connection, otherwise the first
frame must be ``auth``. Unauthenticated frames are rejected with
``UNAUTHORIZED``; rate-limited keys with ``RATE_LIMITED``.

This module owns the transport + auth + subscription registry only. History
ensure, feed ticks and ``full``/``delta``/``close`` frames land in F-11..F-13;
until then a subscribe is honestly answered with ``datafeed-loading`` and
``resume``/``repair`` with ``SESSION_GONE`` (repair aliases resume).

The protocol core (:class:`LiveConnection`) is socket-free so it unit-tests
without a WS client; :func:`register_live_route` wires the flask-sock loop.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)


PROTOCOL_VERSION = "live/1"

DEFAULT_HISTORY_BARS = 5000
MIN_HISTORY_BARS = 50
MAX_HISTORY_BARS = 100000


def canonical_series_key(meta: dict[str, Any]) -> str | None:
    """``exchange|market|SYMBOL|interval`` or ``None`` when malformed."""
    try:
        from backend.datastore.series import make_key

        return make_key(
            meta.get("exchange"),
            meta.get("symbol"),
            meta.get("interval"),
            meta.get("market"),
        ).text
    except Exception:  # noqa: BLE001
        return None


def clamp_history_bars(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_BARS
    return max(MIN_HISTORY_BARS, min(MAX_HISTORY_BARS, n))


def extract_handshake_key(headers: dict[str, str], args: dict[str, str]) -> str:
    """API key from upgrade headers or query string (mirrors REST middleware)."""
    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    if auth.startswith("ApiKey "):
        return auth[7:]
    return args.get("api_key", "") or args.get("token", "")


def validate_ws_key(raw_key: str) -> tuple[Any | None, str | None]:
    """Validate a WS credential. Returns ``(api_key, None)`` or ``(None, code)``."""
    from backend.middleware.auth import get_key_store

    store = get_key_store()
    api_key = store.validate_key(raw_key or "")
    if api_key is None:
        return None, "UNAUTHORIZED"
    if api_key.is_rate_limited():
        return None, "RATE_LIMITED"
    return api_key, None


class LiveRegistry:
    """In-memory subscription registry (single worker, like live sessions)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._subs: dict[str, dict[str, Any]] = {}
        self._seqs: dict[str, int] = {}

    def subscribe(self, client_id: str, kind: str, series_key: str, history_bars: int) -> dict[str, Any]:
        with self._lock:
            existing = self._subs.get(client_id)
            if existing is not None and existing["series_key"] == series_key:
                return existing
            session = {
                "session_id": f"live_{next(self._ids)}",
                "kind": kind,
                "series_key": series_key,
                "history_bars": history_bars,
            }
            self._subs[client_id] = session
            return session

    def get(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._subs.get(client_id)

    def release(self, client_id: str) -> bool:
        with self._lock:
            self._seqs.pop(client_id, None)
            return self._subs.pop(client_id, None) is not None

    def attach(self, client_id: str, engine_session_id: str | None, bars: list[dict[str, Any]]) -> None:
        """Stash the engine session + evaluated window on a subscription."""
        with self._lock:
            record = self._subs.get(client_id)
            if record is not None:
                record["engine_session_id"] = engine_session_id
                record["bars"] = list(bars)
                record["count"] = len(bars)
                last = bars[-1] if bars else None
                if isinstance(last, dict) and "time" in last:
                    try:
                        record["last_time"] = int(last["time"])
                    except (TypeError, ValueError):
                        record["last_time"] = 0

    def next_seq(self, client_id: str) -> int:
        """Monotonic per-subscription frame sequence (F-12 gap detection)."""
        with self._lock:
            seq = self._seqs.get(client_id, 0) + 1
            self._seqs[client_id] = seq
            return seq


_SHARED_REGISTRY = LiveRegistry()


class LiveConnection:
    """Socket-free protocol core: validate + route one parsed frame."""

    def __init__(
        self,
        registry: LiveRegistry | None = None,
        *,
        authed: bool = False,
        ensurer: Any = None,
        resumer: Any = None,
        store_window: Any = None,
        hub: Any = None,
        sender: Any = None,
        attacher: Any = None,
    ) -> None:
        self._registry = registry or _SHARED_REGISTRY
        self.authed = authed
        self._ensurer = ensurer
        self._resumer = resumer
        self._store_window_fn = store_window
        self._hub = hub
        self._sender = sender
        self._attacher = attacher

    def _attach(self, key_text: str, engine_session_id: str | None, client_id: str) -> None:
        if engine_session_id is None:
            return
        if self._hub is not None and self._sender is not None:
            self._hub.register(client_id, self._sender)
        attacher = self._attacher
        if attacher is None:
            from backend.api.live_push import attach_feed as _attach_feed

            attacher = _attach_feed
        try:
            attacher(key_text, engine_session_id, client_id, self._registry, self._hub)
        except Exception:  # noqa: BLE001 — attach failure degrades to snapshot-only
            logger.warning("feed attach failed for %s (%s)", key_text, client_id, exc_info=True)

    def _ensure(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        ensurer = self._ensurer
        if ensurer is None:
            from backend.api.live_ensure import ensure_live_session

            ensurer = ensure_live_session
        return ensurer(*args, **kwargs)

    def _resume_session(self, session_id: str) -> dict[str, Any]:
        resumer = self._resumer
        if resumer is None:
            from backend.api.live_ensure import resume_live_session

            resumer = resume_live_session
        return resumer(session_id)

    # -- replies ---------------------------------------------------------
    @staticmethod
    def _err(req_id: Any, message: str, code: str, **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "error", "message": message, "code": code}
        if req_id is not None:
            out["id"] = req_id
        out.update(extra)
        return out

    # -- entry point -----------------------------------------------------
    def handle_dict(self, msg: Any) -> list[dict[str, Any]]:
        if not isinstance(msg, dict):
            return [self._err(None, "Frame must be a JSON object", "BAD_FRAME")]
        mtype = str(msg.get("type") or "")
        req_id = msg.get("id")

        if mtype == "ping":
            return [{"type": "pong", **({"id": req_id} if req_id is not None else {})}]
        if mtype == "auth":
            return self._handle_auth(req_id, msg.get("payload"))

        if not self.authed:
            return [
                self._err(
                    req_id,
                    "Authenticate first (auth frame or API key handshake)",
                    "UNAUTHORIZED",
                    retryable=False,
                )
            ]

        if mtype == "subscribe":
            return self._handle_subscribe(req_id, msg.get("payload"))
        if mtype == "resume":
            return self._handle_resume(req_id, msg.get("payload"))
        if mtype == "repair":
            return self._handle_repair(req_id, msg.get("payload"))
        if mtype == "unsubscribe":
            return self._handle_unsubscribe(req_id)
        return [self._err(req_id, f"Unknown type: {mtype}", "UNKNOWN_TYPE")]

    # -- handlers --------------------------------------------------------
    def _handle_auth(self, req_id: Any, payload: Any) -> list[dict[str, Any]]:
        token = payload.get("token", "") if isinstance(payload, dict) else ""
        _, code = validate_ws_key(str(token or ""))
        if code is not None:
            return [self._err(req_id, "Invalid or missing API key", code, retryable=False)]
        self.authed = True
        out: dict[str, Any] = {"type": "auth.ok"}
        if req_id is not None:
            out["id"] = req_id
        return [out]

    def _handle_subscribe(self, req_id: Any, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return [self._err(req_id, "subscribe needs a payload object", "BAD_SUBSCRIBE")]
        kind = payload.get("kind")
        if kind not in ("series", "script"):
            return [self._err(req_id, "kind must be series|script", "BAD_SUBSCRIBE")]
        meta = payload.get("meta")
        series_key = canonical_series_key(meta) if isinstance(meta, dict) else None
        if series_key is None:
            return [self._err(req_id, "meta needs exchange/symbol/interval", "BAD_SUBSCRIBE")]
        if kind == "script" and not payload.get("script"):
            return [self._err(req_id, "script subscriptions need a script", "BAD_SUBSCRIBE")]
        history_bars = clamp_history_bars((meta or {}).get("history_bars"))
        if req_id is None:
            return [self._err(None, "subscribe needs an id", "BAD_SUBSCRIBE")]
        # Tell the client we are fetching before the 4–9s first walk so the
        # HUD is honest and the socket is not silent during ensure.
        pending = self._registry.subscribe(str(req_id), str(kind), series_key, history_bars)
        if self._sender is not None:
            try:
                self._sender(
                    {
                        "type": "status",
                        "id": req_id,
                        "sessionId": pending["session_id"],
                        "payload": {
                            "code": "datafeed-loading",
                            "need": history_bars,
                            "message": "fetching history",
                        },
                    }
                )
            except Exception:  # noqa: BLE001
                logger.warning("early datafeed-loading send failed", exc_info=True)
        ensured = self._ensure(
            str(kind),
            series_key,
            history_bars,
            script=payload.get("script") if isinstance(payload.get("script"), str) else None,
            inputs=payload.get("inputs") if isinstance(payload.get("inputs"), dict) else None,
            libraries=payload.get("libraries") if isinstance(payload.get("libraries"), list) else None,
            profiler=bool(payload.get("profiler")),
            symbol=str(meta.get("symbol", "")) if isinstance(meta, dict) else "",
        )
        if ensured.get("outcome") == "error":
            return [
                self._err(
                    req_id, str(ensured.get("message") or "ensure failed"), str(ensured.get("code") or "ENSURE_FAILED")
                )
            ]
        if ensured.get("outcome") == "short":
            session = self._registry.subscribe(str(req_id), str(kind), series_key, history_bars)
            return [
                {
                    "type": "status",
                    "id": req_id,
                    "sessionId": session["session_id"],
                    "payload": {
                        "code": "datafeed-loading",
                        "have": int(ensured.get("have") or 0),
                        "need": int(ensured.get("need") or history_bars),
                        "message": "fetching history",
                    },
                }
            ]
        return self._emit_ready(str(req_id), str(kind), series_key, ensured)

    def _handle_resume(self, req_id: Any, payload: Any) -> list[dict[str, Any]]:
        record = self._registry.get(str(req_id)) if req_id is not None else None
        if record is None:
            return [
                self._err(
                    req_id,
                    "Unknown subscription — subscribe again",
                    "SESSION_GONE",
                    retryable=True,
                )
            ]
        engine_session_id = record.get("engine_session_id")
        if engine_session_id:
            resumed = self._resume_session(str(engine_session_id))
            if resumed.get("outcome") == "ready":
                return self._emit_snapshot(str(req_id), record, resumed.get("session"))
        # Gone or series-kind: re-ensure history, then snapshot from the store.
        return self._emit_ready(
            str(req_id), str(record.get("kind") or "series"), str(record.get("series_key") or ""), None
        )

    def _emit_ready(
        self, client_id: str, kind: str, series_key: str, ensured: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """status(datafeed-ready) → [status(calculating)] → full → status(live)."""
        from backend.api.live_ensure import parse_live_key

        if ensured is None:
            record = self._registry.get(client_id) or {}
            ensured = self._ensure(kind, series_key, int(record.get("history_bars") or DEFAULT_HISTORY_BARS))
            if ensured.get("outcome") != "ready":
                return [self._status_loading(client_id, ensured)]
        session = self._registry.subscribe(
            client_id, kind, series_key, int(ensured.get("count") or 0) or DEFAULT_HISTORY_BARS
        )
        frames: list[dict[str, Any]] = [make_status(session["session_id"], "datafeed-ready", client_id)]
        bars: list[dict[str, Any]] = []
        full_kwargs: dict[str, Any] = {}
        if kind == "script" and ensured.get("session_id"):
            resumed = self._resume_session(str(ensured["session_id"]))
            live_session = resumed.get("session")
            try:
                result = live_session.result() if live_session is not None else {}
            except Exception:  # noqa: BLE001
                result = {}
            bars = self._bars_of(live_session)
            full_kwargs = {
                "plots": result.get("plots"),
                "series": result.get("series"),
                "bar_times": [b["time"] for b in bars],
                "plot_meta": result.get("plot_meta"),
                "events": result.get("events"),
                "drawings": result.get("drawings"),
            }
            frames.append(make_status(session["session_id"], "calculating", client_id))
        else:
            bars = self._store_window(series_key, int(ensured.get("count") or 0))
            full_kwargs = {"bars": bars}
        self._registry.attach(client_id, ensured.get("session_id"), bars)
        # A series-kind subscription has no engine session (its bars serve
        # straight from the store), but it still needs the feed: without an
        # attach nothing keeps the store moving, and the client only ever got
        # the frozen ensure-time snapshot. The registry's own live id is the
        # listener key — stable per client + series, so a re-subscribe replaces
        # the listener in place instead of stacking one per frame.
        self._attach(series_key, ensured.get("session_id") or session["session_id"], client_id)
        key = parse_live_key(series_key)
        watermark = closed_upto(bars, int(key.interval_seconds) if key else 60, int(time.time()))
        seq = self._registry.next_seq(client_id)
        frames.append(
            make_full(
                session["session_id"],
                seq,
                series_key,
                int(ensured.get("count") or len(bars)),
                watermark,
                client_id,
                **full_kwargs,
            )
        )
        frames.append(make_status(session["session_id"], "live", client_id))
        return frames

    def _emit_snapshot(self, client_id: str, record: dict[str, Any], live_session: Any) -> list[dict[str, Any]]:
        """Resume answer: fresh full snapshot + live (delta-continuation is future work)."""
        from backend.api.live_ensure import parse_live_key

        session_id = str(record.get("engine_session_id") or "")
        bars = self._bars_of(live_session)
        try:
            result = live_session.result() if live_session is not None else {}
        except Exception:  # noqa: BLE001
            result = {}
        key = parse_live_key(str(record.get("series_key") or ""))
        watermark = closed_upto(bars, int(key.interval_seconds) if key else 60, int(time.time()))
        seq = self._registry.next_seq(client_id)
        return [
            make_full(
                session_id,
                seq,
                str(record.get("series_key") or ""),
                len(bars),
                watermark,
                client_id,
                plots=result.get("plots"),
                series=result.get("series"),
                bar_times=[b["time"] for b in bars],
                plot_meta=result.get("plot_meta"),
                events=result.get("events"),
                drawings=result.get("drawings"),
            ),
            make_status(session_id, "live", client_id),
        ]

    @staticmethod
    def _bars_of(live_session: Any) -> list[dict[str, Any]]:
        bars = getattr(live_session, "_ohlcv_data", None) if live_session is not None else None
        return list(bars) if isinstance(bars, list) else []

    def _store_window(self, series_key: str, limit: int) -> list[dict[str, Any]]:
        if self._store_window_fn is not None:
            return list(self._store_window_fn(series_key, limit))
        from backend.api.live_ensure import parse_live_key
        from backend.datastore import series as series_store

        key = parse_live_key(series_key)
        if key is None:
            return []
        try:
            return series_store.get_store().window(key, max(1, limit))
        except Exception:  # noqa: BLE001
            return []

    def _status_loading(self, client_id: str, ensured: dict[str, Any]) -> dict[str, Any]:
        session = self._registry.get(client_id) or {"session_id": f"pending-{client_id}"}
        if ensured.get("outcome") == "error":
            return self._err(
                client_id, str(ensured.get("message") or "ensure failed"), str(ensured.get("code") or "ENSURE_FAILED")
            )
        return {
            "type": "status",
            "id": client_id,
            "sessionId": session["session_id"],
            "payload": {
                "code": "datafeed-loading",
                "have": int(ensured.get("have") or 0),
                "need": int(ensured.get("need") or 0),
                "message": "fetching history",
            },
        }

    def _handle_repair(self, req_id: Any, payload: Any) -> list[dict[str, Any]]:
        # Repair aliases resume: a fresh snapshot from the current store /
        # session is enough to heal a close mismatch or seq gap.
        return self._handle_resume(req_id, payload if isinstance(payload, dict) else {})

    def _handle_unsubscribe(self, req_id: Any) -> list[dict[str, Any]]:
        if req_id is not None:
            record = self._registry.get(str(req_id))
            if record is not None:
                try:
                    from backend.api.feed_manager import get_feed_manager

                    get_feed_manager().unsubscribe(
                        str(record.get("series_key") or ""),
                        str(record.get("engine_session_id") or req_id),
                    )
                except Exception:  # noqa: BLE001
                    pass
            if self._hub is not None:
                try:
                    self._hub.unregister(str(req_id))
                except Exception:  # noqa: BLE001
                    pass
            self._registry.release(str(req_id))
        return [
            {
                "type": "status",
                **({"id": req_id} if req_id is not None else {}),
                "payload": {"code": "idle", "message": "released"},
            }
        ]


def serve_live_ws(ws: Any, registry: LiveRegistry | None = None) -> None:
    """flask-sock loop for ``WS /ws/live`` (handshake pre-auth + frames)."""
    import queue as _queue

    from flask import request as flask_request

    from backend.api.live_push import HUB

    headers = dict(flask_request.headers or {})
    args = dict(flask_request.args or {})
    authed = False
    raw = extract_handshake_key(headers, args)
    if raw:
        _, code = validate_ws_key(raw)
        authed = code is None

    sock_thread = threading.current_thread()
    outbound: _queue.Queue[dict[str, Any]] = _queue.Queue()

    def sender(frame: dict[str, Any]) -> None:
        # flask-sock is not safe from the feed thread; only this loop sends.
        if threading.current_thread() is sock_thread:
            ws.send(json.dumps(frame))
            return
        outbound.put(frame)

    def drain_outbound() -> None:
        while True:
            try:
                frame = outbound.get_nowait()
            except _queue.Empty:
                return
            try:
                ws.send(json.dumps(frame))
            except Exception:  # noqa: BLE001
                return

    conn = LiveConnection(registry, authed=authed, hub=HUB, sender=sender)
    if authed:
        try:
            ws.send(json.dumps({"type": "auth.ok"}))
        except Exception:  # noqa: BLE001
            return
    while True:
        try:
            incoming = ws.receive(timeout=0.05)
        except TypeError:
            incoming = ws.receive()
        except Exception as exc:  # noqa: BLE001 — timeout classes differ by transport
            name = type(exc).__name__.lower()
            if "timeout" in name:
                incoming = ""
            else:
                break
        drain_outbound()
        if incoming is None:
            break
        if incoming == "":
            continue
        try:
            msg = json.loads(incoming) if isinstance(incoming, str) else json.loads(incoming.decode("utf-8"))
        except Exception:  # noqa: BLE001
            ws.send(json.dumps({"type": "error", "message": "Invalid JSON frame", "code": "BAD_JSON"}))
            continue
        for reply in conn.handle_dict(msg):
            ws.send(json.dumps(reply))
        drain_outbound()


def register_live_route(sock: Any, registry: LiveRegistry | None = None) -> None:
    """Register ``WS /ws/live`` on a flask-sock instance."""

    @sock.route("/ws/live")
    def ws_live(ws):  # type: ignore[no-untyped-def]
        """Engine-owned subscription plane (see module docstring)."""
        serve_live_ws(ws, registry)


def subscription_fingerprint(kind: str, series_key: str, script: str = "") -> str:
    """Stable id for a (kind, series, script) subscription."""
    digest = hashlib.sha256(f"{kind}|{series_key}|{script}".encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Frame builders (F-12): status / full / delta / close with seq + watermark.
# ---------------------------------------------------------------------------


def closed_upto(candles: list[dict[str, Any]], interval_s: int, now_s: int) -> int:
    """Newest bar time fully closed at ``now_s`` (0 when none is closed)."""
    upto = 0
    for bar in candles or []:
        try:
            t = int(bar["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if t + interval_s <= now_s and t > upto:
            upto = t
    return upto


def make_status(
    session_id: str,
    code: str,
    req_id: Any = None,
    *,
    have: int | None = None,
    need: int | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "status", "sessionId": session_id, "payload": {"code": code}}
    if req_id is not None:
        out["id"] = req_id
    if have is not None:
        out["payload"]["have"] = have
    if need is not None:
        out["payload"]["need"] = need
    if message is not None:
        out["payload"]["message"] = message
    return out


def make_full(
    session_id: str,
    seq: int,
    series_key: str,
    count: int,
    watermark: int,
    req_id: Any = None,
    *,
    bars: list[dict[str, Any]] | None = None,
    bar_times: list[int] | None = None,
    plots: Any = None,
    series: dict[str, Any] | None = None,
    plot_meta: Any = None,
    events: Any = None,
    drawings: Any = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(seq, int) or seq < 1:
        raise ValueError("full frames need a positive seq")
    payload: dict[str, Any] = {
        "seriesKey": series_key,
        "count": count,
        "closed_upto": watermark,
    }
    if bars is not None:
        payload["bars"] = bars
    if bar_times is not None:
        payload["bar_times"] = bar_times
    if plots is not None:
        payload["plots"] = plots
    if series is not None:
        payload["series"] = series
    if plot_meta is not None:
        payload["plot_meta"] = plot_meta
    if events is not None:
        payload["events"] = events
    if drawings is not None:
        payload["drawings"] = drawings
    if meta is not None:
        payload["meta"] = meta
    out: dict[str, Any] = {
        "type": "full",
        "sessionId": session_id,
        "seq": seq,
        "payload": payload,
    }
    if req_id is not None:
        out["id"] = req_id
    return out


def make_delta(
    session_id: str,
    seq: int,
    base_index: int,
    count: int,
    tail_len: int,
    watermark: int,
    req_id: Any = None,
    *,
    forming: bool | None = None,
    plots: Any = None,
    series: dict[str, Any] | None = None,
    bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(seq, int) or seq < 1:
        raise ValueError("delta frames need a positive seq")
    if tail_len != count - base_index:
        raise ValueError("tail_len must equal count - base_index")
    payload: dict[str, Any] = {
        "base_index": base_index,
        "count": count,
        "tail_len": tail_len,
        "closed_upto": watermark,
    }
    if forming is not None:
        payload["forming"] = forming
    if plots is not None:
        payload["plots"] = plots
    if series is not None:
        payload["series"] = series
    if bars is not None:
        payload["bars"] = bars
    out: dict[str, Any] = {
        "type": "delta",
        "sessionId": session_id,
        "seq": seq,
        "payload": payload,
    }
    if req_id is not None:
        out["id"] = req_id
    return out


def make_close(
    session_id: str,
    seq: int,
    index: int,
    bar_time: int,
    ohlcv: dict[str, Any],
    req_id: Any = None,
) -> dict[str, Any]:
    if not isinstance(seq, int) or seq < 1:
        raise ValueError("close frames need a positive seq")
    for field in ("open", "high", "low", "close", "volume"):
        if field not in ohlcv:
            raise ValueError(f"close frame ohlcv is missing {field}")
    out: dict[str, Any] = {
        "type": "close",
        "sessionId": session_id,
        "seq": seq,
        "payload": {"index": index, "time": bar_time, "ohlcv": dict(ohlcv)},
    }
    if req_id is not None:
        out["id"] = req_id
    return out
