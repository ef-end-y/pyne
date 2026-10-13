# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Engine-owned OHLCV data plane: series store, ``/data/series*`` and ``/data/session*``.

CCXT is replaced by an in-memory fake provider, so no test touches the network.
``now_seconds`` is pinned so the forming-bar decision is deterministic.
"""

from __future__ import annotations

import pytest

from backend.api import data_series as data_api
from backend.api import datafeed as datafeed_mod
from backend.app import app
from backend.datastore import series as series_store
from backend.datastore.series import DataError
from backend.datastore.series import SeriesStore
from backend.datastore.series import make_key
from backend.middleware.sessions import SESSIONS


# Minute-aligned. The candle opening at NOW is still forming at NOW + 10.
NOW = 1_700_000_040
MIN = 60
KEY_BODY = {"exchange": "binance", "symbol": "BTC/USDT", "interval": "1m"}
KEY_TEXT = "binance|spot|BTC/USDT|1m"
SCRIPT = """//@version=6
indicator("data probe")
plot(ta.sma(close, 14))
"""


class FakeExchange:
    """Serves ``fetch_ohlcv`` pages from memory; records every call."""

    def __init__(self) -> None:
        self.candles: list[list[float]] = []  # CCXT rows: [ms, open, high, low, close, volume]
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
        if self.error is not None:
            raise self.error
        rows = [c for c in self.candles if since is None or c[0] >= since]
        return rows[:limit] if limit else rows


def candle(t_s: int, px: float = 100.0) -> list[float]:
    return [t_s * 1000, px, px + 1.0, px - 1.0, px + 0.5, 10.0]


def bar(t_s: int, px: float = 100.0) -> dict:
    return {"time": t_s, "open": px, "high": px + 1.0, "low": px - 1.0, "close": px + 0.5, "volume": 10.0}


def closed_bars(count: int, last: int = NOW - MIN, px: float = 100.0) -> list[dict]:
    """``count`` closed 1m bars ending at ``last`` (strictly before the forming bar)."""
    return [bar(last - MIN * (count - 1 - i), px + i * 0.01) for i in range(count)]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(series_store, "now_seconds", lambda: NOW + 10)
    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.fixture
def exchange(monkeypatch) -> FakeExchange:
    fake = FakeExchange()
    monkeypatch.setattr(datafeed_mod, "_make_provider", lambda _exchange, _credentials=None: fake)
    return fake


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


# ── canonical key ────────────────────────────────────────────────────────────


class TestCanonicalKey:
    def test_exchange_lowercase_symbol_uppercase(self):
        assert make_key(" BINANCE ", "btc/usdt", "1h").text == "binance|spot|BTC/USDT|1h"

    def test_market_defaults_to_spot_and_is_normalized(self):
        assert make_key("binance", "BTC/USDT", "1h").market == "spot"
        assert make_key("binance", "BTC/USDT:USDT", "1h", "SWAP").text == "binance|swap|BTC/USDT:USDT|1h"

    def test_interval_unit_case_is_canonical_and_month_stays_distinct(self):
        assert make_key("binance", "BTC/USDT", "1H").interval == "1h"
        assert make_key("binance", "BTC/USDT", "1M").interval_seconds == 30 * 86_400
        assert make_key("binance", "BTC/USDT", "1m").interval_seconds == MIN

    def test_rejects_unknown_market(self):
        with pytest.raises(DataError) as info:
            make_key("binance", "BTC/USDT", "1h", "options")
        assert info.value.code == "INVALID_MARKET"

    def test_rejects_unknown_interval(self):
        with pytest.raises(DataError) as info:
            make_key("binance", "BTC/USDT", "7x")
        assert info.value.code == "INVALID_INTERVAL"

    def test_rejects_key_separator_in_symbol(self):
        with pytest.raises(DataError) as info:
            make_key("binance", "BTC|USDT", "1h")
        assert info.value.code == "INVALID_KEY"


# ── store ────────────────────────────────────────────────────────────────────


KEY = make_key("binance", "BTC/USDT", "1h")
H = 3_600
T = NOW - NOW % H  # hour-aligned


@pytest.fixture
def store(tmp_path) -> SeriesStore:
    return SeriesStore(tmp_path / "ohlcv.sqlite3")


class TestSeriesStore:
    def test_unknown_series_reports_zero(self, store):
        assert store.status(KEY) == {
            "key": "binance|spot|BTC/USDT|1h",
            "count": 0,
            "oldest": None,
            "newest": None,
            "updated_at": None,
        }

    def test_provide_upserts_by_time_and_reports_counts(self, store):
        first = store.provide(KEY, [bar(T), bar(T + H), bar(T + 2 * H)], source="axis-local")
        assert (first["added"], first["updated"], first["count"], first["newest"]) == (3, 0, 3, T + 2 * H)
        second = store.provide(KEY, [bar(T + 2 * H, 200.0), bar(T + 3 * H)], source="axis-local")
        assert (second["added"], second["updated"], second["count"], second["newest"]) == (1, 1, 4, T + 3 * H)
        assert store.status(KEY)["oldest"] == T

    def test_later_write_wins_for_the_same_time(self, store):
        store.provide(KEY, [bar(T, 100.0)])
        store.provide(KEY, [bar(T, 200.0)])
        assert store.window(KEY, 1)[0]["close"] == 200.5

    def test_window_is_newest_first_limited_and_ascending(self, store):
        store.provide(KEY, [bar(T + H * i) for i in reversed(range(10))])
        assert [b["time"] for b in store.window(KEY, 3)] == [T + 7 * H, T + 8 * H, T + 9 * H]

    def test_duplicate_times_in_one_request_keep_the_last(self, store):
        result = store.provide(KEY, [bar(T, 100.0), bar(T, 300.0)])
        assert result["count"] == 1
        assert store.window(KEY, 1)[0]["close"] == 300.5

    def test_series_are_isolated_by_key(self, store):
        store.provide(KEY, [bar(T)])
        assert store.status(make_key("bybit", "BTC/USDT", "1h"))["count"] == 0

    @pytest.mark.parametrize(
        "bad",
        [
            {"time": T * 1000},  # milliseconds, not unix seconds
            {"time": T + 0.5},  # not an integer
            {"close": float("nan")},  # non-finite
            {"open": True},  # bool is not a number
            {"volume": None},  # required field
        ],
    )
    def test_rejects_invalid_bars(self, store, bad):
        row = {**bar(T), **bad}
        with pytest.raises(DataError) as info:
            store.provide(KEY, [row])
        assert info.value.code == "INVALID_BARS"
        assert store.status(KEY)["count"] == 0


# ── /data/series routes ──────────────────────────────────────────────────────


class TestSeriesRoutes:
    def test_status_of_unknown_series_is_zero(self, client):
        body = client.get("/data/series?exchange=Binance&symbol=btc/usdt&interval=1h").get_json()
        assert body["status"] == "success"
        assert body["key"] == "binance|spot|BTC/USDT|1h"
        assert body["count"] == 0
        assert body["oldest"] is None
        assert body["updated_at"] is None

    def test_provide_then_status_reports_bounds(self, client):
        res = client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(3), "source": "axis-local"})
        assert res.status_code == 200
        body = res.get_json()
        assert set(body) == {"status", "key", "added", "updated", "count", "newest"}
        assert (body["added"], body["updated"], body["count"]) == (3, 0, 3)
        status = client.get("/data/series?exchange=binance&symbol=BTC/USDT&interval=1m").get_json()
        assert status["count"] == 3
        assert status["newest"] == NOW - MIN

    def test_provide_over_5000_bars_is_413(self, client):
        res = client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(5001)})
        assert res.status_code == 413
        assert res.get_json()["code"] == "BATCH_TOO_LARGE"
        assert client.get("/data/series?exchange=binance&symbol=BTC/USDT&interval=1m").get_json()["count"] == 0

    def test_provide_rejects_unknown_fields(self, client):
        res = client.post("/data/series/provide", json={**KEY_BODY, "bars": [], "extra": 1})
        assert res.status_code == 400
        assert res.get_json()["code"] == "UNKNOWN_FIELDS"

    def test_provide_rejects_invalid_bars_with_400(self, client):
        res = client.post("/data/series/provide", json={**KEY_BODY, "bars": [{"time": (NOW - MIN) * 1000}]})
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_BARS"

    def test_bars_route_returns_newest_window_ascending(self, client):
        client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(20)})
        body = client.get("/data/series/bars?exchange=binance&symbol=BTC/USDT&interval=1m&limit=5").get_json()
        assert body["count"] == 5
        assert [b["time"] for b in body["bars"]] == [NOW - MIN * k for k in (5, 4, 3, 2, 1)]

    @pytest.mark.parametrize("limit", ["0", "100001", "abc"])
    def test_bars_limit_is_bounded(self, client, limit):
        res = client.get(f"/data/series/bars?exchange=binance&symbol=BTC/USDT&interval=1m&limit={limit}")
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_LIMIT"

    def test_unknown_market_is_400(self, client):
        res = client.get("/data/series?exchange=binance&symbol=BTC/USDT&interval=1h&market=options")
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_MARKET"


# ── /data/series/fetch ───────────────────────────────────────────────────────


class TestFetch:
    def test_fetch_pages_stop_at_history_bars_and_persist(self, client, exchange, monkeypatch):
        monkeypatch.setattr(data_api, "FETCH_PAGE_LIMIT", 50)
        exchange.candles = [candle(t) for t in range(NOW - 400 * MIN, NOW + 1, MIN)]
        res = client.post("/data/series/fetch", json={**KEY_BODY, "history_bars": 120})
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["key"] == KEY_TEXT
        assert (body["fetched"], body["count"]) == (120, 120)
        assert body["newest"] == NOW  # the forming candle is persisted with the rest
        assert exchange.calls[0]["since"] == (NOW + 10 - 120 * MIN) * 1000
        assert 1 < len(exchange.calls) <= 4
        assert client.get("/data/series?exchange=binance&symbol=BTC/USDT&interval=1m").get_json()["count"] == 120

    def test_fetch_upstream_failure_is_502_and_persists_nothing(self, client, exchange):
        exchange.error = RuntimeError("exchange unavailable")
        res = client.post("/data/series/fetch", json={**KEY_BODY, "history_bars": 50})
        assert res.status_code == 502
        assert res.get_json()["code"] == "UPSTREAM"
        assert client.get("/data/series?exchange=binance&symbol=BTC/USDT&interval=1m").get_json()["count"] == 0

    def test_fetch_without_ccxt_is_503(self, client, monkeypatch):
        monkeypatch.setattr(datafeed_mod, "_make_provider", lambda _exchange, _credentials=None: None)
        res = client.post("/data/series/fetch", json={**KEY_BODY, "history_bars": 50})
        assert res.status_code == 503
        assert res.get_json()["code"] == "CCXT_UNAVAILABLE"

    @pytest.mark.parametrize("history", [0, 100_001])
    def test_history_bars_is_bounded(self, client, exchange, history):
        res = client.post("/data/series/fetch", json={**KEY_BODY, "history_bars": history})
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_HISTORY_BARS"

    def test_collect_candles_stops_once_max_bars_are_held(self, exchange, monkeypatch):
        monkeypatch.setattr(data_api, "FETCH_PAGE_LIMIT", 50)
        exchange.candles = [candle(t) for t in range(NOW - 499 * MIN, NOW - MIN + 1, MIN)]
        key = make_key("binance", "BTC/USDT", "1m")
        rows = data_api.collect_candles(
            exchange, key, since_ms=(NOW - 499 * MIN) * 1000, now_ms=(NOW + 10) * 1000, max_bars=120
        )
        assert len(rows) == 150
        assert len(exchange.calls) == 3


# ── /data/session ────────────────────────────────────────────────────────────


def _create(client, history: int = 50, **extra):
    return client.post("/data/session", json={**KEY_BODY, "history_bars": history, "script": SCRIPT, **extra})


class TestDataSessionCreate:
    def test_empty_store_is_a_miss_with_have_and_need(self, client):
        res = _create(client, 50)
        assert res.status_code == 409
        assert res.get_json() == {
            "status": "miss",
            "code": "DATA_MISS",
            "key": KEY_TEXT,
            "have": 0,
            "need": 50,
        }

    def test_partial_store_is_still_a_miss(self, client):
        client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(30)})
        res = _create(client, 50)
        assert res.status_code == 409
        assert (res.get_json()["have"], res.get_json()["need"]) == (30, 50)

    def test_hit_binds_a_session_over_the_stored_window(self, client):
        client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(60)})
        res = _create(client, 50)
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["session_id"]
        assert body["cache"] == "hit"
        assert body["data"] == {"key": KEY_TEXT, "count": 60, "newest": NOW - MIN}
        assert "series" in body
        assert client.get(f"/session/{body['session_id']}").get_json()["bars"] == 50

    def test_script_is_required(self, client):
        client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(60)})
        res = client.post("/data/session", json={**KEY_BODY, "history_bars": 50})
        assert res.status_code == 400
        assert res.get_json()["code"] == "MISSING_FIELD"

    def test_compile_mode_is_refused(self, client):
        client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(60)})
        res = _create(client, 50, mode="compile")
        assert res.status_code == 400
        assert res.get_json()["code"] == "MODE_UNSUPPORTED"

    def test_unknown_interval_is_400(self, client):
        res = client.post(
            "/data/session",
            json={**KEY_BODY, "interval": "7x", "history_bars": 50, "script": SCRIPT},
        )
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_INTERVAL"

    def test_history_bars_over_the_cap_is_400(self, client):
        res = _create(client, 100_001)
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_HISTORY_BARS"


# ── /data/session/<id>/tick ──────────────────────────────────────────────────


@pytest.fixture
def bound(client, exchange) -> str:
    """A session bound to a stored series whose newest closed bar is NOW-180."""
    client.post("/data/series/provide", json={**KEY_BODY, "bars": closed_bars(58, last=NOW - 3 * MIN)})
    body = _create(client, 50).get_json()
    return body["session_id"]


def _tick(client, session_id: str, **body):
    return client.post(f"/data/session/{session_id}/tick", json={**body} if body else {})


class TestDataSessionTick:
    def test_mismatched_symbol_is_409(self, client, bound):
        res = _tick(client, bound, exchange="binance", symbol="ETH/USDT")
        assert res.status_code == 409
        assert res.get_json()["code"] == "SESSION_KEY_MISMATCH"

    def test_mismatched_exchange_is_409(self, client, bound):
        res = _tick(client, bound, exchange="bybit", symbol="BTC/USDT")
        assert res.status_code == 409
        assert res.get_json()["code"] == "SESSION_KEY_MISMATCH"

    def test_bar_payload_is_rejected_because_identity_only(self, client, bound):
        res = _tick(client, bound, exchange="binance", symbol="BTC/USDT", bar=bar(NOW))
        assert res.status_code == 400
        assert res.get_json()["code"] == "UNKNOWN_FIELDS"

    def test_unknown_session_is_404(self, client):
        res = _tick(client, "deadbeef", exchange="binance", symbol="BTC/USDT")
        assert res.status_code == 404
        assert res.get_json()["code"] == "NO_SESSION"

    def test_tick_appends_closed_bars_and_forms_the_last(self, client, exchange, bound):
        exchange.candles = [candle(NOW - 2 * MIN), candle(NOW - MIN), candle(NOW)]
        res = _tick(client, bound, exchange="binance", symbol="BTC/USDT")
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["session_id"] == bound
        assert (body["appended"], body["forming"], body["unchanged"]) == (2, True, False)
        # Only closed bars are persisted; the forming bar stays in the session.
        status = client.get("/data/series?exchange=binance&symbol=BTC/USDT&interval=1m").get_json()
        assert (status["count"], status["newest"]) == (60, NOW - MIN)
        # Overlap: two bars before the stored newest (NOW-180).
        assert exchange.calls[-1]["since"] == (NOW - 3 * MIN - 2 * MIN) * 1000

    def test_tick_with_no_new_data_is_unchanged(self, client, exchange, bound):
        exchange.candles = [candle(NOW - 2 * MIN), candle(NOW - MIN), candle(NOW)]
        _tick(client, bound, exchange="binance", symbol="BTC/USDT")
        body = _tick(client, bound, exchange="binance", symbol="BTC/USDT").get_json()
        assert (body["appended"], body["forming"], body["unchanged"]) == (0, True, True)

    def test_changed_forming_bar_is_retickd_in_place(self, client, exchange, bound):
        exchange.candles = [candle(NOW - MIN), candle(NOW)]
        _tick(client, bound, exchange="binance", symbol="BTC/USDT")
        exchange.candles[-1] = candle(NOW, px=105.0)
        body = _tick(client, bound, exchange="binance", symbol="BTC/USDT").get_json()
        assert (body["appended"], body["forming"], body["unchanged"]) == (0, True, False)
        assert body["status"] == "success"

    def test_upstream_failure_is_502_and_the_session_is_kept(self, client, exchange, bound):
        exchange.error = RuntimeError("exchange unavailable")
        res = _tick(client, bound, exchange="binance", symbol="BTC/USDT")
        assert res.status_code == 502
        assert res.get_json()["code"] == "UPSTREAM"
        assert client.get(f"/session/{bound}").status_code == 200

    def test_delete_releases_the_bound_session(self, client, bound):
        assert client.delete(f"/session/{bound}").status_code == 200
        res = _tick(client, bound, exchange="binance", symbol="BTC/USDT")
        assert res.status_code == 404


# ── discovery ────────────────────────────────────────────────────────────────


def test_health_advertises_the_data_plane_features(client):
    body = client.get("/health").get_json()
    assert body["features"]["live_modes"] == ["every-tick", "bar-close"]
    assert body["features"]["data_session"] is True
    assert "GET /data/series" in body["endpoints"]
    assert "POST /data/session/<session_id>/tick" in body["endpoints"]
