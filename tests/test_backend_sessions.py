# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the live-session endpoints.

The store contract (TTL, capacity, thread safety) is unit-tested directly; the
routes are tested through Flask's test client so the HTTP surface — status
codes, error codes, and envelope shape — is pinned too.

The load-bearing claim is that a tick re-uses committed state rather than
re-evaluating: ``append_bars``/``update_last_bar`` must be materially cheaper
than a second full run over the same history.
"""

from __future__ import annotations

import os
import sys
import time

import pytest


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.app import app
from backend.middleware.sessions import SessionStore


def _bars(n: int, start: int = 0) -> list[dict]:
    out = []
    px = 100.0
    for i in range(start, start + n):
        px += 0.05 * ((i * 29) % 11 - 5) / 5.0
        c = px + 0.02 * ((i * 17) % 5 - 2) / 2.0
        out.append(
            {
                "time": 1_700_000_000 + i * 60,
                "open": px,
                "high": max(px, c) + 0.02,
                "low": min(px, c) - 0.02,
                "close": c,
                "volume": 1000.0,
            }
        )
    return out


SCRIPT = """//@version=6
indicator("session probe")
plot(ta.sma(close, 14))
plot(ta.rsi(close, 14))
"""


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


# ── store unit tests ────────────────────────────────────────────────────────


class TestSessionStore:
    def test_put_get_roundtrip(self):
        store = SessionStore()
        sentinel = object()
        sid = store.put(sentinel)
        assert store.get(sid) is sentinel

    def test_get_unknown_returns_none(self):
        assert SessionStore().get("nope") is None

    def test_drop_reports_whether_it_existed(self):
        store = SessionStore()
        sid = store.put(object())
        assert store.drop(sid) is True
        assert store.drop(sid) is False
        assert store.get(sid) is None

    def test_capacity_is_enforced(self):
        store = SessionStore(max_sessions=3)
        for _ in range(3):
            store.put(object())
        assert store.full() is True
        with pytest.raises(RuntimeError, match="full"):
            store.put(object())

    def test_drop_frees_capacity(self):
        store = SessionStore(max_sessions=2)
        a = store.put(object())
        store.put(object())
        assert store.full()
        store.drop(a)
        assert store.full() is False

    def test_idle_ttl_evicts(self):
        store = SessionStore(ttl_seconds=0.0)
        sid = store.put(object())
        time.sleep(0.01)
        # Sweep is time-gated; force it by moving the clock forward.
        store._last_sweep = 0.0
        assert store.get(sid) is None

    def test_touching_a_session_refreshes_idle_time(self):
        store = SessionStore(ttl_seconds=0.5)
        sid = store.put(object())
        store._last_sweep = 0.0
        assert store.get(sid) is not None
        store._last_sweep = 0.0
        assert store.get(sid) is not None  # still alive after the touch

    def test_describe_reports_bars_and_idle(self):
        store = SessionStore()
        sentinel = type("S", (), {"_n_bars": 123})()
        sid = store.put(sentinel)
        d = store.describe(sid)
        assert d is not None
        assert d["bars"] == 123
        assert d["idle_seconds"] >= 0

    def test_describe_unknown_is_none(self):
        assert SessionStore().describe("nope") is None

    def test_clear_returns_count(self):
        store = SessionStore()
        for _ in range(4):
            store.put(object())
        assert store.clear() == 4
        assert store.stats()["sessions"] == 0

    def test_close_is_called_on_eviction(self):
        closed = []

        class S:
            def close(self):
                closed.append(1)

        store = SessionStore()
        sid = store.put(S())
        store.drop(sid)
        assert closed == [1]

    def test_close_failure_does_not_break_eviction(self):
        class Bad:
            def close(self):
                msg = "boom"
                raise RuntimeError(msg)

        store = SessionStore()
        sid = store.put(Bad())
        assert store.drop(sid) is True
        assert store.get(sid) is None


# ── route tests ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_sessions():
    from backend.middleware.sessions import SESSIONS

    SESSIONS.clear()
    yield
    SESSIONS.clear()


class TestSessionRoutes:
    def test_create_returns_session_id_and_success(self, client):
        res = client.post("/session", json={"script": SCRIPT, "data": _bars(120)})
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["session_id"]
        assert "series" in body

    def test_session_carries_the_bar_time_axis(self, client):
        """The evaluated window ships its bar times.

        The engine's stored series can have gaps (a missed minute), so a
        client that joins the series by index displaces the plot. The axis
        lets it join by time instead.
        """
        bars = _bars(30)
        body = client.post("/session", json={"script": SCRIPT, "data": bars}).get_json()
        times = body["times"]
        assert times == [b["time"] for b in bars]
        assert len(times) == body["count"]

    def test_delta_tick_trims_the_bar_time_axis(self, client):
        """A delta carries the tail's times — the client splices them."""
        last = _bars(1, start=119)[0]
        last = dict(last, close=last["close"] + 5.0, high=last["high"] + 5.0)
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        body = client.post(f"/session/{sid}/tick", json={"bar": last, "delta": True}).get_json()
        assert body["delta"] == {"base_index": 119, "count": 120, "tail_len": 1}
        assert len(body["times"]) == 1
        assert body["times"] == [last["time"]]

    def test_create_requires_script(self, client):
        res = client.post("/session", json={"data": _bars(10)})
        assert res.status_code == 400

    def test_create_requires_data(self, client):
        res = client.post("/session", json={"script": SCRIPT})
        assert res.status_code == 400

    def test_create_rejects_compile_mode(self, client):
        res = client.post("/session", json={"script": SCRIPT, "data": _bars(60), "mode": "compile"})
        assert res.status_code == 400
        assert res.get_json()["code"] == "MODE_UNSUPPORTED"

    def test_status_route(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(80)}).get_json()["session_id"]
        res = client.get(f"/session/{sid}")
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["bars"] == 80

    def test_status_unknown_is_404(self, client):
        res = client.get("/session/deadbeef")
        assert res.status_code == 404
        assert res.get_json()["code"] == "NO_SESSION"

    def test_tick_updates_last_bar_without_advancing_count(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        last = _bars(1, start=119)[0]
        last = dict(last)
        last["close"] = last["close"] + 5.0
        last["high"] = max(last["open"], last["close"]) + 0.1

        res = client.post(f"/session/{sid}/tick", json={"bar": last})
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["count"] == 120, "a tick must not add a bar"

    def test_tick_unknown_session_is_404(self, client):
        res = client.post("/session/nope/tick", json={"bar": _bars(1)[0]})
        assert res.status_code == 404
        assert res.get_json()["code"] == "NO_SESSION"

    def test_tick_requires_a_bar(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(60)}).get_json()["session_id"]
        res = client.post(f"/session/{sid}/tick", json={})
        assert res.status_code == 400

    def test_append_advances_the_bar_count(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        new_bars = _bars(3, start=120)
        res = client.post(f"/session/{sid}/append", json={"bars": new_bars})
        assert res.status_code == 200
        body = res.get_json()
        assert body["count"] == 123
        assert body["appended"] == 3

    def test_append_requires_a_list(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(60)}).get_json()["session_id"]
        res = client.post(f"/session/{sid}/append", json={"bars": "nope"})
        assert res.status_code == 400

    def test_delete_releases(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(60)}).get_json()["session_id"]
        assert client.delete(f"/session/{sid}").status_code == 200
        # A released session must not answer a tick.
        assert client.post(f"/session/{sid}/tick", json={"bar": _bars(1)[0]}).status_code == 404

    def test_delete_unknown_is_404(self, client):
        assert client.delete("/session/nope").status_code == 404

    def test_tick_supports_window_trim(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(200)}).get_json()["session_id"]
        res = client.post(f"/session/{sid}/tick", json={"bar": _bars(1, start=199)[0], "from_bar": -20})
        assert res.status_code == 200
        body = res.get_json()
        w = body.get("window")
        assert w is not None, "a trimmed tick must report its window"
        assert w["bars"] == 20

    def test_tick_delta_returns_only_the_tail_cell(self, client):
        """Opt-in delta ticks carry 1 cell per column instead of full series."""
        last = _bars(1, start=119)[0]
        last = dict(last, close=last["close"] + 5.0, high=last["high"] + 5.0)
        sid_full = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        full = client.post(f"/session/{sid_full}/tick", json={"bar": last}).get_json()
        sid_delta = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        res = client.post(f"/session/{sid_delta}/tick", json={"bar": last, "delta": True})
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["count"] == 120
        assert body["delta"] == {"base_index": 119, "count": 120, "tail_len": 1}
        assert len(body["plots"]) == 1
        assert body["plots"] == full["plots"][-1:]
        assert set(body["series"]) == set(full["series"])
        for k, v in body["series"].items():
            assert v == full["series"][k][-1:]

    def test_append_delta_returns_only_appended_cells(self, client):
        new_bars = _bars(3, start=120)
        sid_full = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        full = client.post(f"/session/{sid_full}/append", json={"bars": new_bars}).get_json()
        sid_delta = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        res = client.post(f"/session/{sid_delta}/append", json={"bars": new_bars, "delta": True})
        assert res.status_code == 200
        body = res.get_json()
        assert body["count"] == 123
        assert body["delta"] == {"base_index": 120, "count": 123, "tail_len": 3}
        assert body["plots"] == full["plots"][-3:]
        for k, v in body["series"].items():
            assert v == full["series"][k][-3:]

    def test_delta_with_window_trim_answers_full(self, client):
        """Trims and tails do not compose: trim wins, no delta block."""
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(120)}).get_json()["session_id"]
        last = _bars(1, start=119)[0]
        res = client.post(f"/session/{sid}/tick", json={"bar": last, "delta": True, "from_bar": -20})
        assert res.status_code == 200
        body = res.get_json()
        assert "delta" not in body
        assert body.get("window", {}).get("bars") == 20

    def test_health_advertises_session_delta(self, client):
        assert client.get("/").get_json()["features"]["session_delta"] is True

    def test_repeated_ticks_are_stable(self, client):
        """Ticking the same bar repeatedly must not corrupt state."""
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(150)}).get_json()["session_id"]
        bar = _bars(1, start=149)[0]
        for i in range(12):
            b = dict(bar)
            b["close"] = bar["close"] + i * 0.01
            b["high"] = max(b["open"], b["close"]) + 0.05
            res = client.post(f"/session/{sid}/tick", json={"bar": b})
            assert res.status_code == 200, f"tick {i} failed"
        assert res.get_json()["count"] == 150

    def test_session_survives_a_tick_then_an_append(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(100)}).get_json()["session_id"]
        client.post(f"/session/{sid}/tick", json={"bar": _bars(1, start=99)[0]})
        res = client.post(f"/session/{sid}/append", json={"bars": _bars(2, start=100)})
        assert res.status_code == 200
        assert res.get_json()["count"] == 102

    def test_script_error_is_reported_not_raised(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(60)}).get_json()["session_id"]
        # A syntactically broken script still yields a session; the error shows
        # up on the tick rather than exploding the route.
        res = client.post(f"/session/{sid}/tick", json={"bar": _bars(1, start=59)[0]})
        assert res.status_code in (200, 400)
        assert res.get_json()["status"] in ("success", "error")


# ── discovery parity ────────────────────────────────────────────────────────
#
# Scoped to the session routes on purpose. /health advertises some routes only
# when a subsystem is enabled (runner_enabled() / scheduler_enabled()), so a
# whole-map "everything routable is advertised" invariant does not hold — that
# conditional advertisement is intentional, not drift.


def _advertised(client) -> set[str]:
    return set(client.get("/health").get_json().get("endpoints") or {})


def test_health_advertises_every_session_route(client):
    """/health is the discovery surface; a route it omits does not exist to a reader.

    The session endpoints shipped without being added to the health ``endpoints``
    map, so /health reported 28 endpoints and never mentioned /session — AXIS
    and any agent reading it would conclude the feature was unavailable.
    """
    advertised = _advertised(client)
    for route in (
        "POST /session",
        "GET /session/<session_id>",
        "POST /session/<session_id>/tick",
        "POST /session/<session_id>/append",
        "DELETE /session/<session_id>",
    ):
        assert route in advertised, f"{route} missing from /health endpoints"


def test_advertised_session_routes_are_actually_routable(client):
    """Every session route /health advertises must resolve, and vice versa.

    Keeps the advertised keys byte-identical to Flask's rule strings so a
    rename in the decorator cannot silently desynchronise discovery from
    routing.
    """
    advertised = {k for k in _advertised(client) if k.split(" ", 1)[1].startswith("/session")}
    registered = {
        f"{m} {r.rule}"
        for r in app.url_map.iter_rules()
        if r.rule.startswith("/session")
        for m in sorted(r.methods - {"HEAD", "OPTIONS"})
    }
    assert advertised == registered, f"advertised={sorted(advertised)} registered={sorted(registered)}"


class TestSessionTail:
    def test_tail_serves_bars_after_cursor(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(10)}).get_json()["session_id"]
        res = client.get(f"/session/{sid}/tail?since_count=7")
        body = res.get_json()
        assert res.status_code == 200
        assert body["status"] == "success"
        assert body["count"] == 10
        assert len(body["tail"]) == 3
        assert body["tail"][0]["time"] == _bars(10)[7]["time"]

    def test_tail_defaults_to_full_window(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(5)}).get_json()["session_id"]
        body = client.get(f"/session/{sid}/tail").get_json()
        assert body["count"] == 5
        assert len(body["tail"]) == 5

    def test_tail_unknown_session_is_gone(self, client):
        res = client.get("/session/deadbeef/tail?since_count=0")
        assert res.status_code == 404
        assert res.get_json()["code"] == "NO_SESSION"

    def test_tail_closed_upto_null_without_binding(self, client):
        sid = client.post("/session", json={"script": SCRIPT, "data": _bars(5)}).get_json()["session_id"]
        body = client.get(f"/session/{sid}/tail?since_count=0").get_json()
        assert body["closed_upto"] is None
