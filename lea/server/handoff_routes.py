"""Rotas de handoff: a tela Histórico & Handoffs e o botão CLAIM."""

from __future__ import annotations

import logging
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from lea import handoffs
from lea.memory import Memory
from lea.sessions.launcher import LaunchError, Launcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["handoffs"])


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_memory(request: Request) -> Memory:
    return request.app.state.memory


def get_launcher(request: Request) -> Launcher:
    return request.app.state.launcher


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]
MemoryDep = Annotated[Memory, Depends(get_memory)]
LauncherDep = Annotated[Launcher, Depends(get_launcher)]


@router.get("/handoffs")
async def list_handoffs(
    conn: DbDep,
    status: Annotated[str | None, Query(pattern="^(pending|claimed|done)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return {"handoffs": await handoffs.listing(conn, status=status, limit=limit)}


@router.get("/handoffs/{handoff_id}")
async def get_handoff(conn: DbDep, handoff_id: str) -> dict[str, Any]:
    found = await handoffs.get(conn, handoff_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"handoff desconhecido: {handoff_id}")
    return found


@router.post("/sessions/{session_id}/handoff", status_code=201)
async def create_handoff(
    request: Request,
    conn: DbDep,
    memory: MemoryDep,
    session_id: str,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Gera o handoff de uma sessão. É o ``/handoff`` do console.

    Também é o caminho que o hook ``Stop`` usa ao encerrar uma sessão.
    """
    config = request.app.state.config
    try:
        handoff = await handoffs.generate(
            conn,
            memory,
            session_id,
            model=config.agent.default_model,
            reason=str((payload or {}).get("reason") or "pedido no console"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    request.app.state.bus.emit(
        "alert" if handoff.failed_md else "session_end",
        {"handoff": handoff.id, "title": handoff.title},
        session_id=session_id,
    )
    return handoff.as_dict()


@router.post("/handoffs/{handoff_id}/claim", status_code=201)
async def claim_handoff(
    request: Request,
    launcher: LauncherDep,
    handoff_id: str,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Assume o handoff iniciando uma sessão nova no modo ``handoff``.

    O launcher é quem marca o claim, e só depois do processo de pé: um
    handoff marcado para uma sessão que falhou ao subir ficaria preso, com
    ninguém trabalhando nele e ninguém podendo assumi-lo.
    """
    harness = str(payload.get("harness") or "")
    if not harness:
        raise HTTPException(status_code=422, detail="campo obrigatório: harness")

    config = request.app.state.config
    try:
        return await launcher.launch(
            harness_id=harness,
            task=str(payload.get("task") or ""),
            use_worktree=bool(payload.get("use_worktree")),
            branch=payload.get("branch"),
            budget_tokens=config.memory.inject_budget_tokens,
            handoff_id=handoff_id,
        )
    except LaunchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
