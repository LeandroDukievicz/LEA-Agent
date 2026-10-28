"""Memória do LEA: wiki git + índice SQLite, por trás de uma fachada só.

Quem usa memória -- as rotas HTTP, o servidor MCP, o launcher de sessão --
fala com :class:`Memory`. Ter um dono único para a política evita o problema
clássico de dois caminhos de escrita discordarem: um que varre conteúdo
suspeito e outro que esqueceu de varrer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from lea.memory import decay, index, inject, page as page_mod, query
from lea.memory.page import Page, PageError, Tier
from lea.memory.wiki import GitError, Version, Wiki
from lea.security import injection

logger = logging.getLogger(__name__)

__all__ = [
    "GitError",
    "Memory",
    "Page",
    "PageError",
    "Version",
    "WriteResult",
    "Wiki",
    "decay",
    "index",
    "inject",
    "query",
]


@dataclass
class WriteResult:
    """Resultado de gravar uma página."""

    page: Page
    sha: str
    created: bool
    #: Achados do scanner. Não impedem a gravação, mas impedem a injeção
    #: automática desta página em contexto de outro agente.
    flagged: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.page.id,
            "slug": self.page.qualified_slug,
            "path": self.page.path,
            "sha": self.sha,
            "created": self.created,
            "flagged": self.flagged,
        }


class Memory:
    """Fachada de leitura e escrita da memória."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        wiki_root: Path,
        *,
        half_lives: dict[str, float] | None = None,
    ) -> None:
        self.conn = conn
        self.wiki = Wiki(wiki_root)
        self.half_lives = half_lives

    async def setup(self) -> None:
        await self.wiki.ensure_repo()

    # ── leitura ──────────────────────────────────────────────────────────

    async def read(
        self, page_id: str, *, agent: str = "lea", session_id: str | None = None
    ) -> Page:
        """Lê uma página pelo id e **registra o acesso**.

        Ler reforça: é o que faz a força subir e o tier promover. Para olhar
        sem reforçar (listagem, grafo), use ``peek``.
        """
        page = await self.peek(page_id)
        await index.record_access(
            self.conn,
            page_id,
            agent=agent,
            kind="read",
            session_id=session_id,
            half_lives=self.half_lives,
        )
        return page

    async def peek(self, page_id: str) -> Page:
        """Lê sem registrar acesso."""
        path = await self._path_of(page_id)
        return self.wiki.read(path)

    async def search(
        self, text: str, *, project_id: int | None = None, tier: str | None = None, limit: int = 20
    ) -> list[query.Hit]:
        return await query.search(
            self.conn, text, project_id=project_id, tier=tier, limit=limit
        )

    async def tree(self, project_id: int | None = None) -> dict[str, Any]:
        return await query.tree(self.conn, project_id)

    async def graph(self, page_id: str, depth: int = 2) -> query.Graph:
        return await query.graph(self.conn, page_id, depth)

    async def backlinks(self, page_id: str) -> list[dict[str, Any]]:
        return await query.backlinks(self.conn, page_id)

    async def versions(self, page_id: str, limit: int = 50) -> list[Version]:
        return await self.wiki.versions(await self._path_of(page_id), limit)

    async def diff(self, page_id: str, sha: str) -> str:
        return await self.wiki.diff(await self._path_of(page_id), sha)

    async def projection(self, page_id: str) -> decay.Projection:
        """Força agora, em 7 e em 30 dias -- a barra de decaimento da UI."""
        async with self.conn.execute(
            "SELECT tier, strength, last_access_at, access_count_7d FROM pages WHERE id = ?",
            (page_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise GitError(f"página desconhecida: {page_id}")

        return decay.project(
            row["strength"],
            row["tier"],
            index._parse(row["last_access_at"]),
            accesses_7d=row["access_count_7d"],
            half_lives=self.half_lives,
        )

    # ── escrita ──────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        path: str,
        title: str,
        body: str,
        tags: list[str] | None = None,
        tier: Tier = "warm",
        agent: str = "lea",
        message: str | None = None,
    ) -> WriteResult:
        """Cria ou atualiza uma página, com commit e reindexação.

        Conteúdo suspeito **não bloqueia a gravação**: bloquear perderia o
        trabalho do agente num falso positivo, e uma página com um trecho
        esquisito ainda pode ser exatamente o incidente que o usuário quer
        registrar. O que fazemos é marcar -- e ``inject`` se recusa a mandar
        página marcada para o contexto de outro agente, que é onde o estrago
        aconteceria.
        """
        if not path.endswith(".md"):
            path = f"{path}.md"

        existing = self._existing(path)
        if existing is not None:
            page = existing
            page.title = title
            page.body = body
            page.tier = tier
            page.updated_by = agent
            if tags is not None:
                page.tags = tags
            created = False
        else:
            page = page_mod.make(title, body, path=path, tier=tier, tags=tags, author=agent)
            created = True

        findings = injection.scan(body, injection.Scope.STRICT)
        flagged = [f"{f.reason}: {f.excerpt}" for f in findings]
        if flagged:
            logger.warning("página %s gravada com %d achado(s)", path, len(flagged))

        default_message = f"{'cria' if created else 'atualiza'} {page.qualified_slug}"
        sha = await self.wiki.write(page, message=message or default_message, author=agent)
        await index.upsert(self.conn, page, sha)
        await index.record_access(
            self.conn, page.id, agent=agent, kind="write", half_lives=self.half_lives
        )

        return WriteResult(page=page, sha=sha, created=created, flagged=flagged)

    async def cite(self, page_id: str, *, agent: str, session_id: str | None = None) -> float:
        """Marca a página como citada num handoff -- o reforço mais forte."""
        return await index.record_access(
            self.conn,
            page_id,
            agent=agent,
            kind="handoff_cite",
            session_id=session_id,
            half_lives=self.half_lives,
        )

    # ── manutenção ───────────────────────────────────────────────────────

    async def reindex(self) -> dict[str, int | list[str]]:
        """Reconstrói o índice a partir do git."""
        await self.wiki.ensure_repo()
        return await index.reindex(self.conn, self.wiki)

    async def apply_decay(self) -> dict[str, int]:
        """Decaimento e ajuste de tier de toda a wiki. Não apaga nada."""
        return await index.apply_decay(self.conn, self.half_lives)

    async def context_for(
        self, *, project_id: int | None = None, task: str = "", budget_tokens: int = 8000
    ) -> inject.Selection:
        """Escolhe o que injetar numa sessão nova."""
        return await inject.select(
            self.conn,
            self.wiki,
            project_id=project_id,
            task=task,
            budget_tokens=budget_tokens,
        )

    # ── internos ─────────────────────────────────────────────────────────

    async def _path_of(self, page_id: str) -> str:
        async with self.conn.execute("SELECT path FROM pages WHERE id = ?", (page_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise GitError(f"página desconhecida: {page_id}")
        return row["path"]

    def _existing(self, path: str) -> Page | None:
        try:
            return self.wiki.read(path)
        except (GitError, PageError):
            return None
