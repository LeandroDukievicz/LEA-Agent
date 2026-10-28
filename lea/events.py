"""Barramento de eventos interno: tudo que acontece vira evento, e o
WebSocket apenas repassa.

Quem produz (sessão, cron, canal, agente) não conhece quem consome. A UI é só
mais um assinante -- é o que permite o daemon rodar headless no homelab sem
nenhuma janela aberta, e a interface conectar depois sem perder nada do que
está acontecendo a partir dali.

Filas são **limitadas**. Um assinante lento (aba em segundo plano, conexão
ruim) não pode fazer a memória do daemon crescer sem limite: quando a fila
dele enche, o evento mais antigo é descartado e um contador registra a perda.
Perder linha de feed antigo é aceitável; derrubar o daemon não é.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Eventos em fila por assinante antes de começar a descartar.
DEFAULT_MAX_QUEUE = 512


def _now() -> str:
    """Instante atual em ISO-8601 UTC.

    Tudo é gravado em UTC; a conversão para o fuso do usuário acontece na
    borda, ao exibir. Guardar horário local no banco é o que faz o relatório
    semanal errar na mudança de horário de verão.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Event:
    """Uma coisa que aconteceu.

    ``type`` usa o mesmo vocabulário da coluna ``events.type`` do banco, para
    um evento persistido e um transmitido não divergirem de nome.
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "at": self.at,
            "payload": self.payload,
        }


class Subscription:
    """Fila de um assinante. Use via ``EventBus.subscribe()``."""

    def __init__(self, bus: EventBus, max_queue: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0

    def _offer(self, event: Event) -> None:
        """Enfileira sem bloquear, abrindo espaço se preciso."""
        try:
            self._queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        # Descarta o mais antigo e tenta de novo. O `except` cobre a corrida em
        # que outra task esvaziou a fila entre as duas operações.
        try:
            self._queue.get_nowait()
            self.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover - corrida rara
            pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - corrida rara
            self.dropped += 1

    async def get(self) -> Event:
        return await self._queue.get()

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while True:
            yield await self.get()

    def close(self) -> None:
        self._bus._unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """Pub/sub em processo. Publicar nunca bloqueia nem levanta exceção."""

    def __init__(self, max_queue: int = DEFAULT_MAX_QUEUE) -> None:
        self._subscribers: set[Subscription] = set()
        self._max_queue = max_queue

    def subscribe(self) -> Subscription:
        sub = Subscription(self, self._max_queue)
        self._subscribers.add(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: Event) -> None:
        """Entrega a todos os assinantes.

        Não bloqueia: quem publica costuma ser o caminho quente de uma sessão
        (cada tool call vira evento) e não pode ficar esperando a UI.
        """
        for sub in tuple(self._subscribers):
            sub._offer(event)

    def emit(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Event:
        """Monta e publica um evento. Atalho para o caminho comum."""
        event = Event(type=type, payload=payload or {}, session_id=session_id)
        self.publish(event)
        return event


#: Barramento do processo. O daemon usa este; testes criam o seu.
bus = EventBus()
