"""Handoffs: geração (com e sem LLM), persistência e claim.

A especificação pede explicitamente teste de "geração de handoff (com LLM
mockado)". O caminho sem LLM é testado com o mesmo peso: ele é o que roda
quando não há credencial, e é justamente no dia em que o modelo está fora que
perder o handoff doeria mais.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from lea import handoffs, llm
from lea.db import session as db
from lea.memory import Memory

SEM_MODELO = "anthropic/claude-sonnet-5"


@pytest_asyncio.fixture
async def memory(lea_home: Path):
    conn = await db.connect(lea_home / "lea.db")
    mem = Memory(conn, lea_home / "wiki")
    await mem.setup()
    try:
        yield mem
    finally:
        await conn.close()


async def _sessao(
    memory: Memory,
    session_id: str = "s-7f3a",
    *,
    task: str = "ajustar o webhook de pagamento para aceitar retry",
    harness: str = "codex",
    worktree: str | None = "/tmp/wt/s-7f3a",
) -> str:
    await memory.conn.execute(
        "INSERT INTO projects (slug, name, path, created_at) VALUES (?, ?, ?, ?)",
        ("cliente-acme", "Cliente Acme", "/tmp/acme", "2026-10-28T00:00:00+00:00"),
    )
    await memory.conn.execute(
        """
        INSERT INTO sessions (id, project_id, harness, agent_label, status, mode,
                              branch, worktree_path, task, started_at)
        VALUES (?, 1, ?, ?, 'active', 'interactive', ?, ?, ?, ?)
        """,
        (
            session_id,
            harness,
            "Codex CLI",
            f"lea/{session_id}",
            worktree,
            task,
            "2026-10-28T10:00:00+00:00",
        ),
    )
    await memory.conn.commit()
    return session_id


async def _sessao_minima(memory: Memory, session_id: str) -> str:
    """Sessão só com o essencial, para ser alvo de um claim.

    Existe porque `claimed_by_session_id` tem foreign key: no fluxo real o
    launcher cria a sessão antes de marcar o claim, e o banco cobra isso.
    """
    await memory.conn.execute(
        "INSERT INTO sessions (id, harness, status, mode, started_at) "
        "VALUES (?, 'claude-code', 'active', 'handoff', ?)",
        (session_id, "2026-10-28T11:00:00+00:00"),
    )
    await memory.conn.commit()
    return session_id


async def _evento(memory: Memory, session_id: str, type: str, payload: dict) -> None:
    await memory.conn.execute(
        "INSERT INTO events (session_id, at, type, payload_json) VALUES (?, ?, ?, ?)",
        (session_id, "2026-10-28T10:05:00+00:00", type, json.dumps(payload)),
    )
    await memory.conn.commit()


# ── caminho sem LLM ──────────────────────────────────────────────────────


async def test_gera_handoff_sem_credencial(memory: Memory, monkeypatch) -> None:
    """Sem chave de API o handoff ainda sai -- mais seco, mas existe."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    await _evento(memory, sid, "tool_call", {"tool_name": "Bash"})
    await _evento(memory, sid, "tool_call", {"tool_name": "Edit"})
    await _evento(memory, sid, "tool_call", {"tool_name": "Bash"})

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    assert h.deterministic
    assert h.id.startswith("hf-0x")
    assert "webhook" in h.title
    assert "Codex" in h.stopped_at_md
    assert "Bash (2x)" in h.stopped_at_md, h.stopped_at_md
    assert h.open_items


async def test_determinístico_registra_falha_por_codigo_de_saida(
    memory: Memory, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    await _evento(memory, sid, "session_end", {"exit_code": 2})

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    assert "código 2" in h.failed_md


async def test_determinístico_nao_inventa_falha(memory: Memory, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    await _evento(memory, sid, "session_end", {"exit_code": 0})

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    assert h.failed_md == ""


async def test_determinístico_cita_a_worktree(memory: Memory, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    assert any("worktree" in item for item in h.open_items)


# ── caminho com LLM mockado ──────────────────────────────────────────────


class _SummarizerFalso:
    model = "claude-sonnet-5"

    def __init__(self, draft: handoffs.HandoffDraft) -> None:
        self.draft = draft
        self.prompt: str | None = None
        self.system: str | None = None

    async def structured(self, *, system, prompt, schema):
        self.system, self.prompt = system, prompt
        return self.draft, {"tokens_in": 1200, "tokens_out": 180}


async def test_gera_handoff_com_llm(memory: Memory, monkeypatch) -> None:
    sid = await _sessao(memory)
    await _evento(memory, sid, "tool_call", {"tool_name": "Bash"})

    falso = _SummarizerFalso(
        handoffs.HandoffDraft(
            title="Webhook de pagamento com retry",
            stopped_at="A validação do payload está pronta; falta o backoff.",
            failed="O teste test_retry_backoff quebra com timeout.",
            open_items=["Implementar backoff exponencial", "Rodar a suíte de pagamento"],
            cited_pages=[],
        )
    )
    monkeypatch.setattr(llm, "get_summarizer", lambda model: falso)

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    assert not h.deterministic
    assert h.title == "Webhook de pagamento com retry"
    assert "backoff" in h.stopped_at_md
    assert len(h.open_items) == 2


async def test_material_da_sessao_vai_delimitado_ao_modelo(
    memory: Memory, monkeypatch
) -> None:
    """Conteúdo de sessão é dado, não instrução."""
    sid = await _sessao(memory)
    falso = _SummarizerFalso(handoffs.HandoffDraft(title="t", stopped_at="s", failed=""))
    monkeypatch.setattr(llm, "get_summarizer", lambda model: falso)

    await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    assert "<sessao>" in falso.prompt
    assert "<eventos>" in falso.prompt
    assert "DADO" in falso.system


async def test_cai_para_deterministico_quando_o_modelo_falha(
    memory: Memory, monkeypatch
) -> None:
    """Provedor fora do ar não pode custar o handoff."""

    class _Quebrado:
        model = "x"

        async def structured(self, **kwargs):
            raise llm.LLMUnavailable("sem cota agora")

    sid = await _sessao(memory)
    monkeypatch.setattr(llm, "get_summarizer", lambda model: _Quebrado())

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    assert h.deterministic
    assert h.title


async def test_pagina_citada_inexistente_e_descartada(memory: Memory, monkeypatch) -> None:
    """O modelo pode citar uma página plausível que não existe."""
    sid = await _sessao(memory)
    falso = _SummarizerFalso(
        handoffs.HandoffDraft(
            title="t",
            stopped_at="s",
            failed="",
            cited_pages=["cliente-acme/nao-existe", "inventada"],
        )
    )
    monkeypatch.setattr(llm, "get_summarizer", lambda model: falso)

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    assert h.cited_pages == []


# ── persistência ─────────────────────────────────────────────────────────


async def test_handoff_vira_pagina_de_wiki_citavel(memory: Memory, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    page = memory.wiki.read(f"handoffs/{h.id}.md")
    assert page.title == h.title
    assert "## Onde parou" in page.body
    assert page.tier == "hot"

    versions = await memory.wiki.versions(f"handoffs/{h.id}.md")
    assert versions, "a página precisa de commit para ser citável no histórico"


async def test_sessao_fica_marcada_como_handoff(memory: Memory, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    async with memory.conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)) as c:
        assert (await c.fetchone())["status"] == "handoff"


async def test_paginas_lidas_pela_sessao_entram_como_citadas(
    memory: Memory, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    r = await memory.write(path="cliente-acme/webhook", title="Webhook", body="retry")
    await memory.read(r.page.id, agent="codex", session_id=sid)

    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    assert "cliente-acme/webhook" in h.cited_pages


async def test_citar_reforca_a_pagina(memory: Memory, monkeypatch) -> None:
    """Citação em handoff é o sinal mais forte do modelo de decaimento."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    r = await memory.write(path="cliente-acme/webhook", title="Webhook", body="retry")
    await memory.read(r.page.id, agent="codex", session_id=sid)

    await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    async with memory.conn.execute(
        "SELECT COUNT(*) FROM page_access WHERE page_id = ? AND kind = 'handoff_cite'",
        (r.page.id,),
    ) as c:
        assert (await c.fetchone())[0] == 1


async def test_sessao_inexistente_levanta(memory: Memory) -> None:
    with pytest.raises(ValueError, match="sessão desconhecida"):
        await handoffs.generate(memory.conn, memory, "s-nada", model=SEM_MODELO)


# ── claim ────────────────────────────────────────────────────────────────


async def test_claim_marca_como_assumido(memory: Memory, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    await _sessao_minima(memory, "s-novo")

    result = await handoffs.claim(memory.conn, h.id, "s-novo")
    assert result["status"] == "claimed"
    assert result["claimed_by_session_id"] == "s-novo"
    assert result["claimed_at"]


async def test_claim_duplo_e_recusado(memory: Memory, monkeypatch) -> None:
    """Duas interfaces clicando CLAIM não podem dar o mesmo trabalho a dois agentes."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)
    await _sessao_minima(memory, "s-primeiro")
    await _sessao_minima(memory, "s-segundo")

    await handoffs.claim(memory.conn, h.id, "s-primeiro")
    with pytest.raises(ValueError, match="já está claimed"):
        await handoffs.claim(memory.conn, h.id, "s-segundo")


async def test_claim_de_handoff_inexistente(memory: Memory) -> None:
    with pytest.raises(ValueError, match="desconhecido"):
        await handoffs.claim(memory.conn, "hf-0xnada", "s-1")


async def test_listagem_filtra_por_estado(memory: Memory, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = await _sessao(memory)
    h = await handoffs.generate(memory.conn, memory, sid, model=SEM_MODELO)

    assert len(await handoffs.listing(memory.conn, status="pending")) == 1
    await _sessao_minima(memory, "s-novo")
    await handoffs.claim(memory.conn, h.id, "s-novo")
    assert await handoffs.listing(memory.conn, status="pending") == []
    assert len(await handoffs.listing(memory.conn, status="claimed")) == 1


# ── camada de modelo ─────────────────────────────────────────────────────


def test_split_model() -> None:
    assert llm.split_model("anthropic/claude-sonnet-5") == ("anthropic", "claude-sonnet-5")
    assert llm.split_model("claude-opus-5") == ("anthropic", "claude-opus-5")
    assert llm.split_model("ollama/llama3") == ("ollama", "llama3")


def test_sem_credencial_nao_ha_summarizer(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.get_summarizer("anthropic/claude-sonnet-5") is None


def test_provedor_sem_backend_nao_ha_summarizer(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-teste")
    assert llm.get_summarizer("ollama/llama3") is None


def test_com_credencial_ha_summarizer(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-teste")
    s = llm.get_summarizer("anthropic/claude-sonnet-5")
    assert s is not None and s.model == "claude-sonnet-5"


def test_nao_pega_credencial_emprestada_de_outra_ferramenta(monkeypatch) -> None:
    """A credencial do Claude Code é daquele programa, não nossa."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.credential_available("anthropic") is False


def test_describe_backend_explica_o_motivo(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    info = llm.describe_backend("anthropic/claude-sonnet-5")
    assert info["available"] is False
    assert "ANTHROPIC_API_KEY" in info["reason"]
