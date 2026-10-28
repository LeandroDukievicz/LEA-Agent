"""Launcher: transforma "quero rodar o Claude Code nisto" numa sessão viva.

A sequência de boot é a parte visível: a interface desenha cada passo como
uma linha ✓ no console enquanto acontece. A ordem é a do protótipo --
comando, worktree, hooks, MCP, memória injetada, tarefa, sessão ativa -- e
existe para o usuário ver *onde* parou quando algo falha.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from lea import harnesses, paths
from lea.events import EventBus
from lea.memory import Memory
from lea.memory.inject import render as render_context
from lea.sessions import pty, worktree

logger = logging.getLogger(__name__)

#: Variáveis que o processo filho recebe, para os hooks saberem quem são.
ENV_SESSION = "LEA_SESSION_ID"
ENV_PROJECT = "LEA_PROJECT"


def new_session_id() -> str:
    """Id curto no formato do protótipo (``s-7f3a``)."""
    return f"s-{secrets.token_hex(2)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LaunchError(RuntimeError):
    """A sessão não pôde ser iniciada."""


@dataclass
class Running:
    """Uma sessão viva, com o processo e o que precisamos para controlá-la."""

    id: str
    spawned: pty.Spawned
    project_id: int | None
    harness: str
    worktree_path: str | None = None
    #: Assinantes do fluxo do terminal (a interface pode ter mais de uma aba).
    output: list[asyncio.Queue[bytes]] = field(default_factory=list)
    #: Tudo que o processo já escreveu, para uma aba que conecta depois
    #: conseguir redesenhar a tela em vez de começar em branco.
    scrollback: bytearray = field(default_factory=bytearray)

    #: Teto do histórico guardado em memória, por sessão.
    SCROLLBACK_LIMIT: int = 256 * 1024

    def publish(self, chunk: bytes) -> None:
        self.scrollback.extend(chunk)
        if len(self.scrollback) > self.SCROLLBACK_LIMIT:
            del self.scrollback[: len(self.scrollback) - self.SCROLLBACK_LIMIT]
        for queue in list(self.output):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # Aba lenta não pode segurar a saída do agente.
                pass


class Launcher:
    """Cria e guarda as sessões vivas deste daemon."""

    def __init__(self, conn: aiosqlite.Connection, memory: Memory, bus: EventBus) -> None:
        self.conn = conn
        self.memory = memory
        self.bus = bus
        self.running: dict[str, Running] = {}
        self._pumps: set[asyncio.Task[None]] = set()
        self._closing = False

    async def shutdown(self, grace: float = 3.0) -> None:
        """Encerra as sessões vivas antes do daemon sair.

        Sem isto, as tasks que leem o PTY sobrevivem ao fim do ``lifespan`` e
        tentam gravar num banco já fechado -- o que acontece em todo Ctrl-C.
        A flag ``_closing`` faz o ``_finish`` parar de tocar no banco a partir
        daqui; o que importa gravar já foi gravado quando a sessão iniciou.
        """
        self._closing = True

        for running in list(self.running.values()):
            try:
                await pty.stop(running.spawned, grace=grace)
            except Exception:
                logger.warning("falha ao encerrar sessão %s", running.id, exc_info=True)

        for task in list(self._pumps):
            task.cancel()
        if self._pumps:
            await asyncio.gather(*self._pumps, return_exceptions=True)
        self._pumps.clear()
        self.running.clear()

    # ── criação ──────────────────────────────────────────────────────────

    async def launch(
        self,
        *,
        harness_id: str,
        project_id: int | None = None,
        task: str = "",
        mode: str = "interactive",
        branch: str | None = None,
        use_worktree: bool = False,
        budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Inicia uma sessão e devolve o estado inicial dela."""
        if not pty.supported():
            raise LaunchError(
                "terminal interativo exige PTY POSIX; no Windows isso chega na Fase 7"
            )

        harness = harnesses.get(harness_id)
        if harness is None:
            raise LaunchError(f"harness desconhecido: {harness_id}")
        info = harness.detect()
        if info is None:
            raise LaunchError(f"{harness.name} não está instalado nesta máquina")

        project = await self._project(project_id)
        session_id = new_session_id()
        steps: list[dict[str, Any]] = []

        # 1. comando
        command = harness.launch_cmd(task=task or None)
        steps.append(_step("comando", " ".join(command)))

        # 2. worktree isolada, quando pedida
        cwd = project["path"] if project else str(Path.home())
        tree_path: str | None = None
        if use_worktree:
            if not project:
                raise LaunchError("worktree exige um projeto")
            tree_path = str(worktree.path_for(session_id))
            created_branch = branch or f"lea/{session_id}"
            try:
                await worktree.create(project["path"], tree_path, created_branch)
            except worktree.WorktreeError as exc:
                raise LaunchError(str(exc)) from exc
            cwd = tree_path
            steps.append(_step("worktree", f"{tree_path} ({created_branch})"))
        elif project:
            steps.append(_step("diretório", cwd))

        # 3. integração do harness
        state = await harness.status(harnesses.hook_command())
        steps.append(
            _step(
                "hooks",
                f"{state.hooks.value} · mcp {state.mcp.value}",
                ok=state.hooks.value == "ok",
            )
        )
        for note in state.notes:
            steps.append(_step("aviso", note, ok=False))

        # 4. memória injetada
        selection = await self.memory.context_for(
            project_id=project_id,
            task=task,
            budget_tokens=budget_tokens or 8000,
        )
        steps.append(
            _step(
                "memória",
                f"{len(selection.items)} página(s) · {selection.tokens} tokens",
            )
        )
        for warning in selection.warnings:
            steps.append(_step("memória marcada", warning, ok=False))

        # 5. grava a sessão antes de subir o processo
        await self.conn.execute(
            """
            INSERT INTO sessions
                (id, project_id, harness, agent_label, status, mode, branch,
                 worktree_path, task, started_at)
            VALUES (?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                project_id,
                harness_id,
                info.name,
                mode,
                branch,
                tree_path,
                task,
                _now(),
            ),
        )
        await self.conn.commit()

        # 6. sobe o processo
        env = {ENV_SESSION: session_id}
        if project:
            env[ENV_PROJECT] = project["slug"]
        context = render_context(selection)
        if context:
            # O bloco vai por variável de ambiente para o hook SessionStart
            # poder devolvê-lo sem precisar consultar o daemon de novo.
            env["LEA_MEMORY_CONTEXT"] = context

        try:
            spawned = await pty.spawn(command, cwd=cwd, env=env)
        except Exception as exc:
            await self._mark(session_id, "error")
            await self._event(session_id, "alert", {"message": f"falha ao iniciar: {exc}"})
            raise LaunchError(f"não consegui iniciar {harness.name}: {exc}") from exc

        running = Running(
            id=session_id,
            spawned=spawned,
            project_id=project_id,
            harness=harness_id,
            worktree_path=tree_path,
        )
        self.running[session_id] = running
        pump = asyncio.create_task(self._pump(running))
        self._pumps.add(pump)
        pump.add_done_callback(self._pumps.discard)

        await self.conn.execute(
            "UPDATE sessions SET status = 'active', pid = ? WHERE id = ?",
            (spawned.pid, session_id),
        )
        await self.conn.commit()
        steps.append(_step("sessão", f"ativa · pid {spawned.pid}"))

        await self._event(
            session_id,
            "session_start",
            {
                "harness": harness_id,
                "project_id": project_id,
                "task": task,
                "steps": steps,
            },
        )

        return {
            "id": session_id,
            "harness": harness_id,
            "project_id": project_id,
            "status": "active",
            "pid": spawned.pid,
            "worktree_path": tree_path,
            "cwd": cwd,
            "boot": steps,
            "memory": selection.as_dict(),
        }

    # ── fluxo do terminal ────────────────────────────────────────────────

    async def _pump(self, running: Running) -> None:
        """Lê o PTY até o fim e reparte para os assinantes."""
        try:
            async for chunk in pty.read_stream(running.spawned):
                running.publish(chunk)
        except Exception:
            logger.exception("fluxo da sessão %s caiu", running.id)
        finally:
            await self._finish(running)

    async def _finish(self, running: Running) -> None:
        """Fecha a sessão quando o processo termina, por qualquer motivo."""
        code = running.spawned.process.returncode
        pty.close(running.spawned)
        self.running.pop(running.id, None)

        for queue in list(running.output):
            queue.put_nowait(b"")

        if self._closing:
            # Daemon desligando: o banco já pode estar fechado, e escrever
            # agora só produziria "no active connection" no log de saída.
            return

        await self.conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ? "
            "AND status NOT IN ('ended', 'handoff')",
            (_now(), running.id),
        )
        await self.conn.commit()

        if running.worktree_path:
            # A worktree fica: pode haver trabalho não commitado dentro dela,
            # e apagar automaticamente seria destruir o que a sessão fez. A
            # limpeza é uma ação do usuário.
            pass

        await self._event(running.id, "session_end", {"exit_code": code})

    # ── auxiliares ───────────────────────────────────────────────────────

    async def _project(self, project_id: int | None) -> dict[str, Any] | None:
        if project_id is None:
            return None
        async with self.conn.execute(
            "SELECT id, slug, name, path FROM projects WHERE id = ?", (project_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise LaunchError(f"projeto desconhecido: {project_id}")
        return dict(row)

    async def _mark(self, session_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
        )
        await self.conn.commit()

    async def _event(self, session_id: str, type: str, payload: dict[str, Any]) -> None:
        """Grava no banco e publica no barramento, nessa ordem.

        Grava antes de publicar porque a interface pode pedir o histórico
        logo depois de receber o evento ao vivo -- e encontrar um feed que
        mostra algo que a consulta não devolve é confuso.
        """
        await self.conn.execute(
            "INSERT INTO events (session_id, at, type, payload_json) VALUES (?, ?, ?, ?)",
            (session_id, _now(), type, json.dumps(payload, ensure_ascii=False)),
        )
        await self.conn.commit()
        self.bus.emit(type, payload, session_id=session_id)


def _step(label: str, detail: str, ok: bool = True) -> dict[str, Any]:
    return {"label": label, "detail": detail, "ok": ok}


def worktrees_root() -> Path:
    return paths.worktrees_dir()
