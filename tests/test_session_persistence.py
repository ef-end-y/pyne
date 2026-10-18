# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live sessions survive a process restart via the SQLite session log.

Event-sourced persistence: the bar log mirrors ``InterpretSession._ohlcv_data``
(create → full bars, tick → replace last, append → extend), so a restart
rebuilds the session with a single ``create_session`` over identical bars and
ticking continues O(1) after one re-evaluation.

Restart simulation swaps the ``SESSIONS`` binding in ``backend.app`` for a
fresh ``SessionStore`` on the same db file — the in-memory dict is empty while
the log survives, exactly like a gunicorn worker recycle or container restart.
"""

from __future__ import annotations

import time

import pytest

import backend.app as app_module

from backend.api import datafeed as datafeed_mod
from backend.app import app
from backend.datastore import series as series_store
from backend.datastore import session_log
from backend.middleware import sessions as sessions_mod
from backend.middleware.auth import reset_key_store
from backend.middleware.sessions import SessionStore


SCRIPT = """//@version=6
indicator("persist probe")
plot(ta.sma(close, 5))
"""


def _bar(t: int, px: float = 100.0) -> dict:
    return {"time": t, "open": px, "high": px + 1.0, "low": px - 1.0, "close": px + 0.5, "volume": 10.0}


def _bars(n: int, t0: int = 1_700_000_000) -> list[dict]:
    return [_bar(t0 + i * 60, 100.0 + i * 0.1) for i in range(n)]


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Fresh session store on a temp db, wired into the Flask routes."""
    path = str(tmp_path / "sessions.db")
    store = SessionStore(db_path=path)
    monkeypatch.setattr(app_module, "SESSIONS", store)
    return path, store


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def _restart(monkeypatch, db_path: str) -> SessionStore:
    """Simulate a worker recycle: empty memory, same log file."""
    fresh = SessionStore(db_path=db_path)
    monkeypatch.setattr(app_module, "SESSIONS", fresh)
    return fresh


class TestRestartRoundTrip:
    def test_create_tick_append_survive_restart(self, client, db_path, monkeypatch):
        path, _ = db_path
        created = client.post("/session", json={"script": SCRIPT, "data": _bars(10)})
        assert created.status_code == 200, created.json
        sid = created.json["session_id"]

        tick_bar = _bar(1_700_000_000 + 10 * 60, 101.5)
        ticked = client.post(f"/session/{sid}/tick", json={"bar": tick_bar})
        assert ticked.status_code == 200, ticked.json
        before = ticked.json["plots"]

        fresh = _restart(monkeypatch, path)
        assert fresh.stats()["sessions"] == 0

        status = client.get(f"/session/{sid}")
        assert status.status_code == 200, status.json
        assert status.json["bars"] == 10

        # Same forming bar re-ticked after the restart: identical output.
        again = client.post(f"/session/{sid}/tick", json={"bar": tick_bar})
        assert again.status_code == 200, again.json
        assert again.json["plots"] == before

        appended = client.post(f"/session/{sid}/append", json={"bars": [_bar(1_700_000_000 + 11 * 60, 102.0)]})
        assert appended.status_code == 200, appended.json
        assert appended.json["appended"] == 1

    def test_unknown_id_still_404_after_restart(self, client, db_path, monkeypatch):
        path, _ = db_path
        _restart(monkeypatch, path)
        r = client.get("/session/does-not-exist")
        assert r.status_code == 404
        assert r.json["code"] == "NO_SESSION"

    def test_delete_removes_the_log_row(self, client, db_path):
        path, _ = db_path
        created = client.post("/session", json={"script": SCRIPT, "data": _bars(5)})
        sid = created.json["session_id"]
        assert session_log.load_record(path, sid) is not None

        gone = client.delete(f"/session/{sid}")
        assert gone.status_code == 200
        assert session_log.load_record(path, sid) is None
        assert client.get(f"/session/{sid}").status_code == 404

    def test_expired_row_is_not_resurrected(self, client, monkeypatch, tmp_path):
        path = str(tmp_path / "sessions.db")
        short = SessionStore(db_path=path, ttl_seconds=0.05)
        monkeypatch.setattr(app_module, "SESSIONS", short)
        created = client.post("/session", json={"script": SCRIPT, "data": _bars(5)})
        assert created.status_code == 200, created.json
        sid = created.json["session_id"]

        time.sleep(0.15)
        fresh = SessionStore(db_path=path, ttl_seconds=0.05)
        monkeypatch.setattr(app_module, "SESSIONS", fresh)
        r = client.get(f"/session/{sid}")
        assert r.status_code == 404
        assert session_log.load_record(path, sid) is None

    def test_capacity_still_enforced(self, client, monkeypatch, tmp_path):
        path = str(tmp_path / "sessions.db")
        tiny = SessionStore(db_path=path, max_sessions=2)
        monkeypatch.setattr(app_module, "SESSIONS", tiny)
        for _ in range(2):
            r = client.post("/session", json={"script": SCRIPT, "data": _bars(5)})
            assert r.status_code == 200, r.json
        full = client.post("/session", json={"script": SCRIPT, "data": _bars(5)})
        assert full.status_code == 503
        assert full.json["code"] == "SESSIONS_FULL"


class TestSessionLogPrimitives:
    def test_tick_replaces_last_append_extends(self, tmp_path):
        path = str(tmp_path / "sessions.db")
        bars = _bars(4)
        session_log.record_create(path, "abc", script=SCRIPT, symbol="T", bars=bars)
        session_log.record_tick(path, "abc", _bar(9_999_999, 555.0))
        rec = session_log.load_record(path, "abc")
        assert rec is not None
        assert len(rec["bars"]) == 4
        assert rec["bars"][-1]["close"] == 555.5

        session_log.record_append(path, "abc", [_bars(1, t0=9_999_999 + 60)])
        rec = session_log.load_record(path, "abc")
        assert len(rec["bars"]) == 5

    def test_unknown_record_is_none(self, tmp_path):
        assert session_log.load_record(str(tmp_path / "sessions.db"), "nope") is None

    def test_sessions_module_singleton_untouched(self):
        assert isinstance(sessions_mod.SESSIONS, SessionStore)


# ── data sessions ────────────────────────────────────────────────────────────
# Same mechanics as tests/test_data_series.py: CCXT is faked, `now_seconds` is
# pinned, every /data request carries a minted key.

DNOW = 1_700_000_040
DMIN = 60
DKEY = {"exchange": "binance", "symbol": "BTC/USDT", "interval": "1m"}
DSCRIPT = """//@version=6
indicator("data persist probe")
plot(ta.sma(close, 14))
"""


class DFakeExchange:
    def __init__(self) -> None:
        self.candles: list[list[float]] = []
        self.calls: list[dict] = []

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
        rows = [c for c in self.candles if since is None or c[0] >= since]
        return rows[:limit] if limit else rows


def _dcandle(t_s: int, px: float = 100.0) -> list[float]:
    return [t_s * 1000, px, px + 1.0, px - 1.0, px + 0.5, 10.0]


def _dbar(t_s: int, px: float = 100.0) -> dict:
    return {"time": t_s, "open": px, "high": px + 1.0, "low": px - 1.0, "close": px + 0.5, "volume": 10.0}


@pytest.fixture
def dclient(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(series_store, "now_seconds", lambda: DNOW + 10)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token-session-persist")
    monkeypatch.setenv("STORE_BACKEND", "json")
    monkeypatch.setenv("API_KEY_STORE", str(tmp_path / "api_keys.json"))
    reset_key_store()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        minted = c.post(
            "/auth/create_key",
            json={"tier": "hobby"},
            headers={"X-Admin-Token": "test-admin-token-session-persist"},
        )
        assert minted.status_code == 200, minted.json
        c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {minted.json['api_key']}"
        yield c
    reset_key_store()


@pytest.fixture
def dexchange(monkeypatch) -> DFakeExchange:
    fake = DFakeExchange()
    monkeypatch.setattr(datafeed_mod, "_make_provider", lambda _exchange, _credentials=None, _market="spot": fake)
    return fake


class TestDataSessionRestart:
    def test_bound_session_ticks_on_after_restart(self, dclient, dexchange, tmp_path, monkeypatch):
        path = str(tmp_path / "sessions.db")
        monkeypatch.setattr(app_module, "SESSIONS", SessionStore(db_path=path))

        bars = [_dbar(DNOW - DMIN * (58 - i), 100.0 + i * 0.01) for i in range(58)]
        assert dclient.post("/data/series/provide", json={**DKEY, "bars": bars}).status_code == 200
        created = dclient.post("/data/session", json={**DKEY, "history_bars": 50, "script": DSCRIPT})
        assert created.status_code == 200, created.json
        sid = created.json["session_id"]

        dexchange.candles = [_dcandle(DNOW - 2 * DMIN), _dcandle(DNOW - DMIN), _dcandle(DNOW)]
        first = dclient.post(f"/data/session/{sid}/tick", json={"exchange": "binance", "symbol": "BTC/USDT"})
        assert first.status_code == 200, first.json
        assert first.json["unchanged"] is False
        before = first.json["plots"]

        # Worker recycle: memory gone, journal + series store survive.
        monkeypatch.setattr(app_module, "SESSIONS", SessionStore(db_path=path))

        status = dclient.get(f"/session/{sid}")
        assert status.status_code == 200, status.json

        second = dclient.post(f"/data/session/{sid}/tick", json={"exchange": "binance", "symbol": "BTC/USDT"})
        assert second.status_code == 200, second.json
        assert second.json["plots"] == before

    def test_released_binding_does_not_resurrect(self, dclient, tmp_path, monkeypatch):
        path = str(tmp_path / "sessions.db")
        monkeypatch.setattr(app_module, "SESSIONS", SessionStore(db_path=path))
        bars = [_dbar(DNOW - DMIN * (58 - i)) for i in range(58)]
        dclient.post("/data/series/provide", json={**DKEY, "bars": bars})
        sid = dclient.post("/data/session", json={**DKEY, "history_bars": 50, "script": DSCRIPT}).json["session_id"]
        assert dclient.delete(f"/session/{sid}").status_code == 200

        monkeypatch.setattr(app_module, "SESSIONS", SessionStore(db_path=path))
        assert dclient.get(f"/session/{sid}").status_code == 404
