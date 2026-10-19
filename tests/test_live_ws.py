# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ``WS /ws/live`` transport + auth (F-10).

The socket loop needs a real WS client, so these tests drive the socket-free
protocol core (:class:`LiveConnection`) plus the handshake/key helpers with a
fresh registry per test — no network, no key store writes.
"""

from __future__ import annotations

import os
import sys
import threading


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.api.live_ws import (  # noqa: E402
    LiveConnection,
    LiveRegistry,
    canonical_series_key,
    clamp_history_bars,
    extract_handshake_key,
)


META = {"exchange": "Binance", "market": "spot", "symbol": "btc/usdt", "interval": "1m"}


def _conn(**kw):
    kw.setdefault("registry", LiveRegistry())
    kw.setdefault("authed", True)
    kw.setdefault(
        "ensurer",
        lambda kind, key, history, **_: {"outcome": "short", "have": 0, "need": 50},
    )
    return LiveConnection(**kw)


def test_canonical_key_normalizes_casing():
    assert canonical_series_key(META) == "binance|spot|BTC/USDT|1m"
    assert canonical_series_key({**META, "market": "linear"}) == "binance|swap|BTC/USDT|1m"
    assert canonical_series_key({"exchange": "x"}) is None
    assert canonical_series_key("nope") is None


def test_history_bars_clamped():
    assert clamp_history_bars(10_000) == 10_000
    assert clamp_history_bars(1) == 50
    assert clamp_history_bars(999_999) == 100_000
    assert clamp_history_bars("junk") == 5000


def test_handshake_key_prefers_bearer_then_query():
    assert extract_handshake_key({"Authorization": "Bearer abc"}, {}) == "abc"
    assert extract_handshake_key({"Authorization": "ApiKey xyz"}, {}) == "xyz"
    assert extract_handshake_key({}, {"api_key": "q"}) == "q"
    assert extract_handshake_key({}, {}) == ""


def test_unauthenticated_subscribe_rejected():
    conn = LiveConnection(registry=LiveRegistry(), authed=False)
    replies = conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})
    assert replies[0]["code"] == "UNAUTHORIZED"
    assert conn.handle_dict({"type": "ping"}) == [{"type": "pong"}]


def test_bad_auth_token_rejected():
    conn = LiveConnection(registry=LiveRegistry(), authed=False)
    replies = conn.handle_dict({"type": "auth", "payload": {"token": "definitely-not-a-key"}})
    assert replies[0]["code"] == "UNAUTHORIZED"
    assert conn.authed is False


def test_subscribe_registers_and_reports_loading():
    conn = _conn()
    replies = conn.handle_dict(
        {"type": "subscribe", "id": "sub-1", "payload": {"kind": "script", "meta": META, "script": "indicator() {}"}}
    )
    assert len(replies) == 1
    status = replies[0]
    assert status["type"] == "status"
    assert status["id"] == "sub-1"
    assert status["sessionId"].startswith("live_")
    assert status["payload"]["code"] == "datafeed-loading"
    assert status["payload"]["need"] == 50


def test_subscribe_validates_shape():
    conn = _conn()
    assert (
        conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "nope", "meta": META}})[0]["code"]
        == "BAD_SUBSCRIBE"
    )
    assert (
        conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series"}})[0]["code"] == "BAD_SUBSCRIBE"
    )
    assert (
        conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "script", "meta": META}})[0]["code"]
        == "BAD_SUBSCRIBE"
    )


def test_resubscribe_same_id_is_idempotent():
    conn = _conn()
    first = conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})
    second = conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})
    assert first[0]["sessionId"] == second[0]["sessionId"]


def test_resume_unknown_is_gone():
    conn = _conn()
    replies = conn.handle_dict({"type": "resume", "id": "ghost", "payload": {"count": 3, "last_time": 1}})
    assert replies[0]["code"] == "SESSION_GONE"


def test_repair_unknown_is_gone():
    conn = _conn()
    replies = conn.handle_dict({"type": "repair", "id": "ghost", "payload": {"from_time": 1}})
    assert replies[0]["code"] == "SESSION_GONE"


def test_repair_of_a_live_sub_resumes():
    bar = {"time": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    conn = _conn(
        ensurer=lambda *_a, **_k: {"outcome": "ready", "session_id": None, "count": 1, "newest": 1},
        store_window=lambda *_a, **_k: [bar],
        attacher=lambda *_a, **_k: None,
    )
    conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})
    replies = conn.handle_dict({"type": "repair", "id": "s", "payload": {"from_time": 1}})
    assert replies[0].get("code") != "REPAIR_UNAVAILABLE"
    assert "full" in {r.get("type") for r in replies}


def test_subscribe_emits_loading_before_ensure():
    order: list[object] = []

    def ensurer(*_a, **_k):
        order.append("ensure")
        return {"outcome": "short", "have": 0, "need": 50}

    def sender(frame):
        order.append(("send", (frame.get("payload") or {}).get("code") or frame.get("type")))

    conn = _conn(ensurer=ensurer, sender=sender)
    conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})
    assert order[0] == ("send", "datafeed-loading")
    assert "ensure" in order
    assert order.index(("send", "datafeed-loading")) < order.index("ensure")


def test_unsubscribe_releases():
    conn = _conn()
    conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})
    out = conn.handle_dict({"type": "unsubscribe", "id": "s"})
    assert out[0]["payload"]["code"] == "idle"
    assert (
        conn.handle_dict({"type": "resume", "id": "s", "payload": {"count": 1, "last_time": 1}})[0]["code"]
        == "SESSION_GONE"
    )


def test_unsubscribe_during_ensure_skips_attach():
    started = threading.Event()
    release = threading.Event()
    attached: list[int] = []
    bar = {"time": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}

    def ensurer(*_a, **_k):
        started.set()
        assert release.wait(2)
        return {"outcome": "ready", "session_id": None, "count": 1, "newest": 1}

    conn = LiveConnection(
        registry=LiveRegistry(),
        authed=True,
        ensurer=ensurer,
        store_window=lambda *_a, **_k: [bar],
        attacher=lambda *_a, **_k: attached.append(1),
    )

    def worker():
        conn.handle_dict({"type": "subscribe", "id": "s", "payload": {"kind": "series", "meta": META}})

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(2)
    idle = conn.handle_dict({"type": "unsubscribe", "id": "s"})
    assert idle[0]["payload"]["code"] == "idle"
    release.set()
    t.join(2)
    assert attached == []
    assert conn._registry.get("s") is None


def test_unknown_type_and_non_dict():
    conn = _conn()
    assert conn.handle_dict({"type": "bogus"})[0]["code"] == "UNKNOWN_TYPE"
    assert conn.handle_dict([1, 2])[0]["code"] == "BAD_FRAME"


def test_next_seq_is_monotonic_per_subscription():
    from backend.api.live_ws import LiveRegistry as LR

    reg = LR()
    assert [reg.next_seq("a"), reg.next_seq("a"), reg.next_seq("b")] == [1, 2, 1]


def test_frame_builders_carry_seq_and_watermark():
    from backend.api.live_ws import closed_upto as _upto
    from backend.api.live_ws import make_close, make_delta, make_full, make_status

    status = make_status("live_1", "datafeed-loading", req_id="s", have=0, need=5000)
    assert status["payload"]["code"] == "datafeed-loading"
    assert status["id"] == "s"

    full = make_full("live_1", 7, "binance|spot|BTC/USDT|1m", 100, 120, bar_times=[1, 2])
    assert full["seq"] == 7
    assert full["payload"]["closed_upto"] == 120
    assert full["payload"]["bar_times"] == [1, 2]

    delta = make_delta("live_1", 8, 99, 100, 1, 120, plots=[[9]], series={"a": [9]})
    assert delta["payload"]["tail_len"] == 1

    close = make_close("live_1", 9, 99, 120, {"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10})
    assert close["payload"]["ohlcv"]["close"] == 1.5

    try:
        make_delta("live_1", 9, 90, 100, 5, 120)
    except ValueError:
        pass
    else:
        raise AssertionError("bad tail_len must raise")

    assert _upto([{"time": 100}, {"time": 160}], 60, 220) == 160
    assert _upto([{"time": 200}], 60, 220) == 0


class _FakeSession:
    def __init__(self, bars):
        self._ohlcv_data = list(bars)

    def result(self):
        n = len(self._ohlcv_data)
        return {"plots": [[1.0] * n], "series": {"a": [1.0] * n}, "count": n}


def _bars10():
    return [
        {"time": 1_700_000_000 + i * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
        for i in range(10)
    ]


def _ready_conn():
    bars = _bars10()
    session = _FakeSession(bars)

    def ensurer(kind, key, history, **kw):
        return {"outcome": "ready", "session_id": "ses-1", "count": len(bars), "newest": bars[-1]["time"]}

    def resumer(session_id):
        return (
            {"outcome": "ready", "session": session, "count": len(bars)}
            if session_id == "ses-1"
            else {"outcome": "gone"}
        )

    return (
        LiveConnection(
            registry=LiveRegistry(),
            authed=True,
            ensurer=ensurer,
            resumer=resumer,
            store_window=lambda _key, _limit: list(bars),
        ),
        bars,
    )


def test_subscribe_ready_emits_ready_calculating_full_live():
    conn, bars = _ready_conn()
    replies = conn.handle_dict(
        {"type": "subscribe", "id": "sub-1", "payload": {"kind": "script", "meta": META, "script": "indicator() {}"}}
    )
    codes = [(r.get("type"), r.get("payload", {}).get("code")) for r in replies]
    assert codes == [
        ("status", "datafeed-ready"),
        ("status", "calculating"),
        ("full", None),
        ("status", "live"),
    ]
    full = replies[2]
    assert full["seq"] == 1
    assert full["payload"]["count"] == 10
    assert full["payload"]["bar_times"] == [b["time"] for b in bars]
    assert len(full["payload"]["series"]["a"]) == 10


def test_series_subscribe_carries_bars_not_plots():
    conn, bars = _ready_conn()
    replies = conn.handle_dict({"type": "subscribe", "id": "sub-s", "payload": {"kind": "series", "meta": META}})
    full = [r for r in replies if r.get("type") == "full"][0]
    assert full["payload"]["bars"] == bars
    assert "plots" not in full["payload"]


def test_resume_replies_snapshot_and_live():
    conn, _bars = _ready_conn()
    conn.handle_dict(
        {"type": "subscribe", "id": "sub-1", "payload": {"kind": "script", "meta": META, "script": "indicator() {}"}}
    )
    replies = conn.handle_dict({"type": "resume", "id": "sub-1", "payload": {"count": 10, "last_time": 1}})
    types = [r.get("type") for r in replies]
    assert types == ["full", "status"]
    assert replies[0]["seq"] == 2
    assert replies[1]["payload"]["code"] == "live"
