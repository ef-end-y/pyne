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

"""Live DataFeed.poll_ohlcv drives interpret and compile realtime ticks (D-04).

Without a feed, last-bar ticks re-visit the same historical OHLC.
With a feed, each last-bar tick evaluates the next polled forming candle.
"""

from __future__ import annotations

from pynescript.runtime import Runtime
from pynescript.util.datafeed import MockDataFeed


def _bars(n: int = 5) -> list[dict[str, float | int]]:
    out: list[dict[str, float | int]] = []
    for i in range(n):
        c = 100.0 + i
        out.append(
            {
                "time": 1_704_067_200_000 + i * 86_400_000,
                "open": c,
                "high": c + 1.0,
                "low": c - 1.0,
                "close": c,
                "volume": 1000.0,
            }
        )
    return out


_PLOT_CLOSE = """//@version=6
indicator("rt feed")
plot(close, "c")
"""


def _scripted_closes(*closes: float, ts: int = 1_704_067_200_000 + 4 * 86_400_000) -> list[list[float | int]]:
    candles: list[list[float | int]] = []
    for i, close in enumerate(closes):
        o = float(closes[0])
        candles.append([ts, o, close + 1.0, o - 1.0, float(close), 10.0 + i])
    return candles


def test_feed_ticks_use_third_polled_close() -> None:
    """``realtime_ticks=3`` + feed → last plotted close is the third poll."""
    candles = _scripted_closes(210.0, 220.0, 230.0)
    feed = MockDataFeed(scripted=candles)
    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="interpret",
        realtime_ticks=3,
        data_feed=feed,
    )
    assert "error" not in out, out.get("error")
    plotted = [float(v) for v in out["series"]["c"]]
    assert plotted[-1] == 230.0, plotted
    assert plotted[-1] != hist_close
    assert plotted[:-1] == [float(b["close"]) for b in bars[:-1]]


def test_no_feed_keeps_historical_close() -> None:
    """Without ``data_feed``, ticks re-visit the same last-bar OHLC."""
    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(_PLOT_CLOSE, bars, mode="interpret", realtime_ticks=3)
    assert "error" not in out, out.get("error")
    assert [float(v) for v in out["series"]["c"]] == [float(b["close"]) for b in bars]
    assert float(out["series"]["c"][-1]) == hist_close


def test_poll_none_is_noop() -> None:
    """``poll_ohlcv`` returning None leaves the historical last bar in place."""

    class _NonePoll:
        def poll_ohlcv(self, symbol: str, timeframe: str = "1m"):
            return None

    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="interpret",
        realtime_ticks=3,
        data_feed=_NonePoll(),
    )
    assert "error" not in out, out.get("error")
    assert float(out["series"]["c"][-1]) == hist_close


def test_historical_ticks_one_ignores_feed() -> None:
    """``realtime_ticks=1`` (no window) does not consume the feed."""
    candles = _scripted_closes(999.0)
    feed = MockDataFeed(scripted=candles)
    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="interpret",
        realtime_ticks=1,
        data_feed=feed,
    )
    assert "error" not in out, out.get("error")
    assert float(out["series"]["c"][-1]) == hist_close
    assert feed.poll_ohlcv("RT") == candles[0]


def test_compile_feed_ticks_use_third_polled_close() -> None:
    """Compile object-mode last-bar ticks poll the same forming OHLC as interpret."""
    candles = _scripted_closes(210.0, 220.0, 230.0)
    feed = MockDataFeed(scripted=candles)
    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="compile",
        realtime_ticks=3,
        data_feed=feed,
    )
    assert "error" not in out, out.get("error")
    plotted = [float(v) for v in out["series"]["c"]]
    assert plotted[-1] == 230.0, plotted
    assert plotted[-1] != hist_close
    assert plotted[:-1] == [float(b["close"]) for b in bars[:-1]]


def test_compile_matches_interpret_feed_ticks() -> None:
    """Dual-host: last plotted close is the third polled candle on both backends."""
    candles = _scripted_closes(210.0, 220.0, 230.0)
    bars = _bars(5)
    interp = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="interpret",
        realtime_ticks=3,
        data_feed=MockDataFeed(scripted=list(candles)),
    )
    compiled = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="compile",
        realtime_ticks=3,
        data_feed=MockDataFeed(scripted=list(candles)),
    )
    assert "error" not in interp, interp.get("error")
    assert "error" not in compiled, compiled.get("error")
    assert float(interp["series"]["c"][-1]) == 230.0
    assert float(compiled["series"]["c"][-1]) == 230.0
    assert [float(v) for v in compiled["series"]["c"]] == [float(v) for v in interp["series"]["c"]]


def test_compile_no_feed_keeps_historical_close() -> None:
    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(_PLOT_CLOSE, bars, mode="compile", realtime_ticks=3)
    assert "error" not in out, out.get("error")
    assert float(out["series"]["c"][-1]) == hist_close


def test_compile_poll_none_is_noop() -> None:
    class _NonePoll:
        def poll_ohlcv(self, symbol: str, timeframe: str = "1m"):
            return None

    bars = _bars(5)
    hist_close = float(bars[-1]["close"])
    out = Runtime(symbol="RT").run(
        _PLOT_CLOSE,
        bars,
        mode="compile",
        realtime_ticks=3,
        data_feed=_NonePoll(),
    )
    assert "error" not in out, out.get("error")
    assert float(out["series"]["c"][-1]) == hist_close
