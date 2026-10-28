"""Agendador de tarefas do LEA, sem daemon ou dependência externa extra."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from lea.agent import AgentService
from lea.events import EventBus

logger = logging.getLogger(__name__)
POLL_SECONDS = 10
MAX_SEARCH_DAYS = 366 * 8


class CronError(ValueError):
    """Expressão cron ou fuso horário inválido."""


@dataclass(frozen=True)
class CronSchedule:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        fields = expression.split()
        if len(fields) != 5:
            raise CronError("cron precisa ter cinco campos: minuto hora dia mês dia-da-semana")
        minute, _ = _field(fields[0], 0, 59, "minuto")
        hour, _ = _field(fields[1], 0, 23, "hora")
        day, day_wildcard = _field(fields[2], 1, 31, "dia")
        month, _ = _field(fields[3], 1, 12, "mês")
        weekday, weekday_wildcard = _field(fields[4], 0, 7, "dia-da-semana")
        weekday = frozenset(0 if item == 7 else item for item in weekday)
        return cls(minute, hour, day, month, weekday, day_wildcard, weekday_wildcard)

    def matches_date(self, current: date) -> bool:
        if current.month not in self.month:
            return False
        day_match = current.day in self.day
        # POSIX cron combina dia do mês e dia da semana com OR quando ambos
        # foram limitados; quando só um foi limitado, usa esse campo.
        weekday = (current.weekday() + 1) % 7
        weekday_match = weekday in self.weekday
        if self.day_wildcard and self.weekday_wildcard:
            return True
        if self.day_wildcard:
            return weekday_match
        if self.weekday_wildcard:
            return day_match
        return day_match or weekday_match

    def next_after(self, after: datetime, timezone_name: str) -> datetime:
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CronError(f"fuso horário desconhecido: {timezone_name}") from exc
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        after_utc = after.astimezone(timezone.utc)
        first_day = after_utc.astimezone(zone).date()
        times = [time(h, m) for h in sorted(self.hour) for m in sorted(self.minute)]

        for offset in range(MAX_SEARCH_DAYS):
            day = first_day + timedelta(days=offset)
            if not self.matches_date(day):
                continue
            for clock in times:
                local = datetime.combine(day, clock, tzinfo=zone)
                candidate_utc = local.astimezone(timezone.utc)
                # Horário que não existe por avanço de DST é pulado.
                if candidate_utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                    continue
                if candidate_utc > after_utc:
                    return candidate_utc
        raise CronError("expressão cron não tem ocorrência nos próximos oito anos")


def next_run(expression: str, timezone_name: str, after: datetime | None = None) -> str:
    schedule = CronSchedule.parse(expression)
    value = schedule.next_after(after or datetime.now(timezone.utc), timezone_name)
    return value.isoformat(timespec="seconds")


def validate(expression: str, timezone_name: str) -> str:
    """Valida cron e timezone e devolve a próxima execução em UTC."""
    return next_run(expression, timezone_name)


class CronScheduler:
    """Busca ocorrências vencidas e inicia sessões autônomas."""

    def __init__(self, conn: aiosqlite.Connection, agent: AgentService, bus: EventBus) -> None:
        self.conn = conn
        self.agent = agent
        self.bus = bus
        self._loop_task: asyncio.Task[None] | None = None
        self._runs: dict[int, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        # Uma execução ainda aberta indica que o processo caiu durante o job.
        await self.conn.execute(
            "UPDATE cron_runs SET status = 'error', ended_at = ?, summary = 'daemon reiniciado durante a execução' "
            "WHERE ended_at IS NULL",
            (_now(),),
        )
        await self.conn.commit()
        self._loop_task = asyncio.create_task(self._loop(), name="lea-cron-scheduler")

    async def shutdown(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        runs = list(self._runs.values())
        for task in runs:
            task.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)
        self._runs.clear()

    async def run_due(self) -> int:
        """Executa uma varredura; útil também para chamada explícita."""
        now = _now()
        async with self.conn.execute(
            "SELECT * FROM cron_jobs WHERE status = 'active' AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at, id",
            (now,),
        ) as cursor:
            jobs = await cursor.fetchall()

        started = 0
        slots_remaining = max(
            0,
            self.agent.config.agent.max_parallel_tasks - self.agent.queued_or_running,
        )
        for job in jobs:
            job_id = int(job["id"])
            if job_id in self._runs and not self._runs[job_id].done():
                await self._advance(job, now)
                continue
            async with self.conn.execute(
                "SELECT 1 FROM cron_runs WHERE job_id = ? AND ended_at IS NULL LIMIT 1", (job_id,)
            ) as cursor:
                if await cursor.fetchone():
                    await self._advance(job, now)
                    continue
            if slots_remaining <= 0:
                # Deixa a ocorrência vencida na fila até uma sessão liberar slot.
                continue
            run_id = await self._claim(job, now)
            if run_id is None:
                continue
            task = asyncio.create_task(self._execute(job, run_id), name=f"lea-cron:{job_id}")
            self._runs[job_id] = task
            task.add_done_callback(lambda done, jid=job_id: self._runs.pop(jid, None))
            started += 1
            slots_remaining -= 1
        return started

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("falha na varredura do cron")
            await asyncio.sleep(POLL_SECONDS)

    async def _claim(self, job: aiosqlite.Row, now: str) -> int | None:
        now_dt = datetime.fromisoformat(now)
        next_at = next_run(job["cron_expr"], job["timezone"], now_dt)
        # Avançar e gravar o run na mesma transação impede duplicação após
        # reconectar ao WebSocket ou reiniciar o loop de polling.
        updated = await self.conn.execute(
            "UPDATE cron_jobs SET next_run_at = ? WHERE id = ? AND status = 'active' AND next_run_at <= ?",
            (next_at, int(job["id"]), now),
        )
        if updated.rowcount != 1:
            await self.conn.commit()
            return None
        cursor = await self.conn.execute(
            "INSERT INTO cron_runs (job_id, started_at, status, log_json) VALUES (?, ?, 'ok', '[]')",
            (int(job["id"]), now),
        )
        run_id = int(cursor.lastrowid or 0)
        await self.conn.commit()
        return run_id

    async def _advance(self, job: aiosqlite.Row, now: str) -> None:
        next_at = next_run(job["cron_expr"], job["timezone"], datetime.fromisoformat(now))
        await self.conn.execute(
            "UPDATE cron_jobs SET next_run_at = ? WHERE id = ? AND status = 'active'",
            (next_at, int(job["id"])),
        )
        await self.conn.commit()

    async def _execute(self, job: aiosqlite.Row, run_id: int) -> None:
        job_id = int(job["id"])
        session_id: str | None = None
        try:
            session, task = await self.agent.start(
                project_id=int(job["project_id"]),
                task=job["instruction_nl"],
                skill_id=int(job["skill_id"]) if job["skill_id"] is not None else None,
                source=f"cron:{job_id}",
                backend=job["agent_backend"],
            )
            session_id = session["id"]
            result = await task
            status = "ok" if result["status"] == "ended" else "error"
            summary = str(result.get("summary") or "")[:2000]
            log = [{"session_id": session_id, "status": result["status"], "tokens_in": result["tokens_in"], "tokens_out": result["tokens_out"]}]
        except asyncio.CancelledError:
            await self.conn.execute(
                "UPDATE cron_runs SET ended_at = ?, status = 'error', summary = 'execução interrompida' WHERE id = ?",
                (_now(), run_id),
            )
            await self.conn.commit()
            raise
        except Exception as exc:
            logger.exception("cron job %s falhou", job_id)
            status = "error"
            summary = f"{type(exc).__name__}: {exc}"[:2000]
            log = [{"error": summary}]

        ended = _now()
        await self.conn.execute(
            "UPDATE cron_runs SET ended_at = ?, status = ?, summary = ?, log_json = ? WHERE id = ?",
            (ended, status, summary, json.dumps(log, ensure_ascii=False), run_id),
        )
        await self.conn.commit()
        payload = {"job_id": job_id, "run_id": run_id, "status": status, "summary": summary, "session_id": session_id}
        self.bus.emit("cron", payload, session_id=session_id)


def _field(raw: str, low: int, high: int, label: str) -> tuple[frozenset[int], bool]:
    if not raw:
        raise CronError(f"campo {label} vazio")
    wildcard = raw == "*"
    values: set[int] = set()
    for part in raw.split(","):
        if not part:
            raise CronError(f"campo {label} contém item vazio")
        if "/" in part:
            base, step_raw = part.split("/", 1)
            try:
                step = int(step_raw)
            except ValueError as exc:
                raise CronError(f"passo inválido no campo {label}") from exc
            if step < 1:
                raise CronError(f"passo precisa ser positivo no campo {label}")
        else:
            base, step = part, 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            pair = base.split("-", 1)
            try:
                start, end = map(int, pair)
            except ValueError as exc:
                raise CronError(f"intervalo inválido no campo {label}") from exc
        else:
            try:
                start = int(base)
            except ValueError as exc:
                raise CronError(f"valor inválido no campo {label}: {base}") from exc
            end = high if "/" in part else start
        if start < low or end > high or start > end:
            raise CronError(f"valor fora do intervalo no campo {label} ({low}–{high})")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"campo {label} não seleciona nenhum valor")
    return frozenset(values), wildcard


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
