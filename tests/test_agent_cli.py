"""Backend CLI do agente: políticas nativas e isolamento de credenciais."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from lea import events
from lea.agent import (
    AgentError,
    AgentService,
    _claude_disallowed_tools,
    _cli_environment,
    _write_codex_rules,
)
from lea.db import session as db
from lea.memory import Memory


@pytest_asyncio.fixture
async def agent_setup(lea_home: Path, tmp_path: Path):
    conn = await db.connect(lea_home / "lea.db")
    project = tmp_path / "project"
    project.mkdir()
    memory = Memory(conn, lea_home / "wiki")
    await memory.setup()
    await conn.execute(
        "INSERT INTO projects "
        "(slug, name, path, created_at, allow_autonomous, require_approval_for) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (
            "cli-test",
            "CLI test",
            str(project),
            "2026-10-28T00:00:00+00:00",
            '["git push", "rm -rf"]',
        ),
    )
    await conn.commit()

    from lea.config import Config

    service = AgentService(conn, memory, Config(), events.EventBus())
    try:
        yield service, project
    finally:
        await service.shutdown()
        await conn.close()


def _fake_cli(bin_dir: Path, name: str, capture: Path, result: dict[str, Any]) -> Path:
    executable = bin_dir / name
    source = (
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"capture = pathlib.Path({json.dumps(str(capture))})\n"
        "args = sys.argv[1:]\n"
        "codex_home = pathlib.Path(os.environ['CODEX_HOME'])\n"
        "rules_dir = codex_home / 'rules'\n"
        "rules = []\n"
        "if rules_dir.exists():\n"
        "    rules = [\n"
        "        path.read_text(encoding='utf-8')\n"
        "        for path in rules_dir.glob('lea-agent-*.rules')\n"
        "    ]\n"
        "secret_names = ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', "
        "'CLAUDE_CODE_OAUTH_TOKEN')\n"
        "api_env = {key: bool(os.environ.get(key)) for key in secret_names}\n"
        "payload = {'args': args, 'rules': rules, 'api_env': api_env}\n"
        "capture.write_text(json.dumps(payload), encoding='utf-8')\n"
        f"print({json.dumps(json.dumps(result, ensure_ascii=False))})\n"
    )
    executable.write_text(source, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return executable


@pytest.mark.parametrize("pattern", ["git push", "rm -rf"])
def test_codex_rules_sao_forbidden_e_tem_prefixo(pattern: str, tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    rules_file = _write_codex_rules([pattern])

    assert rules_file is not None
    assert rules_file.parent == codex_home / "rules"
    assert rules_file.stat().st_mode & 0o777 == 0o600
    rule = rules_file.read_text(encoding="utf-8")
    tokens = shlex.split(pattern)
    assert f"pattern = {json.dumps(tokens)}" in rule
    assert 'decision = "forbidden"' in rule
    assert rules_file.name.startswith("lea-agent-")


def test_codex_rejeita_padrao_malformado(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    with pytest.raises(AgentError, match="padrão de aprovação inválido"):
        _write_codex_rules(['git "push'])


def test_claude_converte_aprovacao_em_negacao_nativa() -> None:
    assert _claude_disallowed_tools(["git push", "rm -rf"]) == [
        "Bash(*git push*)",
        "Bash(*rm -rf*)",
    ]
    # Parênteses são delimitadores da gramática do Claude; caso ambíguo nega shell.
    assert _claude_disallowed_tools(["Bash(git push)"]) == ["Bash"]


def test_cli_environment_nao_herda_chaves_de_api(monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(key, "never-forward-this")

    environment = _cli_environment()

    assert all(
        key not in environment
        for key in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
    )


async def test_codex_recebe_regras_temporarias_e_nao_recebe_chaves(
    agent_setup, tmp_path: Path, monkeypatch
) -> None:
    service, _project = agent_setup
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    capture = tmp_path / "codex-capture.json"
    _fake_cli(
        bin_dir,
        "codex",
        capture,
        {"type": "item.completed", "item": {"type": "agent_message", "text": "concluído"}},
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "not-forwarded")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-forwarded")

    session, runner = await service.start(project_id=1, task="somente leitura", backend="codex")
    result = await runner

    captured = json.loads(capture.read_text(encoding="utf-8"))
    argv = captured["args"]
    assert result["status"] == "ended"
    assert result["summary"] == "concluído"
    assert "--approve-for-me" in argv
    assert "--sandbox" not in argv
    assert len(captured["rules"]) == 1
    assert 'decision = "forbidden"' in captured["rules"][0]
    assert all(value is False for value in captured["api_env"].values())
    assert list((codex_home / "rules").glob("lea-agent-*.rules")) == []
    assert session["id"] == result["session_id"]


async def test_claude_recebe_negacoes_de_comando(
    agent_setup, tmp_path: Path, monkeypatch
) -> None:
    service, _project = agent_setup
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    capture = tmp_path / "claude-capture.json"
    _fake_cli(
        bin_dir,
        "claude",
        capture,
        {"result": "concluído", "usage": {"input_tokens": 3, "output_tokens": 2}},
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "not-forwarded")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-forwarded")

    session, runner = await service.start(
        project_id=1,
        task="somente leitura",
        backend="claude-code",
    )
    result = await runner

    captured = json.loads(capture.read_text(encoding="utf-8"))
    argv = captured["args"]
    disallowed_index = argv.index("--disallowedTools")
    assert argv[disallowed_index + 1 : disallowed_index + 3] == [
        "Bash(*git push*)",
        "Bash(*rm -rf*)",
    ]
    assert result["status"] == "ended"
    assert result["summary"] == "concluído"
    assert result["tokens_in"] == 3
    assert all(value is False for value in captured["api_env"].values())
    assert captured["rules"] == []
    assert session["id"] == result["session_id"]
