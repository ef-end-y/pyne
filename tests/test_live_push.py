# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end push loop (F-13/F-19 prerequisite): subscribe → full, then a
feed batch advances the session and emits ``delta`` (+ ``close``) to the
socket. Feeds, sessions and sockets are all fakes except the real protocol,
registry, delta envelope and session math.
"""

from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.api.live_push import HUB, PushHub, attach_feed  # noqa: E402
from backend.api.live_ws import LiveConnection, LiveRegistry  # noqa: E402


META = {"exchange": "binance", "market": "spot", "symbol": "btc/usdt", "interval": "1m"}
KEY_TEXT = "binance|spot|BTC/USDT|1m"


def _bars10():
    return [
        {"time": 1_700_000_000 + i * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
        for i in range(10)
    ]


class _FakeSession:
    def __init__(self, bars):
        self._ohlcv_data = list(bars)
        self.ticks = []
        self.appends = []

    def update_last_bar(self, bar):
        self.ticks.append(dict(bar))
        self._ohlcv_data[-1] = dict(bar)
        return None

    def append_bars(self, bars):
        self.appends.extend([dict(b) for b in bars])
        self._ohlcv_data.extend([dict(b) for b in bars])
        return None

    def result(self):
        n = len(self._ohlcv_data)
        return {"plots": [[1.0] * n], "series": {"a": [1.0] * n}, "count": n}


class _Outcome:
    def __init__(self, **kw):
        base = {"unchanged": False, "appended": 0, "forming": False, "grown": 0, "reticked": 0}
        base.update(kw)
        self.__dict__.update(base)


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []
        self._script = [
            json.dumps(
                {"type": "subscribe", "id": "sub-1", "payload": {"kind": "script", "meta": META, "script": "x"}}
            ),
            None,
        ]

    def receive(self):
        return self._script.pop(0) if self._script else None

    def send(self, raw):
        self.sent.append(json.loads(raw))


def test_push_hub_routes_and_drops_dead_sockets():
    hub = PushHub()
    seen = []
    hub.register("c1", seen.append)
    assert hub.send("c1", {"a": 1}) is True
    assert seen == [{"a": 1}]

    def _boom(_f):
        raise RuntimeError("dead")

    hub.register("c2", _boom)
    assert hub.send("c2", {"a": 1}) is False
    assert hub.send("ghost", {"a": 1}) is False


def test_feed_batch_emits_delta_to_socket(monkeypatch):
    import backend.api.live_push as push_mod

    session = _FakeSession(_bars10())
    registry = LiveRegistry()
    record = registry.subscribe("sub-1", "script", KEY_TEXT, 10)
    registry.attach("sub-1", "ses-1", _bars10())

    sent: list[dict] = []
    hub = PushHub()
    hub.register("sub-1", sent.append)

    monkeypatch.setattr(push_mod, "HUB", hub)

    class _FakeManager:
        def subscribe(self, key_text, session_id, cb):
            self.cb = cb
            return True

        def unsubscribe(self, key_text, session_id):
            return None

    fake_manager = _FakeManager()

    import backend.middleware.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod.SESSIONS, "get", lambda _sid: session)

    attached = []

    def fake_attach(key_text, engine_session_id, client_id, reg, hub=None):
        attached.append((key_text, engine_session_id, client_id))

        def on_candles(candles):
            from backend.datastore import live as live_mod

            binding = live_mod.bind_session(session, _key(), session._ohlcv_data[-1])
            outcome = _Outcome(grown=1, reticked=0, appended=0, forming=True)
            for frame in push_mod.emit_push_frames(
                client_id, reg, session, engine_session_id, binding, outcome, candles, 1_700_000_000 + 10 * 60 + 30
            ):
                (hub or push_mod.HUB).send(client_id, frame)

        fake_manager.subscribe(key_text, engine_session_id, on_candles)
        return True

    def _key():
        from backend.datastore import series as series_store

        return series_store.make_key("binance", "BTC/USDT", "1m", "spot")

    conn = LiveConnection(
        registry=registry,
        authed=True,
        ensurer=lambda kind, key, history, **kw: {"outcome": "ready", "session_id": "ses-1", "count": 10, "newest": 1},
        resumer=lambda sid: {"outcome": "ready", "session": session, "count": 10},
        store_window=lambda _k, _l: _bars10(),
        hub=hub,
        sender=sent.append,
        attacher=fake_attach,
    )
    replies = conn.handle_dict(
        {"type": "subscribe", "id": "sub-1", "payload": {"kind": "script", "meta": META, "script": "x"}}
    )
    assert [r.get("type") for r in replies] == ["status", "status", "full", "status"]
    assert attached == [(KEY_TEXT, "ses-1", "sub-1")]

    # One forming tick on top of the 10-bar window.
    tick = dict(_bars10()[-1])
    tick["close"] = 1.5
    fake_manager.cb([tick])
    assert len(sent) == 1
    delta = sent[0]
    assert delta["type"] == "delta"
    assert delta["seq"] == 2
    assert delta["payload"]["tail_len"] == 1
    assert delta["payload"]["closed_upto"] == 0 or isinstance(delta["payload"]["closed_upto"], int)


def test_serve_loop_subscribe_over_fake_socket():
    from backend.api.live_ws import serve_live_ws
    from backend.app import app

    ws = FakeWs()
    conn_registry = LiveRegistry()

    import backend.api.live_ws as ws_mod

    real_conn_cls = ws_mod.LiveConnection

    def fake_conn_factory(registry=None, authed=False, **kw):
        return real_conn_cls(
            registry=conn_registry,
            authed=True,
            ensurer=lambda kind, key, history, **_: {"outcome": "short", "have": 0, "need": 50},
            **{k: v for k, v in kw.items() if k in ("hub", "sender")},
        )

    ws_mod.LiveConnection = fake_conn_factory  # type: ignore[assignment]
    try:
        with app.test_request_context("/ws/live"):
            serve_live_ws(ws, conn_registry)
    finally:
        ws_mod.LiveConnection = real_conn_cls
    assert ws.sent and ws.sent[0]["payload"]["code"] == "datafeed-loading"
    assert HUB is not None


def test_series_push_emits_bars_delta_and_close():
    from backend.api import live_push as push_mod

    registry = LiveRegistry()
    registry.subscribe("sub-s", "series", KEY_TEXT, 10)
    registry.attach("sub-s", None, _bars10())
    record = registry.get("sub-s")
    assert record is not None
    batch = [
        {"time": 1_700_000_000 + 10 * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        {"time": 1_700_000_000 + 11 * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
    ]
    now_s = 1_700_000_000 + 11 * 60 + 61
    frames = push_mod.emit_series_push("sub-s", registry, record, batch, now_s)
    assert frames[0]["type"] == "delta"
    assert frames[0]["payload"]["base_index"] == 10
    assert frames[0]["payload"]["count"] == 12
    assert frames[0]["payload"]["tail_len"] == 2
    assert frames[0]["payload"]["bars"] == batch
    assert frames[1]["type"] == "close"
    assert frames[1]["payload"]["time"] == 1_700_000_000 + 11 * 60
    assert record["count"] == 12
