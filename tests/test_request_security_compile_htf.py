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

"""Compile dual-host goldens for allowlisted HTF ``request.security`` (D-06).

Interpret already resamples same-symbol simple OHLCV / ``close[1]`` /
allowlisted ``ta.*`` on coarser TFs. Compile must match those series
(nan-aware) instead of chart passthrough or ``np.nan``.
"""

from __future__ import annotations

import math

from typing import Any

from backend.runtime import Runtime
from tests.test_request_data_feed import _aligned_1m_bars
from tests.test_request_data_feed import _daily_bars
from tests.test_request_data_feed import _day_aligned_hourly
from tests.test_request_data_feed import _hour_bars


def _is_na(v: object) -> bool:
    if v is None:
        return True
    if isinstance(v, float):
        return math.isnan(v)
    try:
        return bool(math.isnan(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _run_both(src: str, bars: list[dict], *, symbol: str = "AAPL") -> tuple[dict, dict]:
    rt = Runtime(symbol=symbol)
    si = rt.run(src, bars, mode="interpret")
    sc = rt.run(src, bars, mode="compile")
    assert not si.get("error"), si.get("error")
    assert not sc.get("error"), sc.get("error")
    return si, sc


def _assert_series_match(
    interp: dict[str, Any],
    compiled: dict[str, Any],
    key: str,
    *,
    atol: float = 1e-9,
) -> None:
    ia = list((interp.get("series") or {}).get(key) or [])
    ca = list((compiled.get("series") or {}).get(key) or [])
    assert len(ia) == len(ca) and len(ia) > 0, f"{key}: len interp={len(ia)} compile={len(ca)}"
    for i, (a, b) in enumerate(zip(ia, ca, strict=True)):
        na_a, na_b = _is_na(a), _is_na(b)
        if na_a and na_b:
            continue
        assert not na_a and not na_b, f"{key}[{i}] na mismatch interp={a!r} compile={b!r}"
        assert abs(float(a) - float(b)) <= atol, f"{key}[{i}] interp={a!r} compile={b!r}"


class TestCompileHtfOhlcvResample:
    """1m chart + request ``\"60\"`` simple OHLCV matches interpret, not chart close."""

    def test_htf_close_last_bar_matches_interpret_not_chart(self) -> None:
        bars = _aligned_1m_bars(180)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "60", close), title="sec")
plot(close, title="c")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")
        sec = sc["series"]["sec"]
        # Last completed hour close, not the forming 1m close.
        assert abs(float(sec[-1]) - float(bars[-1]["close"])) > 1e-6
        hour1_close = float(bars[119]["close"])
        assert abs(float(sec[-1]) - hour1_close) < 1e-9

    def test_hourly_to_daily_close_matches_interpret(self) -> None:
        """Chart-length ChartOHLCVProvider stays on chart HTF helpers (compile dual-host)."""
        bars = _day_aligned_hourly(72)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", close), title="sec")
plot(close, title="c")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")
        # Last completed UTC day close, not the forming hour.
        assert abs(float(sc["series"]["sec"][-1]) - float(bars[47]["close"])) < 1e-9
        assert abs(float(sc["series"]["sec"][-1]) - float(sc["series"]["c"][-1])) > 1e-6

    def test_htf_high_and_close_offset_match_interpret(self) -> None:
        bars = _hour_bars()
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "60", high), title="h")
plot(request.security(syminfo.tickerid, "60", close[1]), title="c1")
plot(close, title="c")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "h")
        _assert_series_match(si, sc, "c1")
        hour1_high = max(float(b["high"]) for b in bars[60:120])
        assert abs(float(sc["series"]["h"][-1]) - hour1_high) < 1e-9
        # close[1] on last-completed HTF is hour-0 close (forming hour 3).
        assert abs(float(sc["series"]["c1"][-1]) - float(bars[59]["close"])) < 1e-9


class TestCompileHtfSimpleTaResample:
    """Allowlisted ta.* on HTF is finite after warmup and stepwise-constant."""

    def test_htf_sma_ema_rsi_atr_wma_rma_match_interpret(self) -> None:
        bars = _aligned_1m_bars(12 * 60)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "60", ta.sma(close, 3)), title="sma")
plot(request.security(syminfo.tickerid, "60", ta.ema(close, 5)), title="ema")
plot(request.security(syminfo.tickerid, "60", ta.rsi(close, 5)), title="rsi")
plot(request.security(syminfo.tickerid, "60", ta.atr(3)), title="atr")
plot(request.security(syminfo.tickerid, "60", ta.wma(close, 3)), title="wma")
plot(request.security(syminfo.tickerid, "60", ta.rma(close, 3)), title="rma")
plot(ta.sma(close, 3), title="csma")
"""
        si, sc = _run_both(src, bars)
        for key in ("sma", "ema", "rsi", "atr", "wma", "rma"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 60 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside an HTF bucket (hour 5 uses completed hour 4).
        hour5 = sc["series"]["sma"][5 * 60 : 6 * 60]
        assert all(not _is_na(x) for x in hour5)
        ref = float(hour5[0])
        for v in hour5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["sma"][-1]) - float(sc["series"]["csma"][-1])) > 1e-6


class TestCompileHtfBarmerge:
    """gaps_on / lookahead_on on resample paths: compile matches interpret."""

    def test_gaps_on_ohlcv_matches_interpret(self) -> None:
        bars = _hour_bars()
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "60", close, barmerge.gaps_on, barmerge.lookahead_off)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")
        sec = sc["series"]["sec"]
        assert abs(float(sec[60]) - float(bars[59]["close"])) < 1e-9
        assert all(_is_na(v) for v in sec[61:120])

    def test_lookahead_on_ohlcv_matches_interpret(self) -> None:
        bars = _hour_bars()
        src = """//@version=6
indicator("t")
c = request.security(syminfo.tickerid, "60", close, barmerge.gaps_off, barmerge.lookahead_on)
h = request.security(syminfo.tickerid, "60", high, barmerge.gaps_off, barmerge.lookahead_on)
plot(c, title="sec_c")
plot(h, title="sec_h")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec_c")
        _assert_series_match(si, sc, "sec_h")
        assert abs(float(sc["series"]["sec_c"][0]) - float(bars[59]["close"])) < 1e-9

    def test_gaps_and_lookahead_combined_matches_interpret(self) -> None:
        bars = _hour_bars()
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "60", close, barmerge.gaps_on, barmerge.lookahead_on)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")

    def test_gaps_on_simple_ta_matches_interpret(self) -> None:
        bars = _hour_bars(300)
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "60", ta.sma(close, 3), barmerge.gaps_on, barmerge.lookahead_off)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")

    def test_lookahead_on_simple_ta_matches_interpret(self) -> None:
        bars = _hour_bars(300)
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "60", ta.sma(close, 3), barmerge.gaps_off, barmerge.lookahead_on)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")

    def test_lookahead_on_close_offset_matches_interpret(self) -> None:
        bars = _hour_bars()
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "60", close[1], barmerge.gaps_off, barmerge.lookahead_on)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")


class TestCompileSecurityKeepLimits:
    """Nested ta / foreign still na; same-TF simple close still passthrough."""

    def test_nested_ta_still_na_on_compile(self) -> None:
        bars = _aligned_1m_bars(3 * 60)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "60", ta.sma(ta.ema(close, 3), 3)), title="nested")
plot(request.security(syminfo.tickerid, "60", ta.stdev(close, 5)), title="stdev")
"""
        si, sc = _run_both(src, bars)
        assert all(_is_na(x) for x in sc["series"]["stdev"])
        _assert_series_match(si, sc, "stdev")
        # Nested ta is not allowlisted: compile stays na. Interpret may still
        # hit the chart-pre-eval OHLCV heuristic on a few bars — that is not
        # the HTF simple-ta path and is not required to match.
        assert all(_is_na(x) for x in sc["series"]["nested"])

    def test_foreign_ticker_still_na_on_compile(self) -> None:
        bars = _aligned_1m_bars(40)
        src = """//@version=6
indicator("t")
plot(request.security("MSFT", "D", close), title="ms")
plot(close, title="c")
"""
        si, sc = _run_both(src, bars)
        assert all(_is_na(x) for x in sc["series"]["ms"])
        _assert_series_match(si, sc, "ms")

    def test_same_tf_simple_close_passthrough(self) -> None:
        bars = _daily_bars(30)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", close), title="sec")
plot(close, title="c")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "sec")
        assert abs(float(sc["series"]["sec"][-1]) - float(sc["series"]["c"][-1])) < 1e-9
        assert abs(float(sc["series"]["sec"][-1]) - float(bars[-1]["close"])) < 1e-9
