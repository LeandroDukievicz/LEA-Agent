"""O stdout de um hook é canal de protocolo, não lugar de relatório.

O `lea hook` imprimia o corpo inteiro da resposta do daemon -- `{"ok": true,
"event": ..., "context": ..., "memory": ...}` --, forma que nenhum dos dois CLIs
lê. Duas consequências, e a primeira é a que importa:

- o bloco de memória calculado no `SessionStart` era computado, devolvido,
  impresso e **descartado**. A injeção de contexto da Fase 2 estava inerte nos
  backends de CLI, sem nada no código dizendo isso: o dado chegava correto até a
  última linha e morria na forma errada;
- todo hook, a cada ferramenta usada, escrevia JSON desconhecido em stdout.

Formato conferido em 2026-11-09 contra o hook do ai-memory rodando nesta
máquina, que injeta contexto e funciona: `hookSpecificOutput.additionalContext`.
"""

import json
import subprocess
import sys
from pathlib import Path


def _hook(evento: str, corpo: dict, lea_home: Path, porta: str = "7999") -> str:
    """Roda o `lea hook` num daemon inexistente e devolve o stdout.

    Porta onde nada escuta de propósito: o hook cai na falha-aberta, que é a
    invariante do projeto -- hook nunca derruba a sessão do agente.
    """
    import os

    resultado = subprocess.run(
        [sys.executable, "-m", "lea", "hook", evento],
        input=json.dumps(corpo),
        capture_output=True,
        text=True,
        env={**os.environ, "LEA_HOME": str(lea_home), "LEA_SERVER__PORT": porta},
        timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout


def test_hook_sem_nada_a_dizer_nao_escreve_em_stdout(lea_home):
    """Silêncio é a resposta certa: não há o que o CLI deva ler."""
    for evento in ("PostToolUse", "PreCompact", "Stop", "SessionEnd"):
        assert _hook(evento, {"session_id": "s-1"}, lea_home).strip() == "", evento


def test_o_contexto_sai_no_campo_que_o_cli_le(monkeypatch, lea_home):
    """`hookSpecificOutput.additionalContext`, e não `context` na raiz."""
    from typer.testing import CliRunner

    from lea import cli

    class _Resposta:
        status_code = 200

        def json(self):
            return {"ok": True, "event": "SessionStart", "context": "BLOCO DE MEMÓRIA"}

        def raise_for_status(self):
            return None

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resposta())
    monkeypatch.setattr(cli.tokens, "hook_token", lambda: "t")

    saida = CliRunner().invoke(cli.app, ["hook", "SessionStart"], input="{}")
    assert saida.exit_code == 0, saida.output
    corpo = json.loads(saida.stdout)
    assert corpo["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert corpo["hookSpecificOutput"]["additionalContext"] == "BLOCO DE MEMÓRIA"
    # E nada do relatório interno vaza para o canal de protocolo.
    assert "ok" not in corpo


def test_sessionstart_sem_contexto_fica_calado(monkeypatch, lea_home):
    """Bloco vazio não vira `additionalContext` vazio: não há o que injetar."""
    from typer.testing import CliRunner

    from lea import cli

    class _Resposta:
        status_code = 200

        def json(self):
            return {"ok": True, "event": "SessionStart", "context": "   "}

        def raise_for_status(self):
            return None

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resposta())
    monkeypatch.setattr(cli.tokens, "hook_token", lambda: "t")

    saida = CliRunner().invoke(cli.app, ["hook", "SessionStart"], input="{}")
    assert saida.exit_code == 0
    assert saida.stdout.strip() == ""

