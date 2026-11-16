"""Minimal stdio MCP provider boundary."""

from .client import MCPClient, MCPError, MCPTransport, StdioTransport
from .manager import MCPManager, MCPServerConfig
from .adapter import MCPToolAdapter

__all__ = ["MCPClient", "MCPError", "MCPTransport", "StdioTransport",
           "MCPManager", "MCPServerConfig", "MCPToolAdapter"]
