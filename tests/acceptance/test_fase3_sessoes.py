"""Aceite da Fase 3 — projetos, launcher e sessões.

Critério: iniciar um agente num projeto pela API, ver o boot ✓, os eventos de
tool use no feed e a sessão em "Rodando"; pausar, retomar e parar.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


async def test_inspect_detecta_linguagem_branch_e_git(daemon: dict, repo_git: Path) -> None:
    r = await daemon["client"].post(
        "/api/projects/inspect", headers=daemon["headers"], json={"path": str(repo_git)}
    )
    info = r.json()
    assert info["language"] == "Python"
    assert info["is_git"] and info["default_branch"] == "main"


async def test_abrir_projeto_indexa_a_documentacao(daemon: dict, repo_git: Path) -> None:
    r = await daemon["client"].post(
        "/api/projects",
        headers=daemon["headers"],
        json={"path": str(repo_git), "index_docs": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["indexed_pages"], "o README deveria virar página"


async def test_caminho_invalido_e_recusado(daemon: dict) -> None:
    r = await daemon["client"].post(
        "/api/projects", headers=daemon["headers"], json={"path": "/nao/existe"}
    )
    assert r.status_code == 422


async def test_matriz_de_harnesses(daemon: dict) -> None:
    r = await daemon["client"].get("/api/harnesses", headers=daemon["headers"])
    matriz = {h["id"]: h for h in r.json()["harnesses"]}
    assert {"claude-code", "codex"} <= set(matriz)
    for estado in matriz.values():
        assert estado["hooks"] in {"ok", "error", "missing"}


async def test_ciclo_de_vida_completo_de_uma_sessao(
    daemon: dict, repo_git: Path, harness_falso: None
) -> None:
    """Do boot ao fim, passando por terminal, eventos e controles."""
    client, headers = daemon["client"], daemon["headers"]

    projeto = (
        await client.post(
            "/api/projects",
            headers=headers,
            json={"path": str(repo_git), "index_docs": True},
        )
    ).json()

    r = await client.post(
        "/api/sessions",
        headers=headers,
        json={
            "harness": "claude-code",
            "project_id": projeto["id"],
            "task": "rodar pytest no webhook de pagamento",
            "use_worktree": True,
        },
    )
    assert r.status_code == 201, r.text
    sessao = r.json()
    sid = sessao["id"]

    # boot: os passos que o console desenha linha a linha
    rotulos = [p["label"] for p in sessao["boot"]]
    assert "comando" in rotulos
    assert "worktree" in rotulos
    assert "memória" in rotulos
    assert "sessão" in rotulos
    assert Path(sessao["worktree_path"]).exists()
    assert sessao["memory"]["pages"], "a memória do projeto deveria entrar"

    # terminal recebe a saída do agente
    await asyncio.sleep(0.6)
    running = daemon["app"].state.launcher.running[sid]
    assert b"AGENTE PRONTO" in bytes(running.scrollback)

    # e a entrada chega até ele
    await client.post(f"/api/sessions/{sid}/input", headers=headers, json={"text": "ola\n"})
    await asyncio.sleep(0.6)
    assert b"eco: ola" in bytes(running.scrollback)

    # aparece em "Rodando"
    ativas = (
        await client.get("/api/sessions", headers=headers, params={"status": "active"})
    ).json()["sessions"]
    assert sid in [s["id"] for s in ativas]

    # hook de tool use entra no feed da sessão
    await client.post(
        "/hooks/PostToolUse",
        headers=daemon["hook_headers"],
        json={"session_id": sid, "tool_name": "Bash"},
    )
    eventos = (
        await client.get(f"/api/sessions/{sid}/events", headers=headers)
    ).json()["events"]
    assert "tool_call" in [e["type"] for e in eventos]

    # pausar e retomar
    assert (
        await client.post(f"/api/sessions/{sid}/pause", headers=headers)
    ).json()["status"] == "paused"
    assert (
        await client.post(f"/api/sessions/{sid}/resume", headers=headers)
    ).json()["status"] == "active"

    # parar
    await client.post(f"/api/sessions/{sid}/stop", headers=headers)
    await asyncio.sleep(0.8)

    final = (await client.get(f"/api/sessions/{sid}", headers=headers)).json()
    assert final["status"] in {"ended", "handoff"}
    assert sid not in daemon["app"].state.launcher.running

    # worktree preservada: pode haver trabalho não commitado lá dentro
    assert Path(sessao["worktree_path"]).exists()


async def test_harness_desconhecido_e_recusado(daemon: dict) -> None:
    r = await daemon["client"].post(
        "/api/sessions", headers=daemon["headers"], json={"harness": "inventado"}
    )
    assert r.status_code == 422
    assert "desconhecido" in r.json()["detail"]


async def test_acao_desconhecida_e_recusada(daemon: dict) -> None:
    r = await daemon["client"].post(
        "/api/sessions/s-nada/explodir", headers=daemon["headers"], json={}
    )
    assert r.status_code == 404


async def test_controlar_sessao_que_nao_roda(daemon: dict) -> None:
    """A interface pode ter o botão de uma sessão que acabou de terminar."""
    r = await daemon["client"].post(
        "/api/sessions/s-nada/pause", headers=daemon["headers"], json={}
    )
    assert r.status_code == 422
