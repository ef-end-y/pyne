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

"""Tests for LSP features."""

from __future__ import annotations

from lsprotocol import types as lsp

from pynescript.langserver.features.completion import handle_completion
from pynescript.langserver.features.completion import handle_completion_resolve
from pynescript.langserver.features.convert import handle_code_action
from pynescript.langserver.features.convert import handle_convert_to_v6
from pynescript.langserver.features.definitions import handle_definition
from pynescript.langserver.features.folding import handle_folding_range
from pynescript.langserver.features.formatting import handle_formatting
from pynescript.langserver.features.formatting import handle_range_formatting
from pynescript.langserver.features.highlight import handle_document_highlight
from pynescript.langserver.features.hover import handle_hover
from pynescript.langserver.features.references import handle_references
from pynescript.langserver.features.rename import handle_prepare_rename
from pynescript.langserver.features.rename import handle_rename
from pynescript.langserver.features.signature_help import handle_signature_help
from pynescript.langserver.features.symbols import handle_document_symbols
from pynescript.langserver.providers.builtin_metadata import get_builtin
from pynescript.langserver.providers.builtin_metadata import get_metadata
from pynescript.langserver.providers.completion_items import build_completion_item
from pynescript.langserver.providers.completion_items import build_completion_list
from pynescript.langserver.providers.completion_items import build_module_completion
from pynescript.langserver.workspace import Workspace


class TestBuiltinMetadata:
    """Test builtin metadata loading."""

    def test_metadata_loaded(self) -> None:
        """Test that metadata is loaded."""
        metadata = get_metadata()
        assert len(metadata) > 400, "Should have 400+ builtins"

    def test_ta_sma_metadata(self) -> None:
        """Test ta.sma metadata exists."""
        info = get_builtin("ta.sma")
        assert info is not None
        assert info["label"] == "ta.sma"
        assert "detail" in info
        assert "snippet" in info

    def test_category_inference(self) -> None:
        """Test that categories are inferred correctly."""
        info = get_builtin("ta.sma")
        assert info is not None
        assert info["category"] == "ta.technical_analysis"

        info2 = get_builtin("strategy.entry")
        assert info2 is not None
        assert info2["category"] == "strategy"

    def test_line_fill_alias_in_metadata(self) -> None:
        """``line.fill`` is a runtime alias of ``linefill.new`` and must be in LSP catalog."""
        info = get_builtin("line.fill")
        assert info is not None
        assert info["label"] == "line.fill"
        assert info["category"] == "line"
        assert "line1" in info["snippet"]
        assert "color" in info["documentation"]

    def test_metadata_covers_dispatch_names(self) -> None:
        """Every BuiltinEvaluator dispatch key must appear in LSP metadata.

        AXIS pre-eval flags unknown ``module.member`` paths against this catalog
        (e.g. ``line.fill`` → ``line.all`` when the alias is missing).
        """
        from pynescript.ast.evaluator.builtins import BuiltinEvaluator

        dispatch = BuiltinEvaluator()._build_builtin_map()
        metadata = get_metadata()
        missing = sorted(set(dispatch) - set(metadata))
        assert missing == [], f"LSP metadata missing dispatch names: {missing}"


class TestCompletionList:
    """Test completion list building."""

    def test_full_completion_list(self) -> None:
        """Test building full completion list."""
        cl = build_completion_list()
        assert cl.is_incomplete is False
        assert len(cl.items) > 400

    def test_module_completion(self) -> None:
        """Test module-specific completion (counts track regenerated metadata)."""
        ta_cl = build_module_completion("ta")
        assert len(ta_cl.items) >= 150

        strategy_cl = build_module_completion("strategy")
        assert len(strategy_cl.items) >= 27

    def test_completion_item_structure(self) -> None:
        """Test completion item has correct structure."""
        info = get_builtin("ta.sma")
        assert info is not None
        item = build_completion_item(info)

        assert item.label == "ta.sma"
        assert item.kind == lsp.CompletionItemKind.Function
        assert hasattr(item, "detail")


class TestCompletionHandler:
    """Test completion request handling."""

    def test_handle_completion_no_prefix(self) -> None:
        """Test completion with no prefix."""
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=0, character=0),
            context=lsp.CompletionContext(trigger_kind=lsp.CompletionTriggerKind.Invoked),
        )

        result = handle_completion(params, "//@version=5\n")
        assert isinstance(result, lsp.CompletionList)
        assert len(result.items) > 100

    def test_handle_completion_with_ta_prefix(self) -> None:
        """Test completion filtering by prefix."""
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=0, character=3),  # After "ta."
            context=lsp.CompletionContext(trigger_kind=lsp.CompletionTriggerKind.Invoked),
        )

        # Source with "ta." at cursor
        result = handle_completion(params, "//@version=5\nta.")
        assert isinstance(result, lsp.CompletionList)

    def test_handle_completion_line_fill(self) -> None:
        """``line.`` completes ``line.fill`` (alias of linefill.new)."""
        source = "//@version=6\nindicator('T')\nline."
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=5),
            context=lsp.CompletionContext(
                trigger_kind=lsp.CompletionTriggerKind.TriggerCharacter,
                trigger_character=".",
            ),
        )
        result = handle_completion(params, source)
        labels = [i.label for i in result.items]
        assert "line.fill" in labels

    def test_handle_completion_after_call_open_paren(self) -> None:
        """``plot(ta.`` must complete ``ta`` members, not an empty list."""
        source = "//@version=6\nindicator('T')\nplot(ta."
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=8),
            context=lsp.CompletionContext(
                trigger_kind=lsp.CompletionTriggerKind.TriggerCharacter,
                trigger_character=".",
            ),
        )
        result = handle_completion(params, source)
        labels = [i.label for i in result.items]
        assert "ta.sma" in labels
        sma = next(i for i in result.items if i.label == "ta.sma")
        assert sma.insert_text is not None
        assert not str(sma.insert_text).startswith("ta.")

    def test_handle_completion_keywords(self) -> None:
        """Soft keywords missing from builtin metadata still complete."""
        source = "//@version=6\nindicator('T')\nenu"
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=3),
            context=lsp.CompletionContext(trigger_kind=lsp.CompletionTriggerKind.Invoked),
        )
        result = handle_completion(params, source)
        keywords = [i for i in result.items if i.kind == lsp.CompletionItemKind.Keyword]
        labels = [i.label for i in keywords]
        assert "enum" in labels

    def test_handle_completion_user_enum_members(self) -> None:
        """``Side.`` completes user-enum members with leaf insert text."""
        source = """//@version=6
indicator("T")
enum Side
    buy = "B"
    sell = "S"
s = Side.
"""
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=5, character=9),
            context=lsp.CompletionContext(
                trigger_kind=lsp.CompletionTriggerKind.TriggerCharacter,
                trigger_character=".",
            ),
        )
        result = handle_completion(params, source)
        labels = [i.label for i in result.items]
        assert "Side.buy" in labels
        assert "Side.sell" in labels
        buy = next(i for i in result.items if i.label == "Side.buy")
        assert buy.kind == lsp.CompletionItemKind.EnumMember
        assert buy.insert_text == "buy"

    def test_handle_completion_strategy_risk_keeps_nested_path(self) -> None:
        """``strategy.`` insert text must keep ``risk.`` for nested members."""
        source = "//@version=6\nindicator('T')\nstrategy."
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=9),
            context=lsp.CompletionContext(
                trigger_kind=lsp.CompletionTriggerKind.TriggerCharacter,
                trigger_character=".",
            ),
        )
        result = handle_completion(params, source)
        item = next(i for i in result.items if i.label == "strategy.risk.max_drawdown")
        insert = str(item.insert_text)
        assert insert.startswith("risk.max_drawdown")
        assert not insert.startswith("max_drawdown")
        assert not insert.startswith("strategy.")

        nested_source = "//@version=6\nindicator('T')\nstrategy.risk."
        nested_params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=14),
            context=lsp.CompletionContext(
                trigger_kind=lsp.CompletionTriggerKind.TriggerCharacter,
                trigger_character=".",
            ),
        )
        nested = handle_completion(nested_params, nested_source)
        nested_item = next(i for i in nested.items if i.label == "strategy.risk.max_drawdown")
        nested_insert = str(nested_item.insert_text)
        assert nested_insert.startswith("max_drawdown")
        assert "risk.max_drawdown" not in nested_insert

    def test_completion_resolve_preserves_leaf_insert_text(self) -> None:
        """Resolve must not restore the fully-qualified snippet after ``ta.``."""
        source = "//@version=6\nindicator('T')\nplot(ta."
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=8),
            context=lsp.CompletionContext(
                trigger_kind=lsp.CompletionTriggerKind.TriggerCharacter,
                trigger_character=".",
            ),
        )
        result = handle_completion(params, source)
        sma = next(i for i in result.items if i.label == "ta.sma")
        resolved = handle_completion_resolve(sma)
        assert resolved.insert_text == sma.insert_text
        assert not str(resolved.insert_text).startswith("ta.")

    def test_handle_completion_user_enum_name(self) -> None:
        """Bare prefix offers the user enum type name."""
        source = """//@version=6
indicator("T")
enum Side
    buy
    sell
x = Si
"""
        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=5, character=6),
            context=lsp.CompletionContext(trigger_kind=lsp.CompletionTriggerKind.Invoked),
        )
        result = handle_completion(params, source)
        enums = [i for i in result.items if i.kind == lsp.CompletionItemKind.Enum]
        assert any(i.label == "Side" for i in enums)


class TestHoverHandler:
    """Test hover request handling."""

    def test_handle_hover_ta_sma(self) -> None:
        """Test hover over ta.sma."""
        source = "//@version=5\nindicator('Test')\nplot(ta.sma(close, 14))\n"

        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=10),  # On "sma"
        )

        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result, lsp.Hover)
        assert isinstance(result.contents, lsp.MarkupContent)

    def test_handle_hover_line_fill(self) -> None:
        """Hover over ``line.fill`` (alias of linefill.new)."""
        source = (
            "//@version=6\nindicator('T')\n"
            "line.fill(line.new(0, high, 1, low), line.new(0, low, 1, high), color.blue)\n"
        )
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=6),  # On "fill"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        assert "line.fill" in result.contents.value

    def test_handle_hover_no_symbol(self) -> None:
        """Test hover where there's no symbol."""
        source = "//@version=5\nindicator('Test')\n"

        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=0, character=0),  # On comment
        )

        result = handle_hover(params, source)
        # May return None if cursor isn't on a symbol

    def test_handle_hover_outside_document(self) -> None:
        """Test hover outside document bounds."""
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=100, character=0),
        )

        result = handle_hover(params, "//@version=5\n")
        assert result is None

    def test_handle_hover_user_enum(self) -> None:
        """Hover on a user enum type and ``Enum.member`` path."""
        source = """//@version=6
indicator("T")
enum Side
    buy = "B"
    sell = "S"
s = Side.buy
"""
        type_params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=6),  # on "Side" in decl
        )
        type_hover = handle_hover(type_params, source)
        assert type_hover is not None
        assert isinstance(type_hover.contents, lsp.MarkupContent)
        assert "enum Side" in type_hover.contents.value
        assert "buy" in type_hover.contents.value

        member_params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=5, character=10),  # on "buy" in Side.buy
        )
        member_hover = handle_hover(member_params, source)
        assert member_hover is not None
        assert isinstance(member_hover.contents, lsp.MarkupContent)
        assert "Side.buy" in member_hover.contents.value or "Member of user enum" in member_hover.contents.value

    def test_handle_hover_keyword(self) -> None:
        """Hover on a soft keyword shows a short description."""
        source = "//@version=6\nenum Side\n    buy\n"
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=1, character=1),  # on "enum"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        assert "enum" in result.contents.value
        assert "user-defined enum" in result.contents.value.lower()

    def test_handle_hover_if_keyword(self) -> None:
        """Hover on ``if`` shows a sentence, not a one-word stub."""
        source = "//@version=6\nindicator('T')\nif close > open\n    x = 1\n"
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=1),  # on "if"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value.lower()
        assert "if" in text
        assert "condition" in text or "branch" in text

    def test_handle_hover_var_keyword(self) -> None:
        """Hover on ``var`` documents the persistent declaration mode."""
        source = "//@version=6\nindicator('T')\nvar float x = close\n"
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=1),  # on "var"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value.lower()
        assert "var" in text
        assert "persist" in text or "across bars" in text

    def test_handle_hover_series_qualifier(self) -> None:
        """Hover on ``series`` documents the type qualifier."""
        source = "//@version=6\nindicator('T')\nseries float x = close\n"
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=2),  # on "series"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value.lower()
        assert "series" in text
        assert "qualifier" in text or "bar" in text

    def test_handle_hover_float_type(self) -> None:
        """Hover on ``float`` in a typed decl is the type, not ``float(...)``."""
        source = "//@version=6\nindicator('T')\nfloat len = close\n"
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=2),  # on "float"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value.lower()
        assert "float" in text
        assert "type" in text
        assert "float(...)" not in result.contents.value

    def test_handle_hover_ta_module(self) -> None:
        """Hover on ``ta`` in ``ta.sma`` documents the namespace."""
        source = "//@version=6\nindicator('T')\nplot(ta.sma(close, 14))\n"
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=5),  # on "ta"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value.lower()
        assert "ta" in text
        assert "namespace" in text or "technical" in text

    def test_handle_hover_line_namespace_and_type(self) -> None:
        """``line.fill`` module prefix is the namespace; bare ``line`` is the type."""
        dotted = (
            "//@version=6\nindicator('T')\n"
            "line.fill(line.new(0, high, 1, low), line.new(0, low, 1, high), color.blue)\n"
        )
        ns_params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=1),  # on "line" in line.fill
        )
        ns_hover = handle_hover(ns_params, dotted)
        assert ns_hover is not None
        assert isinstance(ns_hover.contents, lsp.MarkupContent)
        ns_text = ns_hover.contents.value.lower()
        assert "namespace" in ns_text
        assert "line.fill" in ns_text or "line.new" in ns_text

        typed = "//@version=6\nindicator('T')\nline l = line.new(0, high, 1, low)\n"
        type_params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=1),  # on type ``line``
        )
        type_hover = handle_hover(type_params, typed)
        assert type_hover is not None
        assert isinstance(type_hover.contents, lsp.MarkupContent)
        type_text = type_hover.contents.value.lower()
        assert "type" in type_text
        assert "namespace" not in type_text

    def test_handle_hover_log_namespace(self) -> None:
        """Hover on ``log`` in ``log.info`` documents the namespace."""
        source = '//@version=6\nindicator("T")\nlog.info("hi")\n'
        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=1),  # on "log"
        )
        result = handle_hover(params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value.lower()
        assert "namespace" in text
        assert "log.info" in text or "log" in text

    def test_handle_hover_user_function(self) -> None:
        """Hover on a user function shows kind plus the source signature."""
        source = "//@version=6\nindicator('T')\nfoo(a) => a\nplot(foo(1))\n"
        decl_params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=2, character=1),  # on "foo" in decl
        )
        result = handle_hover(decl_params, source)
        assert result is not None
        assert isinstance(result.contents, lsp.MarkupContent)
        text = result.contents.value
        assert "foo" in text
        assert "function" in text.lower()
        assert "foo(a)" in text

        use_params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=3, character=6),  # on "foo" in plot(foo(1))
        )
        use_hover = handle_hover(use_params, source)
        assert use_hover is not None
        assert isinstance(use_hover.contents, lsp.MarkupContent)
        assert "foo(a)" in use_hover.contents.value


class TestDefinitionHandler:
    """Test go-to-definition request handling."""

    def test_handle_definition_function(self) -> None:
        """Test go-to-definition for a function."""
        source = """//@version=5
indicator("Test")

myFunction() =>
    ta.sma(close, 14)

plot(myFunction())
"""
        # Line 6 (0-indexed) is "plot(myFunction())", position 6 is after "plot("
        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=6, character=6),
        )

        result = handle_definition(params, source, "file:///test.pine")
        # Should find the function definition at line 4 (0-indexed)
        assert result is not None
        assert len(result) >= 1

    def test_handle_definition_variable(self) -> None:
        """Test go-to-definition for a variable."""
        source = """//@version=5
indicator("Test")

length = 14
plot(ta.sma(close, length))
"""
        # Line 4 (0-indexed) is "plot(ta.sma(close, length))"
        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=4, character=25),
        )

        result = handle_definition(params, source, "file:///test.pine")
        # Should find the variable definition at line 3 (0-indexed)
        assert result is not None
        assert len(result) >= 1

    def test_handle_definition_no_symbol(self) -> None:
        """Test go-to-definition when cursor is not on a symbol."""
        source = "//@version=5\nindicator('Test')\n"
        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=0, character=0),  # On comment
        )

        result = handle_definition(params, source, "file:///test.pine")
        assert result is None or len(result) == 0

    def test_handle_definition_user_enum_member(self) -> None:
        """Go-to-definition on ``Side.buy`` lands on the member declaration."""
        source = """//@version=6
indicator("T")
enum Side
    buy = "B"
    sell = "S"
s = Side.buy
"""
        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=5, character=10),  # on "buy"
        )
        result = handle_definition(params, source, "file:///test.pine")
        assert result is not None
        assert any(loc.range.start.line == 3 for loc in result)


class TestReferencesHandler:
    """Test find-references request handling."""

    def test_handle_references(self) -> None:
        """Test find references for a variable."""
        source = """//@version=5
indicator("Test")

length = 14
plot(ta.sma(close, length))
plot(ta.ema(close, length))
"""
        # Line 4 is plot(... length) — character 25 lands on "length"
        params = lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=4, character=25),
            context=lsp.ReferenceContext(include_declaration=True),
        )

        result = handle_references(params, source, "file:///test.pine")
        # definition (line 3) + two uses (lines 4 and 5)
        assert len(result) == 3
        lines = sorted(r.range.start.line for r in result)
        assert lines == [3, 4, 5]

    def test_handle_references_no_include_declaration(self) -> None:
        """Test find references without including declaration."""
        source = """//@version=5
indicator("Test")

length = 14
plot(ta.sma(close, length))
"""
        params = lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=4, character=25),  # On "length" use
            context=lsp.ReferenceContext(include_declaration=False),
        )

        result = handle_references(params, source, "file:///test.pine")
        assert len(result) >= 1
        # Declaration line must be excluded
        assert all(r.range.start.line != 3 for r in result)

    def test_handle_references_inside_function_body(self) -> None:
        """Recursive / in-body references must not be skipped when name matches."""
        source = """//@version=5
indicator("T")

myFunc(x) =>
    myFunc(x - 1)

plot(myFunc(1))
"""
        params = lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=3, character=0),  # On "myFunc" def
            context=lsp.ReferenceContext(include_declaration=True),
        )
        result = handle_references(params, source, "file:///test.pine")
        # def, recursive call inside body, outer plot call
        assert len(result) >= 3
        lines = sorted({r.range.start.line for r in result})
        assert 3 in lines  # definition
        assert 4 in lines  # recursive body call
        assert 6 in lines  # plot(myFunc(1))


class TestDocumentSymbolsHandler:
    """Test document symbols request handling."""

    def test_handle_document_symbols(self) -> None:
        """Test document symbols for a script."""
        source = """//@version=5
indicator("Test")

length = 14
fastMA = ta.sma(close, length)
slowMA = ta.ema(close, length)

myFunction() =>
    ta.sma(close, 14)

plot(myFunction())
"""
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )

        result = handle_document_symbols(params, source, "file:///test.pine")
        assert len(result) >= 1

        # Check for function symbol — exactly once (no double-flush)
        func_symbols = [s for s in result if s.kind == lsp.SymbolKind.Function]
        assert len(func_symbols) == 1
        assert func_symbols[0].name == "myFunction"

        # Check for variable symbols
        var_symbols = [s for s in result if s.kind == lsp.SymbolKind.Variable]
        assert len(var_symbols) >= 2

    def test_handle_document_symbols_no_double_flush(self) -> None:
        """Functions must appear once; top-level vars after a function must not nest."""
        source = """//@version=5
indicator("T")

f1() =>
    1

f2() =>
    2

x = 3
"""
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        result = handle_document_symbols(params, source, "file:///test.pine")
        func_names = [s.name for s in result if s.kind == lsp.SymbolKind.Function]
        assert func_names == ["f1", "f2"]
        var_names = [s.name for s in result if s.kind == lsp.SymbolKind.Variable]
        assert "x" in var_names

    def test_handle_document_symbols_empty(self) -> None:
        """Test document symbols for empty source."""
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )

        result = handle_document_symbols(params, "", "file:///test.pine")
        assert len(result) == 0

    def test_handle_document_symbols_selection_contained_in_full_range(self) -> None:
        """Every symbol's selectionRange must be contained in its fullRange.

        VS Code raises ``selectionRange must be contained in fullRange`` and
        drops the whole outline otherwise. Single-line statements previously
        produced zero-width full ranges ending at character 0.
        """

        def _contains(full: lsp.Range, sel: lsp.Range) -> bool:
            return (full.start.line, full.start.character) <= (
                sel.start.line,
                sel.start.character,
            ) and (sel.end.line, sel.end.character) <= (full.end.line, full.end.character)

        def _walk(symbols: list[lsp.DocumentSymbol]) -> list[lsp.DocumentSymbol]:
            out: list[lsp.DocumentSymbol] = []
            for s in symbols:
                out.append(s)
                out.extend(_walk(list(s.children or [])))
            return out

        source = """//@version=5
indicator("Test")

length = 14
fastMA = ta.sma(close, length)

myFunction() =>
    inner = 1
    inner

type MySettings
    int size = 10

settings = MySettings.new()
"""
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        result = handle_document_symbols(params, source, "file:///test.pine")
        assert len(result) >= 1

        for sym in _walk(result):
            assert sym.range is not None and sym.selection_range is not None
            assert _contains(sym.range, sym.selection_range), (
                f"{sym.name}: selection {sym.selection_range} not in full {sym.range}"
            )

    def test_handle_document_symbols_with_type(self) -> None:
        """Test document symbols with user-defined type."""
        source = """//@version=5
indicator("Test")

type MySettings
    int length = 14
    bool enabled = true

settings = MySettings.new()
"""
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )

        result = handle_document_symbols(params, source, "file:///test.pine")
        # Should find the type definition
        type_symbols = [s for s in result if s.kind == lsp.SymbolKind.Class]
        assert len(type_symbols) >= 1
        assert type_symbols[0].name == "MySettings"

    def test_handle_document_symbols_reuses_tree(self) -> None:
        """Passing a pre-parsed tree must not require re-parse of source."""
        from pynescript.ast.helper import parse

        source = """//@version=5
indicator("T")
length = 14
"""
        tree = parse(source, filename="file:///test.pine")
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        # Empty source would fail if tree were ignored and re-parsed from source.
        result = handle_document_symbols(params, source=None, uri="file:///test.pine", tree=tree)
        var_names = [s.name for s in result if s.kind == lsp.SymbolKind.Variable]
        assert "length" in var_names

    def test_handle_document_symbols_enum(self) -> None:
        """EnumDef is an outline enum; members are not top-level variables."""
        source = """//@version=6
indicator("T")
enum Side
    buy = "B"
    sell = "S"
s = Side.buy
"""
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        result = handle_document_symbols(params, source, "file:///test.pine")
        enums = [s for s in result if s.kind == lsp.SymbolKind.Enum]
        assert len(enums) == 1
        assert enums[0].name == "Side"
        members = list(enums[0].children or [])
        assert [m.name for m in members] == ["buy", "sell"]
        assert all(m.kind == lsp.SymbolKind.EnumMember for m in members)
        var_names = [s.name for s in result if s.kind == lsp.SymbolKind.Variable]
        assert "buy" not in var_names
        assert "sell" not in var_names
        assert "s" in var_names


class TestFormattingHandler:
    """Test formatting request handling."""

    def test_handle_formatting(self) -> None:
        """Test document formatting."""
        source = """//@version=5
indicator("Test")

length=14
plot(ta.sma(close,length))
"""
        params = lsp.DocumentFormattingParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            options=lsp.FormattingOptions(tab_size=4, insert_spaces=True),
        )

        result = handle_formatting(params, source)
        assert result is not None
        assert len(result) >= 1

    def test_handle_formatting_empty(self) -> None:
        """Test formatting empty source."""
        params = lsp.DocumentFormattingParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            options=lsp.FormattingOptions(tab_size=4, insert_spaces=True),
        )

        result = handle_formatting(params, "")
        assert result == []

    def test_handle_range_formatting(self) -> None:
        """Test range formatting."""
        source = """//@version=5
indicator("Test")
length=14
plot(ta.sma(close,length))
"""
        params = lsp.DocumentRangeFormattingParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            range=lsp.Range(
                start=lsp.Position(line=2, character=0),
                end=lsp.Position(line=2, character=20),
            ),
            options=lsp.FormattingOptions(tab_size=4, insert_spaces=True),
        )

        result = handle_range_formatting(params, source)
        assert result is not None


class TestSemanticTokensHandler:
    """Semantic tokens + workspace AST reuse."""

    def test_handle_semantic_tokens_basic(self) -> None:
        from pynescript.langserver.features.semantic_tokens import handle_semantic_tokens

        source = """//@version=5
indicator("T")
length = 14
plot(ta.sma(close, length))
"""
        params = lsp.SemanticTokensParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        result = handle_semantic_tokens(params, source)
        assert result is not None
        assert len(result.data) > 0
        assert len(result.data) % 5 == 0

    def test_handle_semantic_tokens_reuses_tree(self) -> None:
        from pynescript.ast.helper import parse
        from pynescript.langserver.features.semantic_tokens import handle_semantic_tokens

        source = """//@version=5
indicator("T")
x = 1
"""
        tree = parse(source)
        params = lsp.SemanticTokensParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        with_tree = handle_semantic_tokens(params, source=None, tree=tree)
        without = handle_semantic_tokens(params, source)
        assert with_tree is not None and without is not None
        assert with_tree.data == without.data

    def test_handle_semantic_tokens_incomplete_source(self) -> None:
        from pynescript.langserver.features.semantic_tokens import handle_semantic_tokens

        params = lsp.SemanticTokensParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        )
        result = handle_semantic_tokens(params, "//@version=5\nplot(ta.")
        assert result is not None
        assert result.data == []


class TestConvertToV6:
    """Source-level Convert-to-v6 command + code action."""

    def test_handle_convert_rewrites_v4(self) -> None:
        src = '//@version=4\nstudy("t")\nplot(sma(close, 14))\n'
        payload = handle_convert_to_v6(src)
        assert payload["from_version"] == 4
        assert payload["to_version"] == 6
        assert payload["changed"] is True
        assert "ta.sma(" in payload["source"]
        assert "indicator(" in payload["source"]

    def test_code_action_offered_for_old_version(self) -> None:
        src = '//@version=5\nindicator("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src)
        assert len(actions) == 1
        assert "v5" in actions[0].title
        assert actions[0].edit is not None

    def test_code_action_hidden_for_v6(self) -> None:
        src = '//@version=6\nindicator("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        assert handle_code_action(params, src) == []

    def test_w001_quickfix_inserts_version_pragma(self) -> None:
        src = 'indicator("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src)
        quick = [a for a in actions if a.kind == lsp.CodeActionKind.QuickFix]
        assert len(quick) == 1
        action = quick[0]
        edit = _first_text_edit(action)
        assert edit.new_text == "//@version=6\n"
        assert edit.range.start == lsp.Position(line=0, character=0)
        assert edit.range.end == lsp.Position(line=0, character=0)

    def test_w001_quickfix_hidden_when_version_present(self) -> None:
        src = '//@version=6\nindicator("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src)
        assert not any(a.kind == lsp.CodeActionKind.QuickFix for a in actions)

    def test_v5_still_offers_convert_not_w001(self) -> None:
        src = '//@version=5\nindicator("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src)
        assert any(a.kind == lsp.CodeActionKind.RefactorRewrite for a in actions)
        assert not any(a.kind == lsp.CodeActionKind.QuickFix for a in actions)

    def test_w002_quickfix_replaces_old_pragma(self) -> None:
        src = '//@version=4\nstudy("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src)
        assert any(a.kind == lsp.CodeActionKind.RefactorRewrite for a in actions)
        quick = [a for a in actions if a.kind == lsp.CodeActionKind.QuickFix]
        assert len(quick) == 1
        edit = _first_text_edit(quick[0])
        assert edit.new_text == "//@version=6"
        from pynescript.langserver.workspace import _apply_text_edit

        fixed = _apply_text_edit(src, edit.range, edit.new_text)
        assert fixed.startswith("//@version=6\n")
        assert "//@version=4" not in fixed
        assert fixed.count("//@version=") == 1

    def test_w002_quickfix_from_lint_warnings(self) -> None:
        from pynescript.ast.linter import lint_script

        src = '// license\n//@version=3\nindicator("t")\nplot(close)\n'
        warnings = lint_script(src)
        assert any(w.code == "W002" for w in warnings)
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=1, character=0),
                end=lsp.Position(line=1, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src, warnings)
        quick = [a for a in actions if a.kind == lsp.CodeActionKind.QuickFix]
        assert len(quick) == 1
        edit = _first_text_edit(quick[0])
        from pynescript.langserver.workspace import _apply_text_edit

        fixed = _apply_text_edit(src, edit.range, edit.new_text)
        assert fixed.startswith("// license\n//@version=6\n")
        assert "//@version=3" not in fixed
        assert fixed.count("//@version=") == 1

    def test_w002_quickfix_hidden_for_v6(self) -> None:
        src = '//@version=6\nindicator("t")\nplot(close)\n'
        params = lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///t.pine"),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=1),
            ),
            context=lsp.CodeActionContext(diagnostics=[]),
        )
        actions = handle_code_action(params, src)
        assert not any(a.kind == lsp.CodeActionKind.QuickFix for a in actions)


def _first_text_edit(action: lsp.CodeAction) -> lsp.TextEdit:
    """Return the first TextEdit on a code action workspace edit."""
    edit = action.edit
    assert edit is not None
    if edit.document_changes:
        change = edit.document_changes[0]
        assert isinstance(change, lsp.TextDocumentEdit)
        first = change.edits[0]
        assert isinstance(first, lsp.TextEdit)
        return first
    if edit.changes:
        uri_edits = next(iter(edit.changes.values()))
        first = uri_edits[0]
        assert isinstance(first, lsp.TextEdit)
        return first
    missing = "code action has no text edit"
    raise AssertionError(missing)


def _position_after(source: str, prefix: str) -> lsp.Position:
    """LSP position immediately after the first occurrence of *prefix*."""
    idx = source.index(prefix) + len(prefix)
    line = source.count("\n", 0, idx)
    col = idx - (source.rfind("\n", 0, idx) + 1)
    return lsp.Position(line=line, character=col)


def _signature_params(source: str, prefix: str) -> lsp.SignatureHelpParams:
    return lsp.SignatureHelpParams(
        text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        position=_position_after(source, prefix),
    )


def _param_label(param: lsp.ParameterInformation) -> str:
    label = param.label
    return label if isinstance(label, str) else ""


class TestSignatureHelpHandler:
    """textDocument/signatureHelp for builtins and user FunctionDef."""

    def test_builtin_plot_after_paren(self) -> None:
        source = "//@version=6\nindicator('T')\nplot(close)\n"
        result = handle_signature_help(_signature_params(source, "plot("), source)
        assert result is not None
        assert result.active_signature == 0
        assert result.active_parameter == 0
        assert result.signatures
        assert "plot" in result.signatures[0].label

    def test_builtin_ta_sma_after_comma(self) -> None:
        source = "//@version=6\nindicator('T')\nta.sma(close, 14)\n"
        result = handle_signature_help(_signature_params(source, "ta.sma(close,"), source)
        assert result is not None
        assert result.active_signature == 0
        assert result.active_parameter == 1
        labels = [_param_label(p) for p in (result.signatures[0].parameters or [])]
        assert len(labels) >= 2

    def test_nested_parens_innermost_callee(self) -> None:
        source = "//@version=6\nindicator('T')\nplot(math.max(close, open))\n"
        result = handle_signature_help(_signature_params(source, "math.max(close,"), source)
        assert result is not None
        label = result.signatures[0].label
        assert "max" in label
        assert not label.startswith("plot")

    def test_user_function_active_parameter(self) -> None:
        source = "//@version=6\nindicator('T')\nf(int x, int y) => x + y\nplot(f(1, 2))\n"
        result = handle_signature_help(_signature_params(source, "plot(f(1,"), source)
        assert result is not None
        assert result.active_parameter == 1
        labels = [_param_label(p) for p in (result.signatures[0].parameters or [])]
        assert any("y" in lab for lab in labels)

    def test_outside_call_returns_none(self) -> None:
        source = "//@version=6\nindicator('T')\nlength = 14\n"
        params = lsp.SignatureHelpParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=_position_after(source, "len"),
        )
        assert handle_signature_help(params, source) is None

    def test_unknown_callee_returns_none(self) -> None:
        source = "//@version=6\nindicator('T')\nzzz_unknown(1, 2)\n"
        assert handle_signature_help(_signature_params(source, "zzz_unknown("), source) is None

    def test_empty_source_returns_none(self) -> None:
        params = lsp.SignatureHelpParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=0, character=0),
        )
        assert handle_signature_help(params, "") is None
        assert handle_signature_help(params, None) is None


_FOLD_SOURCE = """//@version=6
indicator("T")

if close > 0
    x = 1
    y = 2
else if close < 0
    x = 3
else
    x = 4

for i = 0 to 10
    s = i

for v in array.from(1, 2)
    t = v

while bar_index < 3
    n = 1

switch close
    0 => 0
    => 1

once bar_index == 0
    z = 1

f() =>
    a = 1
    a

g() => 1

type Point
    float x
    float y

enum Side
    buy
    sell

//#region foo
plot(close)
//#endregion

# region hash
plot(1)
# endregion
"""


def _folding_params() -> lsp.FoldingRangeParams:
    return lsp.FoldingRangeParams(
        text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
    )


def _line_of(source: str, needle: str) -> int:
    for i, line in enumerate(source.splitlines()):
        if needle in line:
            return i
    msg = f"needle not found: {needle!r}"
    raise AssertionError(msg)


def _fold_starting(ranges: list[lsp.FoldingRange], start_line: int) -> lsp.FoldingRange:
    matches = [r for r in ranges if r.start_line == start_line]
    assert matches, f"no fold starting at line {start_line}"
    return max(matches, key=lambda r: r.end_line)


def _assert_block_fold(
    ranges: list[lsp.FoldingRange],
    source: str,
    start_needle: str,
    last_body_needle: str,
    next_needle: str | None = None,
) -> lsp.FoldingRange:
    start = _line_of(source, start_needle)
    last_body = _line_of(source, last_body_needle)
    rng = _fold_starting(ranges, start)
    assert rng.end_line >= last_body, (start_needle, rng, last_body)
    if next_needle is not None:
        nxt = _line_of(source, next_needle)
        assert rng.end_line < nxt, (start_needle, rng, nxt)
    return rng


class TestFoldingRangeHandler:
    """Test textDocument/foldingRange for blocks and region comments."""

    def test_handle_folding_range_blocks_and_regions(self) -> None:
        """if/for/while/switch/once/type/enum/function and //#region folds."""
        result = handle_folding_range(_folding_params(), _FOLD_SOURCE)
        assert result
        src = _FOLD_SOURCE

        _assert_block_fold(result, src, "if close > 0", "x = 4", "for i = 0 to 10")
        _assert_block_fold(result, src, "else if close < 0", "x = 4", "for i = 0 to 10")
        _assert_block_fold(result, src, "for i = 0 to 10", "s = i", "for v in")
        _assert_block_fold(result, src, "for v in", "t = v", "while bar_index")
        _assert_block_fold(result, src, "while bar_index", "n = 1", "switch close")
        _assert_block_fold(result, src, "switch close", "=> 1", "once bar_index")
        _assert_block_fold(result, src, "once bar_index", "z = 1", "f() =>")
        _assert_block_fold(result, src, "f() =>", "    a", "g() =>")
        _assert_block_fold(result, src, "type Point", "float y", "enum Side")
        _assert_block_fold(result, src, "enum Side", "sell", "//#region foo")

        # One-line function is not a fold.
        g_line = _line_of(src, "g() => 1")
        assert all(r.start_line != g_line for r in result)

        regions = [r for r in result if r.kind == lsp.FoldingRangeKind.Region]
        assert len(regions) == 2
        slash = next(r for r in regions if r.start_line == _line_of(src, "//#region foo"))
        assert slash.end_line == _line_of(src, "//#endregion")
        hashed = next(r for r in regions if r.start_line == _line_of(src, "# region hash"))
        assert hashed.end_line == _line_of(src, "# endregion")

    def test_handle_folding_range_empty_and_unparsed(self) -> None:
        """Empty source and parse failure return an empty list."""
        params = _folding_params()
        assert handle_folding_range(params, "") == []
        assert handle_folding_range(params, None) == []
        assert handle_folding_range(params, "this is not valid pine @#$") == []
        assert handle_folding_range(params, "x = 1", tree=None) == []

    def test_handle_folding_range_reuses_tree(self) -> None:
        """Passing a pre-parsed tree must not require re-parse of source."""
        from pynescript.ast.helper import parse

        tree = parse(_FOLD_SOURCE, filename="file:///test.pine")
        result = handle_folding_range(_folding_params(), source=None, tree=tree)
        if_fold = _fold_starting(result, _line_of(_FOLD_SOURCE, "if close > 0"))
        assert if_fold.end_line > if_fold.start_line
        # Regions come from source lines; omitted when source is None.
        assert all(r.kind != lsp.FoldingRangeKind.Region for r in result)


class TestCapabilities:
    """Advertised capabilities must match implemented handlers only."""

    def test_signature_help_provider(self) -> None:
        from pynescript.langserver.config import get_server_capabilities

        caps = get_server_capabilities()
        assert caps.signature_help_provider is not None
        triggers = list(caps.signature_help_provider.trigger_characters or [])
        assert "(" in triggers
        assert "," in triggers
        assert caps.code_action_provider is not None
        kinds = list(caps.code_action_provider.code_action_kinds or [])
        assert lsp.CodeActionKind.QuickFix in kinds
        assert lsp.CodeActionKind.RefactorRewrite in kinds
        assert caps.execute_command_provider is not None
        assert "pynescript.convertToV6" in (caps.execute_command_provider.commands or [])
        assert caps.hover_provider is True
        assert caps.definition_provider is True
        assert caps.document_highlight_provider is True
        assert caps.rename_provider is not None
        assert caps.rename_provider.prepare_provider is True
        assert caps.semantic_tokens_provider is not None
        assert caps.semantic_tokens_provider.full is True
        assert caps.semantic_tokens_provider.range is False
        assert caps.folding_range_provider is True


_FOO_SOURCE = """//@version=6
indicator("T")
foo = close
plot(foo)
"""

_FOO_IF_SOURCE = """//@version=6
indicator("T")
foo = close
if bar_index > 0
    plot(foo)
"""

_FOO_TA_SOURCE = """//@version=6
indicator("T")
foo = close
plot(ta.sma(foo, 14))
"""


def _highlight_params(source: str, prefix: str) -> lsp.DocumentHighlightParams:
    return lsp.DocumentHighlightParams(
        text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        position=_position_after(source, prefix),
    )


def _prepare_rename_params(source: str, prefix: str) -> lsp.PrepareRenameParams:
    return lsp.PrepareRenameParams(
        text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        position=_position_after(source, prefix),
    )


def _rename_params(source: str, prefix: str, new_name: str) -> lsp.RenameParams:
    return lsp.RenameParams(
        text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
        position=_position_after(source, prefix),
        new_name=new_name,
    )


def _apply_text_edits(source: str, edits: list[lsp.TextEdit]) -> str:
    """Apply LSP text edits (same-line only) from last to first."""
    ordered = sorted(
        edits,
        key=lambda e: (e.range.start.line, e.range.start.character),
        reverse=True,
    )
    lines = source.split("\n")
    for edit in ordered:
        start = edit.range.start
        end = edit.range.end
        assert start.line == end.line
        line = lines[start.line]
        lines[start.line] = line[: start.character] + edit.new_text + line[end.character :]
    return "\n".join(lines)


class TestDocumentHighlightHandler:
    """textDocument/documentHighlight for same-document ident occurrences."""

    def test_highlight_declaration_and_use(self) -> None:
        result = handle_document_highlight(_highlight_params(_FOO_SOURCE, "foo"), _FOO_SOURCE)
        assert result is not None
        assert len(result) >= 2
        texts = [_range_text(_FOO_SOURCE, h.range) for h in result]
        assert texts.count("foo") >= 2

    def test_highlight_same_line_twice(self) -> None:
        source = '//@version=6\nindicator("T")\nfoo = close\nplot(foo + foo)\n'
        result = handle_document_highlight(_highlight_params(source, "foo"), source)
        assert result is not None
        assert len(result) >= 3
        texts = [_range_text(source, h.range) for h in result]
        assert texts.count("foo") >= 3

    def test_highlight_empty_source(self) -> None:
        params = lsp.DocumentHighlightParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.pine"),
            position=lsp.Position(line=0, character=0),
        )
        assert handle_document_highlight(params, "") == []
        assert handle_document_highlight(params, None) == []


class TestRenameHandler:
    """textDocument/prepareRename and textDocument/rename (open workspace docs)."""

    def test_prepare_rename_user_ident(self) -> None:
        result = handle_prepare_rename(_prepare_rename_params(_FOO_SOURCE, "foo"), _FOO_SOURCE)
        assert result is not None
        rng = result if isinstance(result, lsp.Range) else result.range
        assert _range_text(_FOO_SOURCE, rng) == "foo"

    def test_prepare_rename_keyword_null(self) -> None:
        result = handle_prepare_rename(_prepare_rename_params(_FOO_IF_SOURCE, "if"), _FOO_IF_SOURCE)
        assert result is None

    def test_prepare_rename_builtin_namespace_null(self) -> None:
        result = handle_prepare_rename(_prepare_rename_params(_FOO_TA_SOURCE, "ta"), _FOO_TA_SOURCE)
        assert result is None

    def test_rename_declaration_and_uses(self) -> None:
        params = _rename_params(_FOO_SOURCE, "foo", "bar")
        result = handle_rename(params, _FOO_SOURCE, params.text_document.uri)
        assert result is not None
        edits = list((result.changes or {}).get(params.text_document.uri, []))
        assert len(edits) >= 2
        rewritten = _apply_text_edits(_FOO_SOURCE, edits)
        assert "bar = close" in rewritten
        assert "plot(bar)" in rewritten
        assert "foo" not in rewritten.split("indicator", 1)[-1]

    def test_rename_leaves_keywords_and_other_idents(self) -> None:
        source = """//@version=6
indicator("T")
foo = close
foobar = 1
if bar_index > 0
    plot(foo)
"""
        params = _rename_params(source, "foo", "bar")
        result = handle_rename(params, source, params.text_document.uri)
        assert result is not None
        edits = list((result.changes or {}).get(params.text_document.uri, []))
        rewritten = _apply_text_edits(source, edits)
        assert "bar = close" in rewritten
        assert "foobar = 1" in rewritten
        assert "if bar_index > 0" in rewritten
        assert "plot(bar)" in rewritten

    def test_rename_rejects_invalid_new_name(self) -> None:
        uri = "file:///test.pine"
        assert handle_rename(_rename_params(_FOO_SOURCE, "foo", "if"), _FOO_SOURCE, uri) is None
        assert handle_rename(_rename_params(_FOO_SOURCE, "foo", "ta"), _FOO_SOURCE, uri) is None
        assert handle_rename(_rename_params(_FOO_SOURCE, "foo", "1bar"), _FOO_SOURCE, uri) is None

    def test_rename_keyword_is_noop(self) -> None:
        assert handle_rename(_rename_params(_FOO_IF_SOURCE, "if", "iff"), _FOO_IF_SOURCE, "file:///test.pine") is None

    def test_rename_updates_other_open_documents(self) -> None:
        src_a = _FOO_SOURCE
        src_b = """//@version=6
indicator("B")
foo = close
plot(foo)
"""
        uri_a = "file:///a.pine"
        uri_b = "file:///b.pine"
        ws = Workspace()
        doc_a = ws.put_document(uri_a, src_a)
        ws.put_document(uri_b, src_b)
        params = lsp.RenameParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri_a),
            position=_position_after(src_a, "foo"),
            new_name="bar",
        )
        result = handle_rename(params, src_a, uri_a, tree=doc_a.ast, workspace=ws)
        assert result is not None
        changes = result.changes or {}
        assert uri_a in changes
        assert uri_b in changes
        rewritten_a = _apply_text_edits(src_a, list(changes[uri_a]))
        rewritten_b = _apply_text_edits(src_b, list(changes[uri_b]))
        assert "bar = close" in rewritten_a
        assert "plot(bar)" in rewritten_a
        assert "bar = close" in rewritten_b
        assert "plot(bar)" in rewritten_b

    def test_rename_skips_documents_that_are_not_open(self) -> None:
        src_a = _FOO_SOURCE
        src_b = """//@version=6
indicator("B")
foo = close
plot(foo)
"""
        src_closed = """//@version=6
indicator("C")
foo = close
plot(foo)
"""
        uri_a = "file:///a.pine"
        uri_b = "file:///b.pine"
        uri_closed = "file:///closed.pine"
        ws = Workspace()
        doc_a = ws.put_document(uri_a, src_a)
        ws.put_document(uri_b, src_b)
        params = lsp.RenameParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri_a),
            position=_position_after(src_a, "foo"),
            new_name="bar",
        )
        result = handle_rename(params, src_a, uri_a, tree=doc_a.ast, workspace=ws)
        assert result is not None
        changes = result.changes or {}
        assert uri_a in changes
        assert uri_b in changes
        assert uri_closed not in changes
        assert "foo" in src_closed

    def test_prepare_rename_stays_cursor_document(self) -> None:
        ws = Workspace()
        ws.put_document("file:///a.pine", _FOO_SOURCE)
        ws.put_document("file:///b.pine", _FOO_SOURCE)
        result = handle_prepare_rename(_prepare_rename_params(_FOO_SOURCE, "foo"), _FOO_SOURCE)
        assert result is not None
        rng = result if isinstance(result, lsp.Range) else result.range
        assert _range_text(_FOO_SOURCE, rng) == "foo"


def _range_text(source: str, rng: lsp.Range) -> str:
    lines = source.split("\n")
    assert rng.start.line == rng.end.line
    return lines[rng.start.line][rng.start.character : rng.end.character]
