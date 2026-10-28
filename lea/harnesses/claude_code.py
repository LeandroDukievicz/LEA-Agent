"""Adaptador do Claude Code.

Formatos verificados contra uma instalação real (Claude Code 2.1.278) em
2026-10-28, como a especificação manda -- esses arquivos mudam com
frequência e não dá para programar de memória:

* hooks em ``~/.claude/settings.json``, sob a chave ``hooks``, com os nomes
  de evento em PascalCase;
* MCP registrado pelo próprio ``claude mcp add``, em vez de escrevermos o
  JSON na mão. O CLI é dono do formato dele; se amanhã o arquivo mudar de
  lugar ou de forma, o comando continua certo e a gente não quebra nada.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from lea.harnesses import hooks_file
from lea.harnesses.base import (
    LEA_HOOK_EVENTS,
    Harness,
    HarnessInfo,
    HarnessStatus,
    Health,
    InstallReport,
    find_binary,
)

logger = logging.getLogger(__name__)

#: Nome do servidor MCP como ele aparece na config do Claude Code.
MCP_NAME = "lea-memory"


class ClaudeCode(Harness):
    id = "claude-code"
    code = "CC"
    name = "Claude Code"
    binary_name = "claude"

    def __init__(self, home: Path | None = None) -> None:
        #: Permite apontar para um diretório de teste em vez do ``~/.claude``
        #: real. Os testes **precisam** disso: escrever na config de verdade
        #: do usuário durante a suíte seria destrutivo.
        self.home = home or (Path.home() / ".claude")

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
            report.problems.append("claude não encontrado no PATH")
            return report

        self._install_hooks(hook_command, report)
        await self._install_mcp(mcp_command, report)
        return report

    def _install_hooks(self, hook_command: str, report: InstallReport) -> None:
        path = self.settings_file
        try:
            settings = hooks_file.read_json(path)
        except (ValueError, OSError) as exc:
            # JSON quebrado: paramos. Escrever por cima apagaria a config do
            # usuário por causa de um erro de digitação dele.
            report.problems.append(f"não consegui ler {path}: {exc}")
            return

        hooks_map = settings.setdefault("hooks", {})
        if not isinstance(hooks_map, dict):
            report.problems.append(f"{path}: 'hooks' não é um objeto")
            return

        marker = _marker(hook_command)
        changed = False
        for event in LEA_HOOK_EVENTS:
            command = f"{hook_command} {event}"
            try:
                if hooks_file.upsert(hooks_map, event, command, marker):
                    changed = True
                    report.changed.append(f"hook {event}")
                else:
                    report.unchanged.append(f"hook {event}")
            except ValueError as exc:
                report.problems.append(str(exc))
                return

        if not changed:
            return

        saved = hooks_file.backup(path)
        if saved:
            report.backups.append(str(saved))
        try:
            hooks_file.write_json(path, settings)
        except OSError as exc:
            report.problems.append(f"não consegui gravar {path}: {exc}")

    async def _install_mcp(self, mcp_command: list[str], report: InstallReport) -> None:
        """Registra o servidor MCP pelo CLI do próprio Claude Code."""
        if await self._mcp_registered():
            report.unchanged.append(f"mcp {MCP_NAME}")
            return

        # `--scope user` deixa a memória disponível em todo projeto, que é o
        # ponto do LEA: a wiki é compartilhada, não por repositório.
        code, _, err = await _run(
            self.binary_name, "mcp", "add", "--scope", "user", MCP_NAME, "--", *mcp_command
        )
        if code == 0:
            report.changed.append(f"mcp {MCP_NAME}")
        else:
            report.problems.append(f"claude mcp add falhou: {err.strip()[:200]}")

    async def _mcp_registered(self) -> bool:
        code, out, _ = await _run(self.binary_name, "mcp", "get", MCP_NAME)
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

        state.hooks = self._hooks_health(hook_command, state)
        state.mcp = Health.OK if await self._mcp_registered() else Health.MISSING
        if state.mcp is Health.MISSING:
            state.notes.append(f"servidor MCP {MCP_NAME} não registrado")
        return state

    def _hooks_health(self, hook_command: str | None, state: HarnessStatus) -> Health:
        try:
            settings = hooks_file.read_json(self.settings_file)
        except (ValueError, OSError) as exc:
            state.notes.append(f"settings.json ilegível: {exc}")
            return Health.ERROR

        hooks_map = settings.get("hooks")
        if not isinstance(hooks_map, dict):
            return Health.MISSING

        marker = _marker(hook_command) if hook_command else "lea"
        present = 0
        stale = 0
        for event in LEA_HOOK_EVENTS:
            expected = f"{hook_command} {event}" if hook_command else None
            found = hooks_file.find_ours(hooks_map, event, marker)
            if found is None:
                continue
            present += 1
            if expected and found != expected:
                stale += 1

        if present == 0:
            return Health.MISSING
        if stale or present < len(LEA_HOOK_EVENTS):
            state.notes.append(
                f"{present} de {len(LEA_HOOK_EVENTS)} hooks instalados"
                + (f", {stale} apontando para outro caminho" if stale else "")
            )
            return Health.ERROR
        return Health.OK

    # ── execução ─────────────────────────────────────────────────────────

    def launch_cmd(self, *, task: str | None = None) -> list[str]:
        """Comando para abrir uma sessão interativa.

        A tarefa vai como prompt inicial. O Claude Code aceita um prompt
        posicional e segue interativo a partir dele.
        """
        cmd = [self.binary_name]
        if task:
            cmd.append(task)
        return cmd


def _marker(hook_command: str) -> str:
    """Trecho que identifica uma entrada como nossa.

    É o caminho do executável do LEA, sem o evento no fim -- assim uma
    entrada apontando para uma instalação antiga ainda é reconhecida como
    nossa e atualizada, em vez de duplicada.
    """
    return hook_command.split()[0] if hook_command else "lea"


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
