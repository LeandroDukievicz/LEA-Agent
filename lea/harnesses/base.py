"""Contrato comum dos adaptadores de harness.

Um *harness* é um agente de código de terceiro que o LEA supervisiona --
Claude Code, Codex, Cursor, Aider, Gemini CLI. O LEA não substitui nenhum
deles: instala hooks e o servidor MCP de memória, e os deixa continuar sendo
eles mesmos.

Regra que vale para todo adaptador, e que vem de uma observação do mundo real:
a máquina onde isto roda **já tinha** hooks de outra ferramenta configurados
em ``~/.claude/settings.json`` e ``~/.codex/hooks.json``. Sobrescrever teria
quebrado a instalação do usuário em silêncio. Por isso toda escrita de config
é merge, com backup antes, e `install` é idempotente.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: Eventos de ciclo de vida que o LEA entende. Claude Code e Codex usam
#: exatamente estes nomes, em PascalCase, nos arquivos de hook.
LEA_HOOK_EVENTS: tuple[str, ...] = ("SessionStart", "PostToolUse", "PreCompact", "Stop")


class Health(str, Enum):
    """Estado de uma peça da integração, como a matriz da tela CANAIS mostra."""

    OK = "ok"          # ●
    ERROR = "error"    # ◐  instalado, mas apontando para outro lugar
    MISSING = "missing"  # ○  não instalado

    @property
    def glyph(self) -> str:
        return {"ok": "●", "error": "◐", "missing": "○"}[self.value]


@dataclass(frozen=True)
class HarnessInfo:
    """O que foi encontrado na máquina."""

    id: str
    code: str
    name: str
    binary: str
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "binary": self.binary,
            "version": self.version,
        }


@dataclass
class HarnessStatus:
    """Saúde da integração, peça por peça."""

    id: str
    code: str
    name: str
    installed: bool
    version: str = ""
    hooks: Health = Health.MISSING
    mcp: Health = Health.MISSING
    #: Mensagens para a UI: o que falta, o que precisa de ação do usuário.
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "installed": self.installed,
            "version": self.version,
            "hooks": self.hooks.value,
            "mcp": self.mcp.value,
            "notes": self.notes,
        }


@dataclass
class InstallReport:
    """O que a instalação mudou -- e o que ela conscientemente não mudou."""

    harness: str
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: Ação que só o usuário pode concluir (ex.: confiar no hook no Codex).
    manual_steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "ok": self.ok,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "backups": self.backups,
            "problems": self.problems,
            "manual_steps": self.manual_steps,
        }


@runtime_checkable
class Harness(Protocol):
    """O que todo adaptador precisa saber fazer."""

    id: str
    code: str
    name: str

    def detect(self) -> HarnessInfo | None:
        """Informação da instalação local, ou ``None`` se não estiver aqui."""
        ...

    async def install(self, hook_command: str, mcp_command: list[str]) -> InstallReport:
        """Registra hooks e o servidor MCP. Idempotente, com backup e merge."""
        ...

    async def status(self, hook_command: str | None = None) -> HarnessStatus:
        """Saúde atual da integração."""
        ...

    def launch_cmd(self, *, task: str | None = None) -> list[str]:
        """Comando para o launcher iniciar uma sessão deste harness."""
        ...


def find_binary(name: str) -> str | None:
    """Caminho absoluto do executável, ou ``None``.

    Absoluto de propósito: o comando vai para dentro da config de outra
    ferramenta, que pode rodar com um ``PATH`` diferente do nosso.
    """
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None
