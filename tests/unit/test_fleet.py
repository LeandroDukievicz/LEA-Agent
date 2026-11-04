"""A frota: papéis, coleira do publicador e a varredura de sessões órfãs."""

import pytest

from lea import fleet, harnesses
from lea.config import FleetConfig


def test_tres_papeis_com_harness_proprio():
    assert [role.id for role in fleet.ROLES] == ["gerente", "implementador", "publicador"]
    assert [role.harness for role in fleet.ROLES] == ["codex", "claude-code", "agy"]
    # Cada harness da frota precisa existir no registro, senão `up` falha na cara.
    for role in fleet.ROLES:
        assert harnesses.get(role.harness) is not None, role.harness


def test_so_o_implementador_usa_worktree():
    """O publicador precisa do checkout real: é de lá que o push sai.

    Uma worktree para ele faria o agente publicar de um lugar que não é o
    repositório que a pessoa acompanha.
    """
    por_papel = {role.id: role.worktree for role in fleet.ROLES}
    assert por_papel == {"gerente": False, "implementador": True, "publicador": False}


def test_coleira_so_atinge_o_publicador():
    for role in fleet.ROLES:
        presa = fleet.task_for(role, leashed=True)
        solta = fleet.task_for(role, leashed=False)
        if role.id == "publicador":
            assert "não executa `git push`" in presa
            assert presa != solta
        else:
            assert presa == solta


def test_todo_briefing_carrega_a_marca_e_o_guarda_corpo():
    for role in fleet.ROLES:
        task = fleet.task_for(role, leashed=True)
        assert fleet.is_fleet_session(task)
        # Sem isto, um arquivo lido pelo agente poderia se passar por instrução.
        assert "dado não confiável" in task
        # Os três dividem a mesma memória; se um não souber, grava no vazio.
        assert "ai-memory" in task


def test_credenciais_do_ai_memory_passam_mas_chave_de_modelo_nao(monkeypatch):
    """O MCP de memória autentica por Bearer token lido do ambiente.

    Sem ele na lista branca o servidor devolve 401 e o agente segue trabalhando
    sem memória, sem nada no log dizendo por quê.
    """
    from lea.agent import _cli_environment

    monkeypatch.setenv("AI_MEMORY_AUTH_TOKEN", "token-de-teste")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "nao-emprestar")
    monkeypatch.setenv("OPENAI_API_KEY", "nao-emprestar")

    environment = _cli_environment()
    assert environment["AI_MEMORY_AUTH_TOKEN"] == "token-de-teste"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_marca_nao_casa_com_sessao_de_fora():
    assert not fleet.is_fleet_session("revisar o README")
    assert not fleet.is_fleet_session(None)


def test_payload_pede_sessao_interativa_no_projeto_certo():
    payload = fleet.payload_for(fleet.get("gerente"), project_id=7)
    assert payload["project_id"] == 7
    assert payload["mode"] == "interactive"
    # Flags NUNCA viajam no corpo: quem as aplica é o servidor, pelo config.
    assert "extra_args" not in payload
    assert "flags" not in payload


def test_flags_da_frota_comecam_vazias_e_coleira_vem_ligada():
    """O padrão do código, não o config da máquina de quem roda a suíte.

    Sem trava nenhuma configurada, cada CLI fica com as próprias defesas; a
    coleira do publicador vem ligada porque é o único papel cujo erro sai da
    máquina.
    """
    padrao = FleetConfig()
    assert padrao.flags == {}
    assert padrao.leash_publisher is True


async def test_varredura_encerra_sessao_orfa(db_path):
    """Sessão ``active`` de um processo morto é mentira: o PTY foi junto."""
    from lea import sessions
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO sessions (id, harness, status, started_at) VALUES (?, ?, 'active', ?)",
            ("s-orfa", "codex", "2026-11-03T00:00:00Z"),
        )
        await conn.commit()

        assert await sessions.reap_orphans(conn) == 1

        async with conn.execute(
            "SELECT status, ended_at FROM sessions WHERE id = 's-orfa'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row["status"] == "ended"
        assert row["ended_at"]

        # Idempotente: subir o daemon duas vezes não pode reescrever nada.
        assert await sessions.reap_orphans(conn) == 0
