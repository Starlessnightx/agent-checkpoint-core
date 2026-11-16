from __future__ import annotations

import re
from typing import Any

from harness.tools.artifacts import bounded_output
from harness.tools.base import (
    SideEffect, ToolContext, ToolFailure, ToolPermission, ToolResult, ToolSpec,
    object_schema,
)

from .client import MCPError, MCPClient


class MCPToolAdapter:
    def __init__(self, client: MCPClient):
        self.client = client
        self.raw_to_visible: dict[str, str] = {}
        self.visible_to_raw: dict[str, str] = {}

    def visible_name(self, raw_name: str) -> str:
        base = "mcp__" + _safe(self.client.server_name) + "__" + _safe(raw_name)
        candidate = base
        suffix = 2
        while candidate in self.visible_to_raw and self.visible_to_raw[candidate] != raw_name:
            candidate = f"{base}__{suffix}"
            suffix += 1
        self.raw_to_visible[raw_name] = candidate
        self.visible_to_raw[candidate] = raw_name
        return candidate

    def spec(self, raw: dict[str, Any]) -> ToolSpec:
        raw_name = str(raw.get("name", ""))
        visible = self.visible_name(raw_name)
        schema = raw.get("inputSchema") or raw.get("input_schema") or object_schema({})

        def handler(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
            try:
                result = self.client.call_tool(raw_name, arguments)
            except MCPError as exc:
                return ToolResult(False, str(exc), failure=ToolFailure(exc.kind, str(exc), exc.retryable))
            text = "\n".join(str(item.get("text", "")) for item in result.get("content", [])
                               if isinstance(item, dict) and item.get("type") == "text")
            output, artifacts = bounded_output(context, "mcp", text)
            if result.get("isError"):
                return ToolResult(
                    False, output or "MCP tool returned an error", stdout=output,
                    artifacts=artifacts, failure_kind="mcp_tool_error",
                    structured={"mcp": result},
                )
            return ToolResult(
                True, output or f"MCP tool {raw_name} completed", stdout=output,
                artifacts=artifacts,
                structured={"mcp": result, "raw_tool_name": raw_name},
            )

        return ToolSpec(
            visible,
            str(raw.get("description", f"MCP tool {raw_name}")),
            schema,
            handler,
            f"mcp:{self.client.server_name}",
            required_permission=ToolPermission.WORKSPACE_WRITE,
            side_effects=frozenset({SideEffect.EXTERNAL}),
            metadata={
                "mcp": {
                    "raw_name": raw_name,
                    "visible_name": visible,
                    "server_name": self.client.server_name,
                    "side_effects": [SideEffect.EXTERNAL.value],
                    "annotations": dict(raw.get("annotations") or {}),
                    "permission_default": "ask",
                }
            },
        )


def _safe(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return cleaned or "tool"
