from __future__ import annotations

import re
from typing import Any

from harness.tools.base import ToolContext, ToolFailure, ToolResult
from harness.tools.search import search_text
from harness.tools.filesystem import resolve_workspace

from .client import LSPError


def language_for(path: str) -> str:
    lowered = path.lower()
    return "python" if lowered.endswith(".py") else "typescript" if lowered.endswith((".ts", ".tsx")) else "javascript" if lowered.endswith((".js", ".jsx")) else "unknown"


def normalize_location(value: dict[str, Any]) -> dict[str, Any]:
    range_value = value.get("range") or value.get("targetSelectionRange") or {}
    return {"uri": str(value.get("uri") or value.get("targetUri") or ""), "range": {
        "start": _position(range_value.get("start")),
        "end": _position(range_value.get("end")),
    }}


def normalize_diagnostic(value: dict[str, Any]) -> dict[str, Any]:
    severity = {1: "error", 2: "warning", 3: "information", 4: "hint"}.get(value.get("severity"), "unknown")
    return {"range": normalize_location({"range": value.get("range") or {}})["range"],
            "severity": severity, "message": str(value.get("message", "")),
            "source": value.get("source")}


def query(manager: Any, context: ToolContext, arguments: dict[str, Any], method: str) -> ToolResult:
    path = str(arguments.get("path", ""))
    symbol = str(arguments.get("symbol", ""))
    language = language_for(path)
    try:
        target = resolve_workspace(context, path)
        text = context.executor.read_file(target).content.decode("utf-8")
    except (KeyError, OSError, PermissionError, UnicodeDecodeError) as exc:
        return ToolResult(False, str(exc), failure_kind="io")
    if language == "unknown":
        return ToolResult(False, "unsupported language", failure_kind="unsupported_language")
    if not manager or not manager.available(language):
        fallback = search_text(context, {"query": symbol, "path": context.relative(target.parent)}) if symbol else ToolResult(True, "no symbol supplied")
        return ToolResult(fallback.ok, "LSP server unavailable; used text-search fallback",
                          stdout=fallback.stdout, artifacts=fallback.artifacts,
                          failure_kind=None if fallback.ok else fallback.failure_kind,
                          structured={"fallback": "search", "fallback_used": True,
                                      "fallback_kind": "bounded_text_search",
                                      "fallback_reason": "language_server_unavailable",
                                      "search_details": fallback.structured or {},
                                      "language": language})
    try:
        manager.sync_document(language, target, text)
        uri = target.as_uri()
        position = None
        if method in {"textDocument/definition", "textDocument/references"}:
            position = _requested_position(arguments, text, symbol)
            if position is None:
                return ToolResult(
                    False,
                    f"symbol not found within bounded search: {symbol}",
                    failure_kind="symbol_not_found",
                    structured={"fallback": None, "fallback_used": False,
                                "language": language},
                )
            params = {"textDocument": {"uri": uri}, "position": position}
            if method == "textDocument/references":
                params["context"] = {"includeDeclaration": True}
            raw = manager.request(language, method, params)
        else:
            params = {"textDocument": {"uri": uri}}
            try:
                raw = manager.request(language, method, params)
            except LSPError as exc:
                if exc.kind != "method_not_supported":
                    raise
                raw = manager.diagnostics(language, uri)
        values = raw if isinstance(raw, list) else raw.get("items", []) if isinstance(raw, dict) else []
        limit = min(200, max(1, int(arguments.get("limit", 50))))
        normalized = [normalize_diagnostic(value) for value in values[:limit]] if method == "textDocument/diagnostic" else [normalize_location(value) for value in values[:limit]]
        key = "diagnostics" if method == "textDocument/diagnostic" else "locations"
        return ToolResult(
            True, f"LSP returned {len(normalized)} {key}", stdout="",
            structured={key: normalized, "language": language,
                        "position": position, "fallback": None,
                        "fallback_used": False},
        )
    except LSPError as exc:
        return ToolResult(False, str(exc), failure=ToolFailure(exc.kind, str(exc), exc.retryable))


def _position(value: Any) -> dict[str, int]:
    value = value or {}
    return {"line": int(value.get("line", 0)), "character": int(value.get("character", 0))}


def _requested_position(
    arguments: dict[str, Any], text: str, symbol: str
) -> dict[str, int] | None:
    if "line" in arguments and "character" in arguments:
        line = int(arguments["line"])
        character = int(arguments["character"])
        if line < 0 or character < 0:
            return None
        return {"line": line, "character": character}
    if not symbol or len(symbol) > 256:
        return None
    bounded = text[:500_000]
    escaped = re.escape(symbol)
    patterns = (
        rf"(?m)^[ \t]*(?:(?:async[ \t]+)?def|class|function)[ \t]+(?P<symbol>{escaped})(?![A-Za-z0-9_])",
        rf"(?m)^[ \t]*(?:const|let|var)?[ \t]*(?P<symbol>{escaped})(?![A-Za-z0-9_])[ \t]*(?::[^=]+)?=",
        rf"(?P<symbol>(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_]))",
    )
    match = next((found for pattern in patterns if (found := re.search(pattern, bounded))), None)
    if match is None:
        return None
    offset = match.start("symbol")
    line = bounded.count("\n", 0, offset)
    line_start = bounded.rfind("\n", 0, offset) + 1
    prefix = bounded[line_start:offset]
    # LSP character offsets use UTF-16 code units.
    character = len(prefix.encode("utf-16-le")) // 2
    return {"line": line, "character": character}
