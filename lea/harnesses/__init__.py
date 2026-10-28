"""Adaptadores dos agentes de código que o LEA supervisiona."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from lea.harnesses.base import (
    LEA_HOOK_EVENTS,
    Harness,
    HarnessInfo,
    HarnessStatus,
    Health,
    InstallReport,
    find_binary,
)
from lea.harnesses.claude_code import ClaudeCode
from lea.harnesses.codex import Codex

logger = logging.getLogger(__name__)

__all__ = [
    "LEA_HOOK_EVENTS",
    "ClaudeCode",
    "Codex",
    "Harness",
    "HarnessInfo",
    "HarnessStatus",
    "Health",
    "InstallReport",
    "all_harnesses",
    "find_binary",
    "get",
    "hook_command",
    "mcp_command",
]


def all_harnesses() -> list[Harness]:
    """Adaptadores conhecidos, na ordem em que a UI mostra.

    Claude Code e Codex primeiro porque são os dois que a Fase 3 entrega;
    os demais chegam depois, e a matriz os mostra como ausentes até lá.
    """
    return [ClaudeCode(), Codex()]


def get(harness_id: str) -> Harness | None:
    return next((h for h in all_harnesses() if h.id == harness_id), None)


def lea_binary() -> str:
    """Caminho absoluto do executável ``lea``.

    Vai para dentro da config de outra ferramenta, que roda com outro
    ``PATH`` -- por isso nunca o nome solto. Se o console script não estiver
    instalado (checkout de desenvolvimento), caímos para
    ``<python> -m lea``, que funciona igual.
    """
    found = shutil.which("lea")
    if found:
        return str(Path(found).resolve())

    # Fallback: o mesmo interpretador que está rodando agora.
    return f"{Path(sys.executable).resolve()} -m lea"


def hook_command() -> str:
    """Prefixo do comando de hook. O evento é acrescentado pelo adaptador."""
    return f"{lea_binary()} hook"


def mcp_command() -> list[str]:
    """Comando que sobe o servidor MCP de memória, como lista de argumentos."""
    return [*lea_binary().split(), "mcp"]
