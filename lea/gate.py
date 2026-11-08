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

Fluxo, quando o comando casa com um padrão que exige aprovação:

1. a ferramenta é recusada e uma aprovação entra na fila;
2. a pessoa decide (``lea approvals approve``, a API, ou o dashboard);
3. na próxima tentativa o portão encontra a aprovação concedida e deixa passar.

Recusar e deixar repetir, em vez de segurar o hook esperando a decisão: um hook
pendurado trava o CLI por tempo indeterminado, e o tempo-limite de hook não é
nosso para definir.
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


def matched_pattern(tool_name: str, tool_input: Any, patterns: list[str]) -> str | None:
    """Padrão que esta chamada viola, ou None se ela pode seguir."""
    if tool_name.lower() in _READ_ONLY:
        return None
    command = command_of(tool_input).lower()
    if not command:
        return None
    for pattern in patterns:
        needle = pattern.strip().lower()
        if needle and needle in command:
            return pattern.strip()
    return None


async def _known_session(conn: aiosqlite.Connection, session_id: str) -> bool:
    """A fila de aprovações referencia `sessions`; o que não está lá não entra."""
    async with conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) as cursor:
        return await cursor.fetchone() is not None


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
) -> dict[str, Any]:
    """Veredito do portão para uma chamada de ferramenta.

    Devolve ``{"allow": True}`` ou ``{"allow": False, "reason": ..., "approval_id": ...}``.
    Criar a aprovação aqui, e não na borda HTTP, mantém a decisão e o registro
    no mesmo lugar: quem recusa é quem enfileira.
    """
    pattern = matched_pattern(tool_name, tool_input, patterns)
    if pattern is None:
        return {"allow": True}

    command = command_of(tool_input)
    if await _granted(conn, session_id, command):
        return {"allow": True, "approved": True}

    import secrets

    from lea.agent import _now

    approval_id = f"approval-{secrets.token_hex(8)}"
    reason = f"comando corresponde ao padrão {pattern!r}"
    # A fila só aceita sessão que o LEA conhece: `agent_approvals.session_id` é
    # chave estrangeira, e uma sessão que a pessoa abriu no terminal por fora
    # faria o INSERT estourar. Sem o teste, o erro subia como 500, o `lea hook`
    # caía na falha-aberta e TODO comando passava em qualquer sessão de fora --
    # ou seja, o portão ficava desligado exatamente onde mais parecia ligado.
    #
    # Não registrar não é liberar: a recusa vale igual. Não saber de quem é a
    # sessão é motivo para barrar, nunca para deixar passar.
    if session_id and await _known_session(conn, session_id):
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
