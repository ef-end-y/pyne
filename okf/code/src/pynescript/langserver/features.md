---
type: "Code Module"
title: "src/pynescript/langserver/features"
description: "LSP feature handlers (one module per capability area)."
resource: "src/pynescript/langserver/features"
tags: [code, features, langserver, pynescript]
status: stable
generated:
  by: process:axis-okf/1
  at: 2026-10-19T03:03:27Z
sources:
  - id: tree
    resource: "src/pynescript/langserver/features"
    title: "src/pynescript/langserver/features"
    author: process:git
okf_lock: generated
---

# Files

* `__init__.py`
* `completion.py` — handle_completion, handle_completion_resolve
* `convert.py` — convert_payload, handle_code_action, handle_convert_to_v6, whole_document_edit
* `definitions.py` — DefinitionFinder, handle_definition
* `diagnostics.py` — create_diagnostic_related_info, create_quick_fix, lint_warnings_to_diagnostics
* `document_color.py` — handle_color_presentation, handle_document_color
* `folding.py` — handle_folding_range
* `formatting.py` — handle_formatting, handle_range_formatting
* `highlight.py` — IdentHit, collect_ident_hits, cursor_ident, handle_document_highlight, is_renamable_ident, is_valid_pine_ident, resolve_tree
* `hover.py` — handle_hover
* `inlay_hints.py` — handle_inlay_hints
* `references.py` — ReferencesFinder, handle_references
* `rename.py` — handle_prepare_rename, handle_rename
* `selection_range.py` — handle_selection_range
* `semantic_tokens.py` — handle_semantic_tokens
* `signature_help.py` — handle_signature_help
* `symbols.py` — DocumentSymbolCollector, handle_document_symbols
