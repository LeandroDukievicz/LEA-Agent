"""Servidor MCP que expõe a memória do LEA aos agentes de código."""

from lea.mcp_server.server import SERVER_NAME, build_server, run

__all__ = ["SERVER_NAME", "build_server", "run"]
