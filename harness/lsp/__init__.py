"""Small, read-only LSP code-intelligence boundary."""

from .client import LSPClient, LSPError, LSPTransport
from .manager import LSPManager, LSPServerConfig
from .transport import StdioLSPTransport
from .tools import lsp_tool_specs

__all__ = ["LSPClient", "LSPError", "LSPTransport", "LSPManager", "LSPServerConfig", "StdioLSPTransport", "lsp_tool_specs"]
