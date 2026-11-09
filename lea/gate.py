"""O portão: decide, antes da ação acontecer, se ela pode acontecer.

Até aqui o LEA só conseguia barrar no backend ``anthropic-api``, onde ele mesmo
roda o laço de ferramentas. Com Codex e Claude Code ele entregava o processo ao
CLI e só via o que tinha acontecido depois -- então ação protegida não era
aprovada nem recusada: era bloqueada por regra, sem meio-termo.

O que faltava era o evento ``PreToolUse``, que os dois CLIs expõem e o LEA não
instalava. O hook roda **antes** da ferramenta e pode recusá-la.

Verificado nesta máquina em 2026-11-08, porque a documentação oficial não
afirma e as fontes de terceiros divergiam: um ``permissionDecision: "deny"``
barra a ferramenta **mesmo com ``--dangerously-skip-permissions``**. A flag
desliga o prompt interativo, não os hooks. É o que torna a supervisão real
mesmo numa frota rodando solta.

Fluxo, numa sessão **interativa**, quando o comando casa com um padrão:

1. a ferramenta é recusada e uma aprovação entra na fila;
2. a pessoa decide (``lea approvals approve``, a API, ou o dashboard);
3. na próxima tentativa o portão encontra a aprovação concedida e deixa passar.

Recusar e deixar repetir, em vez de segurar o hook esperando a decisão: um hook
pendurado trava o CLI por tempo indeterminado, e o tempo-limite de hook não é
nosso para definir.

**Autonomia.** Sessão ``autonomous`` -- é como a frota sobe -- não passa por
essa fila. A diferença é quem está do outro lado: numa sessão interativa a
pessoa está ali e decide em segundos; numa autônoma não tem ninguém olhando
naquele instante, e enfileirar não produz decisão a tempo. Produz um agente sem
a ferramenta, parado, e a pessoa descobrindo horas depois que a tarefa morreu
esperando. Dado o prompt, o papel resolve o que for necessário.

O controle não desaparece: muda de lugar. Vai para a borda onde o trabalho sai
da máquina -- o ``pre-push`` de ``lea/guard.py``, que o git executa antes de
qualquer byte sair e que nenhuma flag de CLI desliga. Travar dentro da máquina
custa autonomia e não compra segurança; travar na saída custa nada e compra a
que importa. Por isso ``lea fleet up`` instala a coleira em vez de só anunciá-la.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

#: Chaves onde cada CLI costuma colocar o comando dentro de ``tool_input``.
#: Claude Code manda ``command``; manter a busca ampla evita um adaptador por
#: CLI só para ler uma string.
_COMMAND_KEYS = ("command", "cmd", "argv", "script", "code")

#: Ferramentas que não executam nada e nunca precisam de aprovação. Passar
#: leitura pelo portão só gastaria uma ida ao banco por arquivo lido.
_READ_ONLY = {"read", "glob", "grep", "ls", "notebookread", "todowrite", "webfetch", "websearch"}


def command_of(tool_input: Any) -> str:
    """Texto do comando que a ferramenta vai executar, para casar com padrão."""
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    for key in _COMMAND_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return shlex.join(value)
    return ""


#: Separadores que encadeiam comandos numa linha de shell. Cada pedaço entre
#: eles é um comando que pode rodar por conta própria.
_SEPARATORS = (";", "&&", "||", "|", "\n")


def _segments(command: str) -> list[list[str]]:
    """Quebra a linha em comandos, e cada comando em tokens.

    Comparar token a token, e não por substring, é o que separa *executar* uma
    ação protegida de apenas *mencioná-la* dentro de um argumento. A versão
    anterior casava por substring e barrava um ``echo`` que citasse o padrão,
    um ``grep`` procurando por ele na documentação, ou um CLI recebendo a
    expressão como prompt -- esta sessão bateu nos três enquanto o consertava.
    """
    texto = command
    for separador in _SEPARATORS:
        texto = texto.replace(separador, "\n")
    saida: list[list[str]] = []
    for parte in texto.split("\n"):
        parte = parte.strip()
        if not parte:
            continue
        try:
            saida.append([token.lower() for token in shlex.split(parte)])
        except ValueError:
            # Aspas desbalanceadas: o shlex desiste. Quebrar por espaço é
            # impreciso, mas preserva o nome do comando no primeiro token, que
            # é o que o padrão compara -- e erra para o lado de barrar. Tratar
            # a linha como um token único, como eu tinha feito antes, erra para
            # o lado de LIBERAR, que num portão é o lado errado.
            saida.append([token.lower() for token in parte.split()])
    return saida


def matched_pattern(tool_name: str, tool_input: Any, patterns: list[str]) -> str | None:
    """Padrão que esta chamada viola, ou None se ela pode seguir."""
    if tool_name.lower() in _READ_ONLY:
        return None
    command = command_of(tool_input)
    if not command.strip():
        return None

    segmentos = _segments(command)
    for pattern in patterns:
        alvo = pattern.strip().lower()
        if not alvo:
            continue
        try:
            tokens = shlex.split(alvo)
        except ValueError:
            tokens = alvo.split()
        if not tokens:
            continue
        # O padrão precisa ser o COMEÇO de um comando: casa com a ação sendo
        # executada, não com o nome dela aparecendo num argumento.
        for segmento in segmentos:
            if segmento[: len(tokens)] == tokens:
                return pattern.strip()
    return None


async def _session_mode(conn: aiosqlite.Connection, session_id: str) -> str | None:
    """Modo da sessão, ou None se o LEA não a conhece.

    Serve duas perguntas de uma vez. A fila de aprovações referencia `sessions`,
    então o que não está lá não entra -- e o modo diz se há alguém esperando
    para decidir: ``interactive`` tem, ``autonomous`` não.
    """
    async with conn.execute(
        "SELECT mode FROM sessions WHERE id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return str(row["mode"]) if row is not None else None


async def _granted(
    conn: aiosqlite.Connection, session_id: str | None, command: str
) -> bool:
    """Já existe aprovação concedida para este comando nesta sessão?

    É o que faz a segunda tentativa passar depois de a pessoa aprovar. A
    comparação é pelo comando exato: aprovar um ``git push`` não libera outro
    diferente só por serem da mesma sessão.
    """
    if not session_id:
        return False
    async with conn.execute(
        "SELECT input_json FROM agent_approvals "
        "WHERE session_id = ? AND status = 'approved' ORDER BY requested_at DESC LIMIT 20",
        (session_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        try:
            saved = json.loads(row["input_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if saved.get("command") == command:
            return True
    return False


async def decide(
    conn: aiosqlite.Connection,
    *,
    session_id: str | None,
    tool_name: str,
    tool_input: Any,
    patterns: list[str],
    harness: str = "",
    gate_external: bool = False,
) -> dict[str, Any]:
    """Veredito do portão para uma chamada de ferramenta.

    Devolve ``{"allow": True}`` ou ``{"allow": False, "reason": ..., "approval_id": ...}``.
    Criar a aprovação aqui, e não na borda HTTP, mantém a decisão e o registro
    no mesmo lugar: quem recusa é quem enfileira.

    Sessão ``autonomous`` passa e fica registrada: ver "Autonomia" abaixo.

    ``gate_external`` decide o que fazer com sessão que o LEA **não** lançou.
    Os hooks são de usuário -- é como os CLIs os expõem --, então instalá-los
    coloca o portão na frente de *todo* terminal da máquina, não só dos agentes
    do LEA. E para essas sessões a fila de aprovação não existe: ela referencia
    `sessions`, e a sessão não está lá. O resultado era bloqueio sem saída:
    recusa sem nada a aprovar, sem override, em trabalho que nada tem a ver com
    a frota. O padrão é deixar passar -- o evento ainda vai para o feed ao
    vivo, que é todo o registro possível sem a sessão no banco. Ligar isto
    recusa também essas chamadas, e aí o único destrave é editar o config: quem
    quer a máquina inteira sob trava não quer um atalho dentro dela.
    """
    pattern = matched_pattern(tool_name, tool_input, patterns)
    if pattern is None:
        return {"allow": True}

    command = command_of(tool_input)
    if await _granted(conn, session_id, command):
        return {"allow": True, "approved": True}

    mode = await _session_mode(conn, session_id) if session_id else None
    nossa = mode is not None
    if nossa and mode == "autonomous":
        # Sessão autônoma: ninguém está olhando no instante em que isso
        # acontece. Enfileirar aqui não produz decisão humana a tempo -- produz
        # um agente sem a ferramenta, parado, e a pessoa descobrindo horas
        # depois pela fila. Dado o prompt, o papel resolve o que for preciso.
        #
        # O controle não desaparece, muda de lugar: vai para a borda onde o
        # trabalho sai da máquina, no `pre-push` que o git executa e nenhuma
        # flag de CLI desliga. Travar dentro da máquina custa autonomia e não
        # compra segurança; travar na saída custa nada e compra a que importa.
        logger.info(
            "portão: %r liberado em sessão autônoma %s", pattern, session_id
        )
        return {"allow": True, "autonomous": True, "pattern": pattern}

    if not nossa and not gate_external:
        # O LEA supervisiona o que ele lançou. Recusar aqui seria bloquear sem
        # oferecer como liberar.
        logger.info(
            "portão: %r casou em sessão de fora (%s); liberado por "
            "agent.gate_external_sessions=false",
            pattern,
            session_id or "sem id",
        )
        return {"allow": True, "external": True, "pattern": pattern}

    import secrets

    from lea.agent import _now

    approval_id = f"approval-{secrets.token_hex(8)}"
    reason = f"comando corresponde ao padrão {pattern!r}"
    # A fila só aceita sessão que o LEA conhece: `agent_approvals.session_id` é
    # chave estrangeira, e uma sessão aberta por fora faria o INSERT estourar.
    # Sem este teste o erro subia como 500, o `lea hook` caía na falha-aberta e
    # todo comando passava -- o portão ficava desligado onde mais parecia
    # ligado. Chegar aqui com sessão desconhecida só acontece com
    # `gate_external` ligado; aí recusamos sem fila, que é o trato declarado.
    if nossa:
        await conn.execute(
            "INSERT INTO agent_approvals "
            "(id, session_id, backend, tool_name, input_json, reason, status, requested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                approval_id,
                session_id,
                harness or "cli",
                tool_name,
                json.dumps({"command": command}, ensure_ascii=False),
                reason,
                _now(),
            ),
        )
        await conn.commit()
    else:
        approval_id = ""

    return {
        "allow": False,
        "reason": reason,
        "approval_id": approval_id or None,
        "command": command,
        "pattern": pattern,
    }


def deny_payload(reason: str, approval_id: str | None) -> dict[str, Any]:
    """Resposta que faz o CLI recusar a ferramenta.

    Formato do ``PreToolUse`` do Claude Code, conferido contra a referência de
    hooks em 2026-11-08. O Codex aceita o mesmo evento; se o formato dele
    divergir, é aqui que se separa.
    """
    detail = reason
    if approval_id:
        detail += (
            f". Aprovação {approval_id} aguardando decisão — "
            f"libere com 'lea approvals approve {approval_id}' e tente de novo."
        )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"LEA: {detail}",
        }
    }
