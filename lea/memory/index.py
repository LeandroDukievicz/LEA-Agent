"""Índice SQLite da wiki: espelho consultável dos arquivos .md.

O índice é **descartável**. A verdade está no git; aqui ficam só as colunas
que tornam busca, ordenação e grafo rápidos. ``reindex`` reconstrói tudo do
zero, e é o que conserta qualquer divergência -- depois de um `git pull`, de
uma edição manual ou de um banco corrompido.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from lea.memory import decay
from lea.memory.page import Page
from lea.memory.wiki import Wiki

logger = logging.getLogger(__name__)

#: Janela do contador de acessos que alimenta o reforço de força.
ACCESS_WINDOW = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


async def reindex(conn: aiosqlite.Connection, wiki: Wiki) -> dict[str, int | list[str]]:
    """Reconstrói o índice inteiro a partir dos arquivos.

    Preserva ``page_access``: o histórico de uso é dado que **não** está no
    git, então apagá-lo junto zeraria a força de toda a wiki.
    """
    pages, problems = wiki.load_all()
    head = await wiki.head_sha()

    # `page_links` e `pages_fts` são derivados puros: limpar e refazer é
    # correto e barato. `pages`, NÃO -- ela é alvo do ON DELETE CASCADE de
    # `page_access`, e um DELETE geral levaria junto todo o histórico de uso,
    # que não está no git e zeraria a força da wiki inteira. Por isso aqui a
    # gente faz upsert do que existe e só apaga o que sumiu de fato.
    await conn.execute("DELETE FROM page_links")
    await conn.execute("DELETE FROM pages_fts")

    for page in pages:
        await _insert(conn, page, head)

    live = [p.id for p in pages]
    if live:
        placeholders = ",".join("?" * len(live))
        await conn.execute(f"DELETE FROM pages WHERE id NOT IN ({placeholders})", live)
    else:
        await conn.execute("DELETE FROM pages")

    await resolve_links(conn)
    await conn.commit()

    for relative, error in problems:
        logger.warning("página ignorada %s: %s", relative, error)

    return {
        "indexed": len(pages),
        "problems": [f"{relative}: {error}" for relative, error in problems],
    }


async def upsert(conn: aiosqlite.Connection, page: Page, sha: str | None = None) -> None:
    """Insere ou atualiza uma página no índice e refaz os links dela."""
    await conn.execute("DELETE FROM pages_fts WHERE page_id = ?", (page.id,))
    await conn.execute("DELETE FROM page_links WHERE src_id = ?", (page.id,))
    await _insert(conn, page, sha)
    await resolve_links(conn, page.id)
    await conn.commit()


async def _insert(conn: aiosqlite.Connection, page: Page, sha: str | None) -> None:
    project_id = await project_id_for(conn, page.project)

    await conn.execute(
        """
        INSERT INTO pages (id, path, project_id, title, tier, strength, updated_at, sha)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            path = excluded.path, project_id = excluded.project_id,
            title = excluded.title, tier = excluded.tier,
            strength = excluded.strength, updated_at = excluded.updated_at,
            sha = excluded.sha
        """,
        (
            page.id,
            page.path,
            project_id,
            page.title,
            page.tier,
            page.strength,
            _iso(_now()),
            sha,
        ),
    )
    await conn.execute(
        "INSERT INTO pages_fts (title, body, tags, page_id) VALUES (?, ?, ?, ?)",
        (page.title, page.body, " ".join(page.tags), page.id),
    )
    for target in page.links():
        await conn.execute(
            "INSERT OR IGNORE INTO page_links (src_id, dst_slug) VALUES (?, ?)",
            (page.id, target),
        )


async def resolve_links(conn: aiosqlite.Connection, src_id: str | None = None) -> int:
    """Liga ``dst_slug`` ao id da página de destino, quando ela existir.

    Link apontando para página inexistente é estado legítimo -- o agente cita
    algo que ainda será escrito -- e fica com ``dst_id`` nulo até alguém criar
    o destino. Por isso isto roda inteiro a cada reindexação.
    """
    where = "WHERE l.src_id = ?" if src_id else ""
    params = (src_id,) if src_id else ()

    # Casa pelo slug qualificado (projeto/slug) ou pelo slug puro. O SQLite
    # não tem função de slug, então comparamos contra o caminho sem extensão.
    sql = f"""
        UPDATE page_links AS l
        SET dst_id = (
            SELECT p.id FROM pages p
            WHERE lower(replace(rtrim(p.path, '.md'), '.md', '')) = l.dst_slug
               OR lower(
                    replace(
                      substr(p.path, instr(p.path, '/') + 1),
                      '.md', ''
                    )
                  ) = l.dst_slug
            LIMIT 1
        )
        {where}
    """
    cursor = await conn.execute(sql, params)
    return cursor.rowcount or 0


async def record_access(
    conn: aiosqlite.Connection,
    page_id: str,
    *,
    agent: str,
    kind: str = "read",
    session_id: str | None = None,
    half_lives: dict[str, float] | None = None,
) -> float:
    """Registra um acesso e recalcula a força. Devolve a força nova.

    É aqui que "ler reforça" vira efeito real -- o acesso entra no histórico,
    o contador de 7 dias sobe e a força é recomputada e persistida.
    """
    now = _now()
    await conn.execute(
        "INSERT INTO page_access (page_id, agent, session_id, at, kind) VALUES (?, ?, ?, ?, ?)",
        (page_id, agent, session_id, _iso(now), kind),
    )

    async with conn.execute(
        "SELECT tier, strength, last_access_at FROM pages WHERE id = ?", (page_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        await conn.commit()
        return 0.0

    recent = await access_count(conn, page_id, ACCESS_WINDOW, now)
    cited = await _cited_in_handoff(conn, page_id)
    elapsed = decay.elapsed_days(_parse(row["last_access_at"]), now)

    strength = decay.strength_at(
        row["strength"],
        row["tier"],
        elapsed,
        accesses_7d=recent,
        cited_in_handoff=cited,
        half_lives=half_lives,
    )

    tier = row["tier"]
    if decay.should_promote(await access_count(conn, page_id, decay.PROMOTE_WINDOW, now)):
        tier = decay.promote(tier)

    await conn.execute(
        "UPDATE pages SET strength = ?, tier = ?, last_access_at = ?, access_count_7d = ? "
        "WHERE id = ?",
        (strength, tier, _iso(now), recent, page_id),
    )
    await conn.commit()
    return strength


async def access_count(
    conn: aiosqlite.Connection,
    page_id: str,
    window: timedelta,
    now: datetime | None = None,
) -> int:
    """Acessos da página dentro da janela."""
    since = _iso((now or _now()) - window)
    async with conn.execute(
        "SELECT COUNT(*) FROM page_access WHERE page_id = ? AND at >= ?",
        (page_id, since),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def apply_decay(
    conn: aiosqlite.Connection, half_lives: dict[str, float] | None = None
) -> dict[str, int]:
    """Recalcula a força de todas as páginas e ajusta tiers.

    Roda na consolidação noturna. Rebaixa quem caiu abaixo do piso; **não
    apaga nada**.
    """
    now = _now()
    promoted = demoted = 0

    async with conn.execute(
        "SELECT id, tier, strength, last_access_at FROM pages"
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        recent = await access_count(conn, row["id"], ACCESS_WINDOW, now)
        cited = await _cited_in_handoff(conn, row["id"])
        strength = decay.strength_at(
            row["strength"],
            row["tier"],
            decay.elapsed_days(_parse(row["last_access_at"]), now),
            accesses_7d=recent,
            cited_in_handoff=cited,
            half_lives=half_lives,
        )

        tier = row["tier"]
        if decay.should_demote(strength) and tier != "cold":
            tier = decay.demote(tier)
            demoted += 1
        elif decay.should_promote(
            await access_count(conn, row["id"], decay.PROMOTE_WINDOW, now)
        ) and tier != "hot":
            tier = decay.promote(tier)
            promoted += 1

        await conn.execute(
            "UPDATE pages SET strength = ?, tier = ?, access_count_7d = ? WHERE id = ?",
            (strength, tier, recent, row["id"]),
        )

    await conn.commit()
    return {"pages": len(rows), "promoted": promoted, "demoted": demoted}


async def project_id_for(conn: aiosqlite.Connection, slug: str | None) -> int | None:
    """Id do projeto pelo slug, ou ``None`` para página global."""
    if not slug:
        return None
    async with conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)) as cursor:
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


async def _cited_in_handoff(conn: aiosqlite.Connection, page_id: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM page_access WHERE page_id = ? AND kind = 'handoff_cite' LIMIT 1",
        (page_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
