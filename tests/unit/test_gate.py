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


async def test_sessao_de_fora_passa_porque_nao_ha_como_aprovar(db_path):
    """Os hooks são de usuário: o portão fica na frente de todo terminal.

    Verificado nesta máquina: instalados os hooks, o primeiro `git push` barrado
    foi o de quem estava *escrevendo o portão*, numa sessão que o LEA não
    lançou. E ali não há saída -- a fila referencia `sessions`, a sessão não
    está lá, então não existe aprovação para conceder. Recusa sem nada a
    aprovar não é supervisão: é a máquina parada.

    O LEA supervisiona o que ele lançou.
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
    assert veredito["allow"] is True
    assert veredito["external"] is True
    assert veredito["pattern"] == "git push"        # passou, mas identificado


async def test_sessao_de_fora_e_recusada_quando_a_trava_e_geral(db_path):
    """`gate_external_sessions` troca o padrão: tudo na máquina sob trava.

    Continua sem fila -- a chave estrangeira não deixa --, e é por isso que o
    teste cobra `approval_id is None`. Sem o teste de sessão conhecida, este
    INSERT estouraria, o erro subiria como HTTP 500, o `lea hook` cairia na
    falha-aberta e TODO comando passaria: o portão ficava desligado exatamente
    onde mais parecia ligado.
    """
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        veredito = await gate.decide(
            conn,
            session_id="sessao-que-o-lea-nunca-abriu",
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
            patterns=["git push"],
            gate_external=True,
        )
    assert veredito["allow"] is False
    assert veredito["approval_id"] is None          # não enfileirou...
    assert "git push" in veredito["reason"]         # ...mas recusou igual


async def test_sessao_sem_id_nenhum_segue_a_mesma_regra(db_path):
    """Hook sem `session_id` é sessão de fora: não há como ser nossa."""
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        solto = await gate.decide(
            conn, session_id=None, tool_name="Bash",
            tool_input={"command": "sudo rm /x"}, patterns=["sudo"],
        )
        travado = await gate.decide(
            conn, session_id=None, tool_name="Bash",
            tool_input={"command": "sudo rm /x"}, patterns=["sudo"],
            gate_external=True,
        )
    assert solto["allow"] is True
    assert travado["allow"] is False


async def test_sessao_autonoma_resolve_sem_pedir(db_path):
    """Dado o prompt, o papel resolve o que for necessário.

    É como a frota sobe. A diferença com a sessão interativa é quem está do
    outro lado: ali a pessoa decide em segundos, aqui não tem ninguém olhando
    naquele instante. Enfileirar não produz decisão a tempo -- produz agente sem
    a ferramenta, parado, e a pessoa descobrindo horas depois pela fila.

    O controle não desaparece, muda de lugar: vai para o `pre-push`, que o git
    executa antes de qualquer byte sair e nenhuma flag de CLI desliga.
    """
    from lea.agent import list_approvals
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO sessions (id, harness, status, mode, started_at) "
            "VALUES (?, ?, 'active', 'autonomous', ?)",
            ("s-frota", "claude-code", "2026-11-09T00:00:00Z"),
        )
        await conn.commit()

        veredito = await gate.decide(
            conn, session_id="s-frota", tool_name="Bash",
            tool_input={"command": "sudo systemctl restart x"}, patterns=["sudo"],
        )
        assert veredito["allow"] is True
        assert veredito["autonomous"] is True
        assert veredito["pattern"] == "sudo"      # passou, mas identificado

        # E nada foi para a fila: pedido que ninguém vai ler é só ruído.
        assert await list_approvals(conn, "pending") == []


async def test_sessao_conhecida_enfileira_e_a_segunda_tentativa_passa(db_path):
    """Recusar e deixar repetir, em vez de pendurar o hook esperando a pessoa."""
    from lea.db import session as db

    async with db.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO sessions (id, harness, status, mode, started_at) "
            "VALUES (?, ?, 'active', 'interactive', ?)",
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
