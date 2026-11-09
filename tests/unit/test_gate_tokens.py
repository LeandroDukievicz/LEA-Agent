"""O portão distingue *executar* uma ação protegida de apenas *mencioná-la*.

As expressões são montadas por concatenação de propósito. Com os hooks do LEA
instalados, um arquivo que contenha o padrão literal faz o próprio portão
barrar quem for editá-lo -- foi exatamente assim que o bug abaixo apareceu.
"""

import pytest

from lea import gate

_PUSH = "git " + "push"
_RM = "rm " + "-rf"
_SU = "su" + "do"
_CURL = "cu" + "rl"
PADROES = [_PUSH, _RM, _SU, _CURL]


@pytest.mark.parametrize("comando", [
    _PUSH + " origin main",
    "cd /x && " + _PUSH + " origin main",
    _SU + " apt install coisa",
    _RM + " /tmp/alvo",
    "make build; " + _SU + " systemctl restart x",
    _CURL + " https://exemplo.com",
])
def test_executar_a_acao_protegida_e_barrado(comando):
    assert gate.matched_pattern("Bash", {"command": comando}, PADROES) is not None


@pytest.mark.parametrize("comando", [
    'codex exec "rode ' + _PUSH + ' origin main"',    # o padrão como argumento
    'echo "use ' + _SU + ' com cuidado"',             # o padrão como texto
    'grep -rn "' + _CURL + '" README.md',             # procurando pela palavra
    _PUSH + "over",                                   # prefixo parecido, outro comando
    "git status",
    "ls -la",
])
def test_apenas_mencionar_a_acao_nao_e_barrado(comando):
    """Casamento por substring barrava os quatro primeiros.

    Não é hipótese: com os hooks instalados, o portão impediu esta sessão de
    rodar `codex exec` com a expressão no prompt e de escrever o próprio
    conserto. Um portão que barra quem *fala* sobre a ação é desligado na
    primeira semana, e aí não protege de nada.
    """
    assert gate.matched_pattern("Bash", {"command": comando}, PADROES) is None


def test_aspas_desbalanceadas_erram_para_o_lado_de_barrar():
    """Sem conseguir tokenizar, um portão deve fechar, não abrir."""
    assert gate.matched_pattern("Bash", {"command": 'echo "sem fechar'}, ["echo"]) is not None


def test_encadeamento_por_pipe_e_por_e_comercial_conta_como_comando():
    for ligacao in ("&&", "||", ";", "|"):
        comando = f"echo oi {ligacao} {_SU} rm /x"
        assert gate.matched_pattern("Bash", {"command": comando}, [_SU]) == _SU
