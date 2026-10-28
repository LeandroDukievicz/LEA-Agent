"""Adaptador do Codex CLI.

Formatos verificados contra uma instalação real (codex-cli 0.164.0) em
2026-10-28:

* hooks em ``~/.codex/hooks.json`` -- **mesma estrutura** do Claude Code
  (``{"hooks": {"SessionStart": [{"matcher": "", "hooks": [...]}]}}``), só
  que ocupando o arquivo inteiro em vez de uma chave do settings;
* MCP registrado por ``codex mcp add <nome> -- <comando>``;
* e uma particularidade que não existe no Claude Code: **confiança de hook**.

Sobre a confiança
-----------------
O Codex guarda, em ``~/.codex/config.toml``, uma entrada por hook::

    [hooks.state."/home/user/.codex/hooks.json:post_tool_use:0:0"]
    trusted_hash = "sha256:849952..."

Um hook recém-instalado **não roda** até ser confiado. Nós não forjamos esse
hash: a canonicalização é interna ao Codex, e chutar produziria uma entrada
inválida ou, pior, uma válida que burlasse uma trava de segurança feita de
propósito. Existe a flag ``--dangerously-bypass-hook-trust``, e o nome dela
diz tudo -- não a usamos nem sugerimos.

O que fazemos é instalar o hook e **avisar** que o Codex vai pedir confiança
na próxima execução. Isso vira ``manual_steps`` no relatório e uma nota na
matriz da tela CANAIS.
"""

from __future__ import annotations

import logging
import tomllib
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
from lea.harnesses.claude_code import _marker, _run, _version

logger = logging.getLogger(__name__)

MCP_NAME = "lea-memory"

#: PascalCase (hooks.json) -> snake_case (chave de confiança no config.toml).
TRUST_EVENT_NAMES = {
    "SessionStart": "session_start",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "Stop": "stop",
}


class Codex(Harness):
    id = "codex"
    code = "CX"
    name = "Codex CLI"
    binary_name = "codex"

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or (Path.home() / ".codex")

    @property
    def hooks_file_path(self) -> Path:
        return self.home / "hooks.json"

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

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
            report.problems.append("codex não encontrado no PATH")
            return report

        self._install_hooks(hook_command, report)
        await self._install_mcp(mcp_command, report)
        return report

    def _install_hooks(self, hook_command: str, report: InstallReport) -> None:
        path = self.hooks_file_path
        try:
            document = hooks_file.read_json(path)
        except (ValueError, OSError) as exc:
            report.problems.append(f"não consegui ler {path}: {exc}")
            return

        hooks_map = document.setdefault("hooks", {})
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
            hooks_file.write_json(path, document)
        except OSError as exc:
            report.problems.append(f"não consegui gravar {path}: {exc}")
            return

        report.manual_steps.append(
            "O Codex exige confiar em cada hook novo antes de executá-lo. "
            "Na próxima vez que você abrir o Codex ele vai perguntar; aceite para "
            "o LEA passar a receber os eventos. Não use "
            "--dangerously-bypass-hook-trust."
        )

    async def _install_mcp(self, mcp_command: list[str], report: InstallReport) -> None:
        if self._mcp_in_config():
            report.unchanged.append(f"mcp {MCP_NAME}")
            return

        code, _, err = await _run(
            self.binary_name, "mcp", "add", MCP_NAME, "--", *mcp_command
        )
        if code == 0:
            report.changed.append(f"mcp {MCP_NAME}")
        else:
            report.problems.append(f"codex mcp add falhou: {err.strip()[:200]}")

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
        state.mcp = Health.OK if self._mcp_in_config() else Health.MISSING
        if state.mcp is Health.MISSING:
            state.notes.append(f"servidor MCP {MCP_NAME} não registrado")
        return state

    def _hooks_health(self, hook_command: str | None, state: HarnessStatus) -> Health:
        try:
            document = hooks_file.read_json(self.hooks_file_path)
        except (ValueError, OSError) as exc:
            state.notes.append(f"hooks.json ilegível: {exc}")
            return Health.ERROR

        hooks_map = document.get("hooks")
        if not isinstance(hooks_map, dict):
            return Health.MISSING

        marker = _marker(hook_command) if hook_command else "lea"
        present = [e for e in LEA_HOOK_EVENTS if hooks_file.find_ours(hooks_map, e, marker)]
        if not present:
            return Health.MISSING

        untrusted = [e for e in present if not self._is_trusted(e)]
        if untrusted:
            state.notes.append(
                f"{len(untrusted)} hook(s) aguardando sua confiança no Codex: "
                + ", ".join(untrusted)
            )
            return Health.ERROR

        if len(present) < len(LEA_HOOK_EVENTS):
            state.notes.append(f"{len(present)} de {len(LEA_HOOK_EVENTS)} hooks instalados")
            return Health.ERROR
        return Health.OK

    def _is_trusted(self, event: str) -> bool:
        """True se o Codex já registrou confiança neste hook.

        A chave é ``<caminho do hooks.json>:<evento_snake>:<i>:<j>``. Os
        índices variam com a posição na lista, então casamos pelo prefixo em
        vez de montar a chave exata.
        """
        config = self._read_config()
        state = config.get("hooks", {}).get("state")
        if not isinstance(state, dict):
            return False

        snake = TRUST_EVENT_NAMES.get(event, event.lower())
        prefix = f"{self.hooks_file_path}:{snake}:"
        return any(
            key.startswith(prefix) and isinstance(value, dict) and value.get("trusted_hash")
            for key, value in state.items()
        )

    def _mcp_in_config(self) -> bool:
        servers = self._read_config().get("mcp_servers")
        return isinstance(servers, dict) and MCP_NAME in servers

    def _read_config(self) -> dict:
        """Lê o ``config.toml``. Erro vira dicionário vazio.

        Aqui a leitura é só diagnóstico: se o arquivo estiver quebrado, a
        saúde sai como "não instalado", o que leva o usuário a reinstalar --
        e a instalação, essa sim, para e avisa em vez de sobrescrever.
        """
        try:
            with self.config_file.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    # ── execução ─────────────────────────────────────────────────────────

    def launch_cmd(self, *, task: str | None = None) -> list[str]:
        cmd = [self.binary_name]
        if task:
            cmd.append(task)
        return cmd
