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

"""In-process MCP protocol tests for the PYNE server."""

from __future__ import annotations

import asyncio
import json

import pytest

from pynescript.mcp.catalog import PROMPT_NAMES
from pynescript.mcp.catalog import TOOL_NAMES


try:
    from mcp import Client

    from pynescript.mcp.server import create_server

    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False
    Client = None  # type: ignore[misc, assignment]
    create_server = None  # type: ignore[misc, assignment]

MINI = """//@version=5
indicator("cli_smoke")
plot(close)
"""

pytestmark = pytest.mark.skipif(not _HAS_MCP, reason="mcp extra not installed")


def _text(result) -> str:
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _payload(result) -> dict:
    structured = result.structured_content
    if isinstance(structured, dict):
        if "ok" in structured:
            return structured
        inner = structured.get("result")
        if isinstance(inner, dict) and "ok" in inner:
            return inner
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return structured
    text = _text(result)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"ok": not result.is_error, "raw": text}


async def _with_client(coro_fn):
    server = create_server()
    async with Client(server, raise_exceptions=True) as client:
        return await coro_fn(client)


def test_list_tools_covers_catalog() -> None:
    async def _run(client):
        listing = await client.list_tools()
        tools = listing.tools if hasattr(listing, "tools") else listing
        names = {t.name for t in tools}
        missing = set(TOOL_NAMES) - names
        assert not missing, missing
        return names

    names = asyncio.run(_with_client(_run))
    assert "pyne_lint" in names
    assert "pyne_execute" in names


def test_call_lint_and_parse() -> None:
    async def _run(client):
        linted = _payload(await client.call_tool("pyne_lint", {"source": MINI}))
        parsed = _payload(await client.call_tool("pyne_parse", {"source": MINI}))
        return linted, parsed

    linted, parsed = asyncio.run(_with_client(_run))
    assert linted.get("ok") is True
    assert isinstance(linted.get("findings"), list)
    assert parsed.get("ok") is True
    assert parsed.get("kind") == "indicator"
    assert parsed.get("title") == "cli_smoke"


def test_call_run_truncated() -> None:
    async def _run(client):
        return _payload(await client.call_tool("pyne_run", {"source": MINI, "bars": 20, "sample": 4}))

    payload = asyncio.run(_with_client(_run))
    assert payload.get("ok") is True
    plots = payload.get("plots") or {}
    assert plots
    sample = next(iter(plots.values()))["sample"]
    assert len(sample) <= 4


def test_call_execute_and_parity() -> None:
    sma = '//@version=6\nindicator("sma")\nplot(ta.sma(close, 5))\n'

    async def _run(client):
        executed = _payload(
            await client.call_tool("pyne_execute", {"source": sma, "bars": 24, "mode": "interpret", "sample": 4})
        )
        parity = _payload(await client.call_tool("pyne_parity", {"source": sma, "bars": 24}))
        return executed, parity

    executed, parity = asyncio.run(_with_client(_run))
    assert executed.get("ok") is True
    plots = executed.get("plots") or {}
    assert plots
    assert "compile_ms" not in plots
    assert next(iter(plots.values()))["len"] == 24
    assert parity.get("ok") is True
    assert parity.get("match") is True


def test_resources_and_prompts() -> None:
    async def _run(client):
        info = await client.read_resource("pyne://info")
        prompts = await client.list_prompts()
        return info, prompts

    info, prompts = asyncio.run(_with_client(_run))
    contents = info.contents if hasattr(info, "contents") else [info]
    text = ""
    for block in contents:
        text += getattr(block, "text", "") or ""
    body = json.loads(text)
    assert body.get("ok") is True or body.get("name") == "pynescript"
    names = {p.name for p in (prompts.prompts if hasattr(prompts, "prompts") else prompts)}
    assert set(PROMPT_NAMES) <= names


def test_builtin_resource() -> None:
    async def _run(client):
        return await client.read_resource("pyne://builtins/ta.sma")

    result = asyncio.run(_with_client(_run))
    contents = result.contents if hasattr(result, "contents") else [result]
    text = "".join(getattr(b, "text", "") or "" for b in contents)
    body = json.loads(text)
    assert body.get("ok") is True
    assert body.get("name") == "ta.sma"
