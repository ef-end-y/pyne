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
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pins for string-interpolation scope and `bool` never-`na` edges.

Interpolation in PYNE is explicit `str.format(fmt, ...)` substitution —
bare `"Price {0}"` literals stay literal (no inline template sugar).
`bool` never yields `na`: `bool(na)` is `False`, `na` conditions are
falsy, and non-`bool` numeric conditions stay permissive at runtime
(the v6-strictness nudge lives in the linter as W105, not the evaluator).
"""

from __future__ import annotations

import pytest

from pynescript.ast.evaluator import NodeLiteralEvaluator
from pynescript.ast.helper import parse


def _eval(src: str):
    return NodeLiteralEvaluator().visit(parse(src, mode="eval").body)


def test_bare_braces_stay_literal() -> None:
    assert _eval('"Price {0}"') == "Price {0}"


def test_str_format_substitutes() -> None:
    assert _eval('str.format("Price: {0}", 42)') == "Price: 42"
    assert _eval('str.format("plain")') == "plain"


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("bool(na)", False),
        ("bool(0)", False),
        ("bool(3.2)", True),
        ("not na", None),
        ("na and true", False),
        ("na or false", False),
    ],
)
def test_bool_edges(expression: str, expected) -> None:
    assert _eval(expression) == expected


def test_runtime_numeric_condition_stays_permissive() -> None:
    from pynescript.runtime import Runtime

    def _bars(n: int = 3) -> list[dict]:
        return [
            {
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 10.0,
                "time": 1_700_000_000 + i * 60,
            }
            for i in range(n)
        ]

    src = """//@version=6
indicator("t")
x = 0.0
if 1
    x := 42.0
plot(x, "x")
"""
    out = Runtime().run(src, _bars())
    assert out["series"]["x"] == [42.0, 42.0, 42.0]
