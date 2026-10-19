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

"""Convert the active Pine document toward v6 (editor command + code action).

``textDocument/codeAction`` also returns the W001 QuickFix (insert
``//@version=6``) via :func:`~pynescript.langserver.features.diagnostics.create_quick_fix`
when the pragma is missing.
"""

from __future__ import annotations

from typing import Any

from lsprotocol import types as lsp

from pynescript.ast.linter import LintWarning
from pynescript.langserver.features.diagnostics import create_quick_fix
from pynescript.util.pine_convert import convert_to_v6
from pynescript.util.pine_convert import detect_version


_V6 = 6


COMMAND_CONVERT_V6 = "pynescript.convertToV6"


def convert_payload(source: str) -> dict[str, Any]:
    """Return conversion metadata and rewritten source."""
    declared = detect_version(source)
    from_ver = 1 if declared is None else declared
    converted = convert_to_v6(source)
    return {
        "source": converted,
        "from_version": from_ver,
        "to_version": _V6,
        "changed": converted != source,
    }


def whole_document_edit(source: str, new_text: str) -> lsp.TextEdit:
    """One replace-edit covering the entire document."""
    lines = source.split("\n")
    end_line = max(0, len(lines) - 1)
    end_col = len(lines[-1]) if lines else 0
    return lsp.TextEdit(
        range=lsp.Range(
            start=lsp.Position(line=0, character=0),
            end=lsp.Position(line=end_line, character=end_col),
        ),
        new_text=new_text,
    )


def handle_convert_to_v6(source: str | None) -> dict[str, Any]:
    """Convert *source* toward v6; empty source yields a no-op payload."""
    text = source or ""
    return convert_payload(text)


def handle_code_action(
    params: lsp.CodeActionParams,
    source: str | None,
    warnings: list[LintWarning] | None = None,
) -> list[lsp.CodeAction]:
    """Offer Convert-to-v6 (older than v6) and a W001 QuickFix (missing pragma)."""
    if not source:
        return []
    declared = detect_version(source)
    from_ver = 1 if declared is None else declared
    uri = params.text_document.uri
    actions: list[lsp.CodeAction] = []
    if from_ver < _V6:
        payload = convert_payload(source)
        title = f"Convert Pine v{from_ver} to v6"
        edit = lsp.WorkspaceEdit(
            changes={uri: [whole_document_edit(source, payload["source"])]},
        )
        actions.append(
            lsp.CodeAction(
                title=title,
                kind=lsp.CodeActionKind.RefactorRewrite,
                edit=edit,
            )
        )
    if declared is None:
        w001 = None
        if warnings:
            w001 = next((w for w in warnings if w.code == "W001"), None)
        if w001 is None:
            w001 = LintWarning(
                code="W001",
                message="Missing @version declaration. Add '//@version=6' at the top.",
                line=1,
            )
        fix = create_quick_fix(w001, uri, source)
        if fix is not None:
            actions.append(fix)
    return actions
