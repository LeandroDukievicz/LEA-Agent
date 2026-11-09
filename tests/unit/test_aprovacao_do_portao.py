"""Aprovação criada pelo portão precisa poder ser aprovada.

A metade da recusa funcionava desde a Fase 5; a da liberação nunca existiu, e
isso só aparece testando o ciclo inteiro contra uma sessão de CLI. O
`resolve_approval` exigia sessão `paused`, condição que vem do backend
`anthropic-api`: ali quem pede a aprovação é o laço de ferramentas do próprio
LEA, que pausa a sessão e fica esperando. O portão dos CLIs não pausa nada --
não pode: o hook recusa a ferramenta e volta na hora, e o CLI segue de pé. A
sessão continua `active`, a condição nunca casava, e `lea approvals approve`
respondia "aprovação pendente não encontrada" para toda aprovação vinda de
Codex ou Claude Code.
"""

from lea import gate
from lea.agent import resolve_approval
from lea.db import session as db


async def _sessao(conn, sid: str, status: str) -> None:
    await conn.execute(
        "INSERT INTO sessions (id, harness, status, started_at) VALUES (?, ?, ?, ?)",
        (sid, "claude-code", status, "2026-11-09T00:00:00Z"),
    )
    await conn.commit()


async def test_aprovacao_de_sessao_ativa_pode_ser_aprovada(db_path):
    async with db.connection(db_path) as conn:
        await _sessao(conn, "s-ativa", "active")
        veredito = await gate.decide(
            conn, session_id="s-ativa", tool_name="Bash",
            tool_input={"command": "git push origin main"}, patterns=["git push"],
        )
        assert veredito["allow"] is False

        # Era aqui que a Fase 5 parava: None, e a pessoa sem como liberar.
        assert await resolve_approval(conn, veredito["approval_id"], "approve") == "s-ativa"

        segunda = await gate.decide(
            conn, session_id="s-ativa", tool_name="Bash",
            tool_input={"command": "git push origin main"}, patterns=["git push"],
        )
        assert segunda["allow"] is True


async def test_sessao_pausada_continua_sendo_retomada(db_path):
    """O caminho do `anthropic-api`: pausou esperando, volta a `active`."""
    async with db.connection(db_path) as conn:
        await _sessao(conn, "s-pausada", "paused")
        veredito = await gate.decide(
            conn, session_id="s-pausada", tool_name="Bash",
            tool_input={"command": "sudo apt install x"}, patterns=["sudo"],
        )
        assert await resolve_approval(conn, veredito["approval_id"], "approve") == "s-pausada"

        async with conn.execute(
            "SELECT status FROM sessions WHERE id = 's-pausada'"
        ) as cursor:
            assert (await cursor.fetchone())["status"] == "active"


async def test_sessao_encerrada_nao_aceita_decisao(db_path):
    """A trava que a exigência de `paused` protegia continua de pé: decidir
    sobre sessão que já acabou libera um comando para ninguém."""
    async with db.connection(db_path) as conn:
        await _sessao(conn, "s-morta", "active")
        veredito = await gate.decide(
            conn, session_id="s-morta", tool_name="Bash",
            tool_input={"command": "git push origin main"}, patterns=["git push"],
        )
        await conn.execute("UPDATE sessions SET status = 'ended' WHERE id = 's-morta'")
        await conn.commit()
        assert await resolve_approval(conn, veredito["approval_id"], "approve") is None


async def test_pendencia_de_sessao_morta_expira(db_path):
    """Aprovação do portão não tem dono que a limpe.

    No `anthropic-api` quem pede espera pela decisão e expira no `finally`. O
    hook do portão volta na hora e ninguém fica esperando -- então a pendência
    sobrevivia à sessão e `lea approvals list` acumulava linhas indecidíveis,
    escondendo as que ainda valem. Meia hora de frota produziu três.
    """
    from lea.agent import expire_stale_approvals, list_approvals

    async with db.connection(db_path) as conn:
        await _sessao(conn, "s-viva", "active")
        await _sessao(conn, "s-foi", "active")
        for sid in ("s-viva", "s-foi"):
            await gate.decide(
                conn, session_id=sid, tool_name="Bash",
                tool_input={"command": "git push origin main"}, patterns=["git push"],
            )
        await conn.execute("UPDATE sessions SET status = 'handoff' WHERE id = 's-foi'")
        await conn.commit()

        assert await expire_stale_approvals(conn) == 1
        pendentes = await list_approvals(conn, "pending")
        assert [p["session_id"] for p in pendentes] == ["s-viva"]

        # Expirar é registro, não apagar: a fila conta quem pediu o quê.
        expiradas = await list_approvals(conn, "expired")
        assert [p["session_id"] for p in expiradas] == ["s-foi"]


async def test_a_varredura_da_subida_roda_sem_orfa(db_path):
    """`reap_orphans` saía cedo quando não havia órfã, e levava a limpeza com ele.

    Pego ao reiniciar o daemon de verdade: a fila continuou com as três linhas
    mortas. Sessão encerrada pela porta da frente (`fleet down`, `SessionEnd`)
    é exatamente o caso sem órfã -- e era o único caso que importava.
    """
    from lea.agent import list_approvals
    from lea.sessions import reap_orphans

    async with db.connection(db_path) as conn:
        await _sessao(conn, "s-fechada", "active")
        await gate.decide(
            conn, session_id="s-fechada", tool_name="Bash",
            tool_input={"command": "git push origin main"}, patterns=["git push"],
        )
        await conn.execute("UPDATE sessions SET status = 'ended' WHERE id = 's-fechada'")
        await conn.commit()

        assert await reap_orphans(conn) == 0          # nenhuma órfã...
        assert await list_approvals(conn, "pending") == []   # ...e a fila limpa
