"""`Stop` é fim de turno; `SessionEnd` é fim de sessão.

Confundir os dois é o tipo de erro que nenhum teste pega sozinho, porque o
sintoma só aparece com um agente de verdade do outro lado: a sessão fica
marcada como encerrada e o processo continua vivo, trabalhando.

Aconteceu na frota, em 2026-11-09. O implementador (Claude Code) abriu a
sessão, rodou oito ferramentas, respondeu -- e o `Stop` daquela primeira
resposta marcou a sessão como `handoff` no banco. O `ps` mostrava o processo
de pé. A tela mentia, o handoff saía no meio do trabalho, e a fila de
aprovação daquela sessão ficava indecidível, porque decidir exige sessão viva.
"""

from lea.harnesses.base import LEA_HOOK_EVENTS
from lea.harnesses.codex import TRUST_EVENT_NAMES
from lea.server.app import _HOOK_EVENT_TYPES


def test_o_lea_instala_os_dois_eventos():
    """Instalar só o `Stop` é o que forçava tratá-lo como fim."""
    assert "Stop" in LEA_HOOK_EVENTS
    assert "SessionEnd" in LEA_HOOK_EVENTS


def test_so_o_sessionend_encerra():
    assert _HOOK_EVENT_TYPES["SessionEnd"] == "session_end"
    assert _HOOK_EVENT_TYPES["Stop"] != "session_end"


def test_todo_evento_instalado_tem_chave_de_confianca_no_codex():
    """O fallback do Codex é `event.lower()`, que produz "sessionend" onde ele
    grava "session_end" -- a chave nunca casaria e o hook ficaria eterno em
    "aguardando confiança", sem nada explicar. O mapa precisa ser completo."""
    assert set(LEA_HOOK_EVENTS) <= set(TRUST_EVENT_NAMES)


def test_todo_evento_instalado_e_aceito_pelo_servidor():
    """Instalar um hook que o daemon responde com 404 é instalar ruído."""
    assert set(LEA_HOOK_EVENTS) <= set(_HOOK_EVENT_TYPES)
