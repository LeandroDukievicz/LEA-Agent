"""Conexão com o SQLite e aplicação das migrações.

Uma conexão só, de escrita, viva enquanto o daemon roda. SQLite aguenta
leitores concorrentes com WAL, mas um escritor por vez -- serializar a escrita
num lugar é mais simples e mais rápido que disputar lock e tratar
``SQLITE_BUSY`` espalhado pelo código.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from lea import paths
from lea.db import schema

logger = logging.getLogger(__name__)

#: PRAGMAs aplicados em toda conexão, em ordem.
_PRAGMAS = (
    # WAL: leitor não bloqueia escritor. Sem isso a UI trava enquanto o agente
    # grava evento.
    "PRAGMA journal_mode = WAL",
    # NORMAL com WAL é durável contra queda de processo; só uma queda de
    # energia pode custar a última transação. É a troca certa para um app
    # desktop que grava evento a cada tool call.
    "PRAGMA synchronous = NORMAL",
    # ON DELETE CASCADE do esquema só vale com isto ligado -- e o SQLite vem
    # com foreign key DESLIGADA por padrão, por compatibilidade histórica.
    "PRAGMA foreign_keys = ON",
    # Espera o lock em vez de devolver SQLITE_BUSY na hora.
    "PRAGMA busy_timeout = 5000",
)


async def connect(path: Path | None = None) -> aiosqlite.Connection:
    """Abre a conexão, aplica os PRAGMAs e migra até a versão mais recente."""
    db_path = path or paths.database_file()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    for pragma in _PRAGMAS:
        await conn.execute(pragma)
    await migrate(conn)
    return conn


@asynccontextmanager
async def connection(path: Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """Conexão de vida curta, para a CLI e os testes."""
    conn = await connect(path)
    try:
        yield conn
    finally:
        await conn.close()


async def current_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def migrate(conn: aiosqlite.Connection) -> int:
    """Aplica as migrações pendentes. Devolve a versão final.

    Cada migração roda inteira ou não roda: um erro no meio desfaz tudo dela e
    o ``user_version`` não avança, então a próxima tentativa recomeça daquela
    migração em vez de pegar o banco meio construído.
    """
    version = await current_version(conn)

    if version > schema.LATEST_VERSION:
        # Banco escrito por um LEA mais novo. Seguir em frente seria escrever
        # num esquema que não conhecemos.
        raise RuntimeError(
            f"O banco está na versão {version}, mas este LEA só conhece até "
            f"{schema.LATEST_VERSION}. Atualize o LEA ou aponte LEA_HOME para outro perfil."
        )

    for index in range(version, schema.LATEST_VERSION):
        target = index + 1
        logger.info("aplicando migração %d", target)
        try:
            await conn.execute("BEGIN")
            for statement in schema.MIGRATIONS[index]:
                await conn.execute(statement)
            # PRAGMA não aceita parâmetro ligado; o valor vem de range(), não
            # de entrada externa.
            await conn.execute(f"PRAGMA user_version = {target}")
            await conn.commit()
        except Exception:
            await conn.rollback()
            logger.exception("migração %d falhou; banco mantido na versão %d", target, index)
            raise

    return schema.LATEST_VERSION


async def integrity_problems(conn: aiosqlite.Connection) -> list[str]:
    """Roda ``integrity_check``. Lista vazia = banco íntegro."""
    async with conn.execute("PRAGMA integrity_check") as cursor:
        rows = await cursor.fetchall()
    issues = [row[0] for row in rows if row[0] != "ok"]
    return issues
