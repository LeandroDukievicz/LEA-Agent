"""Adaptador do Antigravity CLI (``agy``).

Formatos verificados contra uma instalação real (agy 1.4.10) em 2026-11-03,
como a especificação manda -- esses arquivos mudam com frequência e não dá
para programar de memória:

* config em ``~/.gemini/antigravity-cli/settings.json``, com as chaves
  ``permissions``, ``trustedWorkspaces`` e ``allowNonWorkspaceAccess``;
* MCP registrado pelo próprio ``agy mcp add``. A assinatura é
  ``agy mcp add [flags] <nome> <comando> [args...]``: sem ``--scope`` e sem o
  ``--`` separador que o Claude Code exige.

**O agy não expõe hooks de ciclo de vida.** A versão 1.4.10 não tem nenhuma
menção a hook em ``agy --help``, e o ``settings.json`` não tem a chave. Isso
não é um defeito da instalação: é o que o CLI oferece hoje. Então a sessão de
agy aparece no LEA pelo que sai no PTY e pelo que ela grava na memória via
MCP, e não pelos eventos estruturados que Claude Code e Codex entregam. O
adaptador reporta isso como ausência explícita em vez de fingir que está lá,
porque supervisionar sem saber que o canal não existe é pior que não
supervisionar.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from lea.harnesses.base import (
    Harness,
    HarnessInfo,
    HarnessStatus,
    Health,
    InstallReport,
    find_binary,
)

logger = logging.getLogger(__name__)

#: Nome do servidor MCP como ele aparece na config do agy.
MCP_NAME = "lea-memory"

#: Motivo repetido em `install` e `status`, para os dois dizerem a mesma coisa.
NO_HOOKS = "agy 1.4.10 não expõe hooks de ciclo de vida; a sessão é observada pelo PTY"


class Agy(Harness):
    id = "agy"
    code = "AG"
    name = "Antigravity CLI"
    binary_name = "agy"

    def __init__(self, home: Path | None = None) -> None:
        #: Permite apontar para um diretório de teste em vez do
        #: ``~/.gemini/antigravity-cli`` real. Os testes **precisam** disso:
        #: escrever na config de verdade durante a suíte seria destrutivo.
        self.home = home or (Path.home() / ".gemini" / "antigravity-cli")

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    # ── detecção ─────────────────────────────────────────────────────────

    def detect(self) -> HarnessInfo | None:
        binary = find_binary(self.binary_name)
        if not binary:
            return None
        return HarnessInfo(
            id=self.id,
            code=self.code,
            name=self.name,
            binary=binary,
            version=_version(binary),
        )

    # ── instalação ───────────────────────────────────────────────────────

    async def install(self, hook_command: str, mcp_command: list[str]) -> InstallReport:
        report = InstallReport(harness=self.id)

        if not self.detect():
            report.problems.append("agy não encontrado no PATH")
            return report

        await self._install_mcp(mcp_command, report)
        # Não é problema: é limite do CLI. Vai como nota para o relatório não
        # dizer "tudo certo" escondendo que metade da supervisão não existe.
        report.unchanged.append(f"hooks indisponíveis ({NO_HOOKS})")
        return report

    async def _install_mcp(self, mcp_command: list[str], report: InstallReport) -> None:
        """Registra o servidor MCP pelo CLI do próprio agy."""
        if await self._mcp_registered():
            report.unchanged.append(f"mcp {MCP_NAME}")
            return

        if not mcp_command:
            report.problems.append("comando do servidor MCP vazio")
            return

        code, _, err = await _run(
            self.binary_name, "mcp", "add", "--type", "stdio", MCP_NAME, *mcp_command
        )
        if code == 0:
            report.changed.append(f"mcp {MCP_NAME}")
        else:
            report.problems.append(f"agy mcp add falhou: {err.strip()[:200]}")

    async def _mcp_registered(self) -> bool:
        code, out, _ = await _run(self.binary_name, "mcp", "list")
        return code == 0 and MCP_NAME in out

    # ── saúde ────────────────────────────────────────────────────────────

    async def status(self, hook_command: str | None = None) -> HarnessStatus:
        info = self.detect()
        state = HarnessStatus(
            id=self.id,
            code=self.code,
            name=self.name,
            installed=info is not None,
            version=info.version if info else "",
        )
        if info is None:
            state.notes.append("não encontrado no PATH")
            return state

        # MISSING, e não ERROR: não há nada quebrado para consertar. A matriz
        # mostra ○, que é a verdade -- este canal não existe neste CLI.
        state.hooks = Health.MISSING
        state.notes.append(NO_HOOKS)
        state.mcp = Health.OK if await self._mcp_registered() else Health.MISSING
        if state.mcp is Health.MISSING:
            state.notes.append(f"servidor MCP {MCP_NAME} não registrado")
        return state

    # ── execução ─────────────────────────────────────────────────────────

    def launch_cmd(self, *, task: str | None = None) -> list[str]:
        """Comando para abrir uma sessão interativa.

        ``--prompt-interactive`` roda a tarefa como primeiro turno e continua
        a sessão; sem ele, um prompt posicional cairia no modo de impressão e
        o terminal fecharia no fim do turno.
        """
        cmd = [self.binary_name]
        if task:
            cmd += ["--prompt-interactive", task]
        return cmd


def _version(binary: str) -> str:
    import subprocess

    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        return out.strip().split("\n")[0]
    except Exception:
        return ""


async def _run(*args: str) -> tuple[int, str, str]:
    """Roda um comando sem shell e devolve (código, stdout, stderr)."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except (FileNotFoundError, asyncio.TimeoutError) as exc:
        return 1, "", str(exc)
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
