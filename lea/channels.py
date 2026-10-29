"""Canais Telegram e Discord para iniciar tarefas do agente próprio."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import httpx

from lea import events
from lea.agent import AgentError, AgentService, normalize_backend

logger = logging.getLogger(__name__)

PLATFORMS = {
    "telegram": ("LEA Telegram", "LEA_TELEGRAM_BOT_TOKEN"),
    "discord": ("LEA Discord", "LEA_DISCORD_BOT_TOKEN"),
}
MAX_CHANNEL_TASK_CHARS = 20_000
INBOX_RETENTION_DAYS = 30
_NUMERIC_ID = re.compile(r"^-?\d+$")


class ChannelError(ValueError):
    """Configuração inválida de um canal."""


class _TokenLogFilter(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token and self.token in message:
            record.msg = message.replace(self.token, "[token ocultado]")
            record.args = ()
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def configure_route(
    conn: aiosqlite.Connection,
    *,
    platform: str,
    remote_id: str,
    project_id: int,
    allowed_user_ids: list[str],
    backend: str | None = None,
) -> dict[str, Any]:
    """Cria ou atualiza a rota de um chat; tokens nunca entram no SQLite."""
    if platform not in PLATFORMS:
        raise ChannelError("platform precisa ser telegram ou discord")
    remote_id = str(remote_id).strip()
    numeric_id = bool(_NUMERIC_ID.fullmatch(remote_id))
    if remote_id != "*" and (not numeric_id or (platform == "discord" and remote_id.startswith("-"))):
        raise ChannelError("chat_id precisa ser numérico; use * apenas para mensagens privadas")
    users = sorted({str(user).strip() for user in allowed_user_ids if str(user).strip()})
    if not users or any(not user.isdigit() for user in users):
        raise ChannelError("informe ao menos um --user-id numérico autorizado")
    if backend is not None:
        if not isinstance(backend, str):
            raise ChannelError("backend precisa ser texto")
        try:
            backend = normalize_backend(backend)
        except AgentError as exc:
            raise ChannelError(str(exc)) from exc

    async with conn.execute(
        "SELECT allow_autonomous FROM projects WHERE id = ?", (project_id,)
    ) as cursor:
        project = await cursor.fetchone()
    if project is None:
        raise ChannelError(f"projeto desconhecido: {project_id}")
    if not bool(project["allow_autonomous"]):
        raise ChannelError("habilite allow_autonomous no projeto antes de conectar um canal")

    display = PLATFORMS[platform][0]
    async with conn.execute(
        "SELECT config_json FROM channels WHERE platform = ? AND display = ?",
        (platform, display),
    ) as cursor:
        row = await cursor.fetchone()
    try:
        config = json.loads(row["config_json"] or "{}") if row else {}
    except (TypeError, json.JSONDecodeError):
        config = {}
    routes = config.get("routes")
    if not isinstance(routes, dict):
        routes = {}
    routes[remote_id] = {
        "project_id": project_id,
        "allowed_user_ids": users,
        "backend": backend,
    }
    payload = json.dumps({"routes": routes}, ensure_ascii=False)
    await conn.execute(
        "INSERT INTO channels (platform, display, config_json, status, last_error) "
        "VALUES (?, ?, ?, 'configured', NULL) "
        "ON CONFLICT(platform, display) DO UPDATE SET "
        "config_json = excluded.config_json, status = 'configured', last_error = NULL",
        (platform, display, payload),
    )
    await conn.commit()
    return {"platform": platform, "remote_id": remote_id, **routes[remote_id]}


async def remove_route(
    conn: aiosqlite.Connection,
    *,
    platform: str,
    remote_id: str,
) -> bool:
    if platform not in PLATFORMS:
        raise ChannelError("platform precisa ser telegram ou discord")
    display = PLATFORMS[platform][0]
    async with conn.execute(
        "SELECT config_json FROM channels WHERE platform = ? AND display = ?",
        (platform, display),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False
    try:
        config = json.loads(row["config_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        config = {}
    routes = config.get("routes")
    if not isinstance(routes, dict) or remote_id not in routes:
        return False
    del routes[remote_id]
    await conn.execute(
        "UPDATE channels SET config_json = ?, status = ?, last_error = NULL "
        "WHERE platform = ? AND display = ?",
        (
            json.dumps({"routes": routes}, ensure_ascii=False),
            "configured" if routes else "disconnected",
            platform,
            display,
        ),
    )
    await conn.commit()
    return True


async def list_channels(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT platform, display, config_json, status, last_error, connected_at "
        "FROM channels ORDER BY platform, display"
    ) as cursor:
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        try:
            config = json.loads(row["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
        routes = config.get("routes") if isinstance(config, dict) else {}
        if not isinstance(routes, dict):
            routes = {}
        result.append(
            {
                "platform": row["platform"],
                "display": row["display"],
                "status": row["status"],
                "last_error": row["last_error"],
                "connected_at": row["connected_at"],
                "routes": [
                    {
                        "remote_id": remote_id,
                        "project_id": route.get("project_id"),
                        "allowed_user_ids": route.get("allowed_user_ids", []),
                        "backend": route.get("backend"),
                    }
                    for remote_id, route in sorted(routes.items())
                    if isinstance(route, dict)
                ],
            }
        )
    return result


class ChannelManager:
    """Mantém os receptores e encaminha mensagens autorizadas ao agente."""

    def __init__(self, conn: aiosqlite.Connection, agent: AgentService, bus: events.EventBus) -> None:
        self.conn = conn
        self.agent = agent
        self.bus = bus
        self._pollers: list[asyncio.Task[None]] = []
        self._workers: set[asyncio.Task[None]] = set()
        self._event_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._recovered: set[str] = set()
        self._token_log_filters: dict[
            str,
            tuple[list[logging.Logger | logging.Handler], _TokenLogFilter],
        ] = {}

    async def start(self) -> None:
        self._stopping = False
        for platform, (display, token_env) in PLATFORMS.items():
            routes = await self._routes(platform)
            if not routes:
                continue
            token = os.environ.get(token_env, "").strip()
            if not token:
                await self._set_status(
                    platform,
                    "needs_token",
                    f"defina a variável de ambiente {token_env}",
                )
                continue
            runner = self._telegram_loop if platform == "telegram" else self._discord_loop
            task = asyncio.create_task(runner(token), name=f"lea-channel:{platform}")
            self._pollers.append(task)
        if self._pollers:
            self._event_task = asyncio.create_task(self._events_loop(), name="lea-channel-events")

    async def shutdown(self) -> None:
        self._stopping = True
        tasks = [*self._pollers, *self._workers]
        if self._event_task is not None:
            tasks.append(self._event_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pollers.clear()
        self._workers.clear()
        self._event_task = None
        for loggers, log_filter in self._token_log_filters.values():
            for logger_or_handler in loggers:
                logger_or_handler.removeFilter(log_filter)
        self._token_log_filters.clear()

    def _protect_token_logs(self, token: str) -> None:
        if token in self._token_log_filters:
            return
        log_filter = _TokenLogFilter(token)
        loggers: list[logging.Logger | logging.Handler] = [
            logging.getLogger("httpx"),
            logging.getLogger("httpcore"),
            *logging.getLogger().handlers,
        ]
        for logger_or_handler in loggers:
            logger_or_handler.addFilter(log_filter)
        self._token_log_filters[token] = (loggers, log_filter)

    async def _routes(self, platform: str) -> dict[str, dict[str, Any]]:
        display = PLATFORMS[platform][0]
        async with self.conn.execute(
            "SELECT config_json FROM channels WHERE platform = ? AND display = ?",
            (platform, display),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {}
        try:
            config = json.loads(row["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        routes = config.get("routes") if isinstance(config, dict) else None
        return routes if isinstance(routes, dict) else {}

    async def _set_status(self, platform: str, status: str, error: str | None = None) -> None:
        display = PLATFORMS[platform][0]
        await self.conn.execute(
            "UPDATE channels SET status = ?, last_error = ?, "
            "connected_at = CASE WHEN ? = 'connected' THEN ? ELSE connected_at END "
            "WHERE platform = ? AND display = ?",
            (status, error, status, _now(), platform, display),
        )
        await self.conn.commit()

    def _spawn_worker(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _recover(self, platform: str) -> None:
        if platform in self._recovered:
            return
        self._recovered.add(platform)
        async with self.conn.execute(
            "SELECT id, remote_id, session_id FROM channel_inbox "
            "WHERE platform = ? AND status = 'running' ORDER BY id",
            (platform,),
        ) as cursor:
            interrupted = await cursor.fetchall()
        for row in interrupted:
            await self.conn.execute(
                "UPDATE channel_inbox SET status = 'failed', finished_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            await self._send(
                platform,
                row["remote_id"],
                "A tarefa da sessão "
                f"{row['session_id'] or 'desconhecida'} foi interrompida porque o daemon reiniciou.",
            )
        await self.conn.execute(
            "DELETE FROM channel_inbox WHERE status IN ('completed', 'failed') "
            "AND finished_at < datetime('now', ?) ",
            (f"-{INBOX_RETENTION_DAYS} days",),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM channel_inbox WHERE platform = ? AND status = 'queued' ORDER BY id",
            (platform,),
        ) as cursor:
            queued = await cursor.fetchall()
        for row in queued:
            self._spawn_worker(self._process(int(row["id"])), f"lea-channel-inbox:{row['id']}")

    async def _telegram_loop(self, token: str) -> None:
        import httpx

        self._protect_token_logs(token)
        base = f"https://api.telegram.org/bot{token}/"
        offset: int | None = None
        while not self._stopping:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(65, connect=10)) as client:
                    me = await client.get(base + "getMe")
                    bot = _telegram_result(me, token)
                    username = str(bot.get("username") or "")
                    await self._set_status("telegram", "connected")
                    await self._recover("telegram")
                    while not self._stopping:
                        params: dict[str, Any] = {
                            "timeout": 50,
                            "allowed_updates": ["message"],
                        }
                        if offset is not None:
                            params["offset"] = offset
                        response = await client.post(
                            base + "getUpdates",
                            json=params,
                        )
                        updates = _telegram_result(response, token)
                        for update in updates:
                            if not isinstance(update, dict):
                                continue
                            update_id = update.get("update_id")
                            message = update.get("message")
                            if isinstance(message, dict):
                                await self._accept_telegram_message(message, username)
                            if isinstance(update_id, int):
                                offset = max(offset or 0, update_id + 1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = _redact(str(exc), token)[:500] or type(exc).__name__
                logger.warning("canal Telegram desconectado (%s): %s", type(exc).__name__, detail)
                await self._set_status("telegram", "error", detail)
                await asyncio.sleep(5)

    async def _accept_telegram_message(self, message: dict[str, Any], username: str) -> None:
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict) or sender.get("is_bot"):
            return
        remote_id = str(chat.get("id", ""))
        user_id = str(sender.get("id", ""))
        is_private = chat.get("type") == "private"
        route = await self._route("telegram", remote_id, user_id, is_private)
        if route is None:
            return
        text = message.get("text")
        if not isinstance(text, str):
            await self._send("telegram", remote_id, "Por enquanto, envie uma mensagem de texto.")
            return
        task_text = _telegram_task(text, username, is_private)
        if task_text is None:
            return
        if task_text == "__help__":
            await self._send(
                "telegram",
                remote_id,
                "Envie uma tarefa no privado ou use /lea <tarefa> em grupos. "
                "Para revisar aprovações: /lea approvals, /lea approve <id> ou /lea deny <id>.",
            )
            return
        await self._handle_message(
            platform="telegram",
            message_key=f"update:{message.get('message_id')}:{remote_id}",
            remote_id=remote_id,
            user_id=user_id,
            content=task_text,
            is_private=is_private,
            preloaded_route=route,
        )

    async def _discord_loop(self, token: str) -> None:
        self._protect_token_logs(token)
        try:
            import discord
        except ImportError:
            detail = "instale o extra Discord com: uv sync --extra discord"
            await self._set_status("discord", "error", detail)
            logger.warning("canal Discord configurado, mas %s", detail)
            return

        while not self._stopping:
            intents = discord.Intents.none()
            intents.guilds = True
            intents.guild_messages = True
            intents.dm_messages = True
            intents.message_content = True
            client = discord.Client(intents=intents)

            @client.event
            async def on_ready() -> None:
                await self._set_status("discord", "connected")
                await self._recover("discord")
                logger.info("canal Discord conectado como %s", client.user)

            @client.event
            async def on_message(message: Any) -> None:
                if message.author.bot:
                    return
                is_private = message.guild is None
                route = await self._route(
                    "discord",
                    str(message.channel.id),
                    str(message.author.id),
                    is_private,
                )
                if route is None:
                    return
                content = _discord_task(message.content, str(client.user.id), is_private)
                if content is None:
                    return
                if content == "__help__":
                    await self._send(
                        "discord",
                        str(message.channel.id),
                        "Envie a tarefa no privado ou mencione o bot com a tarefa no canal. "
                        "Use `!lea approvals`, `!lea approve <id>` ou "
                        "`!lea deny <id>` para aprovações.",
                    )
                    return
                await self._handle_message(
                    platform="discord",
                    message_key=f"message:{message.id}",
                    remote_id=str(message.channel.id),
                    user_id=str(message.author.id),
                    content=content,
                    is_private=is_private,
                    preloaded_route=route,
                )

            try:
                await self._set_status("discord", "connecting")
                await client.start(token, reconnect=True)
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception as exc:
                detail = _redact(str(exc), token)[:500] or type(exc).__name__
                logger.warning("canal Discord desconectado (%s): %s", type(exc).__name__, detail)
                await self._set_status("discord", "error", detail)
                await asyncio.sleep(5)
            finally:
                if not client.is_closed():
                    await client.close()

    async def _route(
        self,
        platform: str,
        remote_id: str,
        user_id: str,
        is_private: bool,
    ) -> dict[str, Any] | None:
        routes = await self._routes(platform)
        route = routes.get(remote_id)
        if route is None and is_private:
            route = routes.get("*")
        if not isinstance(route, dict):
            return None
        allowed = route.get("allowed_user_ids")
        if not isinstance(allowed, list) or user_id not in {str(item) for item in allowed}:
            return None
        try:
            project_id = int(route.get("project_id"))
        except (TypeError, ValueError):
            return None
        return {**route, "project_id": project_id}

    async def _handle_message(
        self,
        *,
        platform: str,
        message_key: str,
        remote_id: str,
        user_id: str,
        content: str,
        is_private: bool,
        preloaded_route: dict[str, Any] | None = None,
    ) -> None:
        route = preloaded_route or await self._route(platform, remote_id, user_id, is_private)
        if route is None:
            return
        if not content.strip():
            return
        if len(content) > MAX_CHANNEL_TASK_CHARS:
            await self._send(platform, remote_id, "A tarefa excede o limite de 20.000 caracteres.")
            return
        if await self._approval_command(platform, remote_id, user_id, route, content):
            return

        try:
            backend = route.get("backend")
            if backend is not None:
                backend = normalize_backend(str(backend))
        except AgentError:
            backend = None
        cursor = await self.conn.execute(
            "INSERT OR IGNORE INTO channel_inbox "
            "(platform, message_key, remote_id, user_id, project_id, backend, content, "
            "status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (
                platform,
                message_key,
                remote_id,
                user_id,
                route["project_id"],
                backend,
                content,
                _now(),
            ),
        )
        await self.conn.commit()
        if cursor.rowcount != 1:
            return
        inbox_id = int(cursor.lastrowid or 0)
        await self._send(platform, remote_id, "Recebi a tarefa. Vou executá-la agora.")
        self._spawn_worker(self._process(inbox_id), f"lea-channel-inbox:{inbox_id}")

    async def _approval_command(
        self,
        platform: str,
        remote_id: str,
        user_id: str,
        route: dict[str, Any],
        content: str,
    ) -> bool:
        words = content.strip().split()
        if not words:
            return False
        command = words[0].lower().lstrip("/")
        if command in {"approvals", "aprovacoes", "aprovações"} and len(words) == 1:
            async with self.conn.execute(
                "SELECT a.id, a.tool_name, a.reason FROM agent_approvals a "
                "JOIN sessions s ON s.id = a.session_id "
                "WHERE a.status = 'pending' AND s.project_id = ? AND EXISTS ("
                "SELECT 1 FROM channel_inbox i WHERE i.session_id = s.id "
                "AND i.platform = ? AND i.remote_id = ?) ORDER BY a.requested_at",
                (route["project_id"], platform, remote_id),
            ) as cursor:
                approvals = await cursor.fetchall()
            if approvals:
                lines = [f"{row['id']} · {row['tool_name']}: {row['reason']}" for row in approvals]
                reply = "Aprovações pendentes:\n" + "\n".join(lines)
            else:
                reply = "Não há aprovações pendentes deste canal e projeto."
            await self._send(platform, remote_id, reply)
            return True
        if command not in {"approve", "aprovar", "deny", "recusar"}:
            return False
        if len(words) != 2:
            await self._send(platform, remote_id, "Use approve <id> ou deny <id>.")
            return True
        approval_id = words[1]
        decision = "approve" if command in {"approve", "aprovar"} else "deny"
        async with self.conn.execute(
            "SELECT a.id FROM agent_approvals a JOIN sessions s ON s.id = a.session_id "
            "WHERE a.id = ? AND a.status = 'pending' AND s.project_id = ? AND EXISTS ("
            "SELECT 1 FROM channel_inbox i WHERE i.session_id = s.id "
            "AND i.platform = ? AND i.remote_id = ?)",
            (approval_id, route["project_id"], platform, remote_id),
        ) as cursor:
            approval = await cursor.fetchone()
        if approval is None:
            await self._send(
                platform,
                remote_id,
                "Aprovação pendente não encontrada para este canal e projeto.",
            )
            return True
        session_id = await self.agent.decide_approval(
            approval_id,
            decision,
            decided_by=f"{platform}:{user_id}",
        )
        if session_id is None:
            await self._send(platform, remote_id, "A aprovação já foi decidida ou expirou.")
        else:
            label = "aprovada" if decision == "approve" else "recusada"
            await self._send(platform, remote_id, f"Ação {label}; sessão {session_id} retomada.")
        return True

    async def _process(self, inbox_id: int) -> None:
        async with self.conn.execute(
            "SELECT * FROM channel_inbox WHERE id = ?",
            (inbox_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["status"] not in {"queued", "running"}:
            return
        if row["status"] != "queued":
            return
        platform = str(row["platform"])
        remote_id = str(row["remote_id"])
        try:
            claim = await self.conn.execute(
                "UPDATE channel_inbox SET status = 'running' WHERE id = ? AND status = 'queued'",
                (inbox_id,),
            )
            await self.conn.commit()
            if claim.rowcount != 1:
                return
            session, runner = await self.agent.start(
                project_id=int(row["project_id"]),
                task=str(row["content"]),
                backend=row["backend"],
                source=f"channel:{platform}",
            )
            await self.conn.execute(
                "UPDATE channel_inbox SET session_id = ? WHERE id = ? AND status = 'running'",
                (session["id"], inbox_id),
            )
            await self.conn.commit()
            result = await runner
            summary = str(result.get("summary") or "A tarefa terminou sem resumo.")
            final_status = str(result.get("status") or "error")
            label = "Concluído" if final_status == "ended" else "Encerrado com erro"
            response = f"{label} · sessão {session['id']}\n{summary}"
            await self._send(platform, remote_id, response)
            await self.conn.execute(
                "UPDATE channel_inbox SET status = ?, finished_at = ? WHERE id = ?",
                ("completed" if final_status == "ended" else "failed", _now(), inbox_id),
            )
            await self.conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            secret = os.environ.get(PLATFORMS[platform][1], "")
            detail = _redact(str(exc), secret)[:1000] or type(exc).__name__
            logger.warning(
                "mensagem %s no canal %s falhou (%s)", inbox_id, platform, type(exc).__name__
            )
            await self._send(
                platform,
                remote_id,
                f"Não consegui iniciar ou concluir a tarefa: {detail}",
            )
            await self.conn.execute(
                "UPDATE channel_inbox SET status = 'failed', finished_at = ? WHERE id = ?",
                (_now(), inbox_id),
            )
            await self.conn.commit()

    async def _send(self, platform: str, remote_id: str, text: str) -> None:
        token_env = PLATFORMS[platform][1]
        token = os.environ.get(token_env, "").strip()
        if not token:
            return
        chunks = _chunks(text, 4000 if platform == "telegram" else 1900)
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
            for chunk in chunks:
                try:
                    if platform == "telegram":
                        response = await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": remote_id, "text": chunk},
                        )
                        _telegram_result(response, token)
                    else:
                        response = await client.post(
                            f"https://discord.com/api/v10/channels/{remote_id}/messages",
                            headers={"Authorization": f"Bot {token}"},
                            json={"content": chunk, "allowed_mentions": {"parse": []}},
                        )
                        if response.status_code == 429:
                            data = response.json()
                            await asyncio.sleep(min(float(data.get("retry_after", 1)), 10))
                            response = await client.post(
                                f"https://discord.com/api/v10/channels/{remote_id}/messages",
                                headers={"Authorization": f"Bot {token}"},
                                json={"content": chunk, "allowed_mentions": {"parse": []}},
                            )
                        response.raise_for_status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = _redact(str(exc), token)[:400] or type(exc).__name__
                    logger.warning(
                        "não consegui responder no canal %s (%s): %s",
                        platform,
                        type(exc).__name__,
                        detail,
                    )
                    return

    async def _events_loop(self) -> None:
        subscription = self.bus.subscribe()
        try:
            async for event in subscription:
                if event.type != "alert" or event.payload.get("event") != "approval_requested":
                    continue
                if not event.session_id:
                    continue
                async with self.conn.execute(
                    "SELECT platform, remote_id FROM channel_inbox "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (event.session_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    for _ in range(4):
                        await asyncio.sleep(0.05)
                        async with self.conn.execute(
                            "SELECT platform, remote_id FROM channel_inbox "
                            "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                            (event.session_id,),
                        ) as cursor:
                            row = await cursor.fetchone()
                        if row is not None:
                            break
                if row is None:
                    continue
                payload = event.payload
                command = "!lea approve" if row["platform"] == "discord" else "/lea approve"
                denial = "!lea deny" if row["platform"] == "discord" else "/lea deny"
                await self._send(
                    row["platform"],
                    row["remote_id"],
                    "A tarefa precisa da sua aprovação.\n"
                    f"Comando: {payload.get('command') or payload.get('tool')}\n"
                    f"Aprovar: {command} {payload['approval_id']} · "
                    f"Recusar: {denial} {payload['approval_id']}",
                )
        finally:
            subscription.close()


def _telegram_result(response: httpx.Response, token: str) -> Any:
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelError("resposta inválida da API do Telegram") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else None
        raise ChannelError(
            _redact(str(description or "erro retornado pela API do Telegram"), token)
        )
    return data.get("result")


def _telegram_task(text: str, username: str, is_private: bool) -> str | None:
    value = text.strip()
    if value in {"/start", "/help"} or value.startswith("/help@"):
        return "__help__"
    patterns = [r"^/lea(?:@[A-Za-z0-9_]+)?\s*", r"^/ask(?:@[A-Za-z0-9_]+)?\s*"]
    if username:
        patterns.append(rf"^@{re.escape(username)}\s*")
    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)
        if match:
            remainder = value[match.end():].strip()
            return remainder or "__help__"
    if is_private:
        return value
    return None


def _discord_task(text: str, bot_id: str, is_private: bool) -> str | None:
    value = text.strip()
    if value.lower() in {"!lea", "!lea help", "/lea", "/lea help"}:
        return "__help__"
    mention = re.match(rf"^<@!?{re.escape(bot_id)}>\s*", value)
    if mention:
        remainder = value[mention.end():].strip()
        return remainder or "__help__"
    prefix = re.match(r"^!lea\s+", value, flags=re.IGNORECASE)
    if prefix:
        remainder = value[prefix.end():].strip()
        return remainder or "__help__"
    return value if is_private else None


def _chunks(value: str, maximum: int) -> list[str]:
    value = value.strip() or "(sem conteúdo)"
    chunks = []
    while len(value) > maximum:
        split_at = value.rfind("\n", 0, maximum)
        if split_at < maximum // 2:
            split_at = maximum
        chunks.append(value[:split_at].strip())
        value = value[split_at:].strip()
    if value:
        chunks.append(value)
    return chunks


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "[token ocultado]") if secret else value
