"""O portão: o que passa, o que é recusado e o que a segunda tentativa faz."""

import json

from lea import gate


def test_comando_sai_de_onde_cada_cli_coloca():
    assert gate.command_of({"command": "git push"}) == "git push"
    assert gate.command_of({"argv": ["git", "push", "--force"]}) == "git push --force"
    assert gate.command_of("rm -rf /") == "rm -rf /"
    assert gate.command_of({"path": "README.md"}) == ""


def test_ferramenta_de_leitura_nao_passa_pelo_portao():
    """Ler arquivo não executa nada; mandar cada leitura ao banco seria custo
    puro, e `Read` com um caminho que contenha "curl" não é um comando."""
    assert gate.matched_pattern("Read", {"path": "docs/curl.md"}, ["curl"]) is None
    assert gate.matched_pattern("Bash", {"command": "curl example.com"}, ["curl"]) == "curl"


def test_padrao_casa_sem_depender_de_caixa():
    assert gate.matched_pattern("Bash", {"command": "GIT PUSH origin"}, ["git push"]) == "git push"


def test_sem_padrao_a_acao_segue():
    assert gate.matched_pattern("Bash", {"command": "ls -la"}, ["git push", "sudo"]) is None


def test_resposta_de_recusa_tem_o_formato_que_o_cli_le():
    """Formato do PreToolUse do Claude Code, conferido contra a referência e
    contra o CLI real: é o que faz a ferramenta ser barrada."""
    payload = gate.deny_payload("comando protegido", "approval-abc")
    saida = payload["hookSpecificOutput"]
    assert saida["hookEventName"] == "PreToolUse"
    assert saida["permissionDecision"] == "deny"
    assert "approval-abc" in saida["permissionDecisionReason"]
    assert "lea approvals approve" in saida["permissionDecisionReason"]
    json.dumps(payload)                                   # precisa serializar


def test_recusa_sem_aprovacao_ainda_explica():
    saida = gate.deny_payload("comando protegido", None)["hookSpecificOutput"]
    assert saida["permissionDecision"] == "deny"
    assert "approval-" not in saida["permissionDecisionReason"]


async def test_sessao_desconhecida_e_recusada_sem_estourar(db_path):
    """A fila referencia `sessions`; sessão de fora faria o INSERT estourar.

    Essa era a falha: o erro subia como HTTP 500, o `lea hook` caía na
    falha-aberta e TODO comando passava em qualquer sessão que o LEA não tenha
    lançado -- a maioria delas. O portão ficava desligado exatamente onde mais
    parecia ligado. Não registrar não é liberar.
    """
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        veredito = await gate.decide(
            conn,
            session_id="sessao-que-o-lea-nunca-abriu",
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
            patterns=["git push"],
        )
    assert veredito["allow"] is False
    assert veredito["approval_id"] is None          # não enfileirou...
    assert "git push" in veredito["reason"]         # ...mas recusou igual


async def test_sessao_conhecida_enfileira_e_a_segunda_tentativa_passa(db_path):
    """Recusar e deixar repetir, em vez de pendurar o hook esperando a pessoa."""
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO sessions (id, harness, status, started_at) VALUES (?, ?, 'active', ?)",
            ("s-teste", "claude-code", "2026-11-08T00:00:00Z"),
        )
        await conn.commit()

        primeira = await gate.decide(
            conn, session_id="s-teste", tool_name="Bash",
            tool_input={"command": "git push origin main"}, patterns=["git push"],
        )
        assert primeira["allow"] is False
        assert primeira["approval_id"]

        await conn.execute(
            "UPDATE agent_approvals SET status = 'approved' WHERE id = ?",
            (primeira["approval_id"],),
        )
        await conn.commit()

        segunda = await gate.decide(
            conn, session_id="s-teste", tool_name="Bash",
            tool_input={"command": "git push origin main"}, patterns=["git push"],
        )
        assert segunda["allow"] is True

        # Aprovar um comando não libera outro na mesma sessão.
        outro = await gate.decide(
            conn, session_id="s-teste", tool_name="Bash",
            tool_input={"command": "git push --force origin main"}, patterns=["git push"],
        )
        assert outro["allow"] is False
