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

"""`export method` end-to-end: library method usable via `import`.

Covers the previously less-exercised path — a UDT plus an exported
method defined in a library script, imported under an alias, then
constructed (`alias.Type.new(...)`) and invoked in receiver form
(`instance.method()`), plus the plain `export f()` parity call.
"""

from __future__ import annotations

from pynescript.ast.evaluator import NodeLiteralEvaluator


_LIB_SRC = """//@version=6
library("TradeLib")
export type Trade
    float qty
    float price
export method notional(Trade this) =>
    this.qty * this.price
export double(x) => x * 2
"""

_CONSUMER_SRC = """//@version=6
indicator("x")
import user/TradeLib/1 as tl
t = tl.Trade.new(10.0, 5.0)
n = t.notional()
d = tl.double(21)
"""


def test_export_method_usable_via_import() -> None:
    evaluator = NodeLiteralEvaluator()
    evaluator.evaluate_script(_LIB_SRC)
    mod = evaluator.lookup_library(name="TradeLib")
    assert mod is not None
    assert "Trade" in mod.exports
    assert "notional" in mod.exports
    assert "double" in mod.exports

    evaluator.evaluate_script(_CONSUMER_SRC)
    assert evaluator.context["n"] == 50.0
    assert evaluator.context["d"] == 42


def test_export_method_register_library_source() -> None:
    evaluator = NodeLiteralEvaluator()
    evaluator.register_library_source("user", "TradeLib", 1, _LIB_SRC)
    evaluator.evaluate_script(_CONSUMER_SRC)
    assert evaluator.context["n"] == 50.0
    assert evaluator.context["d"] == 42
