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

"""Rename — ``textDocument/prepareRename`` and ``textDocument/rename``.

Public handlers: :func:`handle_prepare_rename`, :func:`handle_rename`.
Same-document only. Pine keywords and builtin namespaces are not renamed.
New names must be a single valid Pine identifier token.
"""

from __future__ import annotations

from typing import Any

from lsprotocol import types as lsp

from pynescript.langserver.features.highlight import collect_ident_hits
from pynescript.langserver.features.highlight import cursor_ident
from pynescript.langserver.features.highlight import is_renamable_ident
from pynescript.langserver.features.highlight import is_valid_pine_ident
from pynescript.langserver.features.highlight import resolve_tree


def handle_prepare_rename(
    params: lsp.PrepareRenameParams,
    source: str | None,
    tree: Any | None = ...,
) -> lsp.Range | None:
    """Return the range of the ident under the cursor, or ``None``.

    Keywords, builtin namespaces (``ta``, ``strategy``, …), members of those
    namespaces (``ta.sma``), and non-idents yield ``None``.
    """
    if not source:
        return None
    if tree is None:
        return None
    segment, start, end, full = cursor_ident(source, params.position.line, params.position.character)
    if not is_renamable_ident(segment, full):
        return None
    line = params.position.line
    return lsp.Range(
        start=lsp.Position(line=line, character=start),
        end=lsp.Position(line=line, character=end),
    )


def handle_rename(
    params: lsp.RenameParams,
    source: str | None,
    uri: str,
    tree: Any | None = ...,
) -> lsp.WorkspaceEdit | None:
    """Rename the ident under the cursor in this document only.

    Args:
        params: Client rename params (position + ``new_name``).
        source: Document text, or ``None``.
        uri: Document URI for the returned workspace edit.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse. Omit (default ``...``) to parse
            from *source*.

    Returns:
        :class:`~lsprotocol.types.WorkspaceEdit`, or ``None`` when the ident
        is not renamable / the new name is invalid / the document is empty.
    """
    if not source:
        return None

    new_name = params.new_name
    if not is_valid_pine_ident(new_name) or not is_renamable_ident(new_name, new_name):
        return None

    segment, _start, _end, full = cursor_ident(source, params.position.line, params.position.character)
    if not is_renamable_ident(segment, full):
        return None

    resolved = resolve_tree(source, tree)
    if resolved is None:
        return None

    hits = collect_ident_hits(source, resolved, segment, include_builtin_attrs=False)
    if not hits:
        return None

    edits = [lsp.TextEdit(range=hit.range, new_text=new_name) for hit in hits]
    return lsp.WorkspaceEdit(changes={uri: edits})
