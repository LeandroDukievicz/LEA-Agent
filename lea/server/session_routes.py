"""Rotas de projetos, harnesses e sessões, mais o terminal por WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, WebSocket
from fastapi import WebSocketDisconnect

from lea import harnesses, projects, sessions
from lea.memory import Memory
from lea.security import tokens
from lea.sessions import pty
from lea.sessions.launcher import LaunchError, Launcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["sessoes"])


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_memory(request: Request) -> Memory:
    return request.app.state.memory


def get_launcher(request: Request) -> Launcher:
    return request.app.state.launcher


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]
MemoryDep = Annotated[Memory, Depends(get_memory)]
LauncherDep = Annotated[Launcher, Depends(get_launcher)]


# ── projetos ─────────────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects(conn: DbDep) -> dict[str, Any]:
    return {"projects": await projects.listing(conn)}


@router.post("/projects/inspect")
async def inspect_project(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Prévia da abertura: o que o LEA detecta sem registrar nada.

    É o que o modal ABRIR PROJETO mostra enquanto o usuário digita o caminho.
    """
    try:
        info = await projects.inspect(str(payload.get("path") or ""))
    except projects.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return info.as_dict()


@router.post("/projects", status_code=201)
async def create_project(
    conn: DbDep,
    memory: MemoryDep,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    allow_autonomous = payload.get("allow_autonomous", False)
    if not isinstance(allow_autonomous, bool):
        raise HTTPException(status_code=422, detail="allow_autonomous precisa ser boolean")
    try:
        result = await projects.open_project(
            conn,
            memory,
            path=str(payload.get("path") or ""),
            name=payload.get("name"),
            index_docs=bool(payload.get("index_docs")),
            allow_autonomous=allow_autonomous,
        )
    except projects.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.as_dict()


@router.patch("/projects/{project_id}/autonomy")
async def update_project_autonomy(
    conn: DbDep,
    project_id: int,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Configura o opt-in e os limites do agente próprio do LEA."""
    if "allow_autonomous" not in payload or not isinstance(payload["allow_autonomous"], bool):
        raise HTTPException(status_code=422, detail="allow_autonomous boolean é obrigatório")
    async with conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)) as cursor:
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"projeto desconhecido: {project_id}")

    updates: dict[str, Any] = {"allow_autonomous": int(payload["allow_autonomous"])}
    if "budget_brl_per_session" in payload:
        raise HTTPException(
            status_code=422,
            detail="limite monetário por sessão ficará disponível quando o cálculo de custos for implementado",
        )
    if "max_duration_min" in payload:
        value = payload["max_duration_min"]
        if value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="max_duration_min precisa ser inteiro ou null") from exc
            if not 1 <= value <= 1440:
                raise HTTPException(status_code=422, detail="max_duration_min precisa ficar entre 1 e 1440")
        updates["max_duration_min"] = value
    if "require_approval_for" in payload:
        value = payload["require_approval_for"]
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise HTTPException(status_code=422, detail="require_approval_for precisa ser uma lista de textos não vazios")
        updates["require_approval_for"] = json.dumps(value, ensure_ascii=False)

    assignment = ", ".join(f"{column} = ?" for column in updates)
    await conn.execute(
        f"UPDATE projects SET {assignment} WHERE id = ?",
        (*updates.values(), project_id),
    )
    paused = 0
    if not payload["allow_autonomous"]:
        cursor = await conn.execute(
            "UPDATE cron_jobs SET status = 'paused' WHERE project_id = ? AND status = 'active'",
            (project_id,),
        )
        paused = cursor.rowcount or 0
    await conn.commit()
    items = await projects.listing(conn)
    item = next((project for project in items if project["id"] == project_id), None)
    return {"project": item, "paused_automations": paused}


# ── harnesses ────────────────────────────────────────────────────────────


@router.get("/harnesses")
async def list_harnesses() -> dict[str, Any]:
    """Matriz harness × hooks/MCP da tela CANAIS & AGENTES."""
    hook_command = harnesses.hook_command()
    states = await asyncio.gather(
        *(h.status(hook_command) for h in harnesses.all_harnesses())
    )
    return {"harnesses": [s.as_dict() for s in states]}


@router.post("/harnesses/{harness_id}/install")
async def install_harness(harness_id: str) -> dict[str, Any]:
    """Instala hooks e o servidor MCP no harness.

    Altera a configuração de uma ferramenta de terceiro na máquina do
    usuário, então: backup antes, merge em vez de sobrescrita, e o relatório
    diz exatamente o que mudou.
    """
    harness = harnesses.get(harness_id)
    if harness is None:
        raise HTTPException(status_code=404, detail=f"harness desconhecido: {harness_id}")

    report = await harness.install(harnesses.hook_command(), harnesses.mcp_command())
    return report.as_dict()


# ── sessões ──────────────────────────────────────────────────────────────


@router.get("/sessions")
async def list_sessions(
    conn: DbDep,
    status: Annotated[str | None, Query()] = None,
    project: Annotated[int | None, Query()] = None,
    harness: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return {
        "sessions": await sessions.listing(
            conn, status=status, project_id=project, harness=harness, limit=limit
        )
    }


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    launcher: LauncherDep,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Inicia um agente. É o botão ▶ INICIAR do launcher."""
    config = request.app.state.config
    try:
        return await launcher.launch(
            harness_id=str(payload.get("harness") or ""),
            project_id=payload.get("project_id"),
            task=str(payload.get("task") or ""),
            mode=payload.get("mode") or "interactive",
            branch=payload.get("branch"),
            use_worktree=bool(payload.get("use_worktree")),
            budget_tokens=config.memory.inject_budget_tokens,
        )
    except LaunchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}")
async def get_session(conn: DbDep, session_id: str) -> dict[str, Any]:
    found = await sessions.listing(conn, limit=500)
    for item in found:
        if item["id"] == session_id:
            return item
    raise HTTPException(status_code=404, detail=f"sessão desconhecida: {session_id}")


@router.get("/sessions/{session_id}/events")
async def session_events(
    conn: DbDep,
    session_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    return {"events": await sessions.events_of(conn, session_id, limit)}


@router.post("/sessions/{session_id}/{action}")
async def control_session(
    launcher: LauncherDep,
    session_id: str,
    action: str,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Pausar, retomar, parar ou mandar entrada."""
    if action not in {"pause", "resume", "stop", "input"}:
        raise HTTPException(status_code=404, detail=f"ação desconhecida: {action}")
    try:
        return await sessions.control(
            launcher, session_id, action, (payload or {}).get("text")
        )
    except LaunchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── terminal ─────────────────────────────────────────────────────────────


@router.websocket("/sessions/{session_id}/pty")
async def session_pty(websocket: WebSocket, session_id: str) -> None:
    """Fluxo bidirecional do terminal da sessão.

    Saída vai como bytes crus (quem desenha junta e decodifica, porque um
    caractere UTF-8 pode ficar partido entre duas leituras); entrada e
    redimensionamento chegam como JSON.
    """
    if not tokens.matches(websocket.app.state.ui_token, websocket.query_params.get("token")):
        await websocket.close(code=1008, reason="token inválido")
        return

    launcher: Launcher = websocket.app.state.launcher
    running = launcher.running.get(session_id)
    if running is None:
        await websocket.close(code=1011, reason="sessão não está rodando")
        return

    await websocket.accept()

    # Quem conecta depois recebe o que já foi escrito, senão a aba abre com
    # um terminal em branco enquanto o agente já está no meio da tarefa.
    if running.scrollback:
        await websocket.send_bytes(bytes(running.scrollback))

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
    running.output.append(queue)

    async def to_browser() -> None:
        while True:
            chunk = await queue.get()
            if not chunk:
                break
            await websocket.send_bytes(chunk)

    async def from_browser() -> None:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "input":
                pty.write(running.spawned, str(message.get("data") or ""))
            elif kind == "resize":
                pty.resize(
                    running.spawned,
                    int(message.get("cols") or pty.DEFAULT_COLS),
                    int(message.get("rows") or pty.DEFAULT_ROWS),
                )

    pump = asyncio.create_task(to_browser())
    pull = asyncio.create_task(from_browser())
    try:
        await asyncio.wait({pump, pull}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket do terminal caiu")
    finally:
        for task in (pump, pull):
            task.cancel()
        if queue in running.output:
            running.output.remove(queue)
