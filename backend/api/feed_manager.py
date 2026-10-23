# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-exchange live feeds fanning out to bound sessions (F-11).

Problem: every data-session tick pays its own bounded CCXT walk
(``sync_data_session``), so N sessions on one series pay N walks that
serialize behind ``key_lock``. This module holds **one live feed per exchange**
and fans each candle batch out to every session bound to that series, which
commits it with the walk-free :func:`apply_feed_candles` (O(1)
``update_last_bar`` / O(delta) ``append_bars`` via ``live.sync_session``).

Transport mirrors the datafeed gateway: ``watchOHLCVForSymbols`` fan-in where
the venue supports it, one ``watch_ohlcv`` task per series otherwise, REST
``fetch_ohlcv`` poll fallback when streaming is unavailable.
``enableRateLimit`` is always on so the engine never earns a venue ban. Each
feed group runs on its own daemon thread with a private asyncio loop (same
pattern as ``/datafeed/watch``) because the Flask worker is synchronous.

Feeds are injectable (``feed_factory``) so tests run on fake streams with no
network.
"""

from __future__ import annotations

import asyncio
import threading

from collections.abc import Callable
from typing import Any


POLL_INTERVAL_S = 5.0


def normalize_candles(rows: Any) -> list[dict[str, Any]]:
    """CCXT ``[ms,o,h,l,c,v]`` rows → ascending unique ``{time(s),…}`` dicts."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    items = rows if isinstance(rows, list) else []
    for row in items:
        try:
            ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            t = int(int(ts) // 1000)
        except (TypeError, ValueError, IndexError):
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
    out.sort(key=lambda b: b["time"])
    return out


def apply_feed_candles(
    session: Any,
    binding: Any,
    candles: list[dict[str, Any]],
    now_s: int,
    *,
    on_apply: Callable[[str, Any], None] | None = None,
) -> Any:
    """Commit feed candles to a session — no upstream walk (F-11 fast path)."""
    from backend.datastore import live as live_mod

    return live_mod.sync_session(session, binding, candles, now_s, on_apply=on_apply)


class ExchangeFeed:
    """One live feed for one exchange, fanning out to N bound series."""

    def __init__(
        self,
        exchange: str,
        *,
        market: str = "spot",
        feed_factory: Callable[[str], Any] | None = None,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self.exchange = exchange.lower()
        self.market = (market or "spot").lower()
        self._feed_factory = feed_factory
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        # (symbol, timeframe) -> {session_id: callback}
        self._series: dict[tuple[str, str], dict[str, Callable[[list[dict[str, Any]]], None]]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fan_in = False

    # -- listeners ------------------------------------------------------
    def add_listener(
        self,
        symbol: str,
        timeframe: str,
        session_id: str,
        callback: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        with self._lock:
            bucket = self._series.setdefault((symbol, timeframe), {})
            bucket[session_id] = callback

    def remove_listener(self, symbol: str, timeframe: str, session_id: str) -> bool:
        with self._lock:
            bucket = self._series.get((symbol, timeframe))
            if not bucket:
                return False
            removed = bucket.pop(session_id, None) is not None
            if not bucket:
                del self._series[(symbol, timeframe)]
            return removed

    @property
    def series_count(self) -> int:
        with self._lock:
            return len(self._series)

    @property
    def fan_in(self) -> bool:
        with self._lock:
            return self._fan_in

    def dispatch(self, symbol: str, timeframe: str, candles: list[dict[str, Any]]) -> int:
        with self._lock:
            callbacks = list(self._series.get((symbol, timeframe), {}).values())
        delivered = 0
        for cb in callbacks:
            try:
                cb(candles)
            except Exception:  # noqa: S112 - one bad session must not starve the rest
                continue
            delivered += 1
        return delivered

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._serve, name=f"feed-{self.exchange}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- loops ----------------------------------------------------------
    def _serve(self) -> None:
        try:
            asyncio.run(self._watch())
        except Exception:  # feed death must not kill the worker
            return

    def _snapshot_series(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._series.keys())

    async def _watch(self) -> None:
        from src.pynescript.util.datafeed import DataFeedError

        feed = self._make_feed()
        streaming = False
        try:
            async with feed:
                try:
                    if await feed.supports_for_symbols():
                        with self._lock:
                            self._fan_in = True
                        await self._watch_fan_in(feed)
                        streaming = True
                    else:
                        await self._watch_per_series(feed)
                        streaming = True
                except DataFeedError:
                    streaming = False
                except (NotImplementedError, AttributeError):
                    pass
        finally:
            try:
                await feed.close()
            except Exception:
                pass
        if not streaming and not self._stop.is_set():
            await self._poll_loop()

    async def _watch_fan_in(self, feed: Any) -> None:
        """One socket for every series on this exchange (preferred).

        Membership is re-read whenever the generator ends or the bound
        series set changes, so a late subscribe joins the fan-in.
        """
        from src.pynescript.util.datafeed import DataFeedError

        while not self._stop.is_set():
            series = self._snapshot_series()
            pairs = [(s, tf) for s, tf in series if s]
            if not pairs:
                await asyncio.sleep(0.2)
                continue
            try:
                async for symbol, timeframe, candles in feed.watch_ohlcv_for_symbols(pairs):
                    if self._stop.is_set():
                        return
                    if set(self._snapshot_series()) != set(series):
                        break
                    batch = normalize_candles(candles)
                    if batch:
                        self.dispatch(symbol, timeframe or pairs[0][1], batch)
            except DataFeedError:
                raise
            except (NotImplementedError, AttributeError):
                return
            await asyncio.sleep(0.05)

    async def _watch_per_series(self, feed: Any) -> None:
        """One ``watch_ohlcv`` task per series (venues without fan-in)."""
        tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

        async def watch_one(symbol: str, timeframe: str) -> None:
            while not self._stop.is_set():
                try:
                    async for candles in feed.watch_ohlcv(symbol, timeframe):
                        if self._stop.is_set():
                            return
                        batch = normalize_candles(candles)
                        if batch:
                            self.dispatch(symbol, timeframe, batch)
                    return
                except (NotImplementedError, AttributeError):
                    return
                except Exception:  # reconnect inside the venue loop
                    await asyncio.sleep(1.0)

        try:
            while not self._stop.is_set():
                for key in self._snapshot_series():
                    if key not in tasks or tasks[key].done():
                        tasks[key] = asyncio.create_task(watch_one(*key))
                # Reap dead series.
                live = set(self._snapshot_series())
                for key, task in list(tasks.items()):
                    if key not in live:
                        task.cancel()
                        del tasks[key]
                await asyncio.sleep(1.0)
        finally:
            for task in tasks.values():
                task.cancel()

    async def _poll_loop(self) -> None:
        feed = self._make_feed()
        try:
            async with feed:
                last: dict[tuple[str, str], int] = {}
                while not self._stop.is_set():
                    for symbol, timeframe in self._snapshot_series():
                        try:
                            rows = await feed.fetch_ohlcv(symbol, timeframe, limit=2)
                        except Exception:
                            rows = []
                        key = (symbol, timeframe)
                        batch = [b for b in normalize_candles(rows) if b["time"] > last.get(key, 0)]
                        if batch:
                            last[key] = batch[-1]["time"]
                            self.dispatch(symbol, timeframe, batch)
                    await asyncio.sleep(self._poll_interval_s)
        finally:
            try:
                await feed.close()
            except Exception:
                pass

    def _make_feed(self) -> Any:
        if self._feed_factory is not None:
            return self._feed_factory(self.exchange)
        from src.pynescript.util.datafeed import CCXTProDataFeed

        default = self.market if self.market in ("spot", "swap", "future") else "spot"
        return CCXTProDataFeed(
            exchange=self.exchange,
            enableRateLimit=True,
            options={"defaultType": default, "fetchMarkets": [default]},
        )


class FeedManager:
    """Owns one :class:`ExchangeFeed` per exchange id."""

    def __init__(self, feed_factory: Callable[[str], Any] | None = None) -> None:
        self._feed_factory = feed_factory
        self._lock = threading.Lock()
        self._feeds: dict[str, ExchangeFeed] = {}

    @staticmethod
    def split_key(key_text: str) -> tuple[str, str, str, str] | None:
        """``exchange|market|SYMBOL|interval`` → ``(exchange, market, SYMBOL, interval)``."""
        parts = (key_text or "").split("|")
        if len(parts) != 4 or not parts[0] or not parts[2] or not parts[3]:
            return None
        market = parts[1] or "spot"
        return parts[0], market, parts[2], parts[3]

    def _feed_for(self, exchange: str, market: str) -> ExchangeFeed:
        bucket = f"{exchange}|{market or 'spot'}"
        with self._lock:
            feed = self._feeds.get(bucket)
            if feed is None:
                feed = ExchangeFeed(exchange, market=market, feed_factory=self._feed_factory)
                self._feeds[bucket] = feed
        feed.start()
        return feed

    def subscribe(
        self, key_text: str, session_id: str, callback: Callable[[list[dict[str, Any]]], None]
    ) -> ExchangeFeed | None:
        split = self.split_key(key_text)
        if split is None:
            return None
        exchange, market, symbol, timeframe = split
        feed = self._feed_for(exchange, market)
        feed.add_listener(symbol, timeframe, session_id, callback)
        return feed

    def unsubscribe(self, key_text: str, session_id: str) -> None:
        split = self.split_key(key_text)
        if split is None:
            return
        exchange, market, symbol, timeframe = split
        with self._lock:
            feed = self._feeds.get(f"{exchange}|{market}")
        if feed is not None:
            feed.remove_listener(symbol, timeframe, session_id)

    def stop_all(self) -> None:
        with self._lock:
            feeds = list(self._feeds.values())
            self._feeds.clear()
        for feed in feeds:
            feed.stop()


_MANAGER = FeedManager()


def get_feed_manager() -> FeedManager:
    return _MANAGER


def now_seconds() -> int:
    import time as _time

    return int(_time.time())
