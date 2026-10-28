"""Métricas da máquina e contadores para a barra de status.

A barra inferior do protótipo mostra, à esquerda: gateway, agentes rodando,
próxima cron, sha da wiki, CPU e memória. Aqui mora a parte que vem do sistema
operacional e do banco.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import aiosqlite
import psutil

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SystemStats:
    """Uso de CPU e memória do processo e da máquina."""

    cpu_percent: float
    memory_percent: float
    memory_used_gb: float
    memory_total_gb: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gib(value: int) -> float:
    return round(value / (1024**3), 2)


def system_stats() -> SystemStats:
    """Lê CPU e memória agora.

    ``cpu_percent(interval=None)`` devolve o uso desde a chamada anterior, sem
    bloquear. A primeira chamada do processo sai 0.0 porque não existe janela
    anterior para comparar -- por isso ``prime()`` é chamado no start do
    daemon, e não tratamos esse 0.0 como leitura válida.
    """
    memory = psutil.virtual_memory()
    return SystemStats(
        cpu_percent=round(psutil.cpu_percent(interval=None), 1),
        memory_percent=round(memory.percent, 1),
        memory_used_gb=_gib(memory.used),
        memory_total_gb=_gib(memory.total),
    )


def prime() -> None:
    """Estabelece a janela de referência do ``cpu_percent``."""
    psutil.cpu_percent(interval=None)


async def counters(conn: aiosqlite.Connection) -> dict[str, int]:
    """Contadores do Overview, numa consulta só por tabela.

    Tudo vem de ``COUNT`` com índice; nada aqui varre tabela inteira.
    """
    return {
        "sessions_active": await _count(
            conn, "SELECT COUNT(*) FROM sessions WHERE status IN ('active', 'starting')"
        ),
        "sessions_paused": await _count(
            conn, "SELECT COUNT(*) FROM sessions WHERE status = 'paused'"
        ),
        "pages": await _count(conn, "SELECT COUNT(*) FROM pages"),
        "pages_hot": await _count(conn, "SELECT COUNT(*) FROM pages WHERE tier = 'hot'"),
        "pages_warm": await _count(conn, "SELECT COUNT(*) FROM pages WHERE tier = 'warm'"),
        "handoffs_pending": await _count(
            conn, "SELECT COUNT(*) FROM handoffs WHERE status = 'pending'"
        ),
        "projects": await _count(conn, "SELECT COUNT(*) FROM projects"),
        "skills": await _count(conn, "SELECT COUNT(*) FROM skills"),
        "cron_active": await _count(
            conn, "SELECT COUNT(*) FROM cron_jobs WHERE status = 'active'"
        ),
    }


async def cost_today_brl(conn: aiosqlite.Connection) -> float:
    """Custo acumulado hoje, em reais.

    O corte do dia usa a data local da máquina, não UTC: o usuário compara
    esse número com o orçamento diário dele, que vira à meia-noite do fuso
    dele. As linhas são gravadas em UTC e convertidas aqui pelo SQLite.
    """
    sql = (
        "SELECT COALESCE(SUM(cost_brl), 0.0) FROM costs "
        "WHERE date(at, 'localtime') = date('now', 'localtime')"
    )
    async with conn.execute(sql) as cursor:
        row = await cursor.fetchone()
    return round(float(row[0]), 2) if row else 0.0


async def _count(conn: aiosqlite.Connection, sql: str) -> int:
    async with conn.execute(sql) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0
