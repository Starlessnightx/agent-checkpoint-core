from __future__ import annotations

from harness.tools.base import ToolContext, ToolSpec, object_schema

from .adapter import query


def lsp_tool_specs() -> tuple[ToolSpec, ...]:
    path = {"type": "string", "description": "Workspace-relative source file."}
    symbol = {"type": "string", "description": "Symbol to locate with a bounded in-file search when line and character are omitted."}
    line = {"type": "integer", "minimum": 0, "description": "Optional zero-based LSP line."}
    character = {"type": "integer", "minimum": 0, "description": "Optional zero-based UTF-16 LSP character offset."}

    def definition(context: ToolContext, args: dict):
        return query(getattr(context, "lsp_manager", None), context, args, "textDocument/definition")

    def references(context: ToolContext, args: dict):
        return query(getattr(context, "lsp_manager", None), context, args, "textDocument/references")

    def diagnostics(context: ToolContext, args: dict):
        return query(getattr(context, "lsp_manager", None), context, args, "textDocument/diagnostic")

    return (
        ToolSpec("lsp_definition", "Find a symbol definition with LSP using an explicit position or a bounded symbol lookup.", object_schema({"path": path, "symbol": symbol, "line": line, "character": character}, ("path",)), definition, "lsp:definition"),
        ToolSpec("lsp_references", "Find symbol references with LSP using an explicit position or a bounded symbol lookup.", object_schema({"path": path, "symbol": symbol, "line": line, "character": character}, ("path",)), references, "lsp:references"),
        ToolSpec("lsp_diagnostics", "Return normalized diagnostics for a Python or TypeScript/JavaScript file.", object_schema({"path": path, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("path",)), diagnostics, "lsp:diagnostics"),
    )
