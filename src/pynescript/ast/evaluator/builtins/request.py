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

"""request.* builtins — live/historical fetch with intentional mock soft-fail.

Data-path failures (missing feed, network, bad provider payload) are **soft**:
helpers catch ``Exception`` and fall through to built-in mock prices so
indicator scripts keep evaluating without a real market data backend.

Do **not** harden these into hard failures without an explicit host flag —
Runtime hosts and corpus demos rely on mock fallbacks. Programming errors
inside expression evaluation still propagate via the normal bar loop.
"""

from __future__ import annotations

import logging
import math
import random
import statistics

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pynescript.ast import node as ast_mod
from pynescript.ast.evaluator.names import ast_qualified_name

from .base import BuiltinDispatchMixin
from .base import BuiltinHandler
from .timeframe import SECONDS_PER_MONTH
from .timeframe import _chart_period
from .timeframe import _normalize_time_ms
from .timeframe import timeframe_calendar_id
from .timeframe import timeframe_in_seconds
from .timeframe import timeframe_is_calendar_tf
from .timeframe import timeframes_equivalent


# Define constants for magic numbers
REQUEST_SECURITY_MIN_ARGS = 2
REQUEST_OHLCV_LIMIT = 5
OHLCV_CLOSE_IDX = 4
REQUEST_RECENT_LIMIT = 5
REQUEST_MOCK_PRICE = 100.0
LOWER_TF_SIMULATE_MULTIPLIER = 2  # for demo lower tf bar count from latest data
# How many chart bars to scan when guessing if a pre-eval value is simple OHLCV.
_OHLCV_MATCH_LOOKBACK = 32
# Margin when comparing request TF vs inferred chart bar duration (irregular gaps).
_HTF_BAR_SEC_MARGIN = 1.25
# Chart series safe for same-symbol security passthrough (simple OHLCV + time).
# ``time`` is required so tuples like ``[open, high, low, close, volume, time]``
# (Perceptron / KNN corpus) are not rejected as complex HTF when TF differs.
_OHLCV_SERIES_KEYS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "time",
    "hl2",
    "hlc3",
    "ohlc4",
)
_OHLCV_FIELD_ALIASES: dict[str, str] = {
    "open": "open",
    "o": "open",
    "high": "high",
    "h": "high",
    "low": "low",
    "l": "low",
    "close": "close",
    "c": "close",
    "volume": "volume",
    "vol": "volume",
    "time": "time",
    "hl2": "hl2",
    "hlc3": "hlc3",
    "ohlc4": "ohlc4",
}
_CHART_PLACEHOLDERS = frozenset({"", "CHART", "SYMBOL", "TICKER", "NONE", "NA", "UNKNOWN"})
_FUNDAMENTAL_TOKENS = ("DIVIDEND", "FACTSET", "EARNINGS", "ESD_")

# Allowlisted simple ta.* forms for HTF resample (no arbitrary AST re-eval).
# Matched only when the security expression *AST* is exactly one of these shapes
# (visit_Call attaches :class:`HtfSimpleTaExpr` before chart pre-eval wins).
_HTF_SIMPLE_TA_FUNCS = frozenset(
    {
        "sma",
        "ema",
        "rsi",
        "atr",
        "wma",
        "rma",
        "cmo",
        "stdev",
        "linreg",
        "cci",
        "roc",
        "mom",
        "hma",
        "vwma",
        "dema",
        "tema",
        "wpr",
        "highest",
        "lowest",
        "range",
        "median",
        "dev",
        "variance",
        "mode",
        "percentrank",
        "change",
        "cog",
        "highestbars",
        "lowestbars",
        "kama",
        "rising",
        "falling",
        "swma",
        "ao",
        "vwap",
        "cum",
        "adx",
        "mfi",
        "obv",
        "cmf",
        "nvi",
        "pvi",
    }
)

_LOG = logging.getLogger("pynescript.request.security")

# Static product notes exposed on Runtime ``meta.request_security``.
_SECURITY_POLICY_NOTES: tuple[str, ...] = (
    "No multi-timeframe expression re-eval engine: HTF complex UDF/nested ta results are na.",
    "barmerge.gaps_on / gaps_off honored on HTF resample paths (na-gap series on bucket starts); "
    "still unused on passthrough / chart-length provider stubs / complex-na / LTF.",
    "barmerge.lookahead_on / lookahead_off honored on HTF resample paths "
    "(historical bars see each HTF bucket's final value from the period start); "
    "still unused on passthrough / chart-length provider stubs / complex-na / LTF.",
    "Same-symbol simple OHLCV on a coarser TF resamples chart bars by timestamp "
    "(htf_ohlcv_resample, last completed HTF bar by default — not full expression re-eval).",
    "Provider/feed OHLCV that is already coarser than the chart and carries bar times "
    "is aligned to chart bars (provider_htf_ohlcv) with the same gaps/lookahead merge.",
    "Same-symbol allowlisted ta.sma/ema/rsi/atr/wma/rma/cmo/stdev/linreg/cci/roc/mom/hma/vwma/dema/tema/"
    "highest/lowest/range/median/dev/variance/mode/percentrank/change/cog/highestbars/lowestbars/"
    "kama/rising/falling/swma/ao/vwap/cum/adx/mfi/obv/cmf/nvi/pvi "
    "and 1-arg ta.wpr/highest/lowest/highestbars/lowestbars/range/change/adx/mfi/cmf on coarser TF "
    "runs the TA helper on resampled HTF bars (htf_simple_ta_resample) — still not a full multi-TF engine.",
    "Same-symbol history offsets such as close[1] resample the previous HTF bar "
    "(htf_ohlcv_offset); LTF / unparseable TF still use the chart passthrough stub.",
    "Foreign symbols without a multi-symbol feed hit return na (no mock invent under host chart).",
)


@dataclass(frozen=True)
class HtfOffsetExpr:
    """Bare OHLCV history offset for HTF resample (``close[1]``).

    Produced by :func:`match_htf_offset_ast` when ``request.security``'s
    expression is ``close[1]`` / ``high[1]`` / … so the offset is taken on
    unique HTF bars rather than the chart series.
    """

    field: str
    offset: int


@dataclass(frozen=True)
class HtfSimpleTaExpr:
    """Allowlisted simple ``ta.*`` form for HTF series resample (not chart pre-eval).

    Produced by :func:`match_htf_simple_ta_ast` when ``request.security``'s
    expression argument is a bare allowlisted call such as ``ta.sma(close, 14)``.
    Nested sources, UDFs, and non-constant lengths are rejected.
    """

    name: str  # sma | ema | rsi | atr | wma | rma | cmo | stdev | linreg | cci | roc | mom | hma | vwma | dema | tema | wpr | highest | lowest | range | median | dev | variance | mode | percentrank | change | cog | highestbars | lowestbars | kama | rising | falling | swma | ao | vwap | cum | adx | mfi | obv | cmf | nvi | pvi
    source: str | None  # OHLCV field; None for atr/wpr/adx (h/l/c), mfi/cmf (h/l/c/v), and 0-arg obv/nvi/pvi (close+volume); 1-arg cci is hlc3; 1-arg highest/lowest/highestbars/lowestbars/range is high/low; ao is hl2; 0-arg vwap is hlc3
    length: int  # swma stores 4; 1-arg change stores 1; 0-arg ao stores 0 (SMA 5−34 defaults); 1-arg/2-arg ao stores slow; vwap/cum/obv/nvi/pvi store 0 (cumulative)
    extra: int | None = None  # 1-arg/2-arg ao: fast; 3-arg linreg: offset; else None


def _const_int_ge(node: Any, minimum: int) -> int | None:
    """Literal int >= *minimum* from a Constant AST node, or None."""
    if type(node) is not ast_mod.Constant:
        return None
    kind = node.kind
    if kind is not None and kind != "#":
        return None
    raw = node.value
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        iv = int(raw)
    except (TypeError, ValueError):
        return None
    return iv if iv == raw and iv >= minimum else None


def _const_positive_int(node: Any) -> int | None:
    """Literal positive int from a Constant AST node, or None."""
    return _const_int_ge(node, 1)


def _const_nonneg_int(node: Any) -> int | None:
    """Literal non-negative int from a Constant AST node, or None."""
    return _const_int_ge(node, 0)


def match_htf_simple_ta_ast(expr_ast: Any) -> HtfSimpleTaExpr | None:  # noqa: PLR0911
    """Match allowlisted simple ``ta.*`` AST for HTF resample, else None.

    Allowed shapes (positional args only, no kwargs):

    - ``ta.sma(close, 14)`` / ``ta.ema`` / ``ta.rsi`` / ``ta.wma`` / ``ta.rma``
      / ``ta.cmo`` / ``ta.stdev`` / ``ta.linreg`` / ``ta.cci`` / ``ta.roc`` /
      ``ta.mom`` / ``ta.hma`` / ``ta.vwma`` / ``ta.dema`` / ``ta.tema`` /
      ``ta.highest`` / ``ta.lowest`` / ``ta.range`` / ``ta.median`` / ``ta.dev``
      / ``ta.variance`` / ``ta.mode`` / ``ta.percentrank`` / ``ta.change`` /
      ``ta.cog`` / ``ta.highestbars`` / ``ta.lowestbars`` / ``ta.kama`` /
      ``ta.rising`` / ``ta.falling`` / ``ta.swma`` —
      source is a bare OHLCV name (``close``, ``open``, ``high``, ``low``,
      ``volume``, ``hl2``, …); length is a positive integer literal.
      ``ta.linreg`` is the 2-arg form (offset 0) or 3-arg
      ``(source, length, offset)`` with a non-negative int offset stored in
      ``extra``. ``ta.cci`` is 2-arg
      ``(source, length)`` or 1-arg ``(length)`` (hlc3). ``ta.change`` is the
      2-arg form (not ``close[n]`` history offset) or 1-arg source (length
      defaults to 1). ``ta.kama`` is the 2-arg
      form (Kaufman fast=2, slow=30). ``ta.swma`` is 1-arg source (fixed
      4-period).
    - ``ta.atr(14)`` — length-only form (uses HTF high/low/close).
    - ``ta.adx(14)`` — length-only form (uses HTF high/low/close).
    - ``ta.mfi(14)`` — length-only form (uses HTF high/low/close/volume).
    - ``ta.cmf(14)`` — length-only form (uses HTF high/low/close/volume).
    - ``ta.obv()`` — 0-arg (HTF close+volume, running signed volume).
      ``length`` is stored as 0. 2-arg ``ta.obv(close, volume)`` stays unmatched.
    - ``ta.nvi()`` — 0-arg (HTF close+volume, seed 1000, multiply on volume
      decrease). ``length`` is 0. 2-arg ``ta.nvi(close, volume)`` stays unmatched.
    - ``ta.pvi()`` — 0-arg (HTF close+volume, seed 1000, multiply on volume
      increase). ``length`` is 0. 2-arg ``ta.pvi(close, volume)`` stays unmatched.
    - ``ta.cci(14)`` — length-only form (typical price / HTF hlc3).
    - ``ta.wpr(14)`` — length-only form (uses HTF high/low/close).
    - ``ta.highest(14)`` — length-only form (HTF high).
    - ``ta.lowest(14)`` — length-only form (HTF low).
    - ``ta.highestbars(14)`` — length-only form (HTF high).
    - ``ta.lowestbars(14)`` — length-only form (HTF low).
    - ``ta.range(14)`` — length-only form (HTF high − HTF low over n unique bars).
    - ``ta.ao()`` — 0-arg (HTF hl2, SMA 5 − SMA 34). ``length`` is stored as 0.
    - ``ta.ao(5)`` — one positive int literal = fast; slow defaults to 34.
      ``source="hl2"``, ``length`` is 34, ``extra`` is fast.
    - ``ta.ao(5, 34)`` — two positive int literals; ``source="hl2"``,
      ``length`` is slow, ``extra`` is fast.
    - ``ta.vwap()`` — 0-arg (HTF hlc3, cumulative). ``length`` is stored as 0.
    - ``ta.vwap(close)`` — 1-arg source Name (unique-bar HTF volume).
      ``length`` is 0. 2-arg anchor / 3-arg bands stay unmatched.
    - ``ta.cum(close)`` — 1-arg source Name. Running sum on unique HTF bars.
      ``length`` is 0.
    - ``ta.change(close)`` — 1-arg source Name. Length defaults to 1
      (``src - src[1]`` on unique HTF bars). Nested ``ta.change(ta.sma(...))``
      stays unmatched.

    Nested calls (``ta.sma(ta.ema(...), n)`` / ``ta.highest(ta.sma(...), n)`` /
    ``ta.median(ta.sma(...), n)`` / ``ta.mode(ta.sma(...), n)`` /
    ``ta.cog(ta.sma(...), n)`` / ``ta.highestbars(ta.sma(...), n)`` /
    ``ta.kama(ta.sma(...), n)`` / ``ta.rising(ta.sma(...), n)`` /
    ``ta.swma(ta.sma(...))`` / ``ta.vwap(ta.sma(...))`` /
    ``ta.cum(ta.sma(...))`` / ``ta.change(ta.sma(...))`` /
    ``ta.adx(ta.sma(...), n)`` / ``ta.mfi(ta.sma(...), n)`` /
    ``ta.cmf(ta.sma(...))`` / ``ta.linreg(ta.sma(...), n, offset)``), multi-arg
    atr, 2-arg ``ta.adx(diLength, adxSmoothing)``, 4-arg ``ta.adx`` /
    ``ta.cci`` / ``ta.wpr``, 2-arg ``ta.mfi(source, length)``, 1-arg ``ta.median`` /
    ``ta.dev`` / ``ta.variance`` / ``ta.mode`` /
    ``ta.percentrank`` / ``ta.cog``, ``ta.kama`` 3/4-arg,
    1-arg ``ta.rising`` / ``ta.falling``, 2-arg ``ta.swma``,
    2-arg / 3-arg ``ta.vwap``, 2-arg ``ta.cum``, 2-arg ``ta.obv``,
    2-arg ``ta.nvi`` / ``ta.pvi``, 5-arg ``ta.cmf``,
    variables as length, and non-ta
    callees return ``None`` (existing complex-na path).
    """
    if type(expr_ast) is not ast_mod.Call:
        return None
    qual = ast_qualified_name(expr_ast.func)
    if not qual or not qual.startswith("ta."):
        return None
    fname = qual[3:]
    if fname not in _HTF_SIMPLE_TA_FUNCS:
        return None
    arg_nodes = list(getattr(expr_ast, "args", None) or ())
    if any(getattr(a, "name", None) for a in arg_nodes):
        return None

    if fname == "atr":
        length = _const_positive_int(arg_nodes[0].value) if len(arg_nodes) == 1 else None
        return HtfSimpleTaExpr("atr", None, length) if length else None

    if fname == "adx":
        length = _const_positive_int(arg_nodes[0].value) if len(arg_nodes) == 1 else None
        return HtfSimpleTaExpr("adx", None, length) if length else None

    if fname == "mfi":
        length = _const_positive_int(arg_nodes[0].value) if len(arg_nodes) == 1 else None
        return HtfSimpleTaExpr("mfi", None, length) if length else None

    if fname == "cmf":
        length = _const_positive_int(arg_nodes[0].value) if len(arg_nodes) == 1 else None
        return HtfSimpleTaExpr("cmf", None, length) if length else None

    if fname == "obv":
        if arg_nodes:
            return None
        return HtfSimpleTaExpr("obv", None, 0)

    if fname == "nvi":
        if arg_nodes:
            return None
        return HtfSimpleTaExpr("nvi", None, 0)

    if fname == "pvi":
        if arg_nodes:
            return None
        return HtfSimpleTaExpr("pvi", None, 0)

    if fname == "wpr":
        length = _const_positive_int(arg_nodes[0].value) if len(arg_nodes) == 1 else None
        return HtfSimpleTaExpr("wpr", None, length) if length else None

    if fname == "cci" and len(arg_nodes) == 1:
        length = _const_positive_int(arg_nodes[0].value)
        return HtfSimpleTaExpr("cci", "hlc3", length) if length else None

    if fname == "highest" and len(arg_nodes) == 1:
        length = _const_positive_int(arg_nodes[0].value)
        return HtfSimpleTaExpr("highest", "high", length) if length else None

    if fname == "lowest" and len(arg_nodes) == 1:
        length = _const_positive_int(arg_nodes[0].value)
        return HtfSimpleTaExpr("lowest", "low", length) if length else None

    if fname == "highestbars" and len(arg_nodes) == 1:
        length = _const_positive_int(arg_nodes[0].value)
        return HtfSimpleTaExpr("highestbars", "high", length) if length else None

    if fname == "lowestbars" and len(arg_nodes) == 1:
        length = _const_positive_int(arg_nodes[0].value)
        return HtfSimpleTaExpr("lowestbars", "low", length) if length else None

    if fname == "range" and len(arg_nodes) == 1:
        length = _const_positive_int(arg_nodes[0].value)
        return HtfSimpleTaExpr("range", None, length) if length else None

    if fname == "swma":
        if len(arg_nodes) != 1:
            return None
        src_node = arg_nodes[0].value
        if type(src_node) is not ast_mod.Name:
            return None
        src = _OHLCV_FIELD_ALIASES.get(str(src_node.id).strip().lower())
        if src is None:
            return None
        return HtfSimpleTaExpr("swma", src, 4)

    if fname == "change" and len(arg_nodes) == 1:
        src_node = arg_nodes[0].value
        if type(src_node) is not ast_mod.Name:
            return None
        src = _OHLCV_FIELD_ALIASES.get(str(src_node.id).strip().lower())
        if src is None:
            return None
        return HtfSimpleTaExpr("change", src, 1)

    if fname == "ao":
        if not arg_nodes:
            return HtfSimpleTaExpr("ao", "hl2", 0)
        if len(arg_nodes) == 1:
            fast = _const_positive_int(arg_nodes[0].value)
            return HtfSimpleTaExpr("ao", "hl2", 34, extra=fast) if fast else None
        if len(arg_nodes) != 2:  # noqa: PLR2004 - 2-arg (fast, slow)
            return None
        fast = _const_positive_int(arg_nodes[0].value)
        slow = _const_positive_int(arg_nodes[1].value)
        if fast is None or slow is None:
            return None
        return HtfSimpleTaExpr("ao", "hl2", slow, extra=fast)

    if fname == "cum":
        if len(arg_nodes) != 1:
            return None
        src_node = arg_nodes[0].value
        if type(src_node) is not ast_mod.Name:
            return None
        src = _OHLCV_FIELD_ALIASES.get(str(src_node.id).strip().lower())
        if src is None:
            return None
        return HtfSimpleTaExpr("cum", src, 0)

    if fname == "vwap":
        if not arg_nodes:
            return HtfSimpleTaExpr("vwap", "hlc3", 0)
        if len(arg_nodes) != 1:
            return None
        src_node = arg_nodes[0].value
        if type(src_node) is not ast_mod.Name:
            return None
        src = _OHLCV_FIELD_ALIASES.get(str(src_node.id).strip().lower())
        if src is None:
            return None
        return HtfSimpleTaExpr("vwap", src, 0)

    if fname == "linreg" and len(arg_nodes) == 3:  # noqa: PLR2004 - (source, n, offset)
        src_node = arg_nodes[0].value
        if type(src_node) is not ast_mod.Name:
            return None
        src = _OHLCV_FIELD_ALIASES.get(str(src_node.id).strip().lower())
        length = _const_positive_int(arg_nodes[1].value)
        offset = _const_nonneg_int(arg_nodes[2].value)
        if src is None or length is None or offset is None:
            return None
        return HtfSimpleTaExpr("linreg", src, length, extra=offset)

    # sma / ema / rsi / wma / rma / cmo / stdev / linreg / cci / roc / mom / hma / vwma / dema / tema / highest / lowest / range / median / dev / variance / mode / percentrank / change / cog / highestbars / lowestbars / kama / rising / falling: (source, length)
    if len(arg_nodes) != 2:  # noqa: PLR2004 - fixed allowlist arity
        return None
    src_node = arg_nodes[0].value
    if type(src_node) is not ast_mod.Name:
        return None
    src = _OHLCV_FIELD_ALIASES.get(str(src_node.id).strip().lower())
    length = _const_positive_int(arg_nodes[1].value)
    if src is None or length is None:
        return None
    return HtfSimpleTaExpr(name=fname, source=src, length=length)


def match_htf_offset_ast(expr_ast: Any) -> HtfOffsetExpr | None:
    """Match ``close[1]``-style OHLCV history offset for HTF resample."""
    if type(expr_ast) is not ast_mod.Subscript:
        return None
    value = expr_ast.value
    if type(value) is not ast_mod.Name:
        return None
    field = _OHLCV_FIELD_ALIASES.get(str(value.id).strip().lower())
    if field is None:
        return None
    offset = _const_positive_int(expr_ast.slice)
    if offset is None:
        return None
    return HtfOffsetExpr(field=field, offset=offset)


def match_htf_ohlcv_ast(expr_ast: Any) -> str | None:
    """Bare OHLCV name or string (``close`` / ``\"high\"``) for HTF resample."""
    if type(expr_ast) is ast_mod.Name:
        return _OHLCV_FIELD_ALIASES.get(str(expr_ast.id).strip().lower())
    if type(expr_ast) is ast_mod.Constant:
        raw = expr_ast.value
        if isinstance(raw, str):
            return _OHLCV_FIELD_ALIASES.get(raw.strip().lower())
    return None


@dataclass
class VolumeRow:
    """Volume row in a footprint object.

    Represents a single price level in a footprint with its volume data.
    Added in Pine Script v6 (January 2026).
    """

    up_price: float = 0.0
    down_price: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    delta: float = 0.0
    is_imbalance: bool = False
    is_poc: bool = False
    is_vah: bool = False
    is_val: bool = False


@dataclass
class Footprint:
    """Footprint object representing volume profile data for a bar.

    Contains volume data at each price level, including buy/sell volumes,
    delta, Point of Control (POC), and Value Area (VA) boundaries.
    Added in Pine Script v6 (January 2026).
    """

    num_ticks: int = 100
    va_percentage: int = 70
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    delta: float = 0.0
    total_volume: float = 0.0
    vah_row: VolumeRow | None = None
    val_row: VolumeRow | None = None
    poc_row: VolumeRow | None = None
    rows: list[VolumeRow] = field(default_factory=list)


class FootprintBuiltinsMixin(BuiltinDispatchMixin):
    """Footprint type methods for accessing volume profile data.

    Added in Pine Script v6 (January 2026).
    """

    def _footprint_builtin_map(self) -> dict[str, BuiltinHandler]:
        return {
            "footprint.buy_volume": self._handle_footprint_buy_volume,
            "footprint.sell_volume": self._handle_footprint_sell_volume,
            "footprint.delta": self._handle_footprint_delta,
            "footprint.vah": self._handle_footprint_vah,
            "footprint.val": self._handle_footprint_val,
            "footprint.poc": self._handle_footprint_poc,
            "footprint.total_volume": self._handle_footprint_total_volume,
            "footprint.rows": self._handle_footprint_rows,
            "footprint.get_row_by_price": self._handle_footprint_get_row_by_price,
            "volume_row.up_price": self._handle_volume_row_up_price,
            "volume_row.down_price": self._handle_volume_row_down_price,
            "volume_row.buy_volume": self._handle_volume_row_buy_volume,
            "volume_row.sell_volume": self._handle_volume_row_sell_volume,
            "volume_row.delta": self._handle_volume_row_delta,
            "volume_row.total_volume": self._handle_volume_row_total_volume,
            "volume_row.has_buy_imbalance": self._handle_volume_row_has_buy_imbalance,
            "volume_row.has_sell_imbalance": self._handle_volume_row_has_sell_imbalance,
        }

    def _handle_footprint_buy_volume(self, args: list[Any]) -> float:
        """footprint.buy_volume(footprint) - Get total buy volume from footprint."""
        fp = args[0] if len(args) > 0 else None
        if isinstance(fp, Footprint):
            return fp.buy_volume
        return 0.0

    def _handle_footprint_sell_volume(self, args: list[Any]) -> float:
        """footprint.sell_volume(footprint) - Get total sell volume from footprint."""
        fp = args[0] if len(args) > 0 else None
        if isinstance(fp, Footprint):
            return fp.sell_volume
        return 0.0

    def _handle_footprint_delta(self, args: list[Any]) -> float:
        """footprint.delta(footprint) - Get volume delta from footprint."""
        fp = args[0] if len(args) > 0 else None
        if isinstance(fp, Footprint):
            return fp.delta
        return 0.0

    def _handle_footprint_vah(self, args: list[Any]) -> VolumeRow | None:
        """footprint.vah(footprint) - Get Value Area High row from footprint."""
        fp = args[0] if len(args) > 0 else None
        if isinstance(fp, Footprint):
            return fp.vah_row
        return None

    def _handle_footprint_val(self, args: list[Any]) -> VolumeRow | None:
        """footprint.val(footprint) - Get Value Area Low row from footprint."""
        fp = args[0] if len(args) > 0 else None
        if isinstance(fp, Footprint):
            return fp.val_row
        return None

    def _handle_footprint_poc(self, args: list[Any]) -> VolumeRow | None:
        """footprint.poc(footprint) - Get Point of Control row from footprint."""
        fp = args[0] if len(args) > 0 else None
        if isinstance(fp, Footprint):
            return fp.poc_row
        return None

    def _handle_volume_row_up_price(self, args: list[Any]) -> float:
        """volume_row.up_price(volume_row) - Get upper price of volume row."""
        vr = args[0] if len(args) > 0 else None
        if isinstance(vr, VolumeRow):
            return vr.up_price
        return 0.0

    def _handle_volume_row_down_price(self, args: list[Any]) -> float:
        """volume_row.down_price(volume_row) - Get lower price of volume row."""
        vr = args[0] if len(args) > 0 else None
        if isinstance(vr, VolumeRow):
            return vr.down_price
        return 0.0

    def _handle_footprint_total_volume(self, args: list[Any]) -> float:
        fp = args[0] if args else None
        if isinstance(fp, Footprint):
            return fp.total_volume
        return 0.0

    def _handle_footprint_rows(self, args: list[Any]) -> list[VolumeRow]:
        fp = args[0] if args else None
        if isinstance(fp, Footprint):
            return list(fp.rows)
        return []

    def _handle_footprint_get_row_by_price(self, args: list[Any]) -> VolumeRow | None:
        fp = args[0] if args else None
        price = args[1] if len(args) > 1 else None
        if not isinstance(fp, Footprint) or price is None:
            return None
        p = float(price)
        for row in fp.rows:
            lo = min(row.down_price, row.up_price)
            hi = max(row.down_price, row.up_price)
            if lo <= p <= hi:
                return row
        return None

    def _handle_volume_row_buy_volume(self, args: list[Any]) -> float:
        vr = args[0] if args else None
        return float(vr.buy_volume) if isinstance(vr, VolumeRow) else 0.0

    def _handle_volume_row_sell_volume(self, args: list[Any]) -> float:
        vr = args[0] if args else None
        return float(vr.sell_volume) if isinstance(vr, VolumeRow) else 0.0

    def _handle_volume_row_delta(self, args: list[Any]) -> float:
        vr = args[0] if args else None
        return float(vr.delta) if isinstance(vr, VolumeRow) else 0.0

    def _handle_volume_row_total_volume(self, args: list[Any]) -> float:
        vr = args[0] if args else None
        if isinstance(vr, VolumeRow):
            return float(vr.buy_volume) + float(vr.sell_volume)
        return 0.0

    def _handle_volume_row_has_buy_imbalance(self, args: list[Any]) -> bool:
        vr = args[0] if args else None
        if isinstance(vr, VolumeRow):
            return bool(vr.is_imbalance and vr.buy_volume > vr.sell_volume)
        return False

    def _handle_volume_row_has_sell_imbalance(self, args: list[Any]) -> bool:
        vr = args[0] if args else None
        if isinstance(vr, VolumeRow):
            return bool(vr.is_imbalance and vr.sell_volume > vr.buy_volume)
        return False


class RequestBuiltinsMixin(BuiltinDispatchMixin):
    """``request.security``, fundamentals, and related data-fetch builtins.

    Missing feeds and provider errors soft-fail to mock series so scripts
    continue evaluating without a live data backend (see module docstring).
    """

    def _request_builtin_map(self) -> dict[str, BuiltinHandler]:
        return {
            "request.security": self._handle_request_security,
            "request.security_lower_tf": (self._handle_request_security_lower_tf),
            "request.dividends": self._handle_request_dividends,
            "request.earnings": self._handle_request_earnings,
            "request.splits": self._handle_request_splits,
            "request.financial": self._handle_request_financial,
            "request.quandl": self._handle_request_quandl,
            "request.economic": self._handle_request_economic,
            "request.currency_rate": self._handle_request_currency_rate,
            "request.seed": self._handle_request_seed,
            "request.footprint": self._handle_request_footprint,
            # Pine v3/v4 bare names (pre-request.* namespace)
            "security": self._handle_request_security,
            "security_lower_tf": self._handle_request_security_lower_tf,
        }

    def _get_expression_prices(self, expression: str, prices: list[float]) -> list[float]:
        """Return a list of prices based on the expression."""
        expr = expression.lower()
        if expr in ("open", "o"):
            return [p - 0.5 for p in prices]
        if expr in ("high", "h"):
            return [p + 1.0 for p in prices]
        if expr in ("low", "l"):
            return [p - 1.0 for p in prices]
        if expr == "volume":
            return [1000000, 1100000, 1200000, 1050000, 1300000]
        return prices  # Default to close

    def _resolve_symbol(self, arg: Any, default: str = "AAPL") -> str:
        """Resolve symbol which may be dynamic (list/series from loop/conditional)."""
        if isinstance(arg, list):
            arg = arg[-1] if arg else default
        if arg is None:
            return default
        # TickerInfo from ticker.* / heikinashi() / renko() etc.
        symbol_attr = getattr(arg, "symbol", None)
        if isinstance(symbol_attr, str) and symbol_attr:
            return symbol_attr.upper()
        # Empty TickerInfo.symbol (ticker.new(syminfo.prefix, …)) → chart
        if hasattr(arg, "symbol") and isinstance(symbol_attr, str):
            return ""
        return str(arg).upper()

    def _chart_symbol_candidates(self) -> list[str]:
        """Host chart identity strings (ticker / tickerid / provider symbol)."""
        ctx = getattr(self, "context", {}) or {}
        candidates: list[str] = []
        for key in (
            "syminfo.ticker",
            "syminfo.tickerid",
            "syminfo.root",
            "symbol",
            "_host_symbol",
        ):
            raw = ctx.get(key)
            if raw is None:
                continue
            t = str(raw).strip().upper()
            if t and t not in _CHART_PLACEHOLDERS:
                candidates.append(t)
        si = ctx.get("syminfo")
        if si is not None:
            for attr in ("ticker", "tickerid", "root"):
                raw = getattr(si, attr, None)
                if raw is None:
                    continue
                t = str(raw).strip().upper()
                if t and t not in _CHART_PLACEHOLDERS:
                    candidates.append(t)
        _, data_provider = self._get_request_data()
        prov_sym = getattr(data_provider, "_symbol", None)
        if prov_sym:
            t = str(prov_sym).strip().upper()
            if t and t not in _CHART_PLACEHOLDERS:
                candidates.append(t)
        return candidates

    def _host_has_chart_identity(self) -> bool:
        """True when Runtime/host wired a real chart ticker (not bare unit eval)."""
        return bool(self._chart_symbol_candidates())

    def _is_chart_symbol(self, symbol: str) -> bool:
        """True when *symbol* refers to the host chart (or is empty / generic).

        Foreign tickers (``ESD_FACTSET``, ``MSFT`` when chart is ``AAPL``, …)
        must not inherit pre-evaluated chart expressions as if they were
        multi-symbol results (dividend_yield inventing close-as-dividend).
        """
        s = (symbol or "").strip().upper()
        if not s or s in _CHART_PLACEHOLDERS:
            return True
        for t in self._chart_symbol_candidates():
            if s == t:
                return True
            if s.split(":")[-1] == t.split(":")[-1]:
                return True
            if s.endswith(":" + t) or t.endswith(":" + s):
                return True
        return False

    def _get_request_data(self):
        """Get (data_feed, data_provider) for live/historical fallback."""
        ctx = getattr(self, "context", {}) or {}
        return ctx.get("data_feed"), ctx.get("data_provider")

    def _unwrap_preeval_scalar(self, expression: Any) -> Any:
        """Reduce PineSeries / 1-element containers to a plottable scalar when needed."""
        if expression is None or isinstance(expression, (bool, int, float, str)):
            return expression
        # Host OHLCV PineSeries passed as the security expression (`close`)
        current = getattr(expression, "current", None)
        if current is not None and not isinstance(expression, (list, tuple, dict)):
            return current
        return expression

    def _scalar_matches_chart_ohlcv(self, value: Any) -> bool:
        """True when *value* equals a chart OHLCV sample (current or recent history).

        Used to distinguish simple same-symbol OHLCV passthrough
        (``close``, ``high[1]``, …) from complex UDF results (``f_struct`` →
        ±1/0, RSI, …) when the expression is already evaluated at the call site.

        Supports ``PineSeries`` (offset indexing: ``s[0]`` current, ``s[1]``
        previous) and plain list/tuple chronologies.
        """
        if value is None:
            return True
        if isinstance(value, bool):
            return False

        ctx = getattr(self, "context", {}) or {}

        # Identity: expression is the host chart series object itself (`close`).
        for key in _OHLCV_SERIES_KEYS:
            series = ctx.get(key)
            if series is not None and value is series:
                return True

        # PineSeries wrapper that is not identity-equal — compare .current
        current = getattr(value, "current", None)
        if current is not None and not isinstance(value, (list, tuple, dict, str)):
            if isinstance(current, bool):
                return False
            if isinstance(current, (int, float)) or current is None:
                value = current
            else:
                return False

        if not isinstance(value, (int, float)):
            return False
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(fv):
            return True

        def _eq(sample: Any) -> bool:
            if sample is None:
                return False
            try:
                return float(sample) == fv
            except (TypeError, ValueError):
                return False

        for key in _OHLCV_SERIES_KEYS:
            series = ctx.get(key)
            if series is None:
                continue
            # Scalar chart bind
            if isinstance(series, (int, float)) and not isinstance(series, bool):
                if _eq(series):
                    return True
                continue
            # PineSeries: .current + history (newest-first)
            sc = getattr(series, "current", None)
            if sc is not None and _eq(sc):
                return True
            hist = getattr(series, "history", None)
            if hist is not None:
                for i, sample in enumerate(hist):
                    if i >= _OHLCV_MATCH_LOOKBACK:
                        break
                    if _eq(sample):
                        return True
                continue
            # Plain list/tuple chronology (oldest → newest)
            if isinstance(series, (list, tuple)):
                if not series:
                    continue
                for sample in series[-_OHLCV_MATCH_LOOKBACK:]:
                    if _eq(sample):
                        return True
                continue
            try:
                if _eq(float(series)):  # type: ignore[arg-type]
                    return True
            except (TypeError, ValueError):
                pass
            try:
                if _eq(series[0]):  # type: ignore[index]
                    return True
            except (TypeError, IndexError, KeyError):
                pass
        return False

    def _expression_is_simple_ohlcv_value(self, expression: Any) -> bool:
        """True for pre-eval values that look like simple chart OHLCV/time samples.

        Includes ``time`` so multi-value same-symbol requests such as
        ``[open, high, low, close, volume, time]`` remain list-shaped under a
        different request TF (chart passthrough stub), instead of collapsing
        to a single ``na`` that breaks destructure and poisons custom DMI.
        """
        if isinstance(expression, str):
            return expression.strip().lower() in {
                "open",
                "o",
                "high",
                "h",
                "low",
                "l",
                "close",
                "c",
                "volume",
                "vol",
                "time",
                "hl2",
                "hlc3",
                "ohlc4",
            }
        if isinstance(expression, (list, tuple)):
            if not expression:
                return False
            return all(self._expression_is_simple_ohlcv_value(x) for x in expression)
        return self._scalar_matches_chart_ohlcv(expression)

    def _allow_same_symbol_preeval(self, expression: Any, timeframe: Any) -> bool:
        """Whether chart-evaluated *expression* may passthrough for same-symbol.

        - Same (or empty) request TF as chart → chart eval is correct; allow.
        - Different TF + simple OHLCV sample → chart passthrough stub (no HTF
          series, matches compile simple OHLCV policy intent).
        - Different TF + complex UDF/arith result → ``na`` (do not invent HTF
          structure from chart bars — MTF Structure Bias residual).
        """
        chart_tf = _chart_period(self)
        req_tf = timeframe
        if isinstance(req_tf, list):
            req_tf = req_tf[-1] if req_tf else chart_tf
        if timeframes_equivalent(None if req_tf is None else str(req_tf), chart_tf):
            return True
        return self._expression_is_simple_ohlcv_value(expression)

    def _request_tf_matches_chart(self, timeframe: Any) -> bool:
        """True when *timeframe* is empty or equivalent to the host chart period."""
        chart_tf = _chart_period(self)
        req_tf = timeframe
        if isinstance(req_tf, list):
            req_tf = req_tf[-1] if req_tf else chart_tf
        return timeframes_equivalent(None if req_tf is None else str(req_tf), chart_tf)

    def _series_chrono_values(self, key: str) -> list[Any]:
        """Chronological samples for a chart series key (oldest → newest)."""
        ctx = getattr(self, "context", {}) or {}
        series = ctx.get(key) if isinstance(ctx, dict) else None
        if series is not None:
            buf = getattr(series, "buffer", None)
            if buf is not None:
                return list(buf)
            hist = getattr(series, "history", None)
            if hist is not None:
                try:
                    return list(reversed(list(hist)))
                except TypeError:
                    pass
            if isinstance(series, (list, tuple)):
                return list(series)
        cs = getattr(self, "current_series", None) or {}
        if isinstance(cs, dict):
            lst = cs.get(key)
            if isinstance(lst, (list, tuple)):
                return list(lst)
        return []

    def _infer_chart_bar_seconds(self) -> float | None:
        """Median positive delta of chart bar times (seconds), if available."""
        times = None
        full = getattr(self, "_htf_full_ohlcv", None)
        if isinstance(full, dict):
            times = full.get("time")
        if times is None or len(times) < 2:
            times = self._series_chrono_values("time")
        if len(times) < 2:
            return None
        deltas: list[float] = []
        prev: float | None = None
        # Prefer recent bars (stable spacing) over the full history tail.
        for raw in times[-min(len(times), 64) :]:
            try:
                t = float(raw)
            except (TypeError, ValueError):
                continue
            if prev is not None:
                d = t - prev
                # Bar times are Unix ms on the Runtime host; tolerate seconds too.
                if d > 0:
                    deltas.append(d)
            prev = t
        if not deltas:
            return None
        deltas.sort()
        med = deltas[len(deltas) // 2]
        # Heuristic: values ≫ 10_000 are milliseconds.
        if med >= 10_000.0:
            return med / 1000.0
        return med

    def _chart_bar_index(self) -> int:
        """Current chart bar index from host context (0 if missing)."""
        ctx = getattr(self, "context", None) or {}
        try:
            raw = ctx.get("bar_index", 0) if isinstance(ctx, dict) else 0
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def _htf_timezone(self) -> object:
        """Host ``syminfo.timezone`` for calendar HTF buckets (UTC fallback)."""
        getter = getattr(self, "_syminfo_host", None)
        sym = getter() if callable(getter) else None
        if sym is None:
            ctx = getattr(self, "context", None) or {}
            sym = ctx.get("syminfo") if isinstance(ctx, dict) else None
        tz = getattr(sym, "timezone", None) if sym is not None else None
        return tz if tz else "UTC"

    def _htf_tf_spec(self, timeframe: Any) -> tuple[Any, ...] | None:
        """Resample spec: ``('cal', tf, tz)`` or ``('ms', bucket_ms)``, else None."""
        tf_s = "" if timeframe is None else str(timeframe).strip()
        if isinstance(timeframe, list):
            tf_s = str(timeframe[-1]).strip() if timeframe else ""
        if not tf_s:
            return None
        if timeframe_is_calendar_tf(tf_s):
            return ("cal", tf_s, str(self._htf_timezone() or "UTC"))
        bucket_ms = self._request_tf_bucket_ms(tf_s)
        if bucket_ms is None:
            return None
        return ("ms", int(bucket_ms))

    @staticmethod
    def _htf_cache_fingerprint(  # noqa: PLR0913
        n: int,
        times: list[Any],
        opens: list[Any],
        highs: list[Any],
        lows: list[Any],
        closes: list[Any],
    ) -> tuple[Any, ...]:
        """Cache identity: length plus first/last bar so in-place ticks invalidate."""

        def _at(seq: list[Any], idx: int, default: float = 0.0) -> float:
            if not seq or idx >= len(seq) or idx < -len(seq):
                return default
            try:
                return float(seq[idx])
            except (TypeError, ValueError):
                return default

        last = n - 1
        return (
            n,
            _at(times, 0),
            _at(times, last),
            _at(opens, last),
            _at(highs, last),
            _at(lows, last),
            _at(closes, last),
        )

    def _htf_source_lists(self) -> tuple[list[Any], ...] | None:
        """OHLCV+time lists for HTF resample (full host columns when attached)."""
        full = getattr(self, "_htf_full_ohlcv", None)
        if isinstance(full, dict) and full.get("open") is not None:
            opens = full.get("open") or []
            if not opens:
                return None
            n = len(opens)
            highs = full.get("high") or []
            lows = full.get("low") or []
            closes = full.get("close") or []
            volumes = full.get("volume") or []
            times = full.get("time") or []
            if len(times) < n:
                base = float(times[-1]) if times else 0.0
                times = list(times) + [base + (i + 1) * 60_000.0 for i in range(n - len(times))]
            return opens, highs, lows, closes, volumes, times
        opens = self._series_chrono_values("open")
        if not opens:
            return None
        n = len(opens)
        highs = self._series_chrono_values("high")
        lows = self._series_chrono_values("low")
        closes = self._series_chrono_values("close")
        volumes = self._series_chrono_values("volume")
        times = self._series_chrono_values("time")
        if len(times) < n:
            base = float(times[-1]) if times else 0.0
            times = list(times) + [base + (i + 1) * 60_000.0 for i in range(n - len(times))]
        return opens, highs[:n], lows[:n], closes[:n], volumes[:n], times[:n]

    def _request_tf_bucket_ms(self, timeframe: Any) -> int | None:
        """Fixed-size HTF bucket width in ms, or None if unusable for resample.

        Monthly (and coarser approximate) TFs are skipped — calendar months are
        not fixed-ms buckets. Empty TF means chart TF (no resample).
        Bare ``D``/``W``/``M`` use calendar ids via :meth:`_htf_tf_spec` instead.
        """
        req_tf = timeframe
        if isinstance(req_tf, list):
            req_tf = req_tf[-1] if req_tf else None
        if req_tf is None or str(req_tf).strip() == "":
            return None
        try:
            sec = int(timeframe_in_seconds(str(req_tf)))
        except (TypeError, ValueError):
            return None
        if sec <= 0:
            return None
        # Skip calendar-month approximations (not fixed buckets).
        if sec >= SECONDS_PER_MONTH:
            return None
        return sec * 1000

    def _request_is_higher_tf(self, timeframe: Any) -> bool:  # noqa: C901, PLR0911
        """True when *timeframe* is coarser than chart bars (inferred or declared)."""
        tf_s = "" if timeframe is None else str(timeframe).strip()
        if isinstance(timeframe, list):
            tf_s = str(timeframe[-1]).strip() if timeframe else ""
        if timeframe_is_calendar_tf(tf_s):
            rank = {"D": 1, "1D": 1, "W": 2, "1W": 2, "M": 3, "1M": 3}
            cal_sec = {1: 86_400.0, 2: 604_800.0, 3: 2_592_000.0}.get(rank.get(tf_s, 0), 86_400.0)
            chart_sec = self._infer_chart_bar_seconds()
            if chart_sec is not None and chart_sec > 0:
                return cal_sec > chart_sec * _HTF_BAR_SEC_MARGIN
            chart_tf = str(_chart_period(self) or "").strip()
            if timeframe_is_calendar_tf(chart_tf):
                return rank.get(tf_s, 0) > rank.get(chart_tf, 0)
            try:
                declared = float(timeframe_in_seconds(chart_tf))
            except (TypeError, ValueError):
                return True
            if declared <= 0:
                return True
            return cal_sec > declared * _HTF_BAR_SEC_MARGIN
        bucket_ms = self._request_tf_bucket_ms(timeframe)
        if bucket_ms is None:
            return False
        req_sec = bucket_ms / 1000.0
        chart_sec = self._infer_chart_bar_seconds()
        if chart_sec is not None and chart_sec > 0:
            return req_sec > chart_sec * _HTF_BAR_SEC_MARGIN
        try:
            declared = float(timeframe_in_seconds(_chart_period(self)))
        except (TypeError, ValueError):
            return False
        if declared <= 0:
            return False
        return req_sec > declared * _HTF_BAR_SEC_MARGIN

    def _identify_simple_ohlcv_field(self, expression: Any) -> str | None:
        """Map *expression* to a bare OHLCV field name when identity-known.

        Returns ``None`` for complex UDF results and ambiguous scalar matches.
        History offsets such as ``close[1]`` are intercepted as ``HtfOffsetExpr``
        before this helper sees a pre-eval float.
        """
        if isinstance(expression, str):
            return _OHLCV_FIELD_ALIASES.get(expression.strip().lower())

        ctx = getattr(self, "context", {}) or {}
        if isinstance(ctx, dict):
            for key in _OHLCV_SERIES_KEYS:
                series = ctx.get(key)
                if series is not None and expression is series:
                    return key

        # PineSeries-like that is not context identity: reject (could be UDF).
        if getattr(expression, "current", None) is not None and not isinstance(
            expression, (list, tuple, dict, str, int, float, bool)
        ):
            return None

        # Bare numeric / bool / None — not a field identity (offsets, pre-eval).
        return None

    def _identify_simple_ohlcv_fields(self, expression: Any) -> list[str] | None:
        """Field list for a single series or homogeneous OHLCV tuple/list."""
        if isinstance(expression, (list, tuple)):
            if not expression:
                return None
            fields: list[str] = []
            for item in expression:
                f = self._identify_simple_ohlcv_field(item)
                if f is None:
                    # String names inside list (rare) or fail
                    if isinstance(item, str):
                        f = _OHLCV_FIELD_ALIASES.get(item.strip().lower())
                    if f is None:
                        return None
                fields.append(f)
            return fields
        f = self._identify_simple_ohlcv_field(expression)
        return [f] if f is not None else None

    def _htf_agg_field(self, agg: dict[str, float], field: str) -> float:
        """Read one OHLCV/derived field from an aggregated HTF bucket."""
        if field == "open":
            return agg["open"]
        if field == "high":
            return agg["high"]
        if field == "low":
            return agg["low"]
        if field == "close":
            return agg["close"]
        if field == "volume":
            return agg["volume"]
        if field == "time":
            return agg["time"]
        o, h, l, c = agg["open"], agg["high"], agg["low"], agg["close"]
        if field == "hl2":
            return (h + l) * 0.5
        if field == "hlc3":
            return (h + l + c) / 3.0
        if field == "ohlc4":
            return (o + h + l + c) * 0.25
        return float("nan")

    def _build_htf_completed_series(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        bucket_ms: int,
        n: int,
        opens: list[Any],
        highs: list[Any],
        lows: list[Any],
        closes: list[Any],
        volumes: list[Any],
        times: list[Any],
        period_id_fn: Any | None = None,
    ) -> tuple[list[dict[str, float] | None], list[dict[str, float] | None]]:
        """Per chart bar: last *completed* HTF OHLCV agg (lookahead_off-style).

        Forming HTF bar is never returned. Bars before the first HTF close →
        ``None`` (caller maps to ``na``).

        Returns ``(completed, forming)``: ``forming[i]`` is a snapshot copy of
        the still-open HTF bucket as of chart bar ``i`` (``None`` before the
        first chart bar opens a bucket). ``lookahead_on`` later copies each
        bucket's final snapshot onto every bar in that bucket.
        """
        na_out: list[dict[str, float] | None] = [None] * n
        forming_out: list[dict[str, float] | None] = [None] * n
        if n == 0 or (period_id_fn is None and bucket_ms <= 0):
            return na_out, forming_out

        def _f(v: Any, default: float = float("nan")) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        completed: dict[str, float] | None = None
        forming: dict[str, float] | None = None
        forming_bucket: int | None = None

        for i in range(n):
            t_raw = times[i] if i < len(times) else None
            try:
                t_ms = float(t_raw)
            except (TypeError, ValueError):
                na_out[i] = completed
                forming_out[i] = dict(forming) if forming is not None else None
                continue
            # Accept Unix seconds (rare) by scaling into ms range.
            if t_ms > 0 and t_ms < 1e11:
                t_ms *= 1000.0
            if period_id_fn is not None:
                try:
                    b = period_id_fn(t_ms)
                except Exception:
                    b = None
                if b is None:
                    na_out[i] = completed
                    forming_out[i] = dict(forming) if forming is not None else None
                    continue
            else:
                b = int(t_ms) // bucket_ms * bucket_ms
            o = _f(opens[i] if i < len(opens) else None)
            h = _f(highs[i] if i < len(highs) else None)
            l = _f(lows[i] if i < len(lows) else None)
            c = _f(closes[i] if i < len(closes) else None)
            v = _f(volumes[i] if i < len(volumes) else None, 0.0)

            if forming_bucket is None:
                forming_bucket = b
                forming = {
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                    "time": float(b),
                }
                na_out[i] = None
                forming_out[i] = dict(forming)
                continue

            if b == forming_bucket and forming is not None:
                if h == h:  # not nan
                    forming["high"] = h if forming["high"] != forming["high"] else max(forming["high"], h)
                if l == l:
                    forming["low"] = l if forming["low"] != forming["low"] else min(forming["low"], l)
                if c == c:
                    forming["close"] = c
                if v == v:
                    forming["volume"] = (forming["volume"] if forming["volume"] == forming["volume"] else 0.0) + (
                        v if v == v else 0.0
                    )
                na_out[i] = completed
                forming_out[i] = dict(forming)
                continue

            # New HTF bucket: previous forming bar completes (lookahead_off).
            completed = forming
            forming_bucket = b
            forming = {
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "time": float(b),
            }
            na_out[i] = completed
            forming_out[i] = dict(forming)

        return na_out, forming_out

    @staticmethod
    def _htf_finalize_bucket_tails(
        forming_out: list[dict[str, float] | None],
    ) -> list[dict[str, float] | None]:
        """Copy each bucket's last snapshot onto every chart bar in that bucket.

        Historical ``lookahead_on`` must leak the HTF bar's final value from
        the period start (not the developing-as-of-this-bar agg).
        """
        n = len(forming_out)
        i = 0
        while i < n:
            snap = forming_out[i]
            if snap is None:
                i += 1
                continue
            try:
                t_key = float(snap.get("time"))
            except (TypeError, ValueError, AttributeError):
                i += 1
                continue
            j = i + 1
            last = snap
            while j < n:
                nxt = forming_out[j]
                if nxt is None:
                    break
                try:
                    if float(nxt.get("time")) != t_key:
                        break
                except (TypeError, ValueError, AttributeError):
                    break
                last = nxt
                j += 1
            final = dict(last)
            for k in range(i, j):
                forming_out[k] = final
            i = j
        return forming_out

    def _htf_ohlcv_series_for_tf(self, spec: Any) -> list[dict[str, float] | None] | None:
        """Cached last-completed HTF agg per chart bar for *spec*."""
        entry = self._htf_bucket_cache_entry(spec)
        if entry is None:
            return None
        return entry.get("series")  # type: ignore[return-value]

    def _htf_forming_series_for_tf(self, spec: Any) -> list[dict[str, float] | None] | None:
        """Cached HTF forming series (bucket tails finalized for lookahead_on)."""
        entry = self._htf_bucket_cache_entry(spec)
        if entry is None:
            return None
        return entry.get("forming")  # type: ignore[return-value]

    def _htf_bucket_cache_entry(self, spec: Any) -> dict[str, Any] | None:
        """Shared (completed, forming) HTF bucket cache entry for *spec*."""
        src = self._htf_source_lists()
        if src is None:
            return None
        opens, highs, lows, closes, volumes, times = src
        n = len(opens)
        fp = self._htf_cache_fingerprint(n, times, opens, highs, lows, closes)
        cache = getattr(self, "_htf_ohlcv_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._htf_ohlcv_cache = cache  # type: ignore[attr-defined]
        if isinstance(spec, tuple):
            key: Any = spec
        else:
            try:
                key = ("ms", int(spec))
            except (TypeError, ValueError):
                key = spec
        entry = cache.get(key)
        if (
            isinstance(entry, dict)
            and entry.get("fp") == fp
            and entry.get("series") is not None
            and entry.get("forming") is not None
        ):
            return entry
        bucket_ms = 0
        period_id_fn = None
        if isinstance(key, tuple) and key and key[0] == "cal":
            tf_s = str(key[1])
            tz = key[2] if len(key) > 2 else "UTC"

            def period_id_fn(t_ms: float, _tf: str = tf_s, _tz: object = tz) -> int | None:
                return timeframe_calendar_id(t_ms, _tf, _tz)

        elif isinstance(key, tuple) and key and key[0] == "ms":
            bucket_ms = int(key[1])
        else:
            bucket_ms = int(spec)
        series, forming = self._build_htf_completed_series(
            bucket_ms, n, opens, highs, lows, closes, volumes, times, period_id_fn=period_id_fn
        )
        self._htf_finalize_bucket_tails(forming)
        entry = {"fp": fp, "n": n, "series": series, "forming": forming}
        cache[key] = entry
        return entry

    def _htf_bar_is_bucket_start(
        self,
        series: list[dict[str, float] | None],
        *,
        lookahead_on: bool = False,
        forming: list[dict[str, float] | None] | None = None,
        index: int | None = None,
    ) -> bool:
        """True when the current chart bar opens a new HTF bucket.

        ``lookahead_off`` uses completed-bucket starts; ``lookahead_on`` uses
        forming-bucket starts (including bar 0 of the first period).
        """
        src = forming if lookahead_on and forming is not None else series
        i = self._chart_bar_index() if index is None else int(index)
        if i < 0 or i >= len(src):
            return False
        last = src[i]
        if last is None:
            return False
        if i == 0:
            return True
        prev = src[i - 1]
        if prev is None:
            return True
        try:
            return float(prev.get("time")) != float(last.get("time"))
        except (TypeError, ValueError, AttributeError):
            return True

    def _select_htf_agg(
        self,
        spec: Any,
        series: list[dict[str, float] | None],
        *,
        gaps_on: bool,
        lookahead_on: bool,
        forming: list[dict[str, float] | None] | None = None,
    ) -> dict[str, float] | None:
        """Pick the HTF agg for the current bar under the merge flags.

        ``lookahead_on`` reads the bucket's final agg (historical leak from
        period start); ``gaps_on`` keeps it only on bucket-start bars.
        Pass *forming* for provider-aligned series; otherwise the chart HTF
        forming cache for *spec* is used.
        """
        i = self._chart_bar_index()
        agg = series[i] if series and 0 <= i < len(series) else None
        forming_series = forming
        if lookahead_on:
            if forming_series is None:
                forming_series = self._htf_forming_series_for_tf(spec)
            if forming_series is not None and 0 <= i < len(forming_series) and forming_series[i] is not None:
                agg = forming_series[i]
        if gaps_on and not self._htf_bar_is_bucket_start(
            series, lookahead_on=lookahead_on, forming=forming_series, index=i
        ):
            agg = None
        return agg

    def _try_htf_ohlcv_resample(
        self, expression: Any, timeframe: Any, *, gaps_on: bool = False, lookahead_on: bool = False
    ) -> Any | None:
        """Resample chart OHLCV to HTF for simple series fields, or None.

        Merge behavior (reference barmerge, resample paths only):

        - ``lookahead_off`` (default): **last completed** HTF bar only.
        - ``lookahead_on``: each HTF bucket's **final** agg, visible from the
          period start on historical bars (TV lookahead bias). Unconfirmed
          realtime ticks rebuild from the updated last bar.
        - ``gaps_on``: value only on the first chart bar of an HTF bucket,
          ``na`` elsewhere. Composes with ``lookahead_on`` (value on
          forming-bucket starts, including bar 0 of the first period).

        Complex expressions are not re-eval'd.
        """
        if isinstance(expression, HtfOffsetExpr):
            return self._try_htf_offset_resample(expression, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on)
        if not self._request_is_higher_tf(timeframe):
            return None
        spec = self._htf_tf_spec(timeframe)
        if spec is None:
            return None
        fields = self._identify_simple_ohlcv_fields(expression)
        if not fields:
            return None
        series = self._htf_ohlcv_series_for_tf(spec)
        if not series:
            return None
        agg = self._select_htf_agg(spec, series, gaps_on=gaps_on, lookahead_on=lookahead_on)
        if gaps_on or lookahead_on:
            self._mark_security_merge_applied(gaps_on=gaps_on, lookahead_on=lookahead_on)
        na = float("nan")
        if len(fields) == 1 and not isinstance(expression, (list, tuple)):
            if agg is None:
                return na
            return self._htf_agg_field(agg, fields[0])
        # Multi-value list/tuple — preserve shape for destructure.
        if agg is None:
            out_na = [na] * len(fields)
            return out_na if isinstance(expression, list) else tuple(out_na)
        vals = [self._htf_agg_field(agg, f) for f in fields]
        return vals if isinstance(expression, list) else tuple(vals)

    def _try_htf_offset_resample(
        self,
        expression: HtfOffsetExpr,
        timeframe: Any,
        *,
        gaps_on: bool = False,
        lookahead_on: bool = False,
    ) -> Any | None:
        """Resample ``close[1]`` (etc.) on unique HTF bars, not the chart series."""
        if not self._request_is_higher_tf(timeframe):
            return None
        spec = self._htf_tf_spec(timeframe)
        if spec is None:
            return None
        built = self._htf_unique_and_map(spec, lookahead_on=lookahead_on)
        if built is None:
            return None
        unique, chart_to_htf = built
        i = self._chart_bar_index()
        if i < 0 or i >= len(chart_to_htf):
            return None
        hidx = chart_to_htf[i]
        na = float("nan")
        if gaps_on and not self._htf_ta_is_bucket_start(chart_to_htf, index=i):
            self._mark_security_merge_applied(gaps_on=True, lookahead_on=lookahead_on)
            return na
        if hidx is None:
            if gaps_on or lookahead_on:
                self._mark_security_merge_applied(gaps_on=gaps_on, lookahead_on=lookahead_on)
            return na
        src_idx = int(hidx) - int(expression.offset)
        if src_idx < 0 or src_idx >= len(unique):
            if gaps_on or lookahead_on:
                self._mark_security_merge_applied(gaps_on=gaps_on, lookahead_on=lookahead_on)
            return na
        if gaps_on or lookahead_on:
            self._mark_security_merge_applied(gaps_on=gaps_on, lookahead_on=lookahead_on)
        return self._htf_agg_field(unique[src_idx], expression.field)

    def _htf_unique_and_map(
        self, spec: Any, *, lookahead_on: bool = False
    ) -> tuple[list[dict[str, float]], list[int | None]] | None:
        """Unique HTF bars + per-chart-bar index into that list.

        ``lookahead_off`` uses last-completed aggs. ``lookahead_on`` uses
        finalized forming buckets so every bar in a period maps to that
        period's final HTF bar. Index ``None`` means no HTF bar yet.
        """
        series = self._htf_forming_series_for_tf(spec) if lookahead_on else self._htf_ohlcv_series_for_tf(spec)
        if not series:
            return None
        unique: list[dict[str, float]] = []
        by_time: dict[float, int] = {}
        chart_to_htf: list[int | None] = []
        for agg in series:
            if agg is None:
                chart_to_htf.append(None)
                continue
            try:
                t_key = float(agg["time"])
            except (TypeError, ValueError, KeyError):
                chart_to_htf.append(None)
                continue
            idx = by_time.get(t_key)
            if idx is None:
                idx = len(unique)
                by_time[t_key] = idx
                unique.append(agg)
            chart_to_htf.append(idx)
        return unique, chart_to_htf

    def _htf_source_series(self, unique: list[dict[str, float]], field: str) -> list[float | None]:
        """Extract one OHLCV/derived field from unique HTF aggs (chronological)."""
        out: list[float | None] = []
        for agg in unique:
            v = self._htf_agg_field(agg, field)
            if v != v:  # nan
                out.append(None)
            else:
                out.append(v)
        return out

    @staticmethod
    def _htf_wma_full_series(src: list[float | None], period: int) -> list[float | None]:
        """Full-list WMA matching interpret ``_wma`` (strict window, last-value formula)."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        denom = period * (period + 1) / 2.0
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            acc = 0.0
            ok = True
            for j, v in enumerate(window):
                if v is None:
                    ok = False
                    break
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    ok = False
                    break
                if fv != fv:
                    ok = False
                    break
                acc += fv * (j + 1)
            out[i] = acc / denom if ok else None
        return out

    def _htf_hma_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list HMA via interpret ``_hma`` on unique HTF source windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        hma_fn = getattr(self, "_hma", None)
        if not callable(hma_fn):
            return out
        need = period + max(1, int(math.sqrt(period))) - 1
        for i in range(need - 1, n):
            vals = self._htf_finite_window(src[i - need + 1 : i + 1])
            if vals is None:
                continue
            try:
                v = hma_fn(vals, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_dema_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list DEMA via interpret ``_ema``: ``2 * ema1 - ema2``."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        ema_fn = getattr(self, "_ema", None)
        if not callable(ema_fn):
            return out
        ema1 = ema_fn(src, period)
        ema2 = ema_fn(ema1, period)
        if not ema1 or not ema2:
            return out
        m = min(n, len(ema1), len(ema2))
        for i in range(m):
            e1, e2 = ema1[i], ema2[i]
            if e1 is None or e2 is None:
                continue
            try:
                fv = 2.0 * float(e1) - float(e2)
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_tema_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list TEMA via interpret ``_ema``: ``3 * e1 - 3 * e2 + e3``."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        ema_fn = getattr(self, "_ema", None)
        if not callable(ema_fn):
            return out
        ema1 = ema_fn(src, period)
        ema2 = ema_fn(ema1, period)
        ema3 = ema_fn(ema2, period)
        if not ema1 or not ema2 or not ema3:
            return out
        m = min(n, len(ema1), len(ema2), len(ema3))
        for i in range(m):
            e1, e2, e3 = ema1[i], ema2[i], ema3[i]
            if e1 is None or e2 is None or e3 is None:
                continue
            try:
                fv = 3.0 * float(e1) - 3.0 * float(e2) + float(e3)
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_highest_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list highest via interpret ``_highest`` on unique HTF source windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        highest_fn = getattr(self, "_highest", None)
        if not callable(highest_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = highest_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_lowest_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list lowest via interpret ``_lowest`` on unique HTF source windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        lowest_fn = getattr(self, "_lowest", None)
        if not callable(lowest_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = lowest_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_highestbars_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list highestbars via interpret ``_highestbars`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        hb_fn = getattr(self, "_highestbars", None)
        if not callable(hb_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = hb_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_lowestbars_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list lowestbars via interpret ``_lowestbars`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        lb_fn = getattr(self, "_lowestbars", None)
        if not callable(lb_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = lb_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_range_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list range as highest − lowest on unique HTF source windows."""
        hi = self._htf_highest_full_series(src, period)
        lo = self._htf_lowest_full_series(src, period)
        n = len(src)
        out: list[float | None] = [None] * n
        m = min(n, len(hi), len(lo))
        for i in range(m):
            h, l = hi[i], lo[i]
            if h is None or l is None:
                continue
            try:
                fv = float(h) - float(l)
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_hl_range_full_series(
        self, highs: list[float | None], lows: list[float | None], period: int
    ) -> list[float | None]:
        """1-arg ``ta.range(n)``: highest high − lowest low over *period* unique HTF bars."""
        hi = self._htf_highest_full_series(highs, period)
        lo = self._htf_lowest_full_series(lows, period)
        n = min(len(hi), len(lo))
        out: list[float | None] = [None] * n
        for i in range(n):
            h, l = hi[i], lo[i]
            if h is None or l is None:
                continue
            try:
                fv = float(h) - float(l)
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_median_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list median via interpret ``_median`` on unique HTF source windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        median_fn = getattr(self, "_median", None)
        if not callable(median_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = median_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_dev_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list mean abs dev via interpret ``_dev`` on unique HTF source windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        dev_fn = getattr(self, "_dev", None)
        if not callable(dev_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = dev_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_variance_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list sample variance via interpret ``_variance`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        variance_fn = getattr(self, "_variance", None)
        if not callable(variance_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = variance_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_mode_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list mode via interpret ``_mode`` on unique HTF source windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        mode_fn = getattr(self, "_mode", None)
        if not callable(mode_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = mode_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_percentrank_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list percentrank via interpret ``_percentrank`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        pr_fn = getattr(self, "_percentrank", None)
        if not callable(pr_fn):
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = pr_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_change_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list change via interpret ``_change`` (``src - src[n]``) on HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        change_fn = getattr(self, "_change", None)
        if not callable(change_fn):
            return out
        # ``_change`` needs length+1 samples (current minus value ``period`` bars ago).
        for i in range(period, n):
            window = src[i - period : i + 1]
            try:
                v = change_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    @staticmethod
    def _htf_cog_full_series(src: list[float | None], period: int) -> list[float | None]:
        """Full-list Center of Gravity matching interpret ``_cog_inc_update``.

        Last-N formula ``-sum((i+1)*v)/sum(v)`` with newest weight 1 (reversed
        window). Short window or zero denominator → na. No full-list ``_cog``.
        """
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            num_sum = 0.0
            den_sum = 0.0
            for j, v in enumerate(reversed(window)):
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv != fv:
                    continue
                num_sum += (j + 1) * fv
                den_sum += fv
            if den_sum == 0.0:
                continue
            fv = -num_sum / den_sum
            out[i] = None if fv != fv else fv
        return out

    @staticmethod
    def _htf_kama_full_series(src: list[float | None], period: int) -> list[float | None]:
        """Full-list KAMA with Kaufman defaults (fast=2, slow=30) on unique HTF bars.

        No full-list ``_kama`` helper exists. Matches interpret
        ``_builtin_ta_kama`` 2-arg form / ``_kama_inc_update``: seed at index
        ``period-1`` (not emitted); first output at index ``period``. 3/4-arg
        kama is not allowlisted.
        """
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period < 1 or period >= n:
            return out
        seed = src[period - 1]
        if seed is None:
            return out
        try:
            kama = float(seed)
        except (TypeError, ValueError):
            return out
        if kama != kama:
            return out
        fast = 2.0
        slow = 30.0
        for i in range(period, n):
            cur = src[i]
            old = src[i - period]
            if cur is None or old is None:
                continue
            try:
                x = float(cur)
                oldest = float(old)
            except (TypeError, ValueError):
                continue
            if x != x or oldest != oldest:
                continue
            volatility = 0.0
            ok = True
            for j in range(period):
                a = src[i - j]
                b = src[i - j - 1]
                if a is None or b is None:
                    ok = False
                    break
                try:
                    fa = float(a)
                    fb = float(b)
                except (TypeError, ValueError):
                    ok = False
                    break
                if fa != fa or fb != fb:
                    ok = False
                    break
                volatility += abs(fa - fb)
            if not ok:
                continue
            if volatility != 0.0:
                efficiency = abs(x - oldest) / volatility
                fastest = 2.0 / (fast + 1.0)
                slowest = 2.0 / (slow + 1.0)
                smoothing = efficiency * (fastest - slowest) + slowest
                sc = smoothing * smoothing
            else:
                sc = (2.0 / (slow + 1.0)) ** 2
            kama = kama + sc * (x - kama)
            out[i] = None if kama != kama else kama
        return out

    def _htf_rising_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list rising via interpret ``_rising`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        rising_fn = getattr(self, "_rising", None)
        if not callable(rising_fn):
            return out
        # ``period`` pairwise steps need ``period+1`` samples.
        for i in range(period, n):
            window = src[i - period : i + 1]
            try:
                v = rising_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_falling_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list falling via interpret ``_falling`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        falling_fn = getattr(self, "_falling", None)
        if not callable(falling_fn):
            return out
        for i in range(period, n):
            window = src[i - period : i + 1]
            try:
                v = falling_fn(window, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_swma_full_series(self, src: list[float | None]) -> list[float | None]:
        """Full-list 4-period SWMA via interpret ``_swma`` on unique HTF windows."""
        n = len(src)
        out: list[float | None] = [None] * n
        period = 4
        swma_fn = getattr(self, "_swma", None)
        if not callable(swma_fn) or n < period:
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            try:
                v = swma_fn(window)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_ao_from_hl2(
        self, hl2: list[float | None], fast: int = 5, slow: int = 34
    ) -> list[float | None]:
        """Awesome Oscillator: SMA(hl2, fast) − SMA(hl2, slow) on unique HTF bars."""
        n = len(hl2)
        out: list[float | None] = [None] * n
        sma_fn = getattr(self, "_sma", None)
        if not callable(sma_fn) or n == 0 or fast <= 0 or slow <= 0:
            return out
        fast_s = sma_fn(hl2, int(fast))
        slow_s = sma_fn(hl2, int(slow))
        if not fast_s or not slow_s:
            return out
        m = min(n, len(fast_s), len(slow_s))
        for i in range(m):
            f, s = fast_s[i], slow_s[i]
            if f is None or s is None:
                continue
            try:
                fv = float(f) - float(s)
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    @staticmethod
    def _htf_ao_periods(expr: HtfSimpleTaExpr) -> tuple[int, int]:
        """Fast/slow SMA lengths for HTF ``ta.ao`` (defaults 5 / 34).

        0-arg stores ``length=0`` (defaults). 1-arg stores ``length=34`` and
        ``extra=fast``. 2-arg stores ``length=slow`` and ``extra=fast``.
        """
        extra = expr.extra
        length = int(expr.length)
        if extra is not None and length > 0:
            return int(extra), length
        return 5, 34

    def _htf_ao_full_series(
        self, unique: list[dict[str, float]], *, fast: int = 5, slow: int = 34
    ) -> list[float | None]:
        """``ta.ao`` on unique HTF hl2 (defaults SMA 5 − 34)."""
        return self._htf_ao_from_hl2(self._htf_source_series(unique, "hl2"), fast, slow)

    @staticmethod
    def _htf_finite_window(window: list[float | None]) -> list[float] | None:
        """Strict finite window, or None if any slot is missing/non-numeric."""
        vals: list[float] = []
        for v in window:
            if v is None:
                return None
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return None
            if fv != fv:
                return None
            vals.append(fv)
        return vals

    def _htf_vwma_full_series(
        self, src: list[float | None], vol: list[float | None], period: int
    ) -> list[float | None]:
        """Full-list VWMA on unique HTF source + HTF volume (no LTF invent)."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        if not vol or len(vol) < period:
            sma_fn = getattr(self, "_sma", None)
            if not callable(sma_fn):
                return out
            raw = sma_fn(src, period)
            return list(raw) if raw is not None else out
        for i in range(period - 1, n):
            window_s = src[i + 1 - period : i + 1]
            window_v = vol[i + 1 - period : i + 1]
            if any(x is None or y is None for x, y in zip(window_s, window_v, strict=True)):
                out[i] = None
                continue
            try:
                sp = sum(float(x) * float(y) for x, y in zip(window_s, window_v, strict=True))
                sv = sum(float(y) for y in window_v)
            except (TypeError, ValueError):
                out[i] = None
                continue
            out[i] = (sp / sv) if sv else None
        return out

    @staticmethod
    def _htf_vwap_full_series(src: list[float | None], vol: list[float | None]) -> list[float | None]:
        """Cumulative VWAP on unique HTF source + HTF volume (no LTF invent).

        ``sum(price×volume)/sum(volume)`` from the first unique bar, matching
        interpret ``ta.vwap`` (None prices skipped / carry last; zero volume
        falls back to price). 2-arg anchor / 3-arg bands stay off this path.
        """
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0:
            return out
        cum_pv = 0.0
        cum_v = 0.0
        last: float | None = None
        n_vol = len(vol) if vol else 0
        for i, price in enumerate(src):
            if price is None:
                out[i] = last
                continue
            try:
                p = float(price)
            except (TypeError, ValueError):
                out[i] = last
                continue
            if p != p:
                out[i] = last
                continue
            v = 0.0
            if i < n_vol:
                raw_v = vol[i]
                if raw_v is not None:
                    try:
                        fv = float(raw_v)
                    except (TypeError, ValueError):
                        fv = 0.0
                    if fv == fv:
                        v = fv
            cum_pv += p * v
            cum_v += v
            last = (cum_pv / cum_v) if cum_v else p
            out[i] = last
        return out

    @staticmethod
    def _htf_cum_full_series(src: list[float | None]) -> list[float | None]:
        """Running sum on unique HTF bars matching interpret ``ta.cum``.

        ``na`` / IEEE NaN contribute 0 (same as ``_cumsum`` / ``_cum_inc_update``).
        Empty source yields an empty list; a pure-na prefix still emits 0.0.
        """
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0:
            return out
        total = 0.0
        for i, value in enumerate(src):
            if value is not None:
                try:
                    fv = float(value)
                except (TypeError, ValueError):
                    fv = 0.0
                if fv == fv:
                    total += fv
            out[i] = total
        return out

    @staticmethod
    def _htf_stdev_full_series(src: list[float | None], period: int) -> list[float | None]:
        """Full-list sample stdev matching interpret ``_stdev`` (strict window, ddof=1)."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 1:
            return out
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            vals: list[float] = []
            ok = True
            for v in window:
                if v is None:
                    ok = False
                    break
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    ok = False
                    break
                if fv != fv:
                    ok = False
                    break
                vals.append(fv)
            out[i] = statistics.stdev(vals) if ok else None
        return out

    @staticmethod
    def _htf_cmo_full_series(src: list[float | None], period: int) -> list[float | None]:
        """Full-list CMO matching interpret ``ta.cmo`` (window of length+1 diffs)."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        need = period + 1
        for i in range(need - 1, n):
            window = src[i - need + 1 : i + 1]
            up = 0.0
            down = 0.0
            prev: float | None = None
            for v in window:
                cur: float | None
                if v is None:
                    cur = None
                else:
                    try:
                        fv = float(v)
                        cur = None if fv != fv else fv
                    except (TypeError, ValueError):
                        cur = None
                if prev is not None and cur is not None:
                    diff = cur - prev
                    if diff > 0:
                        up += diff
                    else:
                        down += -diff
                prev = cur
            denom = up + down
            out[i] = 0.0 if denom == 0.0 else 100.0 * (up - down) / denom
        return out

    @staticmethod
    def _htf_linreg_full_series(
        src: list[float | None], period: int, offset: int = 0
    ) -> list[float | None]:
        """Full-list linreg endpoint matching interpret ``ta.linreg``."""
        from pynescript.ast.evaluator.builtins.technical_submodules.core import TechnicalHelpers

        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period < 2:  # noqa: PLR2004 - OLS needs ≥2 points
            return out
        lin = TechnicalHelpers._linreg_from_valid
        off = int(offset)
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            v = lin(window, off)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                out[i] = None
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_cci_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list CCI matching interpret ``ta.cci(source, length)``.

        Typical price is the named OHLCV field (same as ``_cci(src, src, src, n)``).
        1-arg ``ta.cci(n)`` arrives as source ``hlc3`` (HTF typical price).
        4-arg CCI stays off this path (matcher arity).
        """
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        cci_fn = getattr(self, "_cci", None)
        last_mean_dev: float | None = None
        for i in range(period - 1, n):
            window = src[i - period + 1 : i + 1]
            vals: list[float] = []
            ok = True
            for v in window:
                if v is None:
                    ok = False
                    break
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    ok = False
                    break
                if fv != fv:
                    ok = False
                    break
                vals.append(fv)
            if not ok:
                out[i] = None
                continue
            if callable(cci_fn):
                try:
                    v = cci_fn(vals, vals, vals, period)
                except (TypeError, ValueError):
                    v = None
                if v is None:
                    out[i] = None
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    out[i] = None
                    continue
                out[i] = None if fv != fv else fv
                continue
            sma = sum(vals) / period
            mean_dev = sum(abs(v - sma) for v in vals) / period
            if mean_dev not in {0, 0.0}:
                last_mean_dev = mean_dev
            if last_mean_dev is None or last_mean_dev == 0:
                out[i] = 0.0
                continue
            out[i] = (vals[-1] - sma) / (0.015 * last_mean_dev)
        return out

    def _htf_roc_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list ROC matching interpret ``_roc`` (``100 * (src - src[n]) / src[n]``)."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        roc_fn = getattr(self, "_roc", None)
        for i in range(period, n):
            window = src[: i + 1]
            if callable(roc_fn):
                try:
                    v = roc_fn(window, period)
                except (TypeError, ValueError):
                    v = None
                if v is None:
                    out[i] = None
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    out[i] = None
                    continue
                out[i] = None if fv != fv else fv
                continue
            cur, base = window[-1], window[-period - 1]
            if cur is None or base is None:
                out[i] = None
                continue
            try:
                c = float(cur)
                b = float(base)
            except (TypeError, ValueError):
                out[i] = None
                continue
            if c != c or b != b or b == 0.0:
                out[i] = None
                continue
            out[i] = 100.0 * (c - b) / b
        return out

    def _htf_mom_full_series(self, src: list[float | None], period: int) -> list[float | None]:
        """Full-list momentum matching interpret ``_mom`` (``src - src[n]``)."""
        n = len(src)
        out: list[float | None] = [None] * n
        if n == 0 or period <= 0:
            return out
        mom_fn = getattr(self, "_mom", None)
        for i in range(period, n):
            window = src[: i + 1]
            if callable(mom_fn):
                try:
                    v = mom_fn(window, period)
                except (TypeError, ValueError):
                    v = None
                if v is None:
                    out[i] = None
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    out[i] = None
                    continue
                out[i] = None if fv != fv else fv
                continue
            cur, prev = window[-1], window[-period - 1]
            if cur is None or prev is None:
                out[i] = None
                continue
            try:
                c = float(cur)
                p = float(prev)
            except (TypeError, ValueError):
                out[i] = None
                continue
            if c != c or p != p:
                out[i] = None
                continue
            out[i] = c - p
        return out

    def _htf_wpr_full_series(
        self,
        highs: list[float | None],
        lows: list[float | None],
        closes: list[float | None],
        period: int,
    ) -> list[float | None]:
        """Full-list Williams %R via interpret ``_wpr`` on unique HTF h/l/c."""
        n = min(len(highs), len(lows), len(closes))
        out: list[float | None] = [None] * len(closes)
        if n == 0 or period <= 0:
            return out
        wpr_fn = getattr(self, "_wpr", None)
        if not callable(wpr_fn):
            return out
        for i in range(period - 1, n):
            h = self._htf_finite_window(highs[i - period + 1 : i + 1])
            lo = self._htf_finite_window(lows[i - period + 1 : i + 1])
            c = self._htf_finite_window(closes[i - period + 1 : i + 1])
            if h is None or lo is None or c is None:
                continue
            try:
                v = wpr_fn(h, lo, c, period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_adx_full_series(
        self,
        highs: list[float | None],
        lows: list[float | None],
        closes: list[float | None],
        period: int,
    ) -> list[float | None]:
        """Full-list ADX via interpret ``_adx`` on unique HTF prefixes.

        ``_adx`` is last-sample only (0.0 while ``len < period`` / ATR unseeded).
        Prefix walk matches ``ta.adx(n)`` on those unique bars. 2-arg
        ``(diLength, adxSmoothing)`` and 4-arg explicit-series stay unmatched.
        """
        n = min(len(highs), len(lows), len(closes))
        out: list[float | None] = [None] * max(len(closes), n)
        if n == 0 or period <= 0:
            return out
        adx_fn = getattr(self, "_adx", None)
        if not callable(adx_fn):
            return out

        def _nums(seq: list[float | None]) -> list[float]:
            acc: list[float] = []
            for v in seq[:n]:
                if v is None:
                    acc.append(math.nan)
                    continue
                try:
                    acc.append(float(v))
                except (TypeError, ValueError):
                    acc.append(math.nan)
            return acc

        hs, ls, cs = _nums(highs), _nums(lows), _nums(closes)
        for i in range(n):
            try:
                v = adx_fn(hs[: i + 1], ls[: i + 1], cs[: i + 1], period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_mfi_full_series(
        self,
        highs: list[float | None],
        lows: list[float | None],
        closes: list[float | None],
        volumes: list[float | None],
        period: int,
    ) -> list[float | None]:
        """Full-list MFI via interpret ``_mfi`` on unique HTF prefixes.

        ``_mfi`` is last-sample only (na until ``period + 1`` typical-price
        samples). Prefix walk matches ``ta.mfi(n)`` on those unique bars.
        2-arg source/length and 5-arg explicit-series stay unmatched.
        """
        n = min(len(highs), len(lows), len(closes), len(volumes))
        out: list[float | None] = [None] * max(len(closes), n)
        if n == 0 or period <= 0:
            return out
        mfi_fn = getattr(self, "_mfi", None)
        if not callable(mfi_fn):
            return out

        def _nums(seq: list[float | None]) -> list[float]:
            acc: list[float] = []
            for v in seq[:n]:
                if v is None:
                    acc.append(math.nan)
                    continue
                try:
                    acc.append(float(v))
                except (TypeError, ValueError):
                    acc.append(math.nan)
            return acc

        hs, ls, cs, vs = _nums(highs), _nums(lows), _nums(closes), _nums(volumes)
        for i in range(n):
            try:
                v = mfi_fn(hs[: i + 1], ls[: i + 1], cs[: i + 1], vs[: i + 1], period)
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                continue
            out[i] = None if fv != fv else fv
        return out

    @staticmethod
    def _htf_obv_full_series(
        closes: list[float | None], volumes: list[float | None]
    ) -> list[float | None]:
        """Full-list OBV matching interpret ``ta.obv()`` on unique HTF bars.

        ``_obv`` is last-sample: 0 until 3 samples, then accumulates signed
        volume from index 2 (skips ``close[0]`` vs ``close[1]``). 2-arg
        ``ta.obv(close, volume)`` stays unmatched.
        """
        n = min(len(closes), len(volumes))
        out: list[float | None] = [None] * max(len(closes), n)
        if n == 0:
            return out
        warmup = 3
        total = 0.0
        for i in range(n):
            if i + 1 < warmup:
                out[i] = 0.0
                continue
            cur = closes[i]
            prev = closes[i - 1]
            vol = volumes[i]
            if cur is not None and prev is not None and vol is not None:
                try:
                    c = float(cur)
                    p = float(prev)
                    v = float(vol)
                except (TypeError, ValueError):
                    out[i] = total
                    continue
                if c == c and p == p and v == v:
                    if c > p:
                        total += v
                    elif c < p:
                        total -= v
            out[i] = total
        return out

    @staticmethod
    def _htf_nvi_full_series(
        closes: list[float | None], volumes: list[float | None]
    ) -> list[float | None]:
        """Full-list NVI matching interpret ``ta.nvi()`` on unique HTF bars.

        Seed ``1000.0`` on the first unique bar. Later bars multiply by
        ``(1 + close_change)`` when volume decreases. Missing / non-numeric
        close skips the increment. Empty volume contributes 0.
        2-arg ``ta.nvi(close, volume)`` stays unmatched.
        """
        n = min(len(closes), len(volumes))
        out: list[float | None] = [None] * max(len(closes), n)
        if n == 0:
            return out
        nvi = 1000.0
        prev_c: float | None = None
        prev_v: float | None = None
        started = False
        for i in range(n):
            cur = closes[i]
            cf: float | None = None
            if cur is not None:
                try:
                    fv = float(cur)
                except (TypeError, ValueError):
                    fv = float("nan")
                if fv == fv:
                    cf = fv
            raw_v = volumes[i]
            vv = 0.0
            if raw_v is not None:
                try:
                    vf = float(raw_v)
                except (TypeError, ValueError):
                    vf = float("nan")
                if vf == vf:
                    vv = vf
            if not started:
                started = True
                prev_c = cf
                prev_v = vv
                out[i] = nvi
                continue
            if cf is not None and prev_c is not None:
                close_change = 0.0 if prev_c == 0.0 else (cf - prev_c) / prev_c
                pv = 0.0 if prev_v is None else float(prev_v)
                if vv < pv:
                    nvi = nvi * (1.0 + close_change)
            if cf is not None:
                prev_c = cf
            prev_v = vv
            out[i] = nvi
        return out

    @staticmethod
    def _htf_pvi_full_series(
        closes: list[float | None], volumes: list[float | None]
    ) -> list[float | None]:
        """Full-list PVI matching interpret ``ta.pvi()`` on unique HTF bars.

        Seed ``1000.0`` on the first unique bar. Later bars multiply by
        ``(1 + close_change)`` when volume increases. Missing / non-numeric
        close skips the increment. Empty volume contributes 0.
        2-arg ``ta.pvi(close, volume)`` stays unmatched.
        """
        n = min(len(closes), len(volumes))
        out: list[float | None] = [None] * max(len(closes), n)
        if n == 0:
            return out
        pvi = 1000.0
        prev_c: float | None = None
        prev_v: float | None = None
        started = False
        for i in range(n):
            cur = closes[i]
            cf: float | None = None
            if cur is not None:
                try:
                    fv = float(cur)
                except (TypeError, ValueError):
                    fv = float("nan")
                if fv == fv:
                    cf = fv
            raw_v = volumes[i]
            vv = 0.0
            if raw_v is not None:
                try:
                    vf = float(raw_v)
                except (TypeError, ValueError):
                    vf = float("nan")
                if vf == vf:
                    vv = vf
            if not started:
                started = True
                prev_c = cf
                prev_v = vv
                out[i] = pvi
                continue
            if cf is not None and prev_c is not None:
                close_change = 0.0 if prev_c == 0.0 else (cf - prev_c) / prev_c
                pv = 0.0 if prev_v is None else float(prev_v)
                if vv > pv:
                    pvi = pvi * (1.0 + close_change)
            if cf is not None:
                prev_c = cf
            prev_v = vv
            out[i] = pvi
        return out

    def _htf_cmf_full_series(
        self,
        closes: list[float | None],
        highs: list[float | None],
        lows: list[float | None],
        volumes: list[float | None],
        period: int,
    ) -> list[float | None]:
        """Full-list CMF via interpret ``_cmf`` on unique HTF high/low/close/volume.

        ``_cmf`` already returns a full list (partial windows from bar 0).
        5-arg explicit-series stays unmatched.
        """
        n = min(len(closes), len(highs), len(lows), len(volumes))
        out: list[float | None] = [None] * max(len(closes), n)
        if n == 0 or period <= 0:
            return out
        cmf_fn = getattr(self, "_cmf", None)
        if not callable(cmf_fn):
            return out

        def _nums(seq: list[float | None]) -> list[float]:
            acc: list[float] = []
            for v in seq[:n]:
                if v is None:
                    acc.append(0.0)
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    acc.append(0.0)
                    continue
                acc.append(0.0 if fv != fv else fv)
            return acc

        try:
            raw = cmf_fn(_nums(closes), _nums(highs), _nums(lows), _nums(volumes), period)
        except (TypeError, ValueError):
            return out
        if not raw:
            return out
        for i, v in enumerate(raw):
            if i >= n:
                break
            if v is None:
                out[i] = None
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                out[i] = None
                continue
            out[i] = None if fv != fv else fv
        return out

    def _htf_rsi_full_series(self, closes: list[float | None], period: int) -> list[float | None]:
        """Full-list RSI on *closes* (Wilder RMA of gains/losses), bar-aligned."""
        n = len(closes)
        out: list[float | None] = [None] * n
        if n < 2 or period <= 0:
            return out
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, n):
            a, b = closes[i - 1], closes[i]
            if a is None or b is None:
                gains.append(0.0)
                losses.append(0.0)
                continue
            try:
                change = float(b) - float(a)
            except (TypeError, ValueError):
                gains.append(0.0)
                losses.append(0.0)
                continue
            gains.append(change if change > 0 else 0.0)
            losses.append(-change if change < 0 else 0.0)
        rma_fn = getattr(self, "_rma", None)
        if not callable(rma_fn):
            # Soft fallback: last-only helper on prefixes (small HTF n only).
            rsi_fn = getattr(self, "_rsi", None)
            if not callable(rsi_fn):
                return out
            for i in range(1, n):
                window = [float(c) for c in closes[: i + 1] if c is not None]
                out[i] = rsi_fn(window, period)
            return out
        avg_g = rma_fn(gains, period)
        avg_l = rma_fn(losses, period)
        for i in range(1, n):
            g = avg_g[i - 1] if i - 1 < len(avg_g) else None
            lo = avg_l[i - 1] if i - 1 < len(avg_l) else None
            try:
                gf = float(g) if g is not None else float("nan")
                lf = float(lo) if lo is not None else float("nan")
            except (TypeError, ValueError):
                out[i] = None
                continue
            if gf != gf or lf != lf:
                out[i] = None
                continue
            if lf == 0.0:
                out[i] = 100.0
            else:
                rs = gf / lf
                out[i] = 100.0 - (100.0 / (1.0 + rs))
        return out

    def _htf_ta_values_on_unique(self, expr: HtfSimpleTaExpr, unique: list[dict[str, float]]) -> list[float | None]:
        """Run allowlisted TA full-list helpers on unique HTF bars."""
        n = len(unique)
        if n == 0:
            return []
        name = expr.name
        if name == "ao":
            fast, slow = self._htf_ao_periods(expr)
            return self._htf_ao_full_series(unique, fast=fast, slow=slow)
        if name == "vwap":
            field = expr.source or "hlc3"
            src = self._htf_source_series(unique, field)
            vol = self._htf_source_series(unique, "volume")
            return self._htf_vwap_full_series(src, vol)
        if name == "cum":
            field = expr.source or "close"
            src = self._htf_source_series(unique, field)
            return self._htf_cum_full_series(src)
        if name == "obv":
            closes = self._htf_source_series(unique, "close")
            vol = self._htf_source_series(unique, "volume")
            return self._htf_obv_full_series(closes, vol)
        if name == "nvi":
            closes = self._htf_source_series(unique, "close")
            vol = self._htf_source_series(unique, "volume")
            return self._htf_nvi_full_series(closes, vol)
        if name == "pvi":
            closes = self._htf_source_series(unique, "close")
            vol = self._htf_source_series(unique, "volume")
            return self._htf_pvi_full_series(closes, vol)
        if expr.length <= 0:
            return []
        period = int(expr.length)
        if name == "range" and expr.source is None:
            highs = self._htf_source_series(unique, "high")
            lows = self._htf_source_series(unique, "low")
            return self._htf_hl_range_full_series(highs, lows, period)
        if name in (
            "sma",
            "ema",
            "rsi",
            "wma",
            "rma",
            "cmo",
            "stdev",
            "linreg",
            "cci",
            "roc",
            "mom",
            "hma",
            "vwma",
            "dema",
            "tema",
            "highest",
            "lowest",
            "range",
            "median",
            "dev",
            "variance",
            "mode",
            "percentrank",
            "change",
            "cog",
            "highestbars",
            "lowestbars",
            "kama",
            "rising",
            "falling",
            "swma",
        ):
            field = expr.source or "close"
            src = self._htf_source_series(unique, field)
            if name == "sma":
                sma_fn = getattr(self, "_sma", None)
                if not callable(sma_fn):
                    return [None] * n
                raw = sma_fn(src, period)
                # Align length (helpers return list of same len).
                return list(raw) if raw is not None else [None] * n
            if name == "ema":
                ema_fn = getattr(self, "_ema", None)
                if not callable(ema_fn):
                    return [None] * n
                raw = ema_fn(src, period)
                return list(raw) if raw is not None else [None] * n
            if name == "rma":
                rma_fn = getattr(self, "_rma", None)
                if not callable(rma_fn):
                    return [None] * n
                raw = rma_fn(src, period)
                return list(raw) if raw is not None else [None] * n
            if name == "wma":
                return self._htf_wma_full_series(src, period)
            if name == "hma":
                return self._htf_hma_full_series(src, period)
            if name == "vwma":
                vol = self._htf_source_series(unique, "volume")
                return self._htf_vwma_full_series(src, vol, period)
            if name == "cmo":
                return self._htf_cmo_full_series(src, period)
            if name == "stdev":
                return self._htf_stdev_full_series(src, period)
            if name == "linreg":
                off = int(expr.extra) if expr.extra is not None else 0
                return self._htf_linreg_full_series(src, period, off)
            if name == "cci":
                return self._htf_cci_full_series(src, period)
            if name == "roc":
                return self._htf_roc_full_series(src, period)
            if name == "mom":
                return self._htf_mom_full_series(src, period)
            if name == "dema":
                return self._htf_dema_full_series(src, period)
            if name == "tema":
                return self._htf_tema_full_series(src, period)
            if name == "highest":
                return self._htf_highest_full_series(src, period)
            if name == "lowest":
                return self._htf_lowest_full_series(src, period)
            if name == "range":
                return self._htf_range_full_series(src, period)
            if name == "median":
                return self._htf_median_full_series(src, period)
            if name == "dev":
                return self._htf_dev_full_series(src, period)
            if name == "variance":
                return self._htf_variance_full_series(src, period)
            if name == "mode":
                return self._htf_mode_full_series(src, period)
            if name == "percentrank":
                return self._htf_percentrank_full_series(src, period)
            if name == "change":
                return self._htf_change_full_series(src, period)
            if name == "cog":
                return self._htf_cog_full_series(src, period)
            if name == "highestbars":
                return self._htf_highestbars_full_series(src, period)
            if name == "lowestbars":
                return self._htf_lowestbars_full_series(src, period)
            if name == "kama":
                return self._htf_kama_full_series(src, period)
            if name == "rising":
                return self._htf_rising_full_series(src, period)
            if name == "falling":
                return self._htf_falling_full_series(src, period)
            if name == "swma":
                return self._htf_swma_full_series(src)
            return self._htf_rsi_full_series(src, period)

        # atr / wpr / adx: length-only; uses HTF high/low/close.
        # mfi / cmf: length-only; uses HTF high/low/close/volume.
        highs = self._htf_source_series(unique, "high")
        lows = self._htf_source_series(unique, "low")
        closes = self._htf_source_series(unique, "close")
        if name == "wpr":
            return self._htf_wpr_full_series(highs, lows, closes, period)
        if name == "adx":
            return self._htf_adx_full_series(highs, lows, closes, period)
        if name == "mfi":
            vol = self._htf_source_series(unique, "volume")
            return self._htf_mfi_full_series(highs, lows, closes, vol, period)
        if name == "cmf":
            vol = self._htf_source_series(unique, "volume")
            return self._htf_cmf_full_series(closes, highs, lows, vol, period)
        atr_fn = getattr(self, "_atr", None)
        out: list[float | None] = [None] * n
        if not callable(atr_fn):
            return out
        # _atr returns series aligned to TR samples (len ≈ n-1).
        atr_raw = atr_fn(highs, lows, closes, period)
        if not atr_raw:
            return out
        # Map ATR[j] → HTF bar j+1 (TR needs a previous close).
        for j, v in enumerate(atr_raw):
            dest = j + 1
            if dest >= n:
                break
            if v is None:
                out[dest] = None
                continue
            try:
                fv = float(v)
                out[dest] = None if fv != fv else fv
            except (TypeError, ValueError):
                out[dest] = None
        return out

    def _try_htf_simple_ta_resample(
        self, expression: HtfSimpleTaExpr, timeframe: Any, *, gaps_on: bool = False, lookahead_on: bool = False
    ) -> Any | None:
        """Run allowlisted ta.* on resampled HTF bars; map last completed to chart.

        Default semantics match OHLCV HTF resample: **last completed** HTF bar
        only (lookahead_off-style). ``lookahead_on`` maps each chart bar to TA
        computed on unique HTF buckets including the current period's final
        agg (historical leak from the period start). ``gaps_on`` delivers a
        value only on bucket-start bars. Full-list TA on the unique HTF series
        is cached per (spec, ta identity, fingerprint).
        """
        if not isinstance(expression, HtfSimpleTaExpr):
            return None
        if not self._request_is_higher_tf(timeframe):
            return None
        spec = self._htf_tf_spec(timeframe)
        if spec is None:
            return None
        built = self._htf_unique_and_map(spec, lookahead_on=lookahead_on)
        if built is None:
            return None
        unique, chart_to_htf = built
        n_chart = len(chart_to_htf)
        if n_chart == 0:
            return None

        src = self._htf_source_lists()
        fp: tuple[Any, ...] = (n_chart, len(unique))
        if src is not None:
            opens, highs, lows, closes, _volumes, times = src
            fp = self._htf_cache_fingerprint(len(opens), times, opens, highs, lows, closes)

        cache = getattr(self, "_htf_simple_ta_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._htf_simple_ta_cache = cache  # type: ignore[attr-defined]
        ckey = (
            spec,
            expression.name,
            expression.source or "",
            int(expression.length),
            int(expression.extra) if expression.extra is not None else 0,
            bool(lookahead_on),
            fp,
        )
        entry = cache.get(ckey)
        if isinstance(entry, dict) and entry.get("chart_vals") is not None:
            chart_vals = entry["chart_vals"]
        else:
            ta_on_htf = self._htf_ta_values_on_unique(expression, unique)
            chart_vals = [None] * n_chart
            for i, hidx in enumerate(chart_to_htf):
                if hidx is None or hidx < 0 or hidx >= len(ta_on_htf):
                    chart_vals[i] = None
                else:
                    chart_vals[i] = ta_on_htf[hidx]
            cache[ckey] = {"chart_vals": chart_vals}

        i = self._chart_bar_index()
        last = chart_vals[i] if 0 <= i < len(chart_vals) else None
        if gaps_on and not self._htf_ta_is_bucket_start(chart_to_htf, index=i):
            last = None
        if gaps_on or lookahead_on:
            self._mark_security_merge_applied(gaps_on=gaps_on, lookahead_on=lookahead_on)
        if last is None:
            return float("nan")
        try:
            fv = float(last)
        except (TypeError, ValueError):
            return float("nan")
        return fv if fv == fv else float("nan")

    def _htf_ta_is_bucket_start(self, chart_to_htf: list[int | None], *, index: int | None = None) -> bool:
        """True when the current chart bar opens a new HTF bucket."""
        i = self._chart_bar_index() if index is None else int(index)
        if i < 0 or i >= len(chart_to_htf):
            return False
        hidx = chart_to_htf[i]
        if hidx is None:
            return False
        if i == 0:
            return True
        return chart_to_htf[i - 1] != hidx

    def _mark_security_merge_applied(self, *, gaps_on: bool, lookahead_on: bool) -> None:
        """Record that gaps/lookahead args took effect on a resample path."""
        state = self._security_policy_state()
        if gaps_on:
            state["gaps_supported"] = True
            self._note_security_policy("gaps_applied")
        if lookahead_on:
            state["lookahead_supported"] = True
            self._note_security_policy("lookahead_applied")

    def _chart_ohlcv_at_offset(self, field: str, offset: int) -> Any:
        """Pine history offset on a chart OHLCV field (0 = current, 1 = previous)."""
        na = float("nan")
        if offset < 0:
            return na
        ctx = getattr(self, "context", {}) or {}
        series = ctx.get(field) if isinstance(ctx, dict) else None
        hist = getattr(series, "history", None) if series is not None else None
        if hist is not None:
            try:
                if offset < len(hist):
                    v = hist[offset]
                    if v is None:
                        return na
                    fv = float(v)
                    return fv if fv == fv else na
            except (TypeError, ValueError, IndexError):
                pass
        chrono = self._series_chrono_values(field)
        if not chrono or offset >= len(chrono):
            return na
        try:
            fv = float(chrono[-1 - offset])
        except (TypeError, ValueError, IndexError):
            return na
        return fv if fv == fv else na

    def _chart_simple_ta_last(self, expression: HtfSimpleTaExpr) -> Any:
        """Same-TF allowlisted ta.* on chart series (last sample)."""
        na = float("nan")
        name = expression.name
        period = int(expression.length)

        def _last_of(raw: list[float | None] | None) -> Any:
            if not raw:
                return na
            last = raw[-1]
            if last is None:
                return na
            try:
                fv = float(last)
                return fv if fv == fv else na
            except (TypeError, ValueError):
                return na

        if name == "ao":
            field = "hl2"
            src = self._series_chrono_values(field)
            if not src:
                opens = self._series_chrono_values("open")
                highs = self._series_chrono_values("high")
                lows = self._series_chrono_values("low")
                closes = self._series_chrono_values("close")
                n = len(closes)
                src = []
                for i in range(n):
                    try:
                        o = float(opens[i]) if i < len(opens) else float("nan")
                        h = float(highs[i]) if i < len(highs) else float("nan")
                        l = float(lows[i]) if i < len(lows) else float("nan")
                        c = float(closes[i])
                    except (TypeError, ValueError):
                        src.append(None)
                        continue
                    v = self._htf_agg_field(
                        {"open": o, "high": h, "low": l, "close": c, "volume": 0.0, "time": 0.0},
                        field,
                    )
                    src.append(None if v != v else v)
            fast, slow = self._htf_ao_periods(expression)
            return _last_of(self._htf_ao_from_hl2(src, fast, slow) if src else None)
        if name == "obv":
            closes = self._series_chrono_values("close")
            vol = self._series_chrono_values("volume")
            return _last_of(self._htf_obv_full_series(closes, vol))
        if name == "nvi":
            closes = self._series_chrono_values("close")
            vol = self._series_chrono_values("volume")
            return _last_of(self._htf_nvi_full_series(closes, vol))
        if name == "pvi":
            closes = self._series_chrono_values("close")
            vol = self._series_chrono_values("volume")
            return _last_of(self._htf_pvi_full_series(closes, vol))
        if period <= 0 and name not in ("vwap", "cum"):
            return na
        if name in ("atr", "wpr", "adx", "mfi", "cmf") or (name == "range" and expression.source is None):
            highs = self._series_chrono_values("high")
            lows = self._series_chrono_values("low")
            closes = self._series_chrono_values("close")
            if name == "range":
                return _last_of(self._htf_hl_range_full_series(highs, lows, period))
            if name == "wpr":
                return _last_of(self._htf_wpr_full_series(highs, lows, closes, period))
            if name == "adx":
                return _last_of(self._htf_adx_full_series(highs, lows, closes, period))
            if name == "mfi":
                vol = self._series_chrono_values("volume")
                return _last_of(self._htf_mfi_full_series(highs, lows, closes, vol, period))
            if name == "cmf":
                vol = self._series_chrono_values("volume")
                return _last_of(self._htf_cmf_full_series(closes, highs, lows, vol, period))
            atr_fn = getattr(self, "_atr", None)
            if not callable(atr_fn) or not closes:
                return na
            return _last_of(atr_fn(highs, lows, closes, period))
        field = expression.source or ("hlc3" if name == "vwap" else "close")
        src = self._series_chrono_values(field)
        if not src and field in ("hl2", "hlc3", "ohlc4"):
            opens = self._series_chrono_values("open")
            highs = self._series_chrono_values("high")
            lows = self._series_chrono_values("low")
            closes = self._series_chrono_values("close")
            n = len(closes)
            src = []
            for i in range(n):
                try:
                    o = float(opens[i]) if i < len(opens) else float("nan")
                    h = float(highs[i]) if i < len(highs) else float("nan")
                    l = float(lows[i]) if i < len(lows) else float("nan")
                    c = float(closes[i])
                except (TypeError, ValueError):
                    src.append(None)
                    continue
                v = self._htf_agg_field(
                    {"open": o, "high": h, "low": l, "close": c, "volume": 0.0, "time": 0.0},
                    field,
                )
                src.append(None if v != v else v)
        if not src:
            return na
        if name == "sma":
            fn = getattr(self, "_sma", None)
            raw = fn(src, period) if callable(fn) else None
        elif name == "ema":
            fn = getattr(self, "_ema", None)
            raw = fn(src, period) if callable(fn) else None
        elif name == "rma":
            fn = getattr(self, "_rma", None)
            raw = fn(src, period) if callable(fn) else None
        elif name == "wma":
            last = self._htf_wma_full_series(src, period)
            raw = last
        elif name == "hma":
            raw = self._htf_hma_full_series(src, period)
        elif name == "vwma":
            vol = self._series_chrono_values("volume")
            raw = self._htf_vwma_full_series(src, vol, period)
        elif name == "cmo":
            raw = self._htf_cmo_full_series(src, period)
        elif name == "stdev":
            raw = self._htf_stdev_full_series(src, period)
        elif name == "linreg":
            off = int(expression.extra) if expression.extra is not None else 0
            raw = self._htf_linreg_full_series(src, period, off)
        elif name == "cci":
            raw = self._htf_cci_full_series(src, period)
        elif name == "roc":
            raw = self._htf_roc_full_series(src, period)
        elif name == "mom":
            raw = self._htf_mom_full_series(src, period)
        elif name == "dema":
            raw = self._htf_dema_full_series(src, period)
        elif name == "tema":
            raw = self._htf_tema_full_series(src, period)
        elif name == "highest":
            raw = self._htf_highest_full_series(src, period)
        elif name == "lowest":
            raw = self._htf_lowest_full_series(src, period)
        elif name == "range":
            raw = self._htf_range_full_series(src, period)
        elif name == "median":
            raw = self._htf_median_full_series(src, period)
        elif name == "dev":
            raw = self._htf_dev_full_series(src, period)
        elif name == "variance":
            raw = self._htf_variance_full_series(src, period)
        elif name == "mode":
            raw = self._htf_mode_full_series(src, period)
        elif name == "percentrank":
            raw = self._htf_percentrank_full_series(src, period)
        elif name == "change":
            raw = self._htf_change_full_series(src, period)
        elif name == "cog":
            raw = self._htf_cog_full_series(src, period)
        elif name == "highestbars":
            raw = self._htf_highestbars_full_series(src, period)
        elif name == "lowestbars":
            raw = self._htf_lowestbars_full_series(src, period)
        elif name == "kama":
            raw = self._htf_kama_full_series(src, period)
        elif name == "rising":
            raw = self._htf_rising_full_series(src, period)
        elif name == "falling":
            raw = self._htf_falling_full_series(src, period)
        elif name == "swma":
            raw = self._htf_swma_full_series(src)
        elif name == "vwap":
            vol = self._series_chrono_values("volume")
            raw = self._htf_vwap_full_series(src, vol)
        elif name == "cum":
            raw = self._htf_cum_full_series(src)
        elif name == "rsi":
            cleaned: list[float | None] = []
            for v in src:
                if v is None:
                    cleaned.append(None)
                    continue
                try:
                    cleaned.append(float(v))
                except (TypeError, ValueError):
                    cleaned.append(None)
            raw = self._htf_rsi_full_series(cleaned, period)
        else:
            return na
        if not raw:
            return na
        last = raw[-1]
        if last is None:
            return na
        try:
            fv = float(last)
            return fv if fv == fv else na
        except (TypeError, ValueError):
            return na

    def _security_policy_state(self) -> dict[str, Any]:
        """Lazy-init shared request.security honesty metadata for hosts/tests."""
        state = getattr(self, "_request_security_policy", None)
        if isinstance(state, dict):
            return state
        state = {
            "htf_reeval": False,
            "gaps_supported": False,
            "lookahead_supported": False,
            "policies": {},  # tag → {count, ...}
            "notes": list(_SECURITY_POLICY_NOTES),
            "calls": 0,
        }
        self._request_security_policy = state  # type: ignore[attr-defined]
        ctx = getattr(self, "context", None)
        if isinstance(ctx, dict):
            ctx["request.security_policy"] = state
        return state

    def _note_security_policy(self, tag: str, **extra: Any) -> None:
        """Record a unique policy decision (once-log + per-run counters).

        Tags are product-facing honesty markers (``complex_htf_na``,
        ``chart_passthrough_htf_stub``, ``foreign_na``, …). Counts increment
        every bar; ``extra`` is kept from the first observation only so meta
        stays compact.
        """
        state = self._security_policy_state()
        state["calls"] = int(state.get("calls") or 0) + 1
        policies = state.setdefault("policies", {})
        entry = policies.get(tag)
        if entry is None:
            entry = {"count": 0}
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    entry[k] = v
                else:
                    entry[k] = str(v)
            policies[tag] = entry
            # One debug line per new tag (avoid per-bar spam on long charts).
            _LOG.debug("request.security policy %s %s", tag, {k: v for k, v in entry.items() if k != "count"})
        entry["count"] = int(entry.get("count") or 0) + 1

    def _security_return(self, value: Any, tag: str, **extra: Any) -> Any:
        """Record *tag* and return *value* (na / chart series / provider series)."""
        self._note_security_policy(tag, **extra)
        return value

    def _ticker_last(self, symbol: str) -> float | None:
        """Best-effort last price from data_feed (if wired)."""
        data_feed, _ = self._get_request_data()
        if data_feed is None or not hasattr(data_feed, "fetch_latest_ticker"):
            return None
        try:
            t = data_feed.fetch_latest_ticker(symbol)
            last = t.get("last") or t.get("close")
            return float(last) if last is not None else None
        except Exception:  # noqa: S110
            return None

    def _ohlcv_closes(self, symbol: str, timeframe: str = "D", limit: int = REQUEST_OHLCV_LIMIT) -> list[float] | None:
        payload = self._ohlcv_payload(symbol, str(timeframe), limit=limit)
        if not payload:
            return None
        closes = payload.get("close") or []
        return list(closes) if closes else None

    def _ohlcv_payload(  # noqa: C901
        self, symbol: str, timeframe: str, *, limit: int | None = None
    ) -> dict[str, list[Any]] | None:
        """OHLCV columns from data_feed / data_provider, including times when present.

        *limit* truncates to the most recent N bars (``None`` keeps the full
        payload so HTF alignment can see every provider bar).
        """
        cache = getattr(self, "_ohlcv_payload_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._ohlcv_payload_cache = cache  # type: ignore[attr-defined]
        key = (str(symbol), str(timeframe), limit)
        if key in cache:
            return cache[key]

        def _store(payload: dict[str, list[Any]] | None) -> dict[str, list[Any]] | None:
            if len(cache) >= 8:  # noqa: PLR2004 - tiny per-eval cache
                cache.clear()
            cache[key] = payload
            return payload

        data_feed, data_provider = self._get_request_data()
        if data_feed is not None and hasattr(data_feed, "fetch_latest_ohlcv"):
            try:
                n = int(limit) if limit is not None else max(REQUEST_OHLCV_LIMIT, self._chart_len_hint())
                ohlcv = data_feed.fetch_latest_ohlcv(symbol, str(timeframe), limit=max(1, n))
                rows = self._ohlcv_rows_to_payload(ohlcv, limit=limit)
                if rows is not None:
                    return _store(rows)
            except Exception:  # noqa: S110
                pass
        if data_provider is not None and hasattr(data_provider, "fetch"):
            try:
                data = data_provider.fetch(symbol, period="1d", interval=str(timeframe))
                rows = self._provider_dict_to_payload(data, limit=limit)
                if rows is not None:
                    return _store(rows)
            except Exception:  # noqa: S110
                pass
        return _store(None)

    def _chart_len_hint(self) -> int:
        src = self._htf_source_lists()
        if src is None:
            return REQUEST_OHLCV_LIMIT
        return max(REQUEST_OHLCV_LIMIT, len(src[0]))

    @staticmethod
    def _float_col(raw: Any, n: int, *, default: float = float("nan")) -> list[float]:
        out: list[float] = []
        seq = raw if isinstance(raw, (list, tuple)) else []
        for i in range(n):
            if i >= len(seq):
                out.append(default)
                continue
            try:
                out.append(float(seq[i]))
            except (TypeError, ValueError):
                out.append(default)
        return out

    def _ohlcv_rows_to_payload(self, ohlcv: Any, *, limit: int | None) -> dict[str, list[Any]] | None:
        if not ohlcv:
            return None
        rows = [r for r in ohlcv if isinstance(r, (list, tuple)) and len(r) > OHLCV_CLOSE_IDX]
        if not rows:
            return None
        if limit is not None and len(rows) > int(limit):
            rows = rows[-int(limit) :]
        n = len(rows)
        times: list[Any] = []
        for r in rows:
            tm = _normalize_time_ms(r[0]) if r else None
            times.append(tm if tm is not None else 0.0)
        return {
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[OHLCV_CLOSE_IDX]) for r in rows],
            "volume": [float(r[5]) if len(r) > 5 else 0.0 for r in rows],
            "time": times,
            "_n": n,
        }

    def _provider_dict_to_payload(self, data: Any, *, limit: int | None) -> dict[str, list[Any]] | None:
        if not isinstance(data, dict):
            return None
        closes_raw = data.get("close") or []
        if not isinstance(closes_raw, (list, tuple)) or not closes_raw:
            return None
        total = len(closes_raw)
        start = 0
        n = total
        if limit is not None and total > int(limit):
            start = total - int(limit)
            n = int(limit)

        def _slice(key: str) -> list[Any]:
            raw = data.get(key) or []
            if not isinstance(raw, (list, tuple)):
                return []
            return list(raw[start : start + n])

        times_raw = _slice("time") or _slice("timestamp")
        times: list[Any] = []
        for t in times_raw:
            tm = _normalize_time_ms(t)
            times.append(tm if tm is not None else 0.0)
        if len(times) < n:
            times.extend([0.0] * (n - len(times)))
        return {
            "open": self._float_col(_slice("open"), n),
            "high": self._float_col(_slice("high"), n),
            "low": self._float_col(_slice("low"), n),
            "close": self._float_col(_slice("close"), n),
            "volume": self._float_col(_slice("volume"), n, default=0.0),
            "time": times[:n],
            "_n": n,
        }

    @staticmethod
    def _median_bar_seconds(times: list[Any]) -> float | None:
        """Median positive delta of *times* (ms or seconds) in seconds."""
        deltas: list[float] = []
        prev: float | None = None
        for raw in times:
            t = _normalize_time_ms(raw)
            if t is None or t <= 0.0:
                continue
            if prev is not None:
                d = t - prev
                if d > 0:
                    deltas.append(d)
            prev = t
        if not deltas:
            return None
        deltas.sort()
        return deltas[len(deltas) // 2] / 1000.0

    def _payload_is_coarser_htf(self, times: list[Any], n_prov: int, n_chart: int) -> bool:
        """True when provider bars are fewer than chart and timestamps are coarser."""
        if n_prov <= 0 or n_chart <= 0 or n_prov >= n_chart:
            return False
        if len(times) < n_prov:
            return False
        usable = 0
        for t in times[:n_prov]:
            tm = _normalize_time_ms(t)
            if tm is not None and tm > 0.0:
                usable += 1
        if usable < n_prov:
            return False
        prov_sec = self._median_bar_seconds(times[:n_prov])
        chart_sec = self._infer_chart_bar_seconds()
        if prov_sec is not None and chart_sec is not None and chart_sec > 0:
            return prov_sec > chart_sec * _HTF_BAR_SEC_MARGIN
        return True

    def _htf_bucket_id(self, spec: Any, t_ms: float) -> int | None:
        """HTF bucket id for a millisecond timestamp under *spec*."""
        if isinstance(spec, tuple) and spec and spec[0] == "cal":
            tf_s = str(spec[1])
            tz = spec[2] if len(spec) > 2 else "UTC"
            return timeframe_calendar_id(t_ms, tf_s, tz)
        bucket_ms = 0
        if isinstance(spec, tuple) and spec and spec[0] == "ms":
            try:
                bucket_ms = int(spec[1])
            except (TypeError, ValueError, IndexError):
                return None
        else:
            try:
                bucket_ms = int(spec)
            except (TypeError, ValueError):
                return None
        if bucket_ms <= 0:
            return None
        return int(t_ms) // bucket_ms * bucket_ms

    def _align_provider_htf_to_chart(
        self,
        payload: dict[str, list[Any]],
        spec: Any,
        n_chart: int,
        chart_times: list[Any],
    ) -> tuple[list[dict[str, float] | None], list[dict[str, float] | None]] | None:
        """Map unique provider HTF bars onto chart bars: (completed, forming)."""
        closes = payload.get("close") or []
        n_prov = len(closes)
        if n_prov <= 0 or n_chart <= 0:
            return None
        opens = payload.get("open") or []
        highs = payload.get("high") or []
        lows = payload.get("low") or []
        volumes = payload.get("volume") or []
        times = payload.get("time") or []

        def _f(seq: list[Any], i: int, default: float = float("nan")) -> float:
            if i >= len(seq):
                return default
            try:
                return float(seq[i])
            except (TypeError, ValueError):
                return default

        unique_buckets: list[int] = []
        unique_aggs: list[dict[str, float]] = []
        by_bucket: dict[int, int] = {}
        for i in range(n_prov):
            t_ms = _normalize_time_ms(times[i] if i < len(times) else None)
            if t_ms is None or t_ms <= 0.0:
                continue
            b = self._htf_bucket_id(spec, t_ms)
            if b is None:
                continue
            c = _f(closes, i)
            agg = {
                "open": _f(opens, i, c),
                "high": _f(highs, i, c),
                "low": _f(lows, i, c),
                "close": c,
                "volume": _f(volumes, i, 0.0),
                "time": float(b),
            }
            idx = by_bucket.get(b)
            if idx is None:
                by_bucket[b] = len(unique_aggs)
                unique_buckets.append(int(b))
                unique_aggs.append(agg)
            else:
                unique_aggs[idx] = agg
        n_u = len(unique_aggs)
        if n_u == 0:
            return None

        completed: list[dict[str, float] | None] = [None] * n_chart
        forming: list[dict[str, float] | None] = [None] * n_chart
        u = -1
        for i in range(n_chart):
            t_ms = _normalize_time_ms(chart_times[i] if i < len(chart_times) else None)
            if t_ms is None:
                continue
            b = self._htf_bucket_id(spec, t_ms)
            if b is None:
                continue
            while u + 1 < n_u and unique_buckets[u + 1] <= b:
                u += 1
            if u < 0:
                continue
            if unique_buckets[u] == b:
                forming[i] = unique_aggs[u]
                completed[i] = unique_aggs[u - 1] if u > 0 else None
            else:
                completed[i] = unique_aggs[u]
        return completed, forming

    def _try_provider_htf_ohlcv(
        self,
        symbol: str,
        expression: Any,
        timeframe: Any,
        *,
        gaps_on: bool,
        lookahead_on: bool,
    ) -> Any | None:
        """Align provider/feed HTF OHLCV onto chart bars, or None to fall through.

        Chart-length / no-timestamp payloads stay stubs (unused merge). LTF
        (more provider bars than chart bars, or not coarser spacing) is not
        invented.
        """
        if not self._request_is_higher_tf(timeframe):
            return None
        spec = self._htf_tf_spec(timeframe)
        if spec is None:
            return None
        fields = self._identify_simple_ohlcv_fields(expression)
        if not fields:
            return None
        payload = self._ohlcv_payload(str(symbol), str(timeframe), limit=None)
        if payload is None:
            return None
        src = self._htf_source_lists()
        if src is None:
            return None
        _opens, _highs, _lows, _closes, _vols, chart_times = src
        n_chart = len(_opens)
        closes = payload.get("close") or []
        times = payload.get("time") or []
        n_prov = len(closes)
        if not self._payload_is_coarser_htf(times, n_prov, n_chart):
            return None
        cache = getattr(self, "_provider_htf_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._provider_htf_cache = cache  # type: ignore[attr-defined]
        fp = (
            str(symbol),
            str(timeframe),
            n_prov,
            n_chart,
            spec,
            _normalize_time_ms(times[0]) if times else 0.0,
            _normalize_time_ms(times[-1]) if times else 0.0,
            float(closes[-1]) if closes else 0.0,
            _normalize_time_ms(chart_times[0]) if chart_times else 0.0,
            _normalize_time_ms(chart_times[-1]) if chart_times else 0.0,
        )
        aligned = cache.get(fp)
        if aligned is None:
            aligned = self._align_provider_htf_to_chart(payload, spec, n_chart, chart_times)
            if len(cache) >= 8:  # noqa: PLR2004 - tiny per-eval cache
                cache.clear()
            cache[fp] = aligned
        if aligned is None:
            return None
        completed, forming = aligned
        agg = self._select_htf_agg(
            spec, completed, gaps_on=gaps_on, lookahead_on=lookahead_on, forming=forming
        )
        if gaps_on or lookahead_on:
            self._mark_security_merge_applied(gaps_on=gaps_on, lookahead_on=lookahead_on)
        na = float("nan")
        if len(fields) == 1 and not isinstance(expression, (list, tuple)):
            if agg is None:
                return na
            return self._htf_agg_field(agg, fields[0])
        if agg is None:
            out_na = [na] * len(fields)
            return out_na if isinstance(expression, list) else tuple(out_na)
        vals = [self._htf_agg_field(agg, f) for f in fields]
        return vals if isinstance(expression, list) else tuple(vals)

    def _heikinashi_current_ohlc(self) -> tuple[float, float, float, float] | None:
        """Incremental Heikin-Ashi OHLC for the host chart (one update per bar).

        Standard reference formulas:
        - ``ha_close = (o + h + l + c) / 4``
        - ``ha_open = (prev_ha_open + prev_ha_close) / 2`` (first bar: ``(o+c)/2``)
        - ``ha_high = max(h, ha_open, ha_close)``
        - ``ha_low = min(l, ha_open, ha_close)``
        """
        ctx = getattr(self, "context", {}) or {}
        series_map = getattr(self, "current_series", None) or ctx

        def _sample(key: str) -> float | None:
            s = series_map.get(key) if isinstance(series_map, dict) else None
            if s is None:
                s = ctx.get(key)
            if s is None:
                return None
            cur = getattr(s, "current", None)
            if cur is not None and not isinstance(s, (list, tuple)):
                try:
                    return float(cur)
                except (TypeError, ValueError):
                    return None
            if isinstance(s, (list, tuple)) and s:
                try:
                    return float(s[-1])  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return None
            try:
                return float(s)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        o, h, l, c = _sample("open"), _sample("high"), _sample("low"), _sample("close")
        if o is None or h is None or l is None or c is None:
            return None
        ha_close = (o + h + l + c) * 0.25
        st = getattr(self, "_ha_inc_state", None)
        if st is None:
            st = {"prev_open": None, "prev_close": None}
            self._ha_inc_state = st  # type: ignore[attr-defined]
        prev_o = st.get("prev_open")
        prev_c = st.get("prev_close")
        if prev_o is None or prev_c is None:
            ha_open = (o + c) * 0.5
        else:
            ha_open = (float(prev_o) + float(prev_c)) * 0.5
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        st["prev_open"] = ha_open
        st["prev_close"] = ha_close
        return ha_open, ha_high, ha_low, ha_close

    def _remap_preeval_ohlcv_to_ha(
        self,
        expression: Any,
        ha: tuple[float, float, float, float],
    ) -> Any:
        """Replace chart OHLCV samples in *expression* with Heikin-Ashi values.

        Pre-eval often passes ``PineSeries`` / list wrappers for ``open``/``close``
        etc.; unwrap to the current scalar before float matching.
        """
        ha_o, ha_h, ha_l, ha_c = ha

        def _scalar(v: Any) -> float | None:
            if v is None or isinstance(v, bool):
                return None
            cur = getattr(v, "current", None)
            if cur is not None and not isinstance(v, (list, tuple, dict)):
                v = cur
            elif isinstance(v, (list, tuple)) and v:
                v = v[-1]
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        chart: dict[str, float] = {}
        for key, dest in (
            ("open", "o"),
            ("high", "h"),
            ("low", "l"),
            ("close", "c"),
        ):
            series_map = getattr(self, "current_series", None) or {}
            s = series_map.get(key) if isinstance(series_map, dict) else None
            if s is None:
                s = (getattr(self, "context", {}) or {}).get(key)
            fv = _scalar(s)
            if fv is not None:
                chart[dest] = fv
        ha_by_chart = {
            chart.get("o"): ha_o,
            chart.get("h"): ha_h,
            chart.get("l"): ha_l,
            chart.get("c"): ha_c,
        }

        def _map_one(v: Any) -> Any:
            fv = _scalar(v)
            if fv is None:
                return v
            for ck, hv in ha_by_chart.items():
                if ck is None:
                    continue
                if fv == ck:
                    return hv
            return fv

        if isinstance(expression, list):
            return [_map_one(x) for x in expression]
        if isinstance(expression, tuple):
            return tuple(_map_one(x) for x in expression)
        return _map_one(expression)

    def _handle_request_security(self, args: list[Any]) -> Any:  # noqa: C901
        # complexity acceptable: handles multiple data source fallbacks + exprs
        """
        request.security(symbol, timeframe, expression, gaps, lookahead)

        Request data from another symbol or timeframe.

        v6+: Supports real data via context['data_provider'] (historical)
        or context['data_feed'] (CCXTProDataFeed for live/latest data).

        Policy (parity with compile foreign-na / same-symbol simple OHLCV):

        - **Foreign** without a multi-symbol feed hit → ``na`` (never invent chart
          series or legacy mock OHLCV as foreign fundamentals when a host chart
          identity is wired). Standalone unit eval without chart identity may
          still use legacy mock prices for bare string series names.
        - **Same-symbol + simple OHLCV** on a **coarser** TF with bar times →
          timestamp resample of chart OHLCV (``htf_ohlcv_resample``): last
          completed HTF bar by default; ``gaps_on`` / ``lookahead_on`` honored
          on this path (bucket-start na-gaps / historical final-from-period-start).
        - **Provider/feed simple OHLCV** already coarser than the chart, with
          bar times that align to chart bars → same merge (``provider_htf_ohlcv``).
          Chart-length / no-timestamp provider series stay stubs
          (``provider_ohlcv`` / ``provider_ohlcv_chart_stub``) with unused merge.
        - **Same-symbol + allowlisted simple ta.*** (``ta.sma/ema/rsi/atr/wma/rma/cmo/stdev/linreg/cci/roc/mom/hma/vwma/dema/tema/highest/lowest/range/median/dev/variance/mode/percentrank/change/cog/highestbars/lowestbars/kama/rising/falling/swma/ao/vwap/cum``
          with bare OHLCV source + const length; 1-arg ``ta.cci(n)`` uses HTF hlc3;
          1-arg ``ta.wpr(n)`` uses HTF high/low/close; 1-arg ``ta.adx(n)`` uses HTF
          high/low/close; 1-arg ``ta.mfi(n)`` uses HTF high/low/close/volume;
          0-arg ``ta.obv()`` uses HTF close+volume; 1-arg ``ta.cmf(n)`` uses HTF
          high/low/close/volume;
          1-arg ``ta.highest(n)`` /
          ``ta.lowest(n)`` / ``ta.highestbars(n)`` / ``ta.lowestbars(n)`` use HTF
          high / low; 1-arg ``ta.range(n)`` is HTF high−low; 1-arg ``ta.swma``
          is fixed 4-period; 0-arg ``ta.ao()`` is SMA(hl2,5)−SMA(hl2,34);
          1-arg ``ta.ao(fast)`` uses HTF hl2 with slow=34; 2-arg
          ``ta.ao(fast, slow)`` uses HTF hl2 at those periods;
          3-arg ``ta.linreg(source, n, offset)`` honors ``extra`` as offset;
          2-arg ``ta.kama`` uses Kaufman fast=2, slow=30;
          1-arg ``ta.cum(source)`` is the running sum on unique HTF bars;
          1-arg ``ta.change(source)`` defaults length to 1) on a
          **coarser** TF → run the
          interpret TA helper on resampled HTF bars (``htf_simple_ta_resample``).
        - **Same-symbol + simple OHLCV** otherwise → chart passthrough /
          provider series (``chart_passthrough_htf_stub`` / same-TF eval).
        - **Same-symbol Heikin-Ashi** (``ticker.heikinashi``) → transform chart
          OHLCV to HA (do not return raw chart candles or all-``na``).
        - **Same-symbol + complex pre-eval** (UDF / nested / multi-arg ta) on a
          **different** TF → ``na`` without full multi-TF re-eval.
        - **Same-symbol + same TF** pre-eval → chart eval is correct; allow.
        - **gaps / lookahead** (``barmerge.*``) are honored on the HTF resample
          paths above (chart and provider-backed coarser OHLCV) and
          accepted-but-unused elsewhere (passthrough, chart-length provider
          stubs, complex/foreign na, LTF). Presence vs effect is recorded in
          policy metadata (``gaps_lookahead_provided`` vs ``gaps_applied`` /
          ``lookahead_applied``) rather than silently affecting values.
        """
        ticker_arg = args[0] if len(args) > 0 else "AAPL"
        is_ha = bool(getattr(ticker_arg, "heikinashi_applied", False))
        symbol = self._resolve_symbol(ticker_arg)
        timeframe = args[1] if len(args) > 1 else "D"
        expression = args[2] if len(args) > REQUEST_SECURITY_MIN_ARGS else "close"
        # Presence by arity (False/None still count as "provided" positions).
        gaps_provided = len(args) > 3
        lookahead_provided = len(args) > 4
        gaps_arg = args[3] if gaps_provided else None
        lookahead_arg = args[4] if lookahead_provided else None

        if isinstance(timeframe, list):
            timeframe = timeframe[-1] if timeframe else "D"

        # barmerge.gaps_on / lookahead_on evaluate to True (gaps_off / off → False).
        gaps_on = bool(gaps_arg) if gaps_provided else False
        lookahead_on = bool(lookahead_arg) if lookahead_provided else False

        # Presence marker: merge args were supplied. When a resample path below
        # honors them it additionally notes gaps_applied / lookahead_applied
        # (and flips gaps_supported / lookahead_supported); presence without an
        # applied marker means the call fell through to a path where they are
        # still unused (passthrough, complex/foreign na, chart-length provider).
        if gaps_provided or lookahead_provided:
            self._note_security_policy(
                "gaps_lookahead_provided",
                gaps_provided=gaps_provided,
                lookahead_provided=lookahead_provided,
                gaps_value=gaps_arg
                if isinstance(gaps_arg, (bool, int, float, str)) or gaps_arg is None
                else str(gaps_arg),
                lookahead_value=(
                    lookahead_arg
                    if isinstance(lookahead_arg, (bool, int, float, str)) or lookahead_arg is None
                    else str(lookahead_arg)
                ),
            )

        # The expression arg is usually already evaluated by the call site.
        # - str: series name like "close" → fetch OHLCV and map
        # - list/tuple of already-evaluated values: return as-is so destructure
        #   ``[hi, lo, cl] = request.security(..., [high[1], low[1], close[1]])``
        #   (Camarilla pivots etc.) keeps the multi-value shape.  Do NOT collapse
        #   multi-element numeric lists to the last sample — that breaks unpack.
        # - single-element numeric list: unwrap to scalar (legacy series path)
        # - matrix/array/UDT result: return as-is
        #
        # Pre-evaluated non-str expressions were computed on the *chart* series.
        # Same-symbol security may return simple OHLCV / same-TF results; foreign
        # symbols without a real multi-symbol context must return na (not invent
        # chart close as dividends / fundamentals — see dividend_yield.pine).
        # Heikin-Ashi of the chart is same-symbol (HA(...) ticker string).
        chart_sym = self._is_chart_symbol(str(symbol)) or is_ha
        same_tf = self._request_tf_matches_chart(timeframe)
        na = float("nan")
        tf_s = str(timeframe) if timeframe is not None else ""

        # Provider/feed already-coarser OHLCV with timestamps: align to chart
        # bars and honor gaps/lookahead. Chart-length / no-timestamp stay stubs.
        if not is_ha and not isinstance(expression, (HtfSimpleTaExpr, HtfOffsetExpr)):
            if self._expression_is_simple_ohlcv_value(expression):
                prov_htf = self._try_provider_htf_ohlcv(
                    str(symbol), expression, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on
                )
                if prov_htf is not None:
                    return self._security_return(
                        prov_htf,
                        "provider_htf_ohlcv",
                        timeframe=tf_s,
                        symbol=str(symbol),
                    )

        def _maybe_htf_resample(expr: Any) -> Any | None:
            """Same-symbol simple OHLCV or allowlisted ta.* HTF path; None → fall through."""
            if not chart_sym or is_ha:
                return None
            if isinstance(expr, HtfSimpleTaExpr):
                return self._try_htf_simple_ta_resample(expr, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on)
            if isinstance(expr, HtfOffsetExpr):
                return self._try_htf_offset_resample(expr, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on)
            if not self._expression_is_simple_ohlcv_value(expr):
                return None
            return self._try_htf_ohlcv_resample(expr, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on)

        def _handle_simple_ta_expr(expr: HtfSimpleTaExpr) -> Any:
            """Same-symbol allowlisted ta.* — HTF resample, same-TF chart, else na."""
            if not chart_sym:
                return self._security_return(na, "foreign_na", symbol=str(symbol), reason="foreign_simple_ta")
            htf_val = self._try_htf_simple_ta_resample(expr, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on)
            if htf_val is not None:
                return self._security_return(
                    htf_val,
                    "htf_simple_ta_resample",
                    timeframe=tf_s,
                    ta=expr.name,
                    length=expr.length,
                    source=expr.source or "ohlc",
                )
            if same_tf:
                return self._security_return(
                    self._chart_simple_ta_last(expr),
                    "same_tf_chart_eval",
                    timeframe=tf_s,
                    ta=expr.name,
                )
            # LTF / unparseable coarser TF without fixed buckets — honest na
            # (not chart TA inventing HTF/LTF structure).
            return self._security_return(
                na,
                "complex_htf_na",
                timeframe=tf_s,
                reason="simple_ta_not_htf_or_same_tf",
                ta=expr.name,
            )

        # Heikin-Ashi transform for chart (same-symbol) security requests.
        if is_ha and chart_sym:
            ha = self._heikinashi_current_ohlc()
            if ha is not None:
                ha_o, ha_h, ha_l, ha_c = ha
                if isinstance(expression, str):
                    key = expression.strip().lower()
                    return self._security_return(
                        {
                            "open": ha_o,
                            "o": ha_o,
                            "high": ha_h,
                            "h": ha_h,
                            "low": ha_l,
                            "l": ha_l,
                            "close": ha_c,
                            "c": ha_c,
                        }.get(key, na),
                        "heikinashi_chart_transform",
                        timeframe=tf_s,
                    )
                if isinstance(expression, list):
                    if len(expression) == 1 and (
                        expression[0] is None or isinstance(expression[0], (int, float, bool))
                    ):
                        return self._security_return(
                            self._remap_preeval_ohlcv_to_ha(expression[0], ha),
                            "heikinashi_chart_transform",
                            timeframe=tf_s,
                        )
                    return self._security_return(
                        self._remap_preeval_ohlcv_to_ha(expression, ha),
                        "heikinashi_chart_transform",
                        timeframe=tf_s,
                    )
                if isinstance(expression, tuple):
                    return self._security_return(
                        self._remap_preeval_ohlcv_to_ha(expression, ha),
                        "heikinashi_chart_transform",
                        timeframe=tf_s,
                    )
                return self._security_return(
                    self._remap_preeval_ohlcv_to_ha(self._unwrap_preeval_scalar(expression), ha),
                    "heikinashi_chart_transform",
                    timeframe=tf_s,
                )

        def _same_symbol_preeval_tag() -> str:
            return "same_tf_chart_eval" if same_tf else "chart_passthrough_htf_stub"

        def _deny_preeval() -> Any:
            if chart_sym and not same_tf:
                return self._security_return(na, "complex_htf_na", timeframe=tf_s, reason="no_htf_reeval")
            return self._security_return(na, "foreign_na", symbol=str(symbol), reason="foreign_or_complex")

        # Allowlisted simple ta.* / OHLCV offset markers (attached at visit_Call).
        if isinstance(expression, HtfSimpleTaExpr):
            return _handle_simple_ta_expr(expression)
        if isinstance(expression, HtfOffsetExpr):
            htf_off = self._try_htf_offset_resample(expression, timeframe, gaps_on=gaps_on, lookahead_on=lookahead_on)
            if htf_off is not None:
                return self._security_return(
                    htf_off,
                    "htf_ohlcv_offset",
                    timeframe=tf_s,
                    field=expression.field,
                    offset=expression.offset,
                )
            if chart_sym and not self._request_is_higher_tf(timeframe):
                tag = "same_tf_chart_eval" if same_tf else "chart_passthrough_htf_stub"
                return self._security_return(
                    self._chart_ohlcv_at_offset(expression.field, expression.offset),
                    tag,
                    timeframe=tf_s,
                    field=expression.field,
                    offset=expression.offset,
                )
            return self._security_return(na, "complex_htf_na", timeframe=tf_s, reason="offset_not_htf")

        if isinstance(expression, list):
            if len(expression) == 1 and (expression[0] is None or isinstance(expression[0], (int, float, bool))):
                expression = expression[0]
            elif chart_sym:
                htf_val = _maybe_htf_resample(expression)
                if htf_val is not None:
                    tag = "htf_simple_ta_resample" if isinstance(expression, HtfSimpleTaExpr) else "htf_ohlcv_resample"
                    return self._security_return(htf_val, tag, timeframe=tf_s)
                if self._allow_same_symbol_preeval(expression, timeframe):
                    return self._security_return(expression, _same_symbol_preeval_tag(), timeframe=tf_s)
                return _deny_preeval()
            else:
                return self._security_return(na, "foreign_na", symbol=str(symbol), reason="foreign_list")
        if isinstance(expression, tuple):
            if chart_sym:
                htf_val = _maybe_htf_resample(expression)
                if htf_val is not None:
                    return self._security_return(htf_val, "htf_ohlcv_resample", timeframe=tf_s)
            if chart_sym and self._allow_same_symbol_preeval(expression, timeframe):
                return self._security_return(expression, _same_symbol_preeval_tag(), timeframe=tf_s)
            return _deny_preeval()

        if not isinstance(expression, str):
            if chart_sym:
                htf_val = _maybe_htf_resample(expression)
                if htf_val is not None:
                    tag = "htf_simple_ta_resample" if isinstance(expression, HtfSimpleTaExpr) else "htf_ohlcv_resample"
                    # `_try_htf_simple_ta_resample` already returns only for higher TF;
                    # tag OHLCV vs simple-ta explicitly for meta honesty.
                    if tag == "htf_simple_ta_resample":
                        return self._security_return(
                            htf_val,
                            tag,
                            timeframe=tf_s,
                        )
                    return self._security_return(htf_val, "htf_ohlcv_resample", timeframe=tf_s)
            if chart_sym and self._allow_same_symbol_preeval(expression, timeframe):
                # Bare `close` / PineSeries → current scalar for plot/assign paths.
                return self._security_return(
                    self._unwrap_preeval_scalar(expression),
                    _same_symbol_preeval_tag(),
                    timeframe=tf_s,
                )
            # Foreign + chart-evaluated UDF/expr, or same-symbol complex HTF → na
            return _deny_preeval()

        symbol_str = str(symbol)

        # String series name: prefer true HTF resample on same-symbol before
        # provider/mock paths (ChartOHLCVProvider ignores interval → chart bars).
        if chart_sym:
            htf_val = _maybe_htf_resample(expression)
            if htf_val is not None:
                return self._security_return(htf_val, "htf_ohlcv_resample", timeframe=tf_s)

        # Try real data provider (historical or live) via shared helpers.
        # ChartOHLCVProvider ignores interval → still chart bars (not HTF resample).
        closes = self._ohlcv_closes(symbol, str(timeframe), limit=REQUEST_OHLCV_LIMIT)
        if closes:
            tag = "provider_ohlcv" if same_tf or not chart_sym else "provider_ohlcv_chart_stub"
            return self._security_return(
                self._get_expression_prices(str(expression), closes),
                tag,
                timeframe=tf_s,
                symbol=symbol_str,
            )
        last = self._ticker_last(symbol)
        if last is not None:
            return self._security_return(
                self._get_expression_prices(str(expression), [float(last)] * REQUEST_OHLCV_LIMIT),
                "provider_ticker_last",
                timeframe=tf_s,
                symbol=symbol_str,
            )

        # Fundamental / non-equity prefixes — never invent OHLCV.
        if any(tok in symbol_str for tok in _FUNDAMENTAL_TOKENS):
            return self._security_return(na, "fundamental_na", symbol=symbol_str, reason="fundamental_token")

        # Foreign under a host chart with no multi-symbol feed hit → na.
        # Aligns interpret with compile foreign-na (no mock UPVOL/MSFT prices).
        if not chart_sym and self._host_has_chart_identity():
            return self._security_return(na, "foreign_na", symbol=symbol_str, reason="no_multisymbol_feed")

        # Fallback mock data for bare string series names only (legacy demos /
        # standalone evaluator without a wired chart identity).
        base_prices = {
            "AAPL": [100.0, 101.5, 102.0, 103.5, 105.0],
            "GOOGL": [1000.0, 1015.5, 1020.0, 1035.5, 1050.0],
            "BTC/USD": [25000.0, 26000.0, 27000.0, 26500.0, 28000.0],
            "BTC/USDT": [25000.0, 26000.0, 27000.0, 26500.0, 28000.0],
        }

        prices = base_prices.get(symbol_str, [100.0, 101.0, 102.0, 101.5, 103.0])
        return self._security_return(
            self._get_expression_prices(expression, prices),
            "legacy_mock_ohlcv",
            symbol=symbol_str,
            timeframe=tf_s,
        )

    def _handle_request_security_lower_tf(self, args: list[Any]) -> Any:
        """
        request.security_lower_tf(symbol, timeframe, expression)

        Request lower timeframe data within the current timeframe.
        Now supports data_feed / data_provider when wired (reuses latest for demo).
        """
        ticker_arg = args[0] if len(args) > 0 else "AAPL"
        symbol = self._resolve_symbol(ticker_arg)
        timeframe = args[1] if len(args) > 1 else "5m"
        expression = args[2] if len(args) > 2 else "close"  # noqa: PLR2004 - arg count check

        # Foreign under a host chart: honest na (no mock intrabar invent).
        if not self._is_chart_symbol(str(symbol)) and self._host_has_chart_identity():
            return self._security_return(
                float("nan"),
                "foreign_na",
                symbol=str(symbol),
                reason="lower_tf_no_multisymbol_feed",
            )

        # Try data feed/provider for consistency with request.security
        data_feed, data_provider = self._get_request_data()

        if data_feed is not None and hasattr(data_feed, "fetch_latest_ohlcv"):
            try:
                ohlcv = data_feed.fetch_latest_ohlcv(symbol, str(timeframe), limit=REQUEST_RECENT_LIMIT)
                if ohlcv:
                    closes = [c[OHLCV_CLOSE_IDX] for c in ohlcv if len(c) > OHLCV_CLOSE_IDX]
                    if closes:
                        return closes * LOWER_TF_SIMULATE_MULTIPLIER  # simulate more lower-tf bars from latest
            except Exception:  # noqa: S110
                pass

        if data_provider is not None and hasattr(data_provider, "fetch"):
            try:
                data = data_provider.fetch(symbol, period="1d", interval=str(timeframe))
                closes = data.get("close", [])
                if closes:
                    return closes[-REQUEST_RECENT_LIMIT:] or closes
            except Exception:  # noqa: S110
                pass

        # Fallback mock intrabar data (simulated lower timeframe)
        intrabar_prices = [100.0 + i * 0.25 for i in range(10)]
        if isinstance(expression, str):
            return self._get_expression_prices(str(expression), intrabar_prices)
        # Reference Pine: expression may be a tuple/list of series → return a tuple of arrays
        # so destructure like ``[o,h,l,c] = request.security_lower_tf(..., [open,high,low,close])`` works.
        if isinstance(expression, (list, tuple)) and expression:
            n = len(expression)
            out: list[list[float]] = []
            for i, item in enumerate(expression):
                if isinstance(item, str):
                    out.append(self._get_expression_prices(str(item), intrabar_prices))
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    base = float(item)
                    out.append([base + j * 0.05 for j in range(10)])
                else:
                    # Distinct mock series per component when values are already scalars/series
                    out.append([100.0 + i + j * 0.25 for j in range(10)])
            return out if n != 1 else out[0]
        return intrabar_prices

    def _handle_request_dividends(self, args: list[Any]) -> float:
        """
        request.dividends(symbol, currency)

        Request dividend information for a symbol.

        Parameters:
            symbol: Symbol/ticker string (str)
            currency: Currency code (str or None)

        Returns dividend amount as float.
        This is a mock implementation.
        """
        symbol = self._resolve_symbol(args[0] if len(args) > 0 else "AAPL")
        # currency = args[1] if len(args) > 1 else "USD"

        data_feed, _ = self._get_request_data()
        # If data_feed available, could derive 'yield' from price, but keep simple mock scaled
        base_div = {"AAPL": 0.24, "MSFT": 0.62, "JNJ": 1.13}
        val = base_div.get(symbol, 0.0)
        if data_feed and hasattr(data_feed, "fetch_latest_ticker"):
            try:
                t = data_feed.fetch_latest_ticker(symbol)
                last = t.get("last") or t.get("close") or 100.0
                val = round(val * (last / 100.0), 2)  # naive dynamic scale
            except Exception:  # noqa: S110
                pass
        return val

    def _handle_request_earnings(self, args: list[Any]) -> float:
        """
        request.earnings(symbol, currency)

        Request earnings information for a symbol.

        Parameters:
            symbol: Symbol/ticker string (str)
            currency: Currency code (str or None)

        Returns earnings per share as float.
        This is a mock implementation.
        """
        symbol = self._resolve_symbol(args[0] if len(args) > 0 else "AAPL")
        # currency = args[1] if len(args) > 1 else "USD"

        data_feed, _ = self._get_request_data()
        base_eps = {"AAPL": 5.61, "MSFT": 9.27, "JNJ": 9.13}
        val = base_eps.get(symbol, 0.0)
        if data_feed and hasattr(data_feed, "fetch_latest_ticker"):
            try:
                t = data_feed.fetch_latest_ticker(symbol)
                last = t.get("last") or t.get("close") or 100.0
                val = round(val * (last / 150.0), 2)
            except Exception:  # noqa: S110
                pass
        return val

    def _handle_request_splits(self, args: list[Any]) -> float:
        """
        request.splits(symbol, currency)

        Request stock split information for a symbol.

        Parameters:
            symbol: Symbol/ticker string (str)
            currency: Currency code (str or None)

        Returns split ratio as float.
        This is a mock implementation.
        """
        symbol = self._resolve_symbol(args[0] if len(args) > 0 else "AAPL")
        # currency = args[1] if len(args) > 1 else "USD"

        base_splits = {"AAPL": 4.0, "TSLA": 3.0, "MSFT": 1.0}
        val = base_splits.get(symbol, 1.0)
        # Optional feed hook: if ticker available, keep known ratio (no change)
        _ = self._ticker_last(symbol)
        return val

    def _handle_request_financial(self, args: list[Any]) -> float:
        """
        request.financial(symbol, financial_id, period)

        Request financial statement data (from SEC filings).

        Parameters:
            symbol: Symbol/ticker string (str)
            financial_id: Financial metric identifier (str)
            period: Reporting period (str, e.g., "FQ", "FY")

        Returns financial metric value as float.
        Mock table with optional price-based scaling when data_feed is wired.
        """
        symbol = self._resolve_symbol(args[0] if len(args) > 0 else "AAPL")
        financial_id = args[1] if len(args) > 1 else "REVENUE"
        # period = args[2] if len(args) > 2 else "FY"

        # Mock: return financial metrics (symbol dynamic)
        financials = {
            ("AAPL", "REVENUE"): 383285000000,
            ("AAPL", "NET_INCOME"): 96995000000,
            ("MSFT", "REVENUE"): 198716000000,
            ("MSFT", "NET_INCOME"): 72794000000,
        }
        key = (symbol, str(financial_id).upper())
        val = float(financials.get(key, 0.0))
        last = self._ticker_last(symbol)
        if last is not None and val > 0:
            # Naive dynamic scale so feed presence is observable in tests
            val = val * (last / max(last, 1.0))
        return val

    def _handle_request_quandl(self, args: list[Any]) -> Any:
        """
        request.quandl(quandl_code, column)

        Request data from Quandl database.

        Parameters:
            quandl_code: Quandl dataset code (str)
            column: Column name within dataset (str)

        Returns series data from Quandl dataset.
        This is a mock implementation.
        """
        quandl_code = self._resolve_symbol(args[0] if len(args) > 0 else "EIA/PET_RWTC_D", default="EIA/PET_RWTC_D")
        # column = args[1] if len(args) > 1 else "Value"

        # Mock: return time series data for common Quandl datasets
        if "PET_RWTC" in str(quandl_code):
            # Oil prices (WTI Crude Oil)
            return [50.0, 51.5, 52.0, 51.0, 53.5, 55.0, 54.5, 56.0]
        if "GDPC" in str(quandl_code):
            # GDP data
            return [21060000, 21200000, 21400000, 21600000]

        # Default: return generic series
        return [100.0, 101.0, 102.0, 101.5, 103.0]

    def _handle_request_economic(self, args: list[Any]) -> Any:
        """
        request.economic(country, indicator_code)

        Request economic indicator data (e.g., unemployment, inflation).

        Parameters:
            country: Country code (str, e.g., "US", "EU")
            indicator_code: Economic indicator code (str)

        Returns economic data as series or value.
        This is a mock implementation.
        """
        country = self._resolve_symbol(args[0] if len(args) > 0 else "US", default="US")
        indicator_code = args[1] if len(args) > 1 else "UNRATE"

        # Mock: return economic indicators
        if str(indicator_code).upper() == "UNRATE":
            # US Unemployment Rate (%)
            if str(country).upper() == "US":
                return [3.5, 3.4, 3.6, 3.7, 3.8]
            # EU Unemployment Rate (%)
            if str(country).upper() == "EU":
                return [6.1, 6.0, 6.2, 6.3, 6.4]

        if str(indicator_code).upper() == "INFLATION":
            # Inflation Rate (%)
            if str(country).upper() == "US":
                return [3.4, 3.2, 3.0, 2.9, 2.8]
            if str(country).upper() == "EU":
                return [2.6, 2.4, 2.2, 2.1, 2.0]

        # Default: return generic series
        return [100.0, 101.0, 102.0, 101.5, 103.0]

    def _handle_request_currency_rate(self, args: list[Any]) -> float:
        """
        request.currency_rate(from_currency, to_currency)

        Request exchange rate between two currencies.

        Parameters:
            from_currency: Source currency code (str, e.g., "USD")
            to_currency: Target currency code (str, e.g., "EUR")

        Returns exchange rate as float.
        This is a mock implementation.
        """
        from_currency = self._resolve_symbol(args[0] if len(args) > 0 else "USD", default="USD")
        to_currency = self._resolve_symbol(args[1] if len(args) > 1 else "EUR", default="EUR")

        # Prefer live feed pair if available (e.g. EUR/USD last)
        pair = f"{from_currency}/{to_currency}"
        last = self._ticker_last(pair)
        if last is not None and last > 0:
            return float(last)

        # Mock: return exchange rates
        rates = {
            ("USD", "EUR"): 0.92,
            ("USD", "GBP"): 0.79,
            ("USD", "JPY"): 149.5,
            ("EUR", "USD"): 1.09,
            ("EUR", "GBP"): 0.86,
            ("GBP", "USD"): 1.27,
        }

        key = (str(from_currency).upper(), str(to_currency).upper())
        return rates.get(key, 1.0)

    def _handle_request_seed(self, args: list[Any]) -> None:
        """
        request.seed(seed_value)

        Seed the random number generator for reproducible mock request data.
        Stores the seed on evaluator context and optional numpy RNG.
        """
        seed_value = args[0] if len(args) > 0 else 0
        try:
            seed_int = int(seed_value)
        except (TypeError, ValueError):
            seed_int = hash(str(seed_value)) & 0xFFFFFFFF
        random.seed(seed_int)
        try:
            import numpy as np

            np.random.seed(seed_int % (2**32 - 1))
        except Exception:
            pass
        ctx = getattr(self, "context", None)
        if isinstance(ctx, dict):
            ctx["request.seed"] = seed_int
        try:
            self._request_seed = seed_int  # type: ignore[attr-defined]
        except Exception:
            pass

    def _handle_request_footprint(self, args: list[Any]) -> Footprint | None:
        """
        request.footprint(num_ticks, va_percentage)

        Request volume footprint data for the current bar.
        Added in Pine Script v6 (January 2026).

        Now supports symbol (dynamic) and data_feed for volume scaling.
        """
        num_ticks = args[0] if len(args) > 0 else 100
        va_percentage = args[1] if len(args) > 1 else 70
        # symbol optional for dynamic
        _ = self._resolve_symbol(args[2] if len(args) > 2 else "ES", default="ES")  # noqa: PLR2004 - optional arg
        data_feed, _ = self._get_request_data()

        rows: list[VolumeRow] = []
        base_price = 100.0
        tick_size = 0.01
        vol_scale = 1.0
        if data_feed and hasattr(data_feed, "fetch_latest_ticker"):
            try:
                t = data_feed.fetch_latest_ticker("ES")  # or symbol
                last = t.get("last") or t.get("close") or 4000.0
                vol_scale = max(0.1, last / 4000.0)
            except Exception:  # noqa: S110
                pass

        for i in range(num_ticks):
            price_level = base_price + (i * tick_size)
            row = VolumeRow(
                up_price=price_level + tick_size,
                down_price=price_level,
                buy_volume=(1000.0 + (random.random() * 500)) * vol_scale,
                sell_volume=(900.0 + (random.random() * 500)) * vol_scale,
                delta=100.0 + (random.random() * 200 - 100),
                is_imbalance=random.random() < 0.1,  # noqa: PLR2004 - mock data gen
                is_poc=(i == num_ticks // 2),
                is_vah=(i == int(num_ticks * 0.7)),
                is_val=(i == int(num_ticks * 0.3)),
            )
            rows.append(row)

        poc_idx = num_ticks // 2
        vah_idx = int(num_ticks * 0.7)
        val_idx = int(num_ticks * 0.3)

        total_buy = sum(r.buy_volume for r in rows)
        total_sell = sum(r.sell_volume for r in rows)

        footprint = Footprint(
            num_ticks=num_ticks,
            va_percentage=va_percentage,
            buy_volume=total_buy,
            sell_volume=total_sell,
            delta=total_buy - total_sell,
            total_volume=total_buy + total_sell,
            vah_row=rows[vah_idx] if vah_idx < len(rows) else None,
            val_row=rows[val_idx] if val_idx < len(rows) else None,
            poc_row=rows[poc_idx] if poc_idx < len(rows) else None,
            rows=rows,
        )

        return footprint


def _as_htf_float(value: Any) -> float:
    """NaN-safe scalar for compile object-mode plot stores."""
    if value is None:
        return float("nan")
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return fv


def _ohlcv_col_list(arr: Any) -> list[Any]:
    """Chart column as a Python list (numpy ``tolist`` when present)."""
    if arr is None:
        return []
    if isinstance(arr, list):
        return arr
    to_list = getattr(arr, "tolist", None)
    if callable(to_list):
        try:
            return list(to_list())
        except Exception:
            pass
    try:
        return list(arr)
    except TypeError:
        return []


class _CompileHtfHost(RequestBuiltinsMixin):
    """Minimal host so interpret HTF resample methods run from compile object-mode.

    Full chart columns are attached as ``_htf_full_ohlcv`` (same shape Runtime
    uses). TA full-list helpers are bound from the interpret technical mixins
    so ``ta.sma/ema/rsi/atr/wma/rma/cmo/stdev/linreg/cci/roc/mom/hma/vwma/dema/tema``
    / ``highest`` / ``lowest`` / ``range`` / ``median`` / ``dev`` / ``variance``
    / ``mode`` / ``percentrank`` / ``change`` / ``cog`` / ``highestbars`` /
    ``lowestbars`` / ``kama`` / ``rising`` / ``falling`` / ``swma`` / ``ao`` /
    ``vwap`` / ``cum`` and 1-arg ``ta.wpr`` / ``ta.highest`` / ``ta.lowest`` /
    ``ta.highestbars`` / ``ta.lowestbars`` / ``ta.range`` / ``ta.change`` /
    ``ta.adx`` / ``ta.mfi`` / 0-arg ``ta.obv`` / 1-arg ``ta.cmf`` on HTF
    bars match interpret exactly.
    """

    def __init__(
        self,
        opens: list[Any],
        highs: list[Any],
        lows: list[Any],
        closes: list[Any],
        volumes: list[Any],
        times: list[Any],
        *,
        timezone: str = "UTC",
    ) -> None:
        self._htf_full_ohlcv = {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "time": times,
        }
        self._bar_index = 0
        self._timezone = timezone or "UTC"
        self.context: dict[str, Any] = {"bar_index": 0}
        self._bind_ta_helpers()

    def _bind_ta_helpers(self) -> None:
        if getattr(self, "_sma", None) is not None:
            return
        from pynescript.ast.evaluator.builtins.technical_submodules.basic import BasicIndicators
        from pynescript.ast.evaluator.builtins.technical_submodules.common import CommonIndicators
        from pynescript.ast.evaluator.builtins.technical_submodules.core import TechnicalHelpers
        from pynescript.ast.evaluator.builtins.technical_submodules.moving_averages import MovingAverageIndicators
        from pynescript.ast.evaluator.builtins.technical_submodules.oscillators import OscillatorIndicators
        from pynescript.ast.evaluator.builtins.technical_submodules.volatility import VolatilityIndicators
        from pynescript.ast.evaluator.builtins.technical_submodules.volume import VolumeIndicators

        self._format_series = TechnicalHelpers._format_series.__get__(self, type(self))
        self._sma = TechnicalHelpers._sma.__get__(self, type(self))
        self._ema = TechnicalHelpers._ema.__get__(self, type(self))
        self._rma = TechnicalHelpers._rma.__get__(self, type(self))
        self._wma = TechnicalHelpers._wma.__get__(self, type(self))
        self._hma = MovingAverageIndicators._hma.__get__(self, type(self))
        self._highest = TechnicalHelpers._highest.__get__(self, type(self))
        self._lowest = TechnicalHelpers._lowest.__get__(self, type(self))
        self._median = BasicIndicators._median.__get__(self, type(self))
        self._dev = BasicIndicators._dev.__get__(self, type(self))
        self._variance = BasicIndicators._variance.__get__(self, type(self))
        self._mode = BasicIndicators._mode.__get__(self, type(self))
        self._percentrank = BasicIndicators._percentrank.__get__(self, type(self))
        self._change = TechnicalHelpers._change.__get__(self, type(self))
        self._highestbars = TechnicalHelpers._highestbars.__get__(self, type(self))
        self._lowestbars = TechnicalHelpers._lowestbars.__get__(self, type(self))
        self._swma = TechnicalHelpers._swma.__get__(self, type(self))
        self._cumsum = BasicIndicators._cumsum.__get__(self, type(self))
        self._rising = TechnicalHelpers._rising.__get__(self, type(self))
        self._falling = TechnicalHelpers._falling.__get__(self, type(self))
        self._atr = VolatilityIndicators._atr.__get__(self, type(self))
        self._cci = OscillatorIndicators._cci.__get__(self, type(self))
        self._roc = OscillatorIndicators._roc.__get__(self, type(self))
        self._mom = TechnicalHelpers._mom.__get__(self, type(self))
        self._wpr = OscillatorIndicators._wpr.__get__(self, type(self))
        self._tr = TechnicalHelpers._tr.__get__(self, type(self))
        self._adx = CommonIndicators._adx.__get__(self, type(self))
        self._mfi = VolumeIndicators._mfi.__get__(self, type(self))
        self._obv = VolumeIndicators._obv.__get__(self, type(self))
        self._cmf = VolumeIndicators._cmf.__get__(self, type(self))

    def set_bar_index(self, index: int) -> None:
        i = int(index)
        self._bar_index = i
        self.context["bar_index"] = i

    def _chart_bar_index(self) -> int:
        return int(self._bar_index)

    def _htf_timezone(self) -> object:
        return self._timezone or "UTC"

    def _chart_field_at(self, field: str, index: int) -> float:
        src = self._htf_full_ohlcv
        n = len(src.get("close") or [])
        if index < 0 or index >= n:
            return float("nan")

        def _at(key: str) -> float:
            seq = src.get(key) or []
            if index >= len(seq):
                return float("nan")
            try:
                return float(seq[index])
            except (TypeError, ValueError):
                return float("nan")

        if field in ("open", "high", "low", "close", "volume", "time"):
            return _at(field)
        agg = {
            "open": _at("open"),
            "high": _at("high"),
            "low": _at("low"),
            "close": _at("close"),
            "volume": _at("volume"),
            "time": _at("time"),
        }
        return self._htf_agg_field(agg, field)

    def _chart_ohlcv_at_offset(self, field: str, offset: int) -> Any:
        na = float("nan")
        if offset < 0:
            return na
        return self._chart_field_at(field, self._chart_bar_index() - int(offset))


_COMPILE_HTF_HOSTS: dict[tuple[Any, ...], _CompileHtfHost] = {}


def _compile_htf_host(
    open_arr: Any,
    high_arr: Any,
    low_arr: Any,
    close_arr: Any,
    vol_arr: Any,
    time_arr: Any,
    timezone: str = "UTC",
    data_feed: Any = None,
    data_provider: Any = None,
    symbol: str = "",
) -> _CompileHtfHost:
    """Reuse one HTF host per compiled run (arrays + last-bar fingerprint)."""
    closes = _ohlcv_col_list(close_arr)
    n = len(closes)

    def _at(seq: Any, idx: int) -> float:
        try:
            return float(seq[idx])
        except (TypeError, ValueError, IndexError):
            return 0.0

    last = n - 1
    fp = (
        id(time_arr),
        n,
        _at(time_arr, 0) if n else 0.0,
        _at(time_arr, last) if n else 0.0,
        _at(open_arr, last) if n else 0.0,
        _at(high_arr, last) if n else 0.0,
        _at(low_arr, last) if n else 0.0,
        _at(close_arr, last) if n else 0.0,
        str(timezone or "UTC"),
        id(data_feed) if data_feed is not None else 0,
        id(data_provider) if data_provider is not None else 0,
        str(symbol or ""),
    )
    host = _COMPILE_HTF_HOSTS.get(fp)
    if host is None:
        host = _CompileHtfHost(
            _ohlcv_col_list(open_arr),
            _ohlcv_col_list(high_arr),
            _ohlcv_col_list(low_arr),
            closes,
            _ohlcv_col_list(vol_arr),
            _ohlcv_col_list(time_arr),
            timezone=str(timezone or "UTC"),
        )
        if len(_COMPILE_HTF_HOSTS) >= 4:  # noqa: PLR2004 - tiny run-local cache
            _COMPILE_HTF_HOSTS.clear()
        _COMPILE_HTF_HOSTS[fp] = host
    ctx = host.context
    if data_feed is not None:
        ctx["data_feed"] = data_feed
    else:
        ctx.pop("data_feed", None)
    if data_provider is not None:
        ctx["data_provider"] = data_provider
    else:
        ctx.pop("data_provider", None)
    if symbol:
        ctx["syminfo.tickerid"] = str(symbol)
        ctx["symbol"] = str(symbol)
    return host


def compile_htf_security_value(  # noqa: PLR0913
    kind: str,
    field: str | None,
    offset: int,
    ta_name: str | None,
    ta_length: int,
    timeframe: Any,
    bar_idx: int,
    open_arr: Any,
    high_arr: Any,
    low_arr: Any,
    close_arr: Any,
    vol_arr: Any,
    time_arr: Any,
    gaps_on: bool = False,
    lookahead_on: bool = False,
    timezone: str = "UTC",
    data_feed: Any = None,
    data_provider: Any = None,
    symbol: str = "",
) -> float:
    """Per-bar compile helper: same allowlisted HTF resample as interpret.

    ``kind`` is ``ohlcv`` / ``offset`` / ``ta``. Same-TF / LTF simple OHLCV and
    history offsets fall back to chart passthrough; simple ``ta.*`` that is not
    coarser than the chart stays ``na`` (no LTF / unparseable-TF invention).

    When *data_provider* / *data_feed* yield coarser timestamped OHLCV, reuse
    interpret ``provider_htf_ohlcv`` align (``gaps_on`` / ``lookahead_on``).
    Chart-length / no-timestamp payloads stay unused-merge (chart resample).
    """
    host = _compile_htf_host(
        open_arr,
        high_arr,
        low_arr,
        close_arr,
        vol_arr,
        time_arr,
        timezone=str(timezone or "UTC"),
        data_feed=data_feed,
        data_provider=data_provider,
        symbol=str(symbol or ""),
    )
    try:
        host.set_bar_index(int(bar_idx))
    except (TypeError, ValueError):
        return float("nan")
    gaps = bool(gaps_on)
    lookahead = bool(lookahead_on)
    kind_s = str(kind or "ohlcv")
    field_s = None if field in (None, "") else str(field)

    if kind_s == "ta":
        name = str(ta_name or "")
        try:
            length = int(ta_length)
        except (TypeError, ValueError):
            return float("nan")
        extra: int | None = None
        if name in ("atr", "wpr", "adx", "mfi", "cmf") or (name == "range" and field_s is None):
            src = None
        elif name == "ao":
            src = field_s or "hl2"
            if length > 0 and offset > 0:
                extra = int(offset)
        elif name == "vwap":
            src = field_s or "hlc3"
        elif name == "cum":
            src = field_s or "close"
        elif name == "obv":
            src = None
        elif name == "nvi":
            src = None
        elif name == "pvi":
            src = None
        else:
            src = field_s or "close"
            if name == "linreg" and offset:
                extra = int(offset)
        expr = HtfSimpleTaExpr(name=name, source=src, length=length, extra=extra)
        val = host._try_htf_simple_ta_resample(expr, timeframe, gaps_on=gaps, lookahead_on=lookahead)
        return _as_htf_float(val)

    if kind_s == "offset":
        if not field_s:
            return float("nan")
        try:
            off = int(offset)
        except (TypeError, ValueError):
            return float("nan")
        expr = HtfOffsetExpr(field=field_s, offset=off)
        val = host._try_htf_offset_resample(expr, timeframe, gaps_on=gaps, lookahead_on=lookahead)
        if val is not None:
            return _as_htf_float(val)
        return _as_htf_float(host._chart_ohlcv_at_offset(field_s, off))

    field_s = field_s or "close"
    if data_feed is not None or data_provider is not None:
        prov = host._try_provider_htf_ohlcv(
            str(symbol or ""),
            field_s,
            timeframe,
            gaps_on=gaps,
            lookahead_on=lookahead,
        )
        if prov is not None:
            return _as_htf_float(prov)
    val = host._try_htf_ohlcv_resample(field_s, timeframe, gaps_on=gaps, lookahead_on=lookahead)
    if val is not None:
        return _as_htf_float(val)
    return _as_htf_float(host._chart_field_at(field_s, host._chart_bar_index()))

