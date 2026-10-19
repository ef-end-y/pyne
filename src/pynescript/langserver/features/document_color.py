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

"""Document colors — ``textDocument/documentColor`` and ``colorPresentation``.

Public handlers: :func:`handle_document_color` and
:func:`handle_color_presentation`. Color literals are lexer ``COLOR`` tokens
(``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``) stored as :class:`~pynescript.ast.node.Constant`
with ``kind="#"``. Each becomes a 0-1 RGBA :class:`~lsprotocol.types.ColorInformation`.
``colorPresentation`` rewrites the hex at that range (``#rrggbb``, or
``#rrggbbaa`` when alpha != 1). Empty source or parse failure returns ``[]``.
"""

from __future__ import annotations

import logging
import re

from typing import Any

from lsprotocol import types as lsp

from pynescript.ast import node as ast
from pynescript.ast.helper import parse
from pynescript.ast.helper import walk


logger = logging.getLogger(__name__)

_HEX_LITERAL = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_HEX_RGB_LEN = 3
_HEX_RRGGBB_LEN = 6
_OPAQUE_ALPHA = 255
_CHANNEL_MAX = 255.0


def handle_document_color(
    params: lsp.DocumentColorParams,
    source: str | None,
    tree: Any | None = ...,
) -> list[lsp.ColorInformation]:
    """Handle textDocument/documentColor request.

    Args:
        params: Client document-color params.
        source: Document text, or ``None``.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse (skips a redundant re-parse).
            Omit (default ``...``) to parse from *source*.

    Returns:
        Color information for each hex literal (possibly empty). Never ``None``.
    """
    if tree is ...:
        if not source:
            return []
        try:
            tree = parse(source, filename=params.text_document.uri)
        except Exception as exc:
            logger.debug("document_color: parse failed: %s", exc)
            return []
    if tree is None:
        return []

    results: list[lsp.ColorInformation] = []
    for node in walk(tree):
        info = _color_info_for_node(node)
        if info is not None:
            results.append(info)
    return results


def handle_color_presentation(
    params: lsp.ColorPresentationParams,
    source: str | None = None,
) -> list[lsp.ColorPresentation]:
    """Handle textDocument/colorPresentation request.

    Rewrites the hex at *params.range*. Opaque colors use ``#rrggbb``;
    otherwise ``#rrggbbaa``. *source* is unused (the client already supplied
    the color and range).
    """
    del source
    hex_str = _color_to_hex(params.color)
    return [
        lsp.ColorPresentation(
            label=hex_str,
            text_edit=lsp.TextEdit(range=params.range, new_text=hex_str),
        )
    ]


def _color_info_for_node(node: Any) -> lsp.ColorInformation | None:
    """Return ColorInformation for a ``kind="#"`` Constant, or ``None``."""
    if not isinstance(node, ast.Constant) or getattr(node, "kind", None) != "#":
        return None
    value = node.value
    if not isinstance(value, str):
        return None
    color = _hex_to_color(value)
    if color is None:
        return None
    rng = _node_to_range(node)
    if rng is None:
        return None
    return lsp.ColorInformation(range=rng, color=color)


def _hex_to_color(value: str) -> lsp.Color | None:
    """Parse a ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa`` literal to 0-1 RGBA."""
    raw = value.strip()
    match = _HEX_LITERAL.fullmatch(raw)
    if match is None:
        return None
    hexpart = match.group(1)
    if len(hexpart) == _HEX_RGB_LEN:
        hexpart = "".join(ch * 2 for ch in hexpart) + "ff"
    elif len(hexpart) == _HEX_RRGGBB_LEN:
        hexpart = hexpart + "ff"
    r = int(hexpart[0:2], 16)
    g = int(hexpart[2:4], 16)
    b = int(hexpart[4:6], 16)
    a = int(hexpart[6:8], 16)
    return lsp.Color(
        red=r / _CHANNEL_MAX,
        green=g / _CHANNEL_MAX,
        blue=b / _CHANNEL_MAX,
        alpha=a / _CHANNEL_MAX,
    )


def _channel_byte(value: float) -> int:
    """Clamp a 0-1 channel to an 8-bit component."""
    return max(0, min(_OPAQUE_ALPHA, round(value * _CHANNEL_MAX)))


def _color_to_hex(color: lsp.Color) -> str:
    """Encode *color* as ``#rrggbb`` or ``#rrggbbaa`` when alpha != 1."""
    r = _channel_byte(color.red)
    g = _channel_byte(color.green)
    b = _channel_byte(color.blue)
    a = _channel_byte(color.alpha)
    if a == _OPAQUE_ALPHA:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"#{r:02x}{g:02x}{b:02x}{a:02x}"


def _node_to_range(node: Any) -> lsp.Range | None:
    """Convert AST node locations to an LSP Range (see ``symbols._node_to_range``)."""
    lineno = getattr(node, "lineno", None)
    if not lineno:
        return None
    col_offset = getattr(node, "col_offset", 0) or 0
    end_lineno = getattr(node, "end_lineno", lineno) or lineno
    end_col_offset = getattr(node, "end_col_offset", None)
    if end_col_offset is None:
        end_col_offset = col_offset + 1 if end_lineno == lineno else 1 << 31
    return lsp.Range(
        start=lsp.Position(line=max(0, lineno - 1), character=max(0, col_offset)),
        end=lsp.Position(line=max(0, end_lineno - 1), character=max(0, end_col_offset)),
    )
