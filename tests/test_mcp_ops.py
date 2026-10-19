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

"""Ops-layer tests for PYNE MCP (no MCP SDK required)."""

from __future__ import annotations

from pathlib import Path

from pynescript.mcp.catalog import TOOL_NAMES
from pynescript.mcp.dispatch import call_tool
from pynescript.mcp.dispatch import parse_kv
from pynescript.mcp.dispatch import resolve_tool_name
from pynescript.mcp.ops.builtins import lookup_builtin
from pynescript.mcp.ops.compile import compile_source
from pynescript.mcp.ops.compile import run_synthetic
from pynescript.mcp.ops.data import fetch_data
from pynescript.mcp.ops.execute import execute_source
from pynescript.mcp.ops.execute import parity_source
from pynescript.mcp.ops.info import info_payload
from pynescript.mcp.ops.language import check_sources
from pynescript.mcp.ops.language import convert_source
from pynescript.mcp.ops.language import diagnostics_source
from pynescript.mcp.ops.language import dump_source
from pynescript.mcp.ops.language import format_source
from pynescript.mcp.ops.language import lint_source
from pynescript.mcp.ops.language import parse_source
from pynescript.mcp.ops.language import symbols_source
from pynescript.mcp.ops.lsp import complete_source
from pynescript.mcp.ops.lsp import hover_source
from pynescript.mcp.serialize import jsonable
from pynescript.mcp.serialize import split_plots
from pynescript.mcp.serialize import strip_secrets
from pynescript.mcp.server import client_config


MINI = """//@version=5
indicator("cli_smoke")
plot(close)
"""

SMA = """//@version=6
indicator("sma")
plot(ta.sma(close, 5))
"""

BAD = """//@version=6
indicator("bad"
plot(close)
"""


def test_info_payload_shape() -> None:
    payload = info_payload()
    assert payload["ok"] is True
    assert payload["name"] == "pynescript"
    assert payload["distribution"] == "hoox-pyne"
    assert payload["entry_points"]["mcp"] == "pyne-mcp"
    assert payload["entry_points"]["cli"] == "pyne"


def test_client_config() -> None:
    cfg = client_config()
    assert cfg["mcpServers"]["pyne"]["command"] == "pyne-mcp"
    mod = client_config(module=True)
    assert mod["mcpServers"]["pyne"]["args"] == ["-m", "pynescript.mcp"]


def test_parse_ok() -> None:
    result = parse_source(MINI)
    assert result["ok"] is True
    assert result["kind"] == "indicator"
    assert result["title"] == "cli_smoke"
    assert result["version"] == 5
    assert result["errors"] == []


def test_parse_error_located() -> None:
    result = parse_source(BAD)
    assert result["ok"] is False
    assert result["kind"] == "parse"
    assert result.get("line")


def test_check_source_and_path(tmp_path: Path) -> None:
    pine = tmp_path / "ok.pine"
    pine.write_text(MINI, encoding="utf-8")
    one = check_sources(source=MINI)
    assert one["ok"] is True
    assert one["fail_count"] == 0
    many = check_sources(path=str(pine))
    assert many["fail_count"] == 0
    bad = check_sources(source=BAD)
    assert bad["fail_count"] == 1


def test_format_and_convert() -> None:
    formatted = format_source(MINI)
    assert formatted["ok"] is True
    assert "indicator" in formatted["source"]
    converted = convert_source(MINI, to=6)
    assert converted["ok"] is True
    assert "//@version=6" in converted["source"] or converted["to"] == 6


def test_lint_and_diagnostics() -> None:
    linted = lint_source(MINI)
    assert linted["ok"] is True
    assert isinstance(linted["findings"], list)
    diag = diagnostics_source(BAD)
    assert diag["ok"] is True
    assert any(item["severity"] == "error" for item in diag["diagnostics"])


def test_dump_and_symbols() -> None:
    dumped = dump_source(MINI)
    assert dumped["ok"] is True
    assert "Script" in dumped["dump"]
    assert dumped.get("tree", {}).get("_type") == "Script"
    outline = symbols_source(MINI)
    assert outline["ok"] is True
    kinds = {s["kind"] for s in outline["symbols"]}
    assert "plot" in kinds


def test_builtin_lookup() -> None:
    hit = lookup_builtin("ta.sma")
    assert hit["ok"] is True
    assert hit["name"] == "ta.sma"
    miss = lookup_builtin("not.a.real.builtin.zzz")
    assert miss["ok"] is False


def test_compile_emit() -> None:
    result = compile_source(MINI, emit=True)
    assert result["ok"] is True
    assert "def " in result["python"] or "execute" in result["python"]


def test_run_synthetic() -> None:
    result = run_synthetic(MINI, bars=16, sample=4)
    assert result["ok"] is True
    assert result["bars"] == 16
    assert result["plots"]
    first = next(iter(result["plots"].values()))
    assert first["len"] == 16
    assert len(first["sample"]) <= 4


def test_split_plots_runtime_envelope() -> None:
    plots, extras = split_plots(
        {
            "plots": [1.0, 2.0, 110.6],
            "series": {"sma": [1.0, 2.0, 110.6]},
            "mode": "compile",
            "compile_ms": 12.3,
            "run_ms": 4.1,
            "compile_cached": False,
            "object_mode": False,
            "events": [{"type": "entry"}],
            "count": 3,
        }
    )
    assert list(plots) == ["sma"]
    assert plots["sma"]["last"] == 110.6
    assert "compile_ms" not in plots
    assert extras["compile_ms"] == 12.3
    assert extras["events_count"] == 1
    assert "events" not in extras


def test_split_plots_compiled_map() -> None:
    plots, extras = split_plots({"plot": [1, 2, 3], "__events": [{"x": 1}, {"x": 2}]})
    assert plots["plot"]["len"] == 3
    assert extras["__events_count"] == 2


def test_execute_interpret() -> None:
    result = execute_source(MINI, bars=20, mode="interpret", sample=4)
    assert result["ok"] is True
    assert result["bars"] == 20
    assert result["plots"]
    assert "compile_ms" not in result["plots"]
    first = next(iter(result["plots"].values()))
    assert first["len"] == 20
    encoded = str(result)
    assert len(encoded) < 200_000


def test_parity_sma() -> None:
    result = parity_source(SMA, bars=24)
    assert result["ok"] is True
    assert result["match"] is True
    assert result["mismatches"] == []


def test_data_mock() -> None:
    result = fetch_data("AAPL", provider="mock", period="1mo")
    assert result["ok"] is True
    assert result["bars"] > 0
    assert result["provider"] == "mock"


def test_hover_sma() -> None:
    src = "plot(ta.sma(close, 14))\n"
    col = src.index("sma")
    result = hover_source(src, line=0, character=col)
    if not result["ok"] and result.get("kind") == "io":
        return
    assert result["ok"] is True
    assert result["found"] is True
    assert "sma" in (result.get("markdown") or "").lower() or "ta.sma" in (result.get("markdown") or "")


def test_complete_ta_dot() -> None:
    src = "ta."
    result = complete_source(src, line=0, character=3, limit=10)
    if not result["ok"] and result.get("kind") == "io":
        return
    assert result["ok"] is True
    labels = [i["label"] for i in result["items"]]
    assert labels


def test_jsonable_nan_and_strip_secrets() -> None:
    assert jsonable(float("nan")) is None
    redacted = strip_secrets({"api_key": "secret", "symbol": "BTC"})
    assert redacted["api_key"] == "***"
    assert redacted["symbol"] == "BTC"


def test_resolve_tool_name_aliases() -> None:
    assert resolve_tool_name("pyne_lint") == "pyne_lint"
    assert resolve_tool_name("lint") == "pyne_lint"
    assert resolve_tool_name("pyne-execute") == "pyne_execute"
    assert resolve_tool_name("not-a-tool") is None
    assert resolve_tool_name("") is None


def test_parse_kv_json_and_string() -> None:
    assert parse_kv("bars=24") == ("bars", 24)
    assert parse_kv("mode=interpret") == ("mode", "interpret")
    assert parse_kv("flag=true") == ("flag", True)
    assert parse_kv("name=ta.sma") == ("name", "ta.sma")


def test_call_tool_lint_and_alias() -> None:
    named = call_tool("pyne_lint", {"source": MINI})
    short = call_tool("lint", {"source": MINI})
    assert named["ok"] is True
    assert short["ok"] is True
    assert isinstance(named["findings"], list)


def test_call_tool_execute_and_unknown() -> None:
    executed = call_tool("execute", {"source": MINI, "bars": 16, "mode": "interpret", "sample": 4})
    assert executed["ok"] is True
    assert executed["bars"] == 16
    assert executed["plots"]
    unknown = call_tool("nope")
    assert unknown["ok"] is False
    assert unknown["kind"] == "io"
    assert "pyne_lint" in unknown["tools"]


def test_call_tool_covers_catalog() -> None:
    from pynescript.mcp.dispatch import _handlers

    assert set(_handlers()) == set(TOOL_NAMES)
