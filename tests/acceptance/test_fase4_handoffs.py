"""Aceite da Fase 4 — handoffs.

Critério da especificação, ao pé da letra: "uma sessão do Codex encerra com
handoff; o claim inicia o Claude Code com o handoff no contexto".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture
def sem_credencial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Força o caminho determinístico, que é o que roda sem chave de API."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


async def test_codex_encerra_com_handoff_e_claude_code_assume(
    daemon: dict, repo_git: Path, harness_falso: None, sem_credencial: None
) -> None:
    client, headers = daemon["client"], daemon["headers"]
    hook_headers = daemon["hook_headers"]

    projeto = (
        await client.post(
            "/api/projects",
            headers=headers,
            json={"path": str(repo_git), "index_docs": True},
        )
    ).json()

    # 1. sessão do Codex
    codex = (
        await client.post(
            "/api/sessions",
            headers=headers,
            json={
                "harness": "codex",
                "project_id": projeto["id"],
                "task": "ajustar o webhook de pagamento para aceitar retry",
            },
        )
    ).json()
    codex_sid = codex["id"]
    await asyncio.sleep(0.4)

    for tool in ("Bash", "Edit", "Bash"):
        await client.post(
            "/hooks/PostToolUse",
            headers=hook_headers,
            json={"session_id": codex_sid, "tool_name": tool},
        )

    # 2. Stop gera o handoff
    corpo = (
        await client.post(
            "/hooks/Stop", headers=hook_headers, json={"session_id": codex_sid}
        )
    ).json()
    assert "handoff" in corpo, corpo
    handoff = corpo["handoff"]

    assert handoff["id"].startswith("hf-0x")
    assert handoff["deterministic"], "sem credencial, o caminho é o determinístico"
    assert "Bash (2x)" in handoff["stopped_at"], handoff["stopped_at"]
    assert handoff["open_items"]

    # 3. sessão marcada, handoff pendente
    sessao = (await client.get(f"/api/sessions/{codex_sid}", headers=headers)).json()
    assert sessao["status"] == "handoff"
    pendentes = (
        await client.get("/api/handoffs", headers=headers, params={"status": "pending"})
    ).json()["handoffs"]
    assert len(pendentes) == 1

    # 4. virou página de wiki citável
    achados = (
        await client.get(
            "/api/pages", headers=headers, params={"q": handoff["title"][:30]}
        )
    ).json()["results"]
    assert any("handoffs/" in a["path"] for a in achados), achados

    # 5. claim pelo Claude Code
    nova = await client.post(
        f"/api/handoffs/{handoff['id']}/claim",
        headers=headers,
        json={"harness": "claude-code"},
    )
    assert nova.status_code == 201, nova.text
    nova = nova.json()
    assert nova["mode"] == "handoff"

    # 6. o handoff é a PRIMEIRA página do contexto
    paginas = nova["memory"]["pages"]
    assert paginas, "contexto vazio"
    assert paginas[0]["reason"] == "handoff assumido", paginas
    assert handoff["id"] in paginas[0]["slug"]

    # 7. e ficou marcado como assumido por essa sessão
    estado = (
        await client.get(f"/api/handoffs/{handoff['id']}", headers=headers)
    ).json()
    assert estado["status"] == "claimed"
    assert estado["claimed_by_session_id"] == nova["id"]

    await client.post(f"/api/sessions/{nova['id']}/stop", headers=headers)
    await asyncio.sleep(0.5)


async def test_claim_duplo_e_recusado(
    daemon: dict, repo_git: Path, harness_falso: None, sem_credencial: None
) -> None:
    """Duas interfaces não podem dar o mesmo trabalho a dois agentes."""
    client, headers = daemon["client"], daemon["headers"]

    projeto = (
        await client.post("/api/projects", headers=headers, json={"path": str(repo_git)})
    ).json()
    sessao = (
        await client.post(
            "/api/sessions",
            headers=headers,
            json={"harness": "codex", "project_id": projeto["id"], "task": "x"},
        )
    ).json()
    await asyncio.sleep(0.3)

    handoff = (
        await client.post(
            "/hooks/Stop",
            headers=daemon["hook_headers"],
            json={"session_id": sessao["id"]},
        )
    ).json()["handoff"]

    primeira = await client.post(
        f"/api/handoffs/{handoff['id']}/claim",
        headers=headers,
        json={"harness": "claude-code"},
    )
    assert primeira.status_code == 201

    segunda = await client.post(
        f"/api/handoffs/{handoff['id']}/claim",
        headers=headers,
        json={"harness": "claude-code"},
    )
    assert segunda.status_code == 422
    assert "claimed" in segunda.json()["detail"]

    await client.post(f"/api/sessions/{primeira.json()['id']}/stop", headers=headers)
    await asyncio.sleep(0.4)


async def test_handoff_manual_pelo_console(
    daemon: dict, repo_git: Path, harness_falso: None, sem_credencial: None
) -> None:
    """O ``/handoff`` do console usa o mesmo caminho do hook Stop."""
    client, headers = daemon["client"], daemon["headers"]

    projeto = (
        await client.post("/api/projects", headers=headers, json={"path": str(repo_git)})
    ).json()
    sessao = (
        await client.post(
            "/api/sessions",
            headers=headers,
            json={"harness": "codex", "project_id": projeto["id"], "task": "x"},
        )
    ).json()
    await asyncio.sleep(0.3)

    r = await client.post(
        f"/api/sessions/{sessao['id']}/handoff",
        headers=headers,
        json={"reason": "pedido no console"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"].startswith("hf-0x")

    await client.post(f"/api/sessions/{sessao['id']}/stop", headers=headers)
    await asyncio.sleep(0.4)


async def test_handoff_de_sessao_inexistente(daemon: dict) -> None:
    r = await daemon["client"].post(
        "/api/sessions/s-nada/handoff", headers=daemon["headers"], json={}
    )
    assert r.status_code == 404
