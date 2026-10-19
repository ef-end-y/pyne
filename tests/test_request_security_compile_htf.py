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
from tests.test_request_data_feed import _ChartLengthStubProvider
from tests.test_request_data_feed import _daily_bars
from tests.test_request_data_feed import _daily_provider_bars
from tests.test_request_data_feed import _DailyHtfProvider
from tests.test_request_data_feed import _day_aligned_hourly
from tests.test_request_data_feed import _hour_bars
from tests.test_request_data_feed import _utc_day0_ms


def _is_na(v: object) -> bool:
    if v is None:
        return True
    if isinstance(v, float):
        return math.isnan(v)
    try:
        return bool(math.isnan(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _run_both(
    src: str,
    bars: list[dict],
    *,
    symbol: str = "AAPL",
    data_provider: Any = None,
    data_feed: Any = None,
) -> tuple[dict, dict]:
    rt = Runtime(symbol=symbol)
    kw: dict[str, Any] = {}
    if data_provider is not None:
        kw["data_provider"] = data_provider
    if data_feed is not None:
        kw["data_feed"] = data_feed
    si = rt.run(src, bars, mode="interpret", **kw)
    sc = rt.run(src, bars, mode="compile", **kw)
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

    def test_htf_cmo_stdev_linreg_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed cmo/stdev/linreg: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.cmo(close, 3)), title="cmo")
plot(request.security(syminfo.tickerid, "D", ta.stdev(close, 3)), title="stdev")
plot(request.security(syminfo.tickerid, "D", ta.linreg(close, 3)), title="linreg")
plot(ta.stdev(close, 3), title="cstdev")
"""
        si, sc = _run_both(src, bars)
        for key in ("cmo", "stdev", "linreg"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["stdev"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["stdev"][-1]) - float(sc["series"]["cstdev"][-1])) > 1e-6

    def test_htf_cci_roc_mom_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed cci/roc/mom: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.cci(close, 3)), title="cci")
plot(request.security(syminfo.tickerid, "D", ta.roc(close, 3)), title="roc")
plot(request.security(syminfo.tickerid, "D", ta.mom(close, 3)), title="mom")
plot(ta.cci(close, 3), title="ccci")
"""
        si, sc = _run_both(src, bars)
        for key in ("cci", "roc", "mom"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["cci"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        # Daily CCI of this walk saturates at 100 like a short chart window;
        # HTF is stepwise, chart CCI is not.
        ccci_day5 = sc["series"]["ccci"][5 * 24 : 6 * 24]
        assert any(not _is_na(v) and abs(float(v) - float(ccci_day5[0])) > 1e-6 for v in ccci_day5)

    def test_htf_hma_vwma_cci1_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed hma/vwma/1-arg cci: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.hma(close, 3)), title="hma")
plot(request.security(syminfo.tickerid, "D", ta.vwma(close, 3)), title="vwma")
plot(request.security(syminfo.tickerid, "D", ta.cci(3)), title="cci1")
plot(ta.hma(close, 3), title="chma")
"""
        si, sc = _run_both(src, bars)
        for key in ("hma", "vwma", "cci1"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["hma"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["hma"][-1]) - float(sc["series"]["chma"][-1])) > 1e-6

    def test_htf_dema_tema_wpr_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed dema/tema/1-arg wpr: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.dema(close, 3)), title="dema")
plot(request.security(syminfo.tickerid, "D", ta.tema(close, 3)), title="tema")
plot(request.security(syminfo.tickerid, "D", ta.wpr(3)), title="wpr")
plot(ta.dema(close, 3), title="cdema")
"""
        si, sc = _run_both(src, bars)
        for key in ("dema", "tema", "wpr"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][7 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 6 (last completed = day 5).
        day6 = sc["series"]["dema"][6 * 24 : 7 * 24]
        assert all(not _is_na(x) for x in day6)
        ref = float(day6[0])
        for v in day6:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["dema"][-1]) - float(sc["series"]["cdema"][-1])) > 1e-6

    def test_htf_highest_lowest_range_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed highest/lowest/range: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.highest(close, 3)), title="hi")
plot(request.security(syminfo.tickerid, "D", ta.lowest(close, 3)), title="lo")
plot(request.security(syminfo.tickerid, "D", ta.highest(3)), title="hi1")
plot(request.security(syminfo.tickerid, "D", ta.lowest(3)), title="lo1")
plot(request.security(syminfo.tickerid, "D", ta.range(close, 3)), title="rng")
plot(ta.highest(close, 3), title="chi")
"""
        si, sc = _run_both(src, bars)
        for key in ("hi", "lo", "hi1", "lo1", "rng"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["hi"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["hi"][-1]) - float(sc["series"]["chi"][-1])) > 1e-6


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
plot(request.security(syminfo.tickerid, "60", ta.cci(5)), title="cci1")
plot(request.security(syminfo.tickerid, "60", ta.cci(high, low, close, 5)), title="cci4")
plot(request.security(syminfo.tickerid, "60", ta.wpr(high, low, close, 5)), title="wpr4")
"""
        si, sc = _run_both(src, bars)
        assert all(_is_na(x) for x in sc["series"]["cci1"])
        assert all(_is_na(x) for x in sc["series"]["cci4"])
        assert all(_is_na(x) for x in sc["series"]["wpr4"])
        _assert_series_match(si, sc, "cci1")
        _assert_series_match(si, sc, "cci4")
        _assert_series_match(si, sc, "wpr4")
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


class TestCompileProviderHtfBarmerge:
    """Daily-on-1h provider/feed HTF: compile object-mode matches interpret (D-06)."""

    def test_gaps_on_daily_provider_matches_interpret(self) -> None:
        bars = _day_aligned_hourly(72)
        prov = _DailyHtfProvider(_daily_provider_bars())
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "D", "close", barmerge.gaps_on, barmerge.lookahead_off)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars, data_provider=prov)
        _assert_series_match(si, sc, "sec")
        sec = sc["series"]["sec"]
        assert all(_is_na(v) for v in sec[0:24])
        assert abs(float(sec[24]) - 111.0) < 1e-9
        assert abs(float(sec[48]) - 222.0) < 1e-9
        assert all(_is_na(v) for v in sec[25:48])
        assert all(_is_na(v) for v in sec[49:])

    def test_lookahead_on_daily_provider_matches_interpret(self) -> None:
        bars = _day_aligned_hourly(72)
        prov = _DailyHtfProvider(_daily_provider_bars())
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "D", "close", barmerge.gaps_off, barmerge.lookahead_on)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars, data_provider=prov)
        _assert_series_match(si, sc, "sec")
        sec = sc["series"]["sec"]
        for i in (0, 12, 23):
            assert abs(float(sec[i]) - 111.0) < 1e-9
        for i in (24, 36, 47):
            assert abs(float(sec[i]) - 222.0) < 1e-9
        for i in (48, 60, 71):
            assert abs(float(sec[i]) - 333.0) < 1e-9

    def test_gaps_and_lookahead_combined_daily_provider_matches_interpret(self) -> None:
        bars = _day_aligned_hourly(72)
        prov = _DailyHtfProvider(_daily_provider_bars())
        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "D", "close", barmerge.gaps_on, barmerge.lookahead_on)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars, data_provider=prov)
        _assert_series_match(si, sc, "sec")
        sec = sc["series"]["sec"]
        assert abs(float(sec[0]) - 111.0) < 1e-9
        assert abs(float(sec[24]) - 222.0) < 1e-9
        assert abs(float(sec[48]) - 333.0) < 1e-9
        assert all(_is_na(v) for v in sec[1:24])
        assert all(_is_na(v) for v in sec[25:48])
        assert all(_is_na(v) for v in sec[49:])

    def test_daily_feed_ohlcv_lookahead_on_matches_interpret(self) -> None:
        bars = _day_aligned_hourly(72)
        day0 = _utc_day0_ms()
        rows = []
        for i, c in enumerate((111.0, 222.0, 333.0)):
            ts = day0 + i * 86_400_000
            rows.append([ts, c, c, c, c, 1.0])

        class _DailyHtfFeed:
            def fetch_latest_ohlcv(self, symbol, timeframe="D", limit=5):
                _ = symbol, timeframe
                n = int(limit) if limit is not None else len(rows)
                return rows[-max(1, n) :] if n < len(rows) else list(rows)

        src = """//@version=6
indicator("t")
v = request.security(syminfo.tickerid, "D", "close", barmerge.gaps_off, barmerge.lookahead_on)
plot(v, title="sec")
"""
        si, sc = _run_both(src, bars, data_feed=_DailyHtfFeed())
        _assert_series_match(si, sc, "sec")
        sec = sc["series"]["sec"]
        assert abs(float(sec[0]) - 111.0) < 1e-9
        assert abs(float(sec[24]) - 222.0) < 1e-9
        assert abs(float(sec[48]) - 333.0) < 1e-9

    def test_chart_length_provider_still_chart_htf_dual_host(self) -> None:
        """ChartOHLCVProvider-like (ignores interval) stays chart HTF, unused provider merge."""
        bars = _day_aligned_hourly(72)
        stub = _ChartLengthStubProvider(bars, symbol="AAPL")
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", close), title="sec")
plot(close, title="c")
"""
        si, sc = _run_both(src, bars, data_provider=stub)
        _assert_series_match(si, sc, "sec")
        assert abs(float(sc["series"]["sec"][-1]) - float(bars[47]["close"])) < 1e-9
        assert abs(float(sc["series"]["sec"][-1]) - float(sc["series"]["c"][-1])) > 1e-6

    def test_chart_length_foreign_stub_unused_merge_on_compile(self) -> None:
        """Foreign ChartOHLCVProvider stub: compile gaps_on == gaps_off (unused merge)."""
        bars = _day_aligned_hourly(48)
        stub = _ChartLengthStubProvider(bars, symbol="AAPL")
        src_on = """//@version=6
indicator("t")
v = request.security("MSFT", "D", "close", barmerge.gaps_on, barmerge.lookahead_off)
plot(v, title="sec")
"""
        src_off = """//@version=6
indicator("t")
v = request.security("MSFT", "D", "close", barmerge.gaps_off, barmerge.lookahead_off)
plot(v, title="sec")
"""
        rt = Runtime(symbol="AAPL")
        on = rt.run(src_on, bars, mode="compile", data_provider=stub)
        off = rt.run(src_off, bars, mode="compile", data_provider=stub)
        assert not on.get("error"), on.get("error")
        assert not off.get("error"), off.get("error")
        sec_on = on["series"]["sec"]
        sec_off = off["series"]["sec"]
        assert len(sec_on) == len(sec_off) == len(bars)
        for a, b in zip(sec_on, sec_off, strict=True):
            na_a, na_b = _is_na(a), _is_na(b)
            assert na_a == na_b
            if not na_a:
                assert abs(float(a) - float(b)) < 1e-9
        # Foreign without a coarser provider series stays na (no LTF / stub invent).
        assert all(_is_na(v) for v in sec_on)
