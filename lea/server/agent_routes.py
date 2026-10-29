"""Rotas para o lea-agent, skills portáveis e tarefas agendadas."""

from __future__ import annotations

import json
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Body, HTTPException, Query, Request

from lea import cron, skills
from lea.agent import AgentError, AgentService, normalize_backend

router = APIRouter(prefix="/api", tags=["agent"])
MAX_INSTRUCTION_CHARS = 20_000


def _agent(request: Request) -> AgentService:
    return request.app.state.agent


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


# ── tarefas próprias do LEA ──────────────────────────────────────────────


@router.post("/agent/runs", status_code=202)
async def start_agent_run(request: Request, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    backend = payload.get("backend")
    if backend is not None and not isinstance(backend, str):
        raise HTTPException(status_code=422, detail="backend precisa ser texto")
    try:
        session, _task = await _agent(request).start(
            project_id=int(payload.get("project_id")),
            task=str(payload.get("task") or ""),
            skill_id=int(payload["skill_id"]) if payload.get("skill_id") is not None else None,
            backend=backend,
        )
    except (AgentError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session


@router.post("/agent/runs/{session_id}/stop")
async def stop_agent_run(request: Request, session_id: str) -> dict[str, Any]:
    if not await _agent(request).cancel(session_id):
        raise HTTPException(status_code=404, detail="sessão autônoma não está rodando")
    return {"id": session_id, "status": "stopping"}


@router.get("/agent/approvals")
async def list_agent_approvals(request: Request) -> dict[str, Any]:
    approvals = await _agent(request).list_approvals("pending")
    return {"approvals": approvals}


@router.post("/agent/approvals/{approval_id}/decision")
async def decide_agent_approval(
    request: Request,
    approval_id: str,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    decision = payload.get("decision")
    if not isinstance(decision, str) or decision not in {"approve", "deny"}:
        raise HTTPException(status_code=422, detail="decision precisa ser 'approve' ou 'deny'")
    session_id = await _agent(request).decide_approval(approval_id, str(decision), decided_by="ui")
    if session_id is None:
        raise HTTPException(status_code=409, detail="aprovação não está mais pendente")
    return {
        "id": approval_id,
        "session_id": session_id,
        "status": "approved" if decision == "approve" else "denied",
    }


# ── skills ───────────────────────────────────────────────────────────────


@router.get("/skills")
async def list_skills(request: Request) -> dict[str, Any]:
    items = await skills.list_skills(_db(request))
    return {"skills": [item.as_dict() for item in items]}


@router.get("/skills/{name}")
async def get_skill(request: Request, name: str) -> dict[str, Any]:
    try:
        item = await skills.get_skill(_db(request), name)
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return item.as_dict(include_body=True)


@router.post("/skills", status_code=201)
async def create_skill(request: Request, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=422, detail="campo obrigatório: content")
    try:
        parsed = skills.parse(content)
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with _db(request).execute("SELECT 1 FROM skills WHERE name = ?", (parsed.name,)) as cursor:
        if await cursor.fetchone() is not None:
            raise HTTPException(status_code=409, detail=f"skill já existe: {parsed.name}")
    try:
        item = await skills.save_skill(
            _db(request),
            content,
            reason=str(payload.get("reason") or "criada pela interface"),
            created_by="ui",
        )
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return item.as_dict(include_body=True)


@router.put("/skills/{name}")
async def update_skill(request: Request, name: str, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=422, detail="campo obrigatório: content")
    try:
        current = await skills.get_skill(_db(request), name)
        skills.parse(content, directory_name=name)
        if current.origin == "auto":
            raise HTTPException(status_code=409, detail="skills automáticas precisam ser aplicadas pela revisão")
        item = await skills.save_skill(
            _db(request), content, reason=str(payload.get("reason") or "atualizada pela interface"), created_by="ui"
        )
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return item.as_dict(include_body=True)


@router.get("/skills/{name}/history")
async def skill_history(
    request: Request,
    name: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    try:
        item = await skills.get_skill(_db(request), name)
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"versions": await skills.history(_db(request), item.id, limit)}


@router.get("/skills/{name}/runs")
async def skill_runs(
    request: Request,
    name: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    try:
        item = await skills.get_skill(_db(request), name)
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"runs": await skills.runs(_db(request), item.id, limit)}


# ── automações ──────────────────────────────────────────────────────────


@router.get("/automations")
async def list_automations(
    request: Request,
    status: Annotated[str | None, Query(pattern="^(active|paused)$")] = None,
) -> dict[str, Any]:
    conn = _db(request)
    query = (
        "SELECT j.*, p.slug AS project_slug, s.name AS skill_name "
        "FROM cron_jobs j LEFT JOIN projects p ON p.id = j.project_id "
        "LEFT JOIN skills s ON s.id = j.skill_id"
    )
    params: tuple[Any, ...] = ()
    if status:
        query += " WHERE j.status = ?"
        params = (status,)
    query += " ORDER BY j.next_run_at, j.id"
    async with conn.execute(query, params) as cursor:
        jobs = await cursor.fetchall()
    result = []
    for row in jobs:
        item = dict(row)
        item["backend"] = item.pop("agent_backend")
        item["project_id"] = int(row["project_id"]) if row["project_id"] is not None else None
        item["skill_id"] = int(row["skill_id"]) if row["skill_id"] is not None else None
        result.append(item)
    return {"automations": result}


@router.post("/automations", status_code=201)
async def create_automation(request: Request, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    conn = _db(request)
    instruction = str(payload.get("instruction_nl") or "").strip()
    expression = str(payload.get("cron_expr") or "").strip()
    timezone_name = str(payload.get("timezone") or "America/Sao_Paulo")
    backend_value = payload.get("backend", request.app.state.config.agent.default_backend)
    project_id = payload.get("project_id")
    if not instruction or not expression or project_id is None:
        raise HTTPException(status_code=422, detail="instruction_nl, cron_expr e project_id são obrigatórios")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise HTTPException(status_code=422, detail=f"instruction_nl excede {MAX_INSTRUCTION_CHARS} caracteres")
    if not isinstance(backend_value, str):
        raise HTTPException(status_code=422, detail="backend precisa ser texto")
    try:
        backend = normalize_backend(backend_value)
        project_id = int(project_id)
        next_at = cron.validate(expression, timezone_name)
    except (AgentError, TypeError, ValueError, cron.CronError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with conn.execute("SELECT allow_autonomous FROM projects WHERE id = ?", (project_id,)) as cursor:
        project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=404, detail=f"projeto desconhecido: {project_id}")
    if not bool(project["allow_autonomous"]):
        raise HTTPException(status_code=422, detail="habilite autonomia no projeto antes de agendar tarefas")

    skill_id = payload.get("skill_id")
    if skill_id is not None:
        try:
            skill_id = int(skill_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="skill_id inválido") from exc
        async with conn.execute("SELECT 1 FROM skills WHERE id = ?", (skill_id,)) as cursor:
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"skill desconhecida: {skill_id}")

    now = _now()
    cursor = await conn.execute(
        "INSERT INTO cron_jobs (instruction_nl, cron_expr, timezone, project_id, skill_id, agent_backend, status, next_run_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (instruction, expression, timezone_name, project_id, skill_id, backend, next_at, now),
    )
    await conn.commit()
    return await _automation(conn, int(cursor.lastrowid or 0))


@router.get("/automations/{job_id}")
async def get_automation(request: Request, job_id: int) -> dict[str, Any]:
    try:
        return await _automation(_db(request), job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"automação desconhecida: {job_id}") from exc


@router.patch("/automations/{job_id}")
async def update_automation(request: Request, job_id: int, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    conn = _db(request)
    async with conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)) as cursor:
        current = await cursor.fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail=f"automação desconhecida: {job_id}")

    instruction = str(payload.get("instruction_nl", current["instruction_nl"])).strip()
    expression = str(payload.get("cron_expr", current["cron_expr"])).strip()
    timezone_name = str(payload.get("timezone", current["timezone"]))
    backend_value = payload.get("backend", current["agent_backend"])
    status = str(payload.get("status", current["status"]))
    if status not in {"active", "paused"}:
        raise HTTPException(status_code=422, detail="status precisa ser active ou paused")
    if not instruction:
        raise HTTPException(status_code=422, detail="instruction_nl não pode ficar vazio")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise HTTPException(status_code=422, detail=f"instruction_nl excede {MAX_INSTRUCTION_CHARS} caracteres")
    if not isinstance(backend_value, str):
        raise HTTPException(status_code=422, detail="backend precisa ser texto")
    try:
        backend = normalize_backend(backend_value)
        proposed_next = cron.validate(expression, timezone_name)
        next_at = proposed_next if status == "active" else current["next_run_at"]
    except (AgentError, cron.CronError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    skill_id = payload.get("skill_id", current["skill_id"])
    if skill_id is not None:
        try:
            skill_id = int(skill_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="skill_id inválido") from exc
        async with conn.execute("SELECT 1 FROM skills WHERE id = ?", (skill_id,)) as cursor:
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"skill desconhecida: {skill_id}")
    if status == "active":
        async with conn.execute("SELECT allow_autonomous FROM projects WHERE id = ?", (current["project_id"],)) as cursor:
            project = await cursor.fetchone()
        if project is None or not bool(project["allow_autonomous"]):
            raise HTTPException(status_code=422, detail="projeto sem autonomia habilitada")

    await conn.execute(
        "UPDATE cron_jobs SET instruction_nl = ?, cron_expr = ?, timezone = ?, skill_id = ?, agent_backend = ?, status = ?, next_run_at = ? WHERE id = ?",
        (instruction, expression, timezone_name, skill_id, backend, status, next_at, job_id),
    )
    await conn.commit()
    return await _automation(conn, job_id)


@router.get("/automations/{job_id}/runs")
async def automation_runs(
    request: Request,
    job_id: int,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    conn = _db(request)
    async with conn.execute("SELECT 1 FROM cron_jobs WHERE id = ?", (job_id,)) as cursor:
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"automação desconhecida: {job_id}")
    async with conn.execute(
        "SELECT * FROM cron_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
        (job_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    runs = []
    for row in rows:
        item = dict(row)
        try:
            item["log"] = json.loads(item.pop("log_json") or "[]")
        except json.JSONDecodeError:
            item["log"] = []
        runs.append(item)
    return {"runs": runs}


async def _automation(conn: aiosqlite.Connection, job_id: int) -> dict[str, Any]:
    async with conn.execute(
        "SELECT j.*, p.slug AS project_slug, s.name AS skill_name "
        "FROM cron_jobs j LEFT JOIN projects p ON p.id = j.project_id "
        "LEFT JOIN skills s ON s.id = j.skill_id WHERE j.id = ?",
        (job_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise KeyError(job_id)
    item = dict(row)
    item["backend"] = item.pop("agent_backend")
    return item


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
