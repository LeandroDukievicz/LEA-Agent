"""Sessões: iniciar, controlar e acompanhar os agentes de código."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from lea.sessions import pty, worktree
from lea.sessions.launcher import LaunchError, Launcher, Running, new_session_id

logger = logging.getLogger(__name__)

__all__ = [
    "LaunchError",
    "Launcher",
    "Running",
    "control",
    "events_of",
    "listing",
    "new_session_id",
    "pty",
    "worktree",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def control(
    launcher: Launcher, session_id: str, action: str, text: str | None = None
) -> dict[str, Any]:
    """Pausa, retoma, para ou manda entrada para uma sessão.

    Erro de "não está rodando" é tratado como pedido inválido e não como
    falha interna: a interface pode ter o botão de uma sessão que acabou de
    terminar, e isso não é um defeito.
    """
    running = launcher.running.get(session_id)
    if running is None:
        raise LaunchError(f"sessão {session_id} não está rodando")

    if action == "pause":
        pty.pause(running.spawned)
        await _set_status(launcher.conn, session_id, "paused")
        await _emit(launcher, session_id, "session_start", {"action": "pausada"})
        return {"id": session_id, "status": "paused"}

    if action == "resume":
        pty.resume(running.spawned)
        await _set_status(launcher.conn, session_id, "active")
        await _emit(launcher, session_id, "session_start", {"action": "retomada"})
        return {"id": session_id, "status": "active"}

    if action == "stop":
        code = await pty.stop(running.spawned)
        # O _pump percebe o fim do fluxo e fecha a sessão; aqui só relatamos.
        return {"id": session_id, "status": "ended", "exit_code": code}

    if action == "input":
        if text is None:
            raise LaunchError("input exige o texto a enviar")
        pty.write(running.spawned, text)
        return {"id": session_id, "sent": len(text)}

    raise LaunchError(f"ação desconhecida: {action}")


async def listing(
    conn: aiosqlite.Connection,
    *,
    status: str | None = None,
    project_id: int | None = None,
    harness: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Sessões, mais recentes primeiro."""
    sql = """
        SELECT s.*, p.slug AS project_slug, p.name AS project_name
        FROM sessions s LEFT JOIN projects p ON p.id = s.project_id
        WHERE 1 = 1
    """
    params: list[Any] = []
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    if project_id is not None:
        sql += " AND s.project_id = ?"
        params.append(project_id)
    if harness:
        sql += " AND s.harness = ?"
        params.append(harness)
    sql += " ORDER BY s.started_at DESC LIMIT ?"
    params.append(limit)

    async with conn.execute(sql, params) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "id": row["id"],
            "harness": row["harness"],
            "agent_label": row["agent_label"],
            "status": row["status"],
            "mode": row["mode"],
            "task": row["task"],
            "branch": row["branch"],
            "worktree_path": row["worktree_path"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "cost_brl": row["cost_brl"],
            "pid": row["pid"],
            "project": (
                {"id": row["project_id"], "slug": row["project_slug"], "name": row["project_name"]}
                if row["project_id"]
                else None
            ),
        }
        for row in rows
    ]


async def events_of(
    conn: aiosqlite.Connection, session_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """Eventos da sessão, do mais antigo para o mais novo.

    A ordem é cronológica porque é como o console desenha: a consulta pega
    os ``limit`` mais recentes e depois inverte, para o usuário ver o fim da
    conversa, não o começo dela.
    """
    async with conn.execute(
        "SELECT id, at, type, payload_json FROM events WHERE session_id = ? "
        "ORDER BY at DESC, id DESC LIMIT ?",
        (session_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "id": row["id"],
            "at": row["at"],
            "type": row["type"],
            "payload": _loads(row["payload_json"]),
        }
        for row in reversed(rows)
    ]


async def _set_status(conn: aiosqlite.Connection, session_id: str, status: str) -> None:
    await conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))
    await conn.commit()


async def _emit(launcher: Launcher, session_id: str, type: str, payload: dict) -> None:
    await launcher._event(session_id, type, payload)


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"value": value}
