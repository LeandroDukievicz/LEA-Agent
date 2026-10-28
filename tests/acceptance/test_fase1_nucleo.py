"""Aceite da Fase 1 — núcleo.

Critério: ``lea doctor`` valida uma instalação limpa; o daemon serve CPU e
memória reais; um hook chega ao WebSocket; e o hook sai com código 0 quando o
daemon está fora, para não derrubar a sessão do agente.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


async def test_health_dispensa_token(daemon: dict) -> None:
    """A interface precisa descobrir se o daemon subiu antes de ter token."""
    r = await daemon["client"].get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_overview_exige_token(daemon: dict) -> None:
    """Loopback não autentica: qualquer processo da máquina alcança a porta."""
    assert (await daemon["client"].get("/api/overview")).status_code == 401
    r = await daemon["client"].get(
        "/api/overview", headers={"X-Lea-Token": "errado"}
    )
    assert r.status_code == 401


async def test_overview_traz_cpu_e_memoria_reais(daemon: dict) -> None:
    r = await daemon["client"].get("/api/overview", headers=daemon["headers"])
    corpo = r.json()

    assert corpo["system"]["memory_total_gb"] > 0
    assert 0 <= corpo["system"]["memory_percent"] <= 100
    assert corpo["server"]["exposed"] is False
    assert corpo["counters"]["projects"] == 0


async def test_hook_vira_evento_no_barramento(daemon: dict) -> None:
    """hook -> barramento -> WebSocket, que é o feed de atividade."""
    bus = daemon["app"].state.bus
    with bus.subscribe() as inscricao:
        r = await daemon["client"].post(
            "/hooks/PostToolUse",
            headers=daemon["hook_headers"],
            json={"session_id": "s-fora", "tool_name": "Bash"},
        )
        assert r.json()["ok"]

        evento = await asyncio.wait_for(inscricao.get(), timeout=2.0)

    assert evento.type == "tool_call"
    assert evento.payload["tool_name"] == "Bash"
    assert evento.at.endswith("+00:00"), "horário gravado em UTC"


async def test_hook_exige_token_proprio(daemon: dict) -> None:
    """O token de hook é separado do da UI de propósito."""
    r = await daemon["client"].post(
        "/hooks/Stop", headers=daemon["headers"], json={}
    )
    assert r.status_code == 401


async def test_hook_de_evento_desconhecido_e_recusado(daemon: dict) -> None:
    r = await daemon["client"].post(
        "/hooks/Inventado", headers=daemon["hook_headers"], json={}
    )
    assert r.status_code == 404


def test_hook_nao_derruba_o_agente_com_daemon_fora(lea_home: Path) -> None:
    """Um hook que quebra o Claude Code é pior que um evento perdido."""
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "LEA_HOME": str(lea_home),
        # Porta onde nada escuta.
        "LEA_SERVER__PORT": "7999",
    }
    resultado = subprocess.run(
        [sys.executable, "-m", "lea", "hook", "Stop"],
        input=json.dumps({"session_id": "s-1"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
