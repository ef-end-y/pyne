# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the per-exchange feed fan-out (F-11). All streams are fakes —
no network, no venue credentials.
"""

from __future__ import annotations

import asyncio
import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.api.feed_manager import ExchangeFeed
from backend.api.feed_manager import FeedManager
from backend.api.feed_manager import apply_feed_candles
from backend.api.feed_manager import normalize_candles


def _candles(*times):
    return [[t * 1000, 100.0, 101.0, 99.0, 100.5, 10.0] for t in times]


def test_normalize_candles_sorts_dedups_and_converts():
    rows = [*_candles(3, 1, 2, 2), ["junk"]]
    out = normalize_candles(rows)
    assert [b["time"] for b in out] == [1, 2, 3]
    assert out[0]["open"] == 100.0
    assert normalize_candles(None) == []


def test_split_key():
    assert FeedManager.split_key("binance|spot|BTC/USDT|1m") == ("binance", "spot", "BTC/USDT", "1m")
    assert FeedManager.split_key("binance|swap|BTC/USDT|1m") == ("binance", "swap", "BTC/USDT", "1m")
    assert FeedManager.split_key("junk") is None


def test_dispatch_fans_out_and_isolates_failures():
    feed = ExchangeFeed("binance")
    seen_a: list = []
    feed.add_listener("BTC/USDT", "1m", "s-a", seen_a.append)
    feed.add_listener("BTC/USDT", "1m", "s-b", lambda _b: (_ for _ in ()).throw(RuntimeError("boom")))
    batch = [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    assert feed.dispatch("BTC/USDT", "1m", batch) == 1
    assert seen_a == [batch]
    assert feed.remove_listener("BTC/USDT", "1m", "s-a") is True
    assert feed.dispatch("BTC/USDT", "1m", batch) == 0


def test_apply_feed_candles_advances_session_without_walk():
    from backend.datastore import live as live_mod
    from backend.datastore import series as series_module
    from src.pynescript.runtime.host import Runtime

    key = series_module.make_key("binance", "BTC/USDT", "1m", "spot")
    bars = [
        {"time": 1_700_000_000 + i * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
        for i in range(5)
    ]
    session = Runtime(symbol="BTCUSDT").create_session("indicator()", bars, mode="interpret")
    binding = live_mod.bind_session(session, key, bars[-1])
    tick = dict(bars[-1])
    tick["close"] = 1.5
    outcome = apply_feed_candles(session, binding, [tick], now_s=bars[-1]["time"] + 30)
    assert outcome.appended == 0
    assert outcome.unchanged is False


class _FakeStreamFeed:
    """Fake CCXTProDataFeed: fan-in capable, replays scripted batches."""

    for_symbols = True

    def __init__(self, batches):
        self._batches = batches
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def close(self):
        return None

    async def supports_for_symbols(self):
        return self.for_symbols

    async def watch_ohlcv_for_symbols(self, pairs):
        self.calls.append(("for_symbols", list(pairs)))
        for symbol, timeframe, rows in self._batches:
            yield (symbol, timeframe, rows)

    async def watch_ohlcv(self, symbol, timeframe, limit=None):
        self.calls.append(("watch", symbol, timeframe))
        for s, tf, rows in self._batches:
            if s == symbol and tf == timeframe:
                yield rows

    async def fetch_ohlcv(self, symbol, timeframe, limit=2):
        return []


def test_fan_in_used_when_supported():
    batches = [("BTC/USDT", "1m", _candles(10)), ("ETH/USDT", "1m", _candles(11))]
    feed = ExchangeFeed("binance", feed_factory=lambda _e: _FakeStreamFeed(batches))
    seen: dict = {}

    def on_btc(b):
        seen.setdefault("btc", []).append(b)
        if len(seen.get("btc", [])) + len(seen.get("eth", [])) >= 2:
            feed.stop()

    def on_eth(b):
        seen.setdefault("eth", []).append(b)
        if len(seen.get("btc", [])) + len(seen.get("eth", [])) >= 2:
            feed.stop()

    feed.add_listener("BTC/USDT", "1m", "s-1", on_btc)
    feed.add_listener("ETH/USDT", "1m", "s-2", on_eth)
    asyncio.run(asyncio.wait_for(feed._watch(), timeout=10))
    assert feed.fan_in is True
    assert [b["time"] for b in seen["btc"][0]] == [10]
    assert [b["time"] for b in seen["eth"][0]] == [11]


def test_per_series_fallback_without_fan_in():
    batches = [("BTC/USDT", "1m", _candles(12))]

    class _NoFanIn(_FakeStreamFeed):
        for_symbols = False

        async def supports_for_symbols(self):
            return False

    feed = ExchangeFeed("binance", feed_factory=lambda _e: _NoFanIn(batches))
    seen: list = []

    def on_batch(b):
        seen.append(b)
        feed.stop()

    feed.add_listener("BTC/USDT", "1m", "s-1", on_batch)
    asyncio.run(asyncio.wait_for(feed._watch(), timeout=10))
    assert feed.fan_in is False
    assert [b["time"] for b in seen[0]] == [12]


def test_fan_in_refreshes_when_membership_changes():
    batches = [("BTC/USDT", "1m", _candles(10)), ("ETH/USDT", "1m", _candles(11))]

    class _PairAware(_FakeStreamFeed):
        async def watch_ohlcv_for_symbols(self, pairs):
            self.calls.append([p[0] for p in pairs])
            wanted = {(s, tf) for s, tf in pairs}
            for symbol, timeframe, rows in self._batches:
                if (symbol, timeframe) in wanted:
                    yield (symbol, timeframe, rows)

    feed = ExchangeFeed("binance", feed_factory=lambda _e: _PairAware(batches))
    seen: dict = {}

    def on_btc(b):
        seen.setdefault("btc", []).append(b)

    def on_eth(b):
        seen.setdefault("eth", []).append(b)
        feed.stop()

    async def run() -> None:
        feed.add_listener("BTC/USDT", "1m", "s-1", on_btc)
        task = asyncio.create_task(feed._watch())
        for _ in range(100):
            if "btc" in seen:
                break
            await asyncio.sleep(0.01)
        feed.add_listener("ETH/USDT", "1m", "s-2", on_eth)
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    assert "btc" in seen
    assert "eth" in seen


def test_datafeed_error_falls_back_to_poll():
    from src.pynescript.util.datafeed import DataFeedError

    feed_holder: list = []

    class _Boom(_FakeStreamFeed):
        async def watch_ohlcv_for_symbols(self, pairs):
            raise DataFeedError("stream died")
            yield  # async generator so `async for` can start

        async def fetch_ohlcv(self, symbol, timeframe, limit=2):
            feed_holder[0].stop()
            return _candles(99)

    feed = ExchangeFeed("binance", feed_factory=lambda _e: _Boom([]), poll_interval_s=0.01)
    feed_holder.append(feed)
    seen: list = []
    feed.add_listener("BTC/USDT", "1m", "s-1", seen.append)
    asyncio.run(asyncio.wait_for(feed._watch(), timeout=5))
    assert seen
    assert [b["time"] for b in seen[0]] == [99]
