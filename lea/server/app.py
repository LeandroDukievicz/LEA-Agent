"""O daemon: API local, WebSocket de eventos e receptor de hooks.

Por que existe um servidor num programa desktop
-----------------------------------------------
A interface **não** é o dono do estado -- ela é um cliente. Isso vem de uma
observação sobre o Hermes Agent, que organiza o produto assim e por isso
consegue ter janela, TUI e painel web sobre o mesmo núcleo, além de rodar sem
janela nenhuma num servidor.

Para o LEA isso resolve três coisas de uma vez:

1. ``lea daemon`` roda headless no homelab, sem interface.
2. Os hooks dos harnesses são processos de terceiros (Claude Code, Codex) que
   precisam alcançar o LEA de fora -- eles não têm como chamar função Python
   nossa, mas sabem fazer um POST.
3. Trocar a camada de interface depois não mexe no produto.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Annotated, Any

import aiosqlite
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

from lea import __version__, events, paths, status
from lea.config import Config, load
from lea.db import session as db
from lea.memory import Memory
from lea import handoffs
from lea import skills
from lea.agent import AgentService
from lea.cron import CronScheduler
from lea.memory.inject import render as render_context
from lea.security import tokens
from lea.server import agent_routes, handoff_routes, memory_routes, session_routes
from lea.sessions.launcher import Launcher

logger = logging.getLogger(__name__)

#: Cabeçalho que carrega o token da interface.
UI_TOKEN_HEADER = "X-Lea-Token"

#: Cabeçalho que carrega o token dos hooks. Separado do da UI de propósito.
HOOK_TOKEN_HEADER = "X-Lea-Hook-Token"

#: Eventos de ciclo de vida que um harness relata -> tipo gravado em `events`.
#: O vocabulário da coluna é fechado por CHECK, então o mapa é o que impede um
#: nome de hook novo virar erro de constraint em tempo de execução.
_HOOK_EVENT_TYPES = {
    "SessionStart": "session_start",
    "PostToolUse": "tool_call",
    "PreCompact": "memory",
    "Stop": "session_end",
}

HOOK_EVENTS = frozenset(_HOOK_EVENT_TYPES)


async def _session_exists(conn: aiosqlite.Connection, session_id: str) -> bool:
    async with conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) as cursor:
        return await cursor.fetchone() is not None


async def _handoff_on_stop(request: Request, session_id: str) -> dict[str, Any] | None:
    """Gera o handoff quando uma sessão encerra.

    Falhar aqui nunca propaga: quem está do outro lado é o agente de código
    terminando, e um erro nosso não pode virar erro de hook na sessão dele.
    """
    try:
        handoff = await handoffs.generate(
            request.app.state.db,
            request.app.state.memory,
            session_id,
            model=request.app.state.config.agent.default_model,
            reason="sessão encerrada pelo agente",
        )
    except Exception:
        logger.exception("não consegui gerar o handoff de %s", session_id)
        return None

    request.app.state.bus.emit(
        "session_end",
        {"handoff": handoff.id, "title": handoff.title},
        session_id=session_id,
    )
    return handoff.as_dict()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepara o que vive enquanto o daemon roda."""
    paths.ensure_layout()
    config = load()

    app.state.config = config
    app.state.bus = events.bus
    app.state.db = await db.connect()
    app.state.ui_token = tokens.ui_token()
    app.state.hook_token = tokens.hook_token()

    memory = Memory(
        app.state.db,
        config.memory.wiki_path,
        half_lives=config.memory.half_life_days.model_dump(),
    )
    await memory.setup()
    app.state.memory = memory
    skill_sync = await skills.sync_from_disk(app.state.db)
    if skill_sync["problems"]:
        logger.warning("%d skill(s) não puderam ser indexadas", len(skill_sync["problems"]))
    app.state.launcher = Launcher(app.state.db, memory, events.bus)
    app.state.agent = AgentService(app.state.db, memory, config, events.bus)
    app.state.cron = CronScheduler(app.state.db, app.state.agent, events.bus)
    await app.state.cron.start()

    status.prime()

    if config.server.is_exposed:
        logger.warning(
            "server.host=%s aceita conexão de fora desta máquina, e as ferramentas "
            "do agente executam comandos aqui",
            config.server.host,
        )

    events.bus.emit("session_start", {"scope": "daemon", "version": __version__})
    try:
        yield
    finally:
        await app.state.cron.shutdown()
        await app.state.agent.shutdown()
        # Ordem importa: as sessões param (e suas tasks de PTY morrem) antes
        # de o banco fechar. Ao contrário, cada sessão viva registra um
        # "no active connection" na saída.
        await app.state.launcher.shutdown()
        events.bus.emit("session_end", {"scope": "daemon"})
        await app.state.db.close()


app = FastAPI(
    title="LEA",
    version=__version__,
    lifespan=lifespan,
    # O daemon é local; não há navegador de terceiro para documentar API.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ── autenticação ─────────────────────────────────────────────────────────


def require_ui_token(
    request: Request,
    x_lea_token: Annotated[str | None, Header(alias=UI_TOKEN_HEADER)] = None,
) -> None:
    """Exige o token da interface.

    Escutar em 127.0.0.1 não autentica: qualquer processo da máquina alcança a
    porta, inclusive uma página aberta no navegador.
    """
    if not tokens.matches(request.app.state.ui_token, x_lea_token):
        raise HTTPException(status_code=401, detail="token inválido")


def require_hook_token(
    request: Request,
    x_lea_hook_token: Annotated[str | None, Header(alias=HOOK_TOKEN_HEADER)] = None,
) -> None:
    if not tokens.matches(request.app.state.hook_token, x_lea_hook_token):
        raise HTTPException(status_code=401, detail="token de hook inválido")


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_config(request: Request) -> Config:
    return request.app.state.config


# ── rotas ────────────────────────────────────────────────────────────────


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Sem autenticação: serve para a interface descobrir se o daemon subiu.

    Só diz que está vivo e em que versão -- nada do conteúdo do usuário.
    """
    return {"status": "ok", "version": __version__}


@app.get("/api/overview", dependencies=[Depends(require_ui_token)])
async def overview(
    conn: Annotated[aiosqlite.Connection, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
) -> dict[str, Any]:
    """Dados da tela Overview e da barra de status."""
    return {
        "version": __version__,
        "system": status.system_stats().as_dict(),
        "counters": await status.counters(conn),
        "cost": {
            "today_brl": await status.cost_today_brl(conn),
            "daily_budget_brl": config.costs.daily_budget_brl,
        },
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "exposed": config.server.is_exposed,
        },
        "ui": config.ui.model_dump(),
    }


@app.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    """Fluxo de eventos globais: feed de atividade, status, alertas.

    O token vem por query string porque a API de WebSocket do navegador não
    deixa definir cabeçalho no handshake. Isso é aceitável aqui -- a URL não
    sai desta máquina e não há servidor de terceiro para registrá-la em log.
    """
    expected = websocket.app.state.ui_token
    if not tokens.matches(expected, websocket.query_params.get("token")):
        await websocket.close(code=1008, reason="token inválido")
        return

    await websocket.accept()
    subscription = websocket.app.state.bus.subscribe()
    try:
        async for event in subscription:
            await websocket.send_json(event.as_dict())
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket caiu com erro")
    finally:
        if subscription.dropped:
            logger.warning("assinante perdeu %d eventos por fila cheia", subscription.dropped)
        subscription.close()


@app.post("/hooks/{event_name}", dependencies=[Depends(require_hook_token)])
async def receive_hook(event_name: str, request: Request) -> dict[str, Any]:
    """Recebe um evento de ciclo de vida de um harness.

    Quem chama é o comando ``lea hook``, rodando dentro do processo do agente
    de código. Todo evento é publicado no barramento; o ``SessionStart``
    também devolve o bloco de memória a injetar. O ``Stop`` passa a gerar
    handoff na Fase 4.
    """
    if event_name not in HOOK_EVENTS:
        raise HTTPException(status_code=404, detail=f"evento desconhecido: {event_name}")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw": payload}

    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None

    event_type = _HOOK_EVENT_TYPES[event_name]
    body = {"hook": event_name, **payload}
    response_handoff: dict[str, Any] | None = None

    # Persistimos só o que pertence a uma sessão conhecida nossa. Um hook de
    # uma sessão que o usuário abriu no terminal, fora do LEA, continua
    # aparecendo no feed ao vivo, mas não vira linha órfã no banco -- a
    # foreign key recusaria, e o evento não tem a quem pertencer.
    if session_id and await _session_exists(request.app.state.db, session_id):
        await request.app.state.db.execute(
            "INSERT INTO events (session_id, at, type, payload_json) VALUES (?, ?, ?, ?)",
            (
                session_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                event_type,
                json.dumps(body, ensure_ascii=False),
            ),
        )
        if event_name == "Stop":
            await request.app.state.db.execute(
                "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ? "
                "AND status NOT IN ('ended', 'handoff')",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), session_id),
            )
        await request.app.state.db.commit()

        if event_name == "Stop":
            handoff = await _handoff_on_stop(request, session_id)
            if handoff:
                response_handoff = handoff

    request.app.state.bus.emit(event_type, body, session_id=session_id)

    response: dict[str, Any] = {"ok": True, "event": event_name}
    if response_handoff is not None:
        response["handoff"] = response_handoff

    if event_name == "SessionStart":
        # O agente que está subindo recebe o bloco de memória para injetar.
        # É o momento em que a wiki vira contexto de uma sessão nova.
        memory: Memory = request.app.state.memory
        config: Config = request.app.state.config
        selection = await memory.context_for(
            project_id=payload.get("project_id"),
            task=str(payload.get("task") or ""),
            budget_tokens=config.memory.inject_budget_tokens,
        )
        response["context"] = render_context(selection)
        response["memory"] = selection.as_dict()

    return response


# Rotas de memória: tela Memória, launcher e manutenção do índice.
app.include_router(memory_routes.router, dependencies=[Depends(require_ui_token)])
app.include_router(agent_routes.router, dependencies=[Depends(require_ui_token)])

# Projetos, harnesses e sessões. O WebSocket do terminal autentica por conta
# própria (query string), então fica fora da dependência de cabeçalho.
# Handoffs ANTES de sessões: `/api/sessions/{id}/handoff` precisa ser casada
# antes de `/api/sessions/{id}/{action}`, que capturaria "handoff" como ação
# desconhecida. O FastAPI casa na ordem de registro.
app.include_router(handoff_routes.router, dependencies=[Depends(require_ui_token)])
app.include_router(session_routes.router, dependencies=[Depends(require_ui_token)])
