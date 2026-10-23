"""Parity oracles for the shadowed technical-indicator copies.

``TechnicalAnalysisMixin`` composes 11 submodule mixins; 70 ``_builtin_ta_*``
handlers are defined in more than one of them and the MRO picks one
(``tests/test_builtin_shadowing.py`` pins which). The losing copies are kept
deliberately — as standalone fallbacks, as coverage targets for
``test_technical_submodules_direct.py``, and as independent references for the
incremental TA suite.

Keeping a copy is only safe while we know *what kind* of reference it is. The
audit (subtask 03) found three classes:

* **oracle** — computes the same result as the winner on the full-recompute
  path; the only divergence is the incremental fast path (or arity/formatting).
  Drift here is a bug, so the pairs below are pinned.
* **stale** — an older algorithm that no longer matches the winner
  (``pivothigh``/``pivotlow`` ignore ``rightbars``, ``stdev``/Volatility omits
  ``last_sample_ok``, ``supertrend``/Common returns a 3-tuple). These must
  never be wired up as parity oracles.
* **fallback** — a minimal local implementation kept so a mixin can be
  composed without ``BasicIndicators`` (``common.py``
  ``_builtin_ta_pivot_point_levels``).

This module pins the oracle-valid subset with the highest blast radius:
``atr``, ``bb``, ``kc``, ``stdev``, ``tr`` and ``supertrend`` (the
``BasicIndicators`` copy, which inlines the same formula as ``_supertrend``).

Everything is compared with incremental TA **off**: the winners short-circuit
into ``_*_inc_update`` when the runtime enables it, and the losers do not have
(or do not need) the same dispatch. The incremental path has its own parity
suite in ``tests/test_ta_incremental.py``.
"""

from __future__ import annotations

import math

import pytest

from pynescript.ast.evaluator import NodeLiteralEvaluator
from pynescript.ast.evaluator.builtins.technical import TechnicalAnalysisMixin as _Winner
from pynescript.ast.evaluator.builtins.technical_submodules.basic import BasicIndicators as _Basic
from pynescript.ast.evaluator.builtins.technical_submodules.volatility import VolatilityIndicators as _Vol


_N_BARS = 45
_FIRST_PARITY_BAR = 12


def _series() -> tuple[list[float], list[float], list[float], list[float]]:
    """Deterministic, non-monotonic OHLCV so warmup and crossovers both occur."""
    high: list[float] = []
    low: list[float] = []
    close: list[float] = []
    volume: list[float] = []
    price = 100.0
    for i in range(_N_BARS):
        price += ((i * 37) % 11 - 5) * 0.8
        o = price + ((i * 13) % 7 - 3) * 0.3
        c = price + ((i * 29) % 9 - 4) * 0.3
        h = max(o, c) + 0.9 + (i % 4) * 0.2
        lo = min(o, c) - 0.7 - (i % 3) * 0.2
        high.append(h)
        low.append(lo)
        close.append(c)
        volume.append(700.0 + (i * 41) % 500)
    return high, low, close, volume


def _evaluator(high: list[float], low: list[float], close: list[float], volume: list[float]) -> NodeLiteralEvaluator:
    ev = NodeLiteralEvaluator()
    ev.current_series = {
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "hl2": [(h + lo) / 2.0 for h, lo in zip(high, low, strict=True)],
        "hlc3": [(h + lo + c) / 3.0 for h, lo, c in zip(high, low, close, strict=True)],
    }
    # Force the full-recompute path: the winners' incremental fast path is
    # covered by tests/test_ta_incremental.py, not here.
    ev._pine_ta_inc_cached = False  # type: ignore[attr-defined]
    return ev


def _assert_same(actual: object, expected: object, where: str) -> None:
    """Recursive equality where NaN matches NaN (both mean ``na``)."""
    if isinstance(actual, float) and isinstance(expected, float):
        assert (math.isnan(actual) and math.isnan(expected)) or actual == pytest.approx(expected), (
            f"{where}: winner={actual!r} reference={expected!r}"
        )
        return
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected), f"{where}: length {len(actual)} != {len(expected)}"
        for i, (a, b) in enumerate(zip(actual, expected, strict=True)):
            _assert_same(a, b, f"{where}[{i}]")
        return
    assert actual == expected, f"{where}: winner={actual!r} reference={expected!r}"


def _walk(name: str, reference: type, args_for, *, lo: int = _FIRST_PARITY_BAR) -> None:
    """Compare winner vs *reference* copy on every growing bar prefix.

    A per-bar walk catches warmup differences, which a single call at the end
    of the series would hide.
    """
    high, low, close, volume = _series()
    winner = getattr(_Winner, name)
    loser = getattr(reference, name)
    for k in range(lo, _N_BARS + 1):
        h, l, c, v = high[:k], low[:k], close[:k], volume[:k]
        args = args_for(h, l, c, v)
        _assert_same(
            winner(_evaluator(h, l, c, v), args),
            loser(_evaluator(h, l, c, v), args),
            f"{name} k={k}",
        )


def test_atr_parity_with_volatility_copy() -> None:
    """Winner (Basic) vs Volatility copy: context form and explicit form."""
    _walk("_builtin_ta_atr", _Vol, lambda h, l, c, v: [14])
    _walk("_builtin_ta_atr", _Vol, lambda h, l, c, v: [h, l, c, 14])


def test_bb_parity_with_volatility_copy() -> None:
    _walk("_builtin_ta_bb", _Vol, lambda h, l, c, v: [c, 20, 2.0])


def test_kc_parity_with_volatility_copy() -> None:
    """Standard 5-arg form. The Volatility copy additionally accepts a bare
    ``(high, low, close)`` triple, which the winner rejects — that shape gap is
    documented in the audit, not pinned here."""
    _walk("_builtin_ta_kc", _Vol, lambda h, l, c, v: [h, l, c, 20, 1.5])
    _walk("_builtin_ta_kc", _Vol, lambda h, l, c, v: [c, 20, 1.5])


def test_stdev_parity_with_basic_copy() -> None:
    """Winner is Advanced; the Basic copy only lacks the incremental shortcut."""
    _walk("_builtin_ta_stdev", _Basic, lambda h, l, c, v: [c, 20])


def test_tr_parity_with_volatility_copy() -> None:
    """Explicit 3-series form. The Volatility copy does not accept the
    zero/one-argument context form that the winner added."""
    _walk("_builtin_ta_tr", _Vol, lambda h, l, c, v: [h, l, c])


def test_supertrend_parity_with_basic_copy() -> None:
    """The Basic copy inlines ``_supertrend`` (same band + direction).

    Unlike the Common copy (3-tuple, zero-close tr), this one is a valid
    reference for the winner's full path.
    """
    _walk("_builtin_ta_supertrend", _Basic, lambda h, l, c, v: [3.0, 10])
