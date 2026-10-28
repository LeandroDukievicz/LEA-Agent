"""Infraestrutura dos testes de aceite.

Estes testes exercitam o LEA pela borda -- HTTP, WebSocket, MCP por stdio,
processos de verdade sob PTY -- em vez de chamar funções internas. São eles
que verificam o critério de aceite de cada fase da especificação.

Por que existem, além da suíte de unidade: um critério como "o claim inicia o
Claude Code com o handoff no contexto" atravessa banco, git, PTY, injeção de
memória e três rotas. Nenhum teste de unidade prova isso, e foi aqui que
apareceram os defeitos que mais custariam em produção -- a task de PTY
gravando num banco já fechado, e a rota de handoff sendo capturada pela rota
genérica de ação.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

#: Processo que faz as vezes de agente de código: anuncia que subiu e ecoa a
#: entrada. Usar o `claude` ou o `codex` de verdade amarraria o teste a uma
#: versão de outra ferramenta; o que se prova aqui é o encanamento do LEA.
AGENTE_FALSO = [
    "python3",
    "-c",
    "import sys;print('AGENTE PRONTO', flush=True);"
    "[print('eco:', l.strip(), flush=True) for l in sys.stdin]",
]


@pytest.fixture
def harness_falso(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faz Claude Code e Codex subirem o processo de eco."""
    from lea.harnesses.claude_code import ClaudeCode
    from lea.harnesses.codex import Codex

    for classe in (ClaudeCode, Codex):
        monkeypatch.setattr(
            classe, "launch_cmd", lambda self, *, task=None: list(AGENTE_FALSO)
        )
        # `detect` consulta o PATH; numa máquina sem os binários instalados o
        # launcher recusaria antes de chegar ao que queremos testar.
        monkeypatch.setattr(
            classe,
            "detect",
            lambda self: _info_falsa(self),
        )


def _info_falsa(harness):  # type: ignore[no-untyped-def]
    from lea.harnesses.base import HarnessInfo

    return HarnessInfo(
        id=harness.id,
        code=harness.code,
        name=harness.name,
        binary="/usr/bin/true",
        version="teste",
    )


@pytest_asyncio.fixture
async def daemon(lea_home: Path) -> AsyncIterator[dict]:
    """Sobe o app com o ciclo de vida real e devolve cliente e tokens."""
    from lea.server.app import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://lea") as client:
            yield {
                "app": app,
                "client": client,
                "headers": {"X-Lea-Token": app.state.ui_token},
                "hook_headers": {"X-Lea-Hook-Token": app.state.hook_token},
            }


@pytest.fixture
def repo_git(lea_home: Path) -> Path:
    """Um projeto git de verdade, com README indexável."""
    root = lea_home / "cliente-acme"
    root.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    run("init", "--initial-branch=main")
    run("config", "user.email", "teste@lea.local")
    run("config", "user.name", "teste")
    (root / "README.md").write_text(
        "# cliente-acme\n\nWebhook de pagamento com retry. Usa pytest.\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nname='acme'\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "inicial")
    return root
