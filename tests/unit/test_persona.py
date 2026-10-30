"""A persona governa o tom. Estes testes fixam o que ela NÃO pode fazer."""

from types import SimpleNamespace

from lea.agent import _system_prompt
from lea.config import DEFAULT_PERSONA

VAZIO = SimpleNamespace(items=[])


def test_persona_entra_no_prompt_e_pode_ser_desligada():
    com = _system_prompt("lea-core", VAZIO, None, DEFAULT_PERSONA)
    sem = _system_prompt("lea-core", VAZIO, None, "")
    assert "humor sarcástico" in com
    assert "humor" not in sem


def test_persona_vem_depois_das_regras_e_nao_as_substitui():
    prompt = _system_prompt("lea-core", VAZIO, None, DEFAULT_PERSONA)
    # Se a persona viesse antes, um texto de tom poderia tentar reescrever as regras.
    assert prompt.index("Não afirme que executou") < prompt.index("Como você fala")
    for regra in ("não confiáveis", "aprovação", "lea-agent"):
        assert regra in prompt
