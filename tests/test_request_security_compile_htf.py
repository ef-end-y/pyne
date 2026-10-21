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
plot(request.security(syminfo.tickerid, "D", ta.range(3)), title="rng1")
plot(ta.highest(close, 3), title="chi")
"""
        si, sc = _run_both(src, bars)
        for key in ("hi", "lo", "hi1", "lo1", "rng", "rng1"):
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

    def test_htf_median_dev_variance_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed median/dev/variance: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.median(close, 3)), title="med")
plot(request.security(syminfo.tickerid, "D", ta.dev(close, 3)), title="dev")
plot(request.security(syminfo.tickerid, "D", ta.variance(close, 3)), title="var")
plot(ta.median(close, 3), title="cmed")
"""
        si, sc = _run_both(src, bars)
        for key in ("med", "dev", "var"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["med"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["med"][-1]) - float(sc["series"]["cmed"][-1])) > 1e-6

    def test_htf_mode_percentrank_change_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed mode/percentrank/change: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.mode(close, 3)), title="mode")
plot(request.security(syminfo.tickerid, "D", ta.percentrank(close, 3)), title="pr")
plot(request.security(syminfo.tickerid, "D", ta.change(close, 3)), title="chg")
plot(ta.change(close, 3), title="cchg")
"""
        si, sc = _run_both(src, bars)
        for key in ("mode", "pr", "chg"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["mode"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["chg"][-1]) - float(sc["series"]["cchg"][-1])) > 1e-6

    def test_htf_cog_highestbars_lowestbars_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed cog/highestbars/lowestbars: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.cog(close, 3)), title="cog")
plot(request.security(syminfo.tickerid, "D", ta.highestbars(close, 3)), title="hb")
plot(request.security(syminfo.tickerid, "D", ta.lowestbars(close, 3)), title="lb")
plot(request.security(syminfo.tickerid, "D", ta.highestbars(3)), title="hb1")
plot(request.security(syminfo.tickerid, "D", ta.lowestbars(3)), title="lb1")
plot(ta.highestbars(close, 3), title="chb")
"""
        si, sc = _run_both(src, bars)
        for key in ("cog", "hb", "lb", "hb1", "lb1"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["cog"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        # Chart highestbars is compiled; HTF is stepwise (not chart passthrough).
        chb_day5 = sc["series"]["chb"][5 * 24 : 6 * 24]
        assert any(not _is_na(v) and abs(float(v) - float(chb_day5[0])) > 1e-6 for v in chb_day5)

    def test_htf_kama_rising_falling_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed kama/rising/falling: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.kama(close, 3)), title="kama")
plot(request.security(syminfo.tickerid, "D", ta.rising(close, 3)), title="ris")
plot(request.security(syminfo.tickerid, "D", ta.falling(close, 3)), title="fal")
plot(ta.kama(close, 3), title="ckama")
"""
        si, sc = _run_both(src, bars)
        for key in ("kama", "ris", "fal"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][4 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        # Stepwise constant inside day 5 (last completed = day 4).
        day5 = sc["series"]["kama"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["kama"][-1]) - float(sc["series"]["ckama"][-1])) > 1e-6

    def test_htf_swma_ao_range1_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed swma / ao / 1-arg range: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 40)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.swma(close)), title="swma")
plot(request.security(syminfo.tickerid, "D", ta.ao()), title="ao")
plot(request.security(syminfo.tickerid, "D", ta.range(3)), title="rng1")
plot(ta.swma(close), title="cswma")
"""
        si, sc = _run_both(src, bars)
        for key in ("swma", "ao", "rng1"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][35 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day36 = sc["series"]["ao"][36 * 24 : 37 * 24]
        assert all(not _is_na(x) for x in day36)
        ref = float(day36[0])
        for v in day36:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["swma"][-1]) - float(sc["series"]["cswma"][-1])) > 1e-6

    def test_htf_ao2_linreg3_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 2-arg ao / 3-arg linreg: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 12)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.ao(3, 8)), title="ao2")
plot(request.security(syminfo.tickerid, "D", ta.linreg(close, 4, 1)), title="lin3")
plot(ta.ao(3, 8), title="cao2")
"""
        si, sc = _run_both(src, bars)
        for key in ("ao2", "lin3"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][9 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day9 = sc["series"]["ao2"][9 * 24 : 10 * 24]
        assert all(not _is_na(x) for x in day9)
        ref = float(day9[0])
        for v in day9:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["ao2"][-1]) - float(sc["series"]["cao2"][-1])) > 1e-6

    def test_htf_vwap_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 0-arg / 1-arg ta.vwap: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.vwap()), title="vw0")
plot(request.security(syminfo.tickerid, "D", ta.vwap(close)), title="vw1")
plot(ta.vwap(), title="cv0")
"""
        si, sc = _run_both(src, bars)
        for key in ("vw0", "vw1"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][2 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["vw0"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["vw0"][-1]) - float(sc["series"]["vw1"][-1])) > 1e-6
        assert abs(float(sc["series"]["vw0"][-1]) - float(sc["series"]["cv0"][-1])) > 1e-6

    def test_htf_cum_ao1_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed ta.cum / 1-arg ta.ao: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 40)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.cum(close)), title="cum")
plot(request.security(syminfo.tickerid, "D", ta.ao(3)), title="ao1")
plot(ta.cum(close), title="ccum")
plot(ta.ao(3), title="cao1")
"""
        si, sc = _run_both(src, bars)
        for key in ("cum", "ao1"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][35 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day36 = sc["series"]["ao1"][36 * 24 : 37 * 24]
        assert all(not _is_na(x) for x in day36)
        ref = float(day36[0])
        for v in day36:
            assert abs(float(v) - ref) < 1e-9
        day5 = sc["series"]["cum"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        cref = float(day5[0])
        for v in day5:
            assert abs(float(v) - cref) < 1e-9
        assert abs(float(sc["series"]["cum"][-1]) - float(sc["series"]["ccum"][-1])) > 1e-6
        assert abs(float(sc["series"]["ao1"][-1]) - float(sc["series"]["cao1"][-1])) > 1e-6

    def test_htf_change1_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 1-arg ta.change(close): compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.change(close)), title="chg1")
plot(ta.change(close), title="cchg1")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "chg1")
        finite = [x for x in sc["series"]["chg1"][2 * 24 :] if not _is_na(x)]
        assert len(finite) > 0
        assert math.isfinite(float(finite[-1]))
        day5 = sc["series"]["chg1"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        assert abs(float(sc["series"]["chg1"][-1]) - float(sc["series"]["cchg1"][-1])) > 1e-6

    def test_htf_adx_mfi_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 1-arg ta.adx / ta.mfi: compile matches interpret."""
        day0 = _utc_day0_ms()
        bars: list[dict] = []
        for i in range(24 * 12):
            base = 100.0 + 8.0 * math.sin(2.0 * math.pi * i / (24 * 5))
            o = round(base, 2)
            c = round(base + (1.2 if i % 4 else -0.8), 2)
            h = round(max(o, c) + 0.9, 2)
            lo = round(min(o, c) - 0.9, 2)
            bars.append(
                {
                    "open": o,
                    "high": h,
                    "low": max(lo, 0.01),
                    "close": c,
                    "time": day0 + i * 3_600_000,
                    "volume": 800.0 + (i % 24) * 40.0,
                }
            )
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.adx(3)), title="adx")
plot(request.security(syminfo.tickerid, "D", ta.mfi(3)), title="mfi")
plot(ta.adx(3), title="cadx")
plot(ta.mfi(3), title="cmfi")
"""
        si, sc = _run_both(src, bars)
        for key in ("adx", "mfi"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][8 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day8 = sc["series"]["adx"][8 * 24 : 9 * 24]
        assert all(not _is_na(x) for x in day8)
        ref = float(day8[0])
        for v in day8:
            assert abs(float(v) - ref) < 1e-9
        cadx_day8 = sc["series"]["cadx"][8 * 24 : 9 * 24]
        cmfi_day8 = sc["series"]["cmfi"][8 * 24 : 9 * 24]
        hadx_day8 = sc["series"]["adx"][8 * 24 : 9 * 24]
        hmfi_day8 = sc["series"]["mfi"][8 * 24 : 9 * 24]
        assert any(abs(float(hadx_day8[i]) - float(cadx_day8[i])) > 1e-6 for i in range(24) if not _is_na(cadx_day8[i]))
        assert any(abs(float(hmfi_day8[i]) - float(cmfi_day8[i])) > 1e-6 for i in range(24) if not _is_na(cmfi_day8[i]))

    def test_htf_obv_cmf_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 0-arg ta.obv / 1-arg ta.cmf: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.obv()), title="obv")
plot(request.security(syminfo.tickerid, "D", ta.cmf(3)), title="cmf")
plot(ta.obv(), title="cobv")
plot(ta.cmf(3), title="ccmf")
"""
        si, sc = _run_both(src, bars)
        for key in ("obv", "cmf"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["obv"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        cobv_day5 = sc["series"]["cobv"][5 * 24 : 6 * 24]
        ccmf_day5 = sc["series"]["ccmf"][5 * 24 : 6 * 24]
        hobv_day5 = sc["series"]["obv"][5 * 24 : 6 * 24]
        hcmf_day5 = sc["series"]["cmf"][5 * 24 : 6 * 24]
        assert any(abs(float(hobv_day5[i]) - float(cobv_day5[i])) > 1e-6 for i in range(24) if not _is_na(cobv_day5[i]))
        assert any(abs(float(hcmf_day5[i]) - float(ccmf_day5[i])) > 1e-6 for i in range(24) if not _is_na(ccmf_day5[i]))

    def test_htf_nvi_pvi_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 0-arg ta.nvi / ta.pvi: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        for i, b in enumerate(bars):
            day = i // 24
            hour = i % 24
            base = 2000.0 if day % 2 == 0 else 800.0
            b["volume"] = base + hour
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.nvi()), title="nvi")
plot(request.security(syminfo.tickerid, "D", ta.pvi()), title="pvi")
plot(ta.nvi(), title="cnvi")
plot(ta.pvi(), title="cpvi")
"""
        si, sc = _run_both(src, bars)
        for key in ("nvi", "pvi"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["nvi"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        cnvi_day5 = sc["series"]["cnvi"][5 * 24 : 6 * 24]
        cpvi_day5 = sc["series"]["cpvi"][5 * 24 : 6 * 24]
        hnvi_day5 = sc["series"]["nvi"][5 * 24 : 6 * 24]
        hpvi_day5 = sc["series"]["pvi"][5 * 24 : 6 * 24]
        assert any(abs(float(hnvi_day5[i]) - float(cnvi_day5[i])) > 1e-6 for i in range(24) if not _is_na(cnvi_day5[i]))
        assert any(abs(float(hpvi_day5[i]) - float(cpvi_day5[i])) > 1e-6 for i in range(24) if not _is_na(cpvi_day5[i]))

    def test_htf_accdist_wad_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 0-arg ta.accdist / ta.wad: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.accdist()), title="ad")
plot(request.security(syminfo.tickerid, "D", ta.wad()), title="wad")
plot(ta.accdist(), title="cad")
plot(ta.wad(), title="cwad")
"""
        si, sc = _run_both(src, bars)
        for key in ("ad", "wad"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["ad"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        cad_day5 = sc["series"]["cad"][5 * 24 : 6 * 24]
        cwad_day5 = sc["series"]["cwad"][5 * 24 : 6 * 24]
        had_day5 = sc["series"]["ad"][5 * 24 : 6 * 24]
        hwad_day5 = sc["series"]["wad"][5 * 24 : 6 * 24]
        assert any(abs(float(had_day5[i]) - float(cad_day5[i])) > 1e-6 for i in range(24) if not _is_na(cad_day5[i]))
        assert any(abs(float(hwad_day5[i]) - float(cwad_day5[i])) > 1e-6 for i in range(24) if not _is_na(cwad_day5[i]))

    def test_htf_tr_tsi_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 0-arg ta.tr / 2-arg ta.tsi: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.tr()), title="tr")
plot(request.security(syminfo.tickerid, "D", ta.tsi(2, 3)), title="tsi")
plot(ta.tr(), title="ctr")
plot(ta.tsi(2, 3), title="ctsi")
"""
        si, sc = _run_both(src, bars)
        for key in ("tr", "tsi"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["tr"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        ctr_day5 = sc["series"]["ctr"][5 * 24 : 6 * 24]
        ctsi_day5 = sc["series"]["ctsi"][5 * 24 : 6 * 24]
        htr_day5 = sc["series"]["tr"][5 * 24 : 6 * 24]
        htsi_day5 = sc["series"]["tsi"][5 * 24 : 6 * 24]
        assert any(abs(float(htr_day5[i]) - float(ctr_day5[i])) > 1e-6 for i in range(24) if not _is_na(ctr_day5[i]))
        assert any(abs(float(htsi_day5[i]) - float(ctsi_day5[i])) > 1e-6 for i in range(24) if not _is_na(ctsi_day5[i]))

    def test_htf_bbw_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 2-arg / 3-arg ta.bbw: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.bbw(close, 3, 2)), title="bbw")
plot(request.security(syminfo.tickerid, "D", ta.bbw(3, 2)), title="bbw2")
plot(ta.bbw(close, 3, 2), title="cbbw")
"""
        si, sc = _run_both(src, bars)
        for key in ("bbw", "bbw2"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["bbw"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["bbw2"][5 * 24 : 6 * 24]:
            assert abs(float(v) - ref) < 1e-9
        cbbw_day5 = sc["series"]["cbbw"][5 * 24 : 6 * 24]
        hbbw_day5 = sc["series"]["bbw"][5 * 24 : 6 * 24]
        assert any(abs(float(hbbw_day5[i]) - float(cbbw_day5[i])) > 1e-6 for i in range(24) if not _is_na(cbbw_day5[i]))

    def test_htf_stoch_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 1-arg / 2-arg ta.stoch %K: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.stoch(3)), title="stoch")
plot(request.security(syminfo.tickerid, "D", ta.stoch(3, 2)), title="stoch2")
plot(ta.stoch(3), title="cstoch")
"""
        si, sc = _run_both(src, bars)
        for key in ("stoch", "stoch2"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["stoch"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["stoch2"][5 * 24 : 6 * 24]:
            assert abs(float(v) - ref) < 1e-9
        cstoch_day5 = sc["series"]["cstoch"][5 * 24 : 6 * 24]
        hstoch_day5 = sc["series"]["stoch"][5 * 24 : 6 * 24]
        assert any(
            abs(float(hstoch_day5[i]) - float(cstoch_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(cstoch_day5[i])
        )

    def test_htf_pivot_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 2-arg pivothigh / pivotlow: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        for b in bars[4 * 24 : 5 * 24]:
            b["high"] = float(b["high"]) + 200.0
            b["low"] = max(float(b["low"]) - 200.0, 0.01)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.pivothigh(2, 2)), title="ph")
plot(request.security(syminfo.tickerid, "D", ta.pivotlow(2, 2)), title="pl")
plot(ta.pivothigh(2, 2), title="cph")
"""
        si, sc = _run_both(src, bars)
        for key in ("ph", "pl"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][7 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day7 = sc["series"]["ph"][7 * 24 :]
        assert all(not _is_na(x) for x in day7)
        ref = float(day7[0])
        for v in day7:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["pl"][7 * 24 :]:
            assert abs(float(v) - float(sc["series"]["pl"][7 * 24])) < 1e-9
        cph_day7 = sc["series"]["cph"][7 * 24 :]
        hph_day7 = sc["series"]["ph"][7 * 24 :]
        assert any(
            abs(float(hph_day7[i]) - float(cph_day7[i])) > 1e-6
            for i in range(len(hph_day7))
            if not _is_na(cph_day7[i])
        ) or all(_is_na(x) for x in cph_day7)

    def test_htf_percentile_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 3-arg percentile TA: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.percentile_nearest_rank(close, 3, 50)), title="pnr")
plot(request.security(syminfo.tickerid, "D", ta.percentile_linear_interpolation(close, 3, 25)), title="pli")
plot(ta.percentile_nearest_rank(close, 3, 50), title="cpnr")
"""
        si, sc = _run_both(src, bars)
        for key in ("pnr", "pli"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["pnr"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["pli"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["pli"][5 * 24])) < 1e-9
        cpnr_day5 = sc["series"]["cpnr"][5 * 24 : 6 * 24]
        hpnr_day5 = sc["series"]["pnr"][5 * 24 : 6 * 24]
        assert any(
            abs(float(hpnr_day5[i]) - float(cpnr_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(cpnr_day5[i])
        )

    def test_htf_dpo_crossover_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 1-arg dpo / OHLCV cross: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.dpo(3)), title="dpo")
plot(request.security(syminfo.tickerid, "D", ta.crossover(close, open)), title="xo")
plot(request.security(syminfo.tickerid, "D", ta.crossunder(close, open)), title="xu")
plot(ta.dpo(3), title="cdpo")
"""
        si, sc = _run_both(src, bars)
        for key in ("dpo", "xo", "xu"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["dpo"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["xo"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["xo"][5 * 24])) < 1e-9
        for v in sc["series"]["xu"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["xu"][5 * 24])) < 1e-9
        cdpo_day5 = sc["series"]["cdpo"][5 * 24 : 6 * 24]
        hdpo_day5 = sc["series"]["dpo"][5 * 24 : 6 * 24]
        assert any(
            abs(float(hdpo_day5[i]) - float(cdpo_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(cdpo_day5[i])
        ) or all(_is_na(x) for x in cdpo_day5)

    def test_htf_correlation_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 3-arg OHLCV correlation: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.correlation(close, volume, 3)), title="corr")
plot(ta.correlation(close, volume, 3), title="ccorr")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "corr")
        finite = [x for x in sc["series"]["corr"][5 * 24 :] if not _is_na(x)]
        assert len(finite) > 0
        assert math.isfinite(float(finite[-1]))
        day5 = sc["series"]["corr"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        ccorr_day5 = sc["series"]["ccorr"][5 * 24 : 6 * 24]
        hcorr_day5 = sc["series"]["corr"][5 * 24 : 6 * 24]
        assert any(
            abs(float(hcorr_day5[i]) - float(ccorr_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(ccorr_day5[i])
        ) or all(_is_na(x) for x in ccorr_day5)

    def test_htf_dpo2_cross_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 2-arg dpo / OHLCV ta.cross: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.dpo(close, 3)), title="dpo")
plot(request.security(syminfo.tickerid, "D", ta.dpo(open, 3)), title="dpoo")
plot(request.security(syminfo.tickerid, "D", ta.cross(close, open)), title="x")
plot(ta.dpo(3), title="cdpo")
"""
        si, sc = _run_both(src, bars)
        for key in ("dpo", "dpoo", "x"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["dpo"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["dpoo"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["dpoo"][5 * 24])) < 1e-9
        for v in sc["series"]["x"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["x"][5 * 24])) < 1e-9
        cdpo_day5 = sc["series"]["cdpo"][5 * 24 : 6 * 24]
        hdpo_day5 = sc["series"]["dpo"][5 * 24 : 6 * 24]
        assert any(
            abs(float(hdpo_day5[i]) - float(cdpo_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(cdpo_day5[i])
        ) or all(_is_na(x) for x in cdpo_day5)

    def test_htf_vpt_wvad_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 0-arg ta.vpt / 1-arg ta.wvad: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.vpt()), title="vpt")
plot(request.security(syminfo.tickerid, "D", ta.pvt()), title="pvt")
plot(request.security(syminfo.tickerid, "D", ta.wvad(3)), title="wvad")
plot(ta.vpt(), title="cvpt")
plot(ta.wvad(3), title="cwvad")
"""
        si, sc = _run_both(src, bars)
        for key in ("vpt", "pvt", "wvad"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["vpt"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["pvt"][5 * 24 : 6 * 24]:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["wvad"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["wvad"][5 * 24])) < 1e-9
        cvpt_day5 = sc["series"]["cvpt"][5 * 24 : 6 * 24]
        cwvad_day5 = sc["series"]["cwvad"][5 * 24 : 6 * 24]
        hvpt_day5 = sc["series"]["vpt"][5 * 24 : 6 * 24]
        hwvad_day5 = sc["series"]["wvad"][5 * 24 : 6 * 24]
        assert any(abs(float(hvpt_day5[i]) - float(cvpt_day5[i])) > 1e-6 for i in range(24) if not _is_na(cvpt_day5[i]))
        assert any(abs(float(hwvad_day5[i]) - float(cwvad_day5[i])) > 1e-6 for i in range(24) if not _is_na(cwvad_day5[i]))

    def test_htf_uo_apo_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 3-arg ta.uo / 3-arg ta.apo: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.uo(2, 3, 4)), title="uo")
plot(request.security(syminfo.tickerid, "D", ta.apo(close, 2, 3)), title="apo")
plot(ta.uo(2, 3, 4), title="cuo")
plot(ta.apo(close, 2, 3), title="capo")
"""
        si, sc = _run_both(src, bars)
        for key in ("uo", "apo"):
            _assert_series_match(si, sc, key)
            finite = [x for x in sc["series"][key][5 * 24 :] if not _is_na(x)]
            assert len(finite) > 0, key
            assert math.isfinite(float(finite[-1])), key
        day5 = sc["series"]["uo"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        for v in sc["series"]["apo"][5 * 24 : 6 * 24]:
            assert abs(float(v) - float(sc["series"]["apo"][5 * 24])) < 1e-9
        cuo_day5 = sc["series"]["cuo"][5 * 24 : 6 * 24]
        capo_day5 = sc["series"]["capo"][5 * 24 : 6 * 24]
        huo_day5 = sc["series"]["uo"][5 * 24 : 6 * 24]
        hapo_day5 = sc["series"]["apo"][5 * 24 : 6 * 24]
        assert any(abs(float(huo_day5[i]) - float(cuo_day5[i])) > 1e-6 for i in range(24) if not _is_na(cuo_day5[i]))
        assert any(
            abs(float(hapo_day5[i]) - float(capo_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(capo_day5[i])
        ) or all(_is_na(x) for x in capo_day5)

    def test_htf_emv_hourly_to_daily_match_interpret(self) -> None:
        """Hourly→daily last-completed 1-arg ta.emv: compile matches interpret."""
        bars = _day_aligned_hourly(24 * 8)
        src = """//@version=6
indicator("t")
plot(request.security(syminfo.tickerid, "D", ta.emv(3)), title="emv")
plot(ta.emv(3), title="cemv")
"""
        si, sc = _run_both(src, bars)
        _assert_series_match(si, sc, "emv")
        finite = [x for x in sc["series"]["emv"][5 * 24 :] if not _is_na(x)]
        assert len(finite) > 0
        assert math.isfinite(float(finite[-1]))
        day5 = sc["series"]["emv"][5 * 24 : 6 * 24]
        assert all(not _is_na(x) for x in day5)
        ref = float(day5[0])
        for v in day5:
            assert abs(float(v) - ref) < 1e-9
        cemv_day5 = sc["series"]["cemv"][5 * 24 : 6 * 24]
        hemv_day5 = sc["series"]["emv"][5 * 24 : 6 * 24]
        assert any(
            abs(float(hemv_day5[i]) - float(cemv_day5[i])) > 1e-6
            for i in range(24)
            if not _is_na(cemv_day5[i])
        ) or all(_is_na(x) for x in cemv_day5)


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
plot(request.security(syminfo.tickerid, "60", ta.vwap(ta.sma(close, 5))), title="vwapn")
plot(request.security(syminfo.tickerid, "60", ta.vwap(close, 1)), title="vwap2")
plot(request.security(syminfo.tickerid, "60", ta.vwap(hlc3, 1, 1)), title="vwap3")
plot(request.security(syminfo.tickerid, "60", ta.cum(ta.sma(close, 5))), title="cumn")
plot(request.security(syminfo.tickerid, "60", ta.change(ta.sma(close, 5))), title="chgn")
plot(request.security(syminfo.tickerid, "60", ta.adx(5, 5)), title="adx2")
plot(request.security(syminfo.tickerid, "60", ta.adx(high, low, close, 5)), title="adx4")
plot(request.security(syminfo.tickerid, "60", ta.mfi(ta.sma(close, 5), 5)), title="mfin")
plot(request.security(syminfo.tickerid, "60", ta.obv(close, volume)), title="obv2")
plot(request.security(syminfo.tickerid, "60", ta.nvi(close, volume)), title="nvi2")
plot(request.security(syminfo.tickerid, "60", ta.pvi(close, volume)), title="pvi2")
plot(request.security(syminfo.tickerid, "60", ta.accdist(high, low, close, volume)), title="ad4")
plot(request.security(syminfo.tickerid, "60", ta.wad(high, low, close, volume)), title="wad4")
plot(request.security(syminfo.tickerid, "60", ta.tr(high, low, close)), title="tr3")
plot(request.security(syminfo.tickerid, "60", ta.tsi(close, 13, 25)), title="tsi3")
plot(request.security(syminfo.tickerid, "60", ta.bbw(ta.sma(close, 5), 5, 2)), title="bbwn")
plot(request.security(syminfo.tickerid, "60", ta.stoch(close, high, low, 5)), title="stoch4")
plot(request.security(syminfo.tickerid, "60", ta.pivothigh(ta.sma(close, 5), 2, 2)), title="phn")
plot(request.security(syminfo.tickerid, "60", ta.pivotlow(ta.ema(close, 5), 2, 2)), title="pln")
plot(request.security(syminfo.tickerid, "60", ta.percentile_nearest_rank(ta.sma(close, 5), 20, 50)), title="pnrn")
plot(request.security(syminfo.tickerid, "60", ta.percentile_linear_interpolation(ta.ema(close, 5), 20, 50)), title="plin")
plot(request.security(syminfo.tickerid, "60", ta.crossover(ta.sma(close, 5), open)), title="xon")
plot(request.security(syminfo.tickerid, "60", ta.crossunder(close, ta.ema(open, 5))), title="xun")
plot(request.security(syminfo.tickerid, "60", ta.cross(ta.sma(close, 5), open)), title="xn")
plot(request.security(syminfo.tickerid, "60", ta.cross(close, ta.ema(open, 5))), title="xn2")
plot(request.security(syminfo.tickerid, "60", ta.correlation(ta.sma(close, 5), volume, 20)), title="corrn")
plot(request.security(syminfo.tickerid, "60", ta.correlation(close, ta.ema(volume, 5), 20)), title="corrn2")
plot(request.security(syminfo.tickerid, "60", ta.cmf(close, high, low, volume, 5)), title="cmf5")
plot(request.security(syminfo.tickerid, "60", ta.cmf(ta.sma(close, 5), high, low, volume, 5)), title="cmfn")
plot(request.security(syminfo.tickerid, "60", ta.vpt(ta.sma(close, 5))), title="vptn")
plot(request.security(syminfo.tickerid, "60", ta.wvad(high, low, close, volume)), title="wvad4")
plot(request.security(syminfo.tickerid, "60", ta.wvad(high, low, close, volume, 5)), title="wvad5")
plot(request.security(syminfo.tickerid, "60", ta.apo(ta.sma(close, 5), 10, 20)), title="apon")
plot(request.security(syminfo.tickerid, "60", ta.emv(close)), title="emvn")
"""
        si, sc = _run_both(src, bars)
        assert all(_is_na(x) for x in sc["series"]["cci1"])
        assert all(_is_na(x) for x in sc["series"]["cci4"])
        assert all(_is_na(x) for x in sc["series"]["wpr4"])
        assert all(_is_na(x) for x in sc["series"]["vwapn"])
        assert all(_is_na(x) for x in sc["series"]["vwap2"])
        assert all(_is_na(x) for x in sc["series"]["vwap3"])
        assert all(_is_na(x) for x in sc["series"]["cumn"])
        assert all(_is_na(x) for x in sc["series"]["chgn"])
        assert all(_is_na(x) for x in sc["series"]["adx2"])
        assert all(_is_na(x) for x in sc["series"]["adx4"])
        assert all(_is_na(x) for x in sc["series"]["mfin"])
        assert all(_is_na(x) for x in sc["series"]["obv2"])
        assert all(_is_na(x) for x in sc["series"]["nvi2"])
        assert all(_is_na(x) for x in sc["series"]["pvi2"])
        assert all(_is_na(x) for x in sc["series"]["ad4"])
        assert all(_is_na(x) for x in sc["series"]["wad4"])
        assert all(_is_na(x) for x in sc["series"]["tr3"])
        assert all(_is_na(x) for x in sc["series"]["tsi3"])
        assert all(_is_na(x) for x in sc["series"]["bbwn"])
        assert all(_is_na(x) for x in sc["series"]["stoch4"])
        assert all(_is_na(x) for x in sc["series"]["phn"])
        assert all(_is_na(x) for x in sc["series"]["pln"])
        assert all(_is_na(x) for x in sc["series"]["pnrn"])
        assert all(_is_na(x) for x in sc["series"]["plin"])
        assert all(_is_na(x) for x in sc["series"]["xon"])
        assert all(_is_na(x) for x in sc["series"]["xun"])
        assert all(_is_na(x) for x in sc["series"]["xn"])
        assert all(_is_na(x) for x in sc["series"]["xn2"])
        assert all(_is_na(x) for x in sc["series"]["corrn"])
        assert all(_is_na(x) for x in sc["series"]["corrn2"])
        assert all(_is_na(x) for x in sc["series"]["cmf5"])
        assert all(_is_na(x) for x in sc["series"]["cmfn"])
        assert all(_is_na(x) for x in sc["series"]["vptn"])
        assert all(_is_na(x) for x in sc["series"]["wvad4"])
        assert all(_is_na(x) for x in sc["series"]["wvad5"])
        assert all(_is_na(x) for x in sc["series"]["apon"])
        assert all(_is_na(x) for x in sc["series"]["emvn"])
        _assert_series_match(si, sc, "tr3")
        _assert_series_match(si, sc, "bbwn")
        _assert_series_match(si, sc, "stoch4")
        _assert_series_match(si, sc, "tsi3")
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
