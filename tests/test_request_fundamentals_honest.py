# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you are redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Honest-na fundamentals: no invented mock values without a provider hook.

``request.dividends`` / ``earnings`` / ``splits`` / ``financial`` /
``quandl`` / ``economic`` / ``currency_rate`` return ``na`` (``None``) when
neither ``data_feed`` nor ``data_provider`` supplies the field via the
optional ``fetch_fundamental(kind, symbol, **params)`` hook. A wired hook
wins; ``currency_rate`` additionally honors a live feed FX pair via
``fetch_latest_ticker``. ``request.seed`` is unchanged (RNG utility).
"""

from __future__ import annotations

import pytest

from pynescript.ast.evaluator import NodeLiteralEvaluator
from pynescript.ast.helper import parse
from pynescript.util.datafeed import MockDataFeed


def _eval(ev: NodeLiteralEvaluator, src: str):
    return ev.visit(parse(src, mode="eval").body)


class _FundamentalStub:
    """Minimal provider exposing the optional fundamentals hook."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.seen: list = []

    def fetch_fundamental(self, kind: str, symbol: str, **params):
        self.seen.append((kind, symbol, params))
        return self._payload.get((kind, symbol))


BARE_NA_CASES = [
    'request.dividends("AAPL")',
    'request.earnings("MSFT")',
    'request.splits("TSLA")',
    'request.financial("AAPL", "REVENUE")',
    'request.quandl("EIA/PET_RWTC_D")',
    'request.economic("US", "UNRATE")',
    'request.currency_rate("USD", "EUR")',
]


@pytest.mark.parametrize("expression", BARE_NA_CASES)
def test_fundamentals_are_na_without_provider(expression: str) -> None:
    assert _eval(NodeLiteralEvaluator(), expression) is None


def test_fundamentals_use_provider_hook_when_wired() -> None:
    stub = _FundamentalStub(
        {
            ("dividends", "AAPL"): 0.24,
            ("earnings", "MSFT"): 9.27,
            ("splits", "TSLA"): 3.0,
            ("financial", "AAPL"): 383285000000.0,
            ("quandl", "EIA/PET_RWTC_D"): [50.0, 51.5],
            ("economic", "US"): [3.5, 3.4],
            ("currency_rate", "USD/EUR"): 0.92,
        }
    )
    ev = NodeLiteralEvaluator(data_provider=stub)
    assert _eval(ev, 'request.dividends("AAPL")') == 0.24
    assert _eval(ev, 'request.earnings("MSFT")') == 9.27
    assert _eval(ev, 'request.splits("TSLA")') == 3.0
    assert _eval(ev, 'request.financial("AAPL", "REVENUE")') == 383285000000.0
    assert _eval(ev, 'request.quandl("EIA/PET_RWTC_D")') == [50.0, 51.5]
    assert _eval(ev, 'request.economic("US", "UNRATE")') == [3.5, 3.4]
    assert _eval(ev, 'request.currency_rate("USD", "EUR")') == 0.92
    assert stub.seen, "hook must be consulted"


def test_fundamentals_na_when_hook_misses() -> None:
    stub = _FundamentalStub({})
    ev = NodeLiteralEvaluator(data_provider=stub)
    assert _eval(ev, 'request.dividends("UNKNOWN_XYZ")') is None


def test_currency_rate_prefers_feed_pair() -> None:
    feed = MockDataFeed(symbol="EUR/USD", start_price=1.12)
    ev = NodeLiteralEvaluator(data_feed=feed)
    assert abs(float(_eval(ev, 'request.currency_rate("EUR", "USD")')) - 1.12) < 0.01


def test_fundamental_na_records_policy() -> None:
    ev = NodeLiteralEvaluator()
    assert _eval(ev, 'request.dividends("AAPL")') is None
    state = getattr(ev, "_request_security_policy", None)
    assert state is not None
    assert "fundamental_na" in state.get("policies", {})


def test_request_seed_unchanged() -> None:
    ev = NodeLiteralEvaluator()
    assert _eval(ev, "request.seed(42)") is None
    assert ev.context.get("request.seed") == 42
