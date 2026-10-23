"""Shadowing contract for the composed technical-indicator mixins.

``TechnicalAnalysisMixin`` (``builtins/technical.py``) is composed from 11
independent mixins. When two mixins define the same method, Python's MRO picks
the first one and every later definition becomes unreachable at runtime.

That duplication is *deliberate* in this package:

* ``common.py`` keeps local fallbacks so a mixin can be composed without
  ``basic.py`` (see ``_builtin_ta_pivot_point_levels``).
* ``economics.py`` / ``volatility.py`` keep reference copies that
  ``tests/test_ta_incremental.py`` uses as independent oracles.
* ``tests/test_technical_submodules_direct.py`` calls the non-winning copies
  directly so coverage is attributed to the defining module.

The risk is not the duplication itself but that it is *unmanaged*: a new
duplicate, or a reordering of the mixin bases, silently changes which
implementation runs. This test pins the current contract, so both failure
modes become loud instead of silent.

If a change here is intentional, update ``KNOWN_DUPLICATES`` in the same
commit and say why in the commit message.

Not every losing copy is a valid reference for the winner. The audit split
them into *oracles* (same full-recompute result; safe to pin),
*stale copies* (drifted algorithm — never use as an oracle) and one
*fallback*. ``tests/test_ta_duplicate_parity.py`` pins the highest-risk
oracle pairs (``atr``, ``bb``, ``kc``, ``stdev``, ``tr``, ``supertrend``) and
its docstring lists the stale ones.
"""

from __future__ import annotations

from pynescript.ast.evaluator.builtins.technical import TechnicalAnalysisMixin


#: Method name -> name of the mixin whose definition currently wins the MRO.
KNOWN_DUPLICATES: dict[str, str] = {
    # -- helper methods (also pinned explicitly by request.py ``__get__``) --
    "_change": "CommonIndicators",
    "_cumsum": "BasicIndicators",
    "_dev": "BasicIndicators",
    "_error": "TechnicalHelpers",
    "_expect_int": "TechnicalHelpers",
    "_median": "BasicIndicators",
    "_mode": "BasicIndicators",
    "_percentrank": "BasicIndicators",
    "_range": "BasicIndicators",
    "_supertrend_inc_update": "AdvancedIndicators",
    "_variance": "BasicIndicators",

    # -- builtin handlers --
    "_builtin_ta_acceleration_factor": "AdvancedIndicators",
    "_builtin_ta_atr": "BasicIndicators",
    "_builtin_ta_barssince": "BasicIndicators",
    "_builtin_ta_bb": "BasicIndicators",
    "_builtin_ta_change": "BasicIndicators",
    "_builtin_ta_cog": "BasicIndicators",
    "_builtin_ta_contrarian_signal": "AdvancedIndicators",
    "_builtin_ta_cross": "BasicIndicators",
    "_builtin_ta_crossover": "BasicIndicators",
    "_builtin_ta_crossunder": "BasicIndicators",
    "_builtin_ta_crowd_sentiment": "AdvancedIndicators",
    "_builtin_ta_cum": "BasicIndicators",
    "_builtin_ta_cumulative_delta": "AdvancedIndicators",
    "_builtin_ta_dev": "BasicIndicators",
    "_builtin_ta_dmi": "BasicIndicators",
    "_builtin_ta_dpo": "AdvancedIndicators",
    "_builtin_ta_economic_impact_score": "AdvancedIndicators",
    "_builtin_ta_ema": "BasicIndicators",
    "_builtin_ta_employment_cycle_indicator": "AdvancedIndicators",
    "_builtin_ta_falling": "BasicIndicators",
    "_builtin_ta_fear_greed_index": "AdvancedIndicators",
    "_builtin_ta_gdp_growth_proxy": "AdvancedIndicators",
    "_builtin_ta_highestbars": "BasicIndicators",
    "_builtin_ta_hma": "BasicIndicators",
    "_builtin_ta_inflation_proxy_indicator": "AdvancedIndicators",
    "_builtin_ta_intelligent_strategy_synthesizer": "AdvancedIndicators",
    "_builtin_ta_kc": "BasicIndicators",
    "_builtin_ta_kcw": "BasicIndicators",
    "_builtin_ta_kst": "AdvancedIndicators",
    "_builtin_ta_linreg": "BasicIndicators",
    "_builtin_ta_liquidity_score": "AdvancedIndicators",
    "_builtin_ta_lowestbars": "BasicIndicators",
    "_builtin_ta_market_structure_pivot": "AdvancedIndicators",
    "_builtin_ta_max": "BasicIndicators",
    "_builtin_ta_mean_reversion_score": "AdvancedIndicators",
    "_builtin_ta_median": "BasicIndicators",
    "_builtin_ta_min": "BasicIndicators",
    "_builtin_ta_mode": "BasicIndicators",
    "_builtin_ta_mom": "BasicIndicators",
    "_builtin_ta_momentum_divergence": "AdvancedIndicators",
    "_builtin_ta_momentum_filter": "AdvancedIndicators",
    "_builtin_ta_multi_timeframe_signal": "AdvancedIndicators",
    "_builtin_ta_order_flow_imbalance": "AdvancedIndicators",
    "_builtin_ta_percentrank": "BasicIndicators",
    "_builtin_ta_pivot_point_levels": "BasicIndicators",
    "_builtin_ta_pivothigh": "BasicIndicators",
    "_builtin_ta_pivotlow": "BasicIndicators",
    "_builtin_ta_range": "BasicIndicators",
    "_builtin_ta_rci": "BasicIndicators",
    "_builtin_ta_rising": "BasicIndicators",
    "_builtin_ta_rma": "BasicIndicators",
    "_builtin_ta_sar": "BasicIndicators",
    "_builtin_ta_sma": "BasicIndicators",
    "_builtin_ta_smart_money_flow": "AdvancedIndicators",
    "_builtin_ta_stdev": "AdvancedIndicators",
    "_builtin_ta_stochrsi": "AdvancedIndicators",
    "_builtin_ta_supertrend": "AdvancedIndicators",
    "_builtin_ta_swma": "BasicIndicators",
    "_builtin_ta_tr": "BasicIndicators",
    "_builtin_ta_trend_confirmation_score": "AdvancedIndicators",
    "_builtin_ta_uo": "AdvancedIndicators",
    "_builtin_ta_variance": "BasicIndicators",
    "_builtin_ta_volume_momentum": "AdvancedIndicators",
    "_builtin_ta_volume_profile_high": "AdvancedIndicators",
    "_builtin_ta_volume_profile_low": "AdvancedIndicators",
    "_builtin_ta_volume_thrust": "AdvancedIndicators",
    "_builtin_ta_vwap": "BasicIndicators",
    "_builtin_ta_vwma": "BasicIndicators",
    "_builtin_ta_wma": "BasicIndicators",
    "_builtin_ta_zigzag": "BasicIndicators",
}


def _mixin_classes() -> list[type]:
    """Concrete mixins in MRO order, excluding the composed class and object."""
    return [c for c in TechnicalAnalysisMixin.__mro__ if c not in (object, TechnicalAnalysisMixin)]


def _duplicate_definitions() -> dict[str, list[str]]:
    """Map every private method name defined by more than one mixin to its owners (MRO order)."""
    owners: dict[str, list[str]] = {}
    for cls in _mixin_classes():
        for name in cls.__dict__:
            if not name.startswith("_") or name.startswith("__"):
                continue
            owners.setdefault(name, []).append(cls.__name__)
    return {name: cls for name, cls in owners.items() if len(cls) > 1}


def test_no_unknown_shadowing() -> None:
    """A duplicate that is not in KNOWN_DUPLICATES is an accidental shadowing bug."""
    duplicates = _duplicate_definitions()
    unknown = sorted(set(duplicates) - set(KNOWN_DUPLICATES))
    assert not unknown, (
        "New method(s) defined by more than one technical mixin: "
        f"{unknown}. Only one of them can ever run. Rename or delete the dead copy, "
        "or add it to KNOWN_DUPLICATES with a comment explaining why both must exist."
    )


def test_known_duplicates_still_duplicated() -> None:
    """Every allowlist entry must still correspond to a real duplicate (no stale entries)."""
    duplicates = _duplicate_definitions()
    stale = sorted(set(KNOWN_DUPLICATES) - set(duplicates))
    assert not stale, (
        f"KNOWN_DUPLICATES lists {stale} but no longer duplicated. "
        "Remove the stale entries so the contract stays accurate."
    )


def test_winning_definition_is_pinned() -> None:
    """The mixin that wins the MRO must not change silently."""
    duplicates = _duplicate_definitions()
    moved = {
        name: (duplicates[name][0], expected)
        for name, expected in KNOWN_DUPLICATES.items()
        if name in duplicates and duplicates[name][0] != expected
    }
    assert not moved, (
        "MRO winner changed for: "
        + ", ".join(f"{name}: {actual} (was {expected})" for name, (actual, expected) in sorted(moved.items()))
        + ". This silently changes indicator behaviour; update KNOWN_DUPLICATES only if that is intended."
    )


def test_winner_is_reachable_from_composed_class() -> None:
    """The pinned winner must actually be the callable reached through the composed class."""
    for name, expected in KNOWN_DUPLICATES.items():
        winner = getattr(TechnicalAnalysisMixin, name)
        owners = [c.__name__ for c in _mixin_classes() if c.__dict__.get(name) is winner]
        assert owners == [expected], f"{name}: reached {owners}, expected [{expected}]"
