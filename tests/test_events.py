"""Barramento de eventos: entrega, isolamento e o limite das filas."""

from __future__ import annotations

import asyncio

from lea.events import Event, EventBus


def test_publica_para_todos_os_assinantes() -> None:
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.emit("alert", {"msg": "cert expira"})
    assert a._queue.qsize() == 1
    assert b._queue.qsize() == 1


def test_publicar_sem_assinante_nao_quebra() -> None:
    EventBus().emit("memory", {"page": "0x1"})


def test_fila_cheia_descarta_o_mais_antigo() -> None:
    """Um assinante lento não pode fazer a memória do daemon crescer sem fim."""
    bus = EventBus(max_queue=3)
    sub = bus.subscribe()
    for i in range(5):
        bus.emit("tool_call", {"i": i})

    assert sub._queue.qsize() == 3
    assert sub.dropped == 2
    # Sobraram os mais recentes.
    restantes = [sub._queue.get_nowait().payload["i"] for _ in range(3)]
    assert restantes == [2, 3, 4]


def test_cancelar_assinatura_para_de_receber() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    bus.emit("alert", {})
    assert sub._queue.qsize() == 0
    assert bus.subscriber_count == 0


def test_context_manager_cancela_no_fim() -> None:
    bus = EventBus()
    with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


async def test_iteracao_assincrona_entrega_em_ordem() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(3):
        bus.emit("message", {"i": i})

    recebidos = []
    async for event in sub:
        recebidos.append(event.payload["i"])
        if len(recebidos) == 3:
            break
    assert recebidos == [0, 1, 2]


async def test_get_espera_o_proximo() -> None:
    bus = EventBus()
    sub = bus.subscribe()

    async def publica_depois() -> None:
        await asyncio.sleep(0.01)
        bus.emit("cron", {"job": 1})

    asyncio.create_task(publica_depois())
    event = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert event.type == "cron"


def test_evento_serializa_para_o_websocket() -> None:
    event = Event(type="skill", payload={"name": "resumir-prs"}, session_id="s-7f3a")
    data = event.as_dict()
    assert set(data) == {"type", "session_id", "at", "payload"}
    assert data["session_id"] == "s-7f3a"
    assert data["at"].endswith("+00:00"), "horário deve ser gravado em UTC"
