"""Migrações, PRAGMAs e o contrato do esquema."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from lea.db import schema
from lea.db import session as db


async def test_migra_ate_a_versao_mais_recente(db_path: Path) -> None:
    async with db.connection(db_path) as conn:
        assert await db.current_version(conn) == schema.LATEST_VERSION


async def test_migrar_e_idempotente(db_path: Path) -> None:
    async with db.connection(db_path) as conn:
        primeira = await db.migrate(conn)
    async with db.connection(db_path) as conn:
        assert await db.migrate(conn) == primeira


async def test_cria_todas_as_tabelas(db_path: Path) -> None:
    esperadas = {
        "projects", "pages", "pages_fts", "page_links", "page_access",
        "sessions", "events", "handoffs",
        "skills", "skill_versions", "skill_runs",
        "cron_jobs", "cron_runs", "channels", "costs",
    }
    async with db.connection(db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ) as cursor:
            existentes = {row[0] for row in await cursor.fetchall()}
    assert esperadas <= existentes


async def test_pragmas_aplicados(db_path: Path) -> None:
    async with db.connection(db_path) as conn:
        async with conn.execute("PRAGMA journal_mode") as cursor:
            assert (await cursor.fetchone())[0].lower() == "wal"
        async with conn.execute("PRAGMA foreign_keys") as cursor:
            assert (await cursor.fetchone())[0] == 1


async def test_foreign_key_em_cascata(db_path: Path) -> None:
    """Apagar o projeto leva junto as páginas dele -- sem isso o índice fica
    com linhas órfãs que a busca continua devolvendo."""
    async with db.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO projects (slug, name, path, created_at) VALUES (?, ?, ?, ?)",
            ("lea-core", "LEA Core", "/tmp/lea-core", "2026-10-28T00:00:00+00:00"),
        )
        await conn.execute(
            "INSERT INTO pages (id, path, project_id, title, updated_at) "
            "VALUES (?, ?, 1, ?, ?)",
            ("0x7a3f19", "lea-core/decaimento.md", "Decaimento", "2026-10-28T00:00:00+00:00"),
        )
        await conn.commit()

        await conn.execute("DELETE FROM projects WHERE id = 1")
        await conn.commit()

        async with conn.execute("SELECT COUNT(*) FROM pages") as cursor:
            assert (await cursor.fetchone())[0] == 0


async def test_check_rejeita_tier_invalido(db_path: Path) -> None:
    async with db.connection(db_path) as conn:
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pages (id, path, title, tier, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("0x1", "a.md", "A", "morna", "2026-10-28T00:00:00+00:00"),
            )


async def test_busca_fts_ignora_acento(db_path: Path) -> None:
    """A interface e o conteúdo são em português: buscar "memoria" tem de
    achar "memória"."""
    async with db.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO pages_fts (title, body, tags, page_id) VALUES (?, ?, ?, ?)",
            ("Decaimento de memória", "A força da página cai com o tempo.", "memoria", "0x1"),
        )
        await conn.commit()
        async with conn.execute(
            "SELECT page_id FROM pages_fts WHERE pages_fts MATCH ?", ("memoria",)
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["0x1"]


async def test_recusa_banco_de_versao_futura(db_path: Path) -> None:
    """Um LEA antigo não pode escrever num banco criado por um mais novo."""
    # Conexão crua, sem passar por db.connection(), que criaria o diretório.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    await conn.execute(f"PRAGMA user_version = {schema.LATEST_VERSION + 5}")
    await conn.commit()
    with pytest.raises(RuntimeError, match="versão"):
        await db.migrate(conn)
    await conn.close()


async def test_integridade_limpa(db_path: Path) -> None:
    async with db.connection(db_path) as conn:
        assert await db.integrity_problems(conn) == []
