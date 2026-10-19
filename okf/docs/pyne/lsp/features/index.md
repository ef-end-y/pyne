# docs/pyne/lsp/features

# Concepts

* [Completion](completion.md) - textDocument/completion and completionItem/resolve from builtin metadata, module-dot triggers, and fuzzy filter.
* [Diagnostics](diagnostics.md) - Push and pull diagnostics: parse errors, lint rules, severity mapping, and the W001/W002/C002 QuickFixes.
* [Folding ranges](folding.md) - textDocument/foldingRange for Pine blocks, type/enum/function bodies, and //region comments.
* [Formatting](formatting.md) - Document and range formatting via AST parse → NodeUnparser round-trip.
* [Hover](hover.md) - textDocument/hover — Markdown documentation cards for Pine builtins from metadata.
* [Inlay Hints](inlay-hints.md) - Inferred type annotations on simple assignments — const, series, and input kinds.
* [Navigation](navigation.md) - Go-to-definition, find-references, document highlight, rename across open documents, document outline, and workspace symbol search over the ASDL AST.
* [Selection ranges](selection-range.md) - textDocument/selectionRange expands from an ident or smallest AST node to the enclosing statement and block.
* [Semantic Tokens](semantic-tokens.md) - textDocument/semanticTokens/full — AST visitor, custom legend, and LSP delta encoding.
