"""Adaptadores de harness.

O foco destes testes é o que a especificação chama de risco principal:
**nunca sobrescrever a configuração do usuário**. Os casos usam fixtures com
o formato real, capturado de instalações reais (Claude Code 2.1.278 e
codex-cli 0.164.0) -- inclusive com hooks de *outra* ferramenta já presentes,
que foi o que havia na máquina onde isto foi escrito.

Nenhum teste toca o ``~/.claude`` ou o ``~/.codex`` de verdade: todos apontam
``home`` para um diretório temporário.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from lea.harnesses import hooks_file
from lea.harnesses.base import LEA_HOOK_EVENTS, Health
from lea.harnesses.claude_code import ClaudeCode
from lea.harnesses.codex import Codex

HOOK_CMD = "/opt/lea/bin/lea hook"

#: Config de outra ferramenta, no formato exato que foi encontrado em uso.
OUTRA_FERRAMENTA = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": "/home/user/.local/lib/ai-memory/ai-memory hook --event post-tool-use",
        }
    ],
}


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "model": "sonnet",
                "statusLine": {"type": "command", "command": "meu-statusline"},
                "hooks": {"PostToolUse": [OUTRA_FERRAMENTA]},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return home


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex"
    home.mkdir()
    (home / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [OUTRA_FERRAMENTA]}}, indent=2), encoding="utf-8"
    )
    (home / "config.toml").write_text(
        'model = "gpt-6"\n\n[projects."/home/user/x"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    return home


# ── merge de hooks ───────────────────────────────────────────────────────


def test_upsert_preserva_entrada_de_outra_ferramenta() -> None:
    hooks_map = {"PostToolUse": [dict(OUTRA_FERRAMENTA)]}
    hooks_file.upsert(hooks_map, "PostToolUse", f"{HOOK_CMD} PostToolUse", "/opt/lea/bin/lea")

    comandos = [e["command"] for g in hooks_map["PostToolUse"] for e in g["hooks"]]
    assert any("ai-memory" in c for c in comandos), "a entrada alheia sumiu"
    assert any("lea hook" in c for c in comandos)


def test_upsert_e_idempotente() -> None:
    hooks_map: dict = {}
    comando = f"{HOOK_CMD} Stop"
    assert hooks_file.upsert(hooks_map, "Stop", comando, "/opt/lea/bin/lea") is True
    assert hooks_file.upsert(hooks_map, "Stop", comando, "/opt/lea/bin/lea") is False
    assert len(hooks_map["Stop"]) == 1


def test_upsert_atualiza_entrada_nossa_desatualizada() -> None:
    """Instalação antiga apontando para outro caminho é corrigida, não duplicada."""
    hooks_map = {
        "Stop": [
            {"matcher": "", "hooks": [{"type": "command", "command": "/antigo/lea hook Stop"}]}
        ]
    }
    hooks_file.upsert(hooks_map, "Stop", "/antigo/lea hook Stop --novo", "/antigo/lea")
    comandos = [e["command"] for g in hooks_map["Stop"] for e in g["hooks"]]
    assert comandos == ["/antigo/lea hook Stop --novo"]


def test_remove_tira_so_as_nossas() -> None:
    hooks_map = {"PostToolUse": [dict(OUTRA_FERRAMENTA)]}
    hooks_file.upsert(hooks_map, "PostToolUse", f"{HOOK_CMD} PostToolUse", "/opt/lea/bin/lea")

    assert hooks_file.remove_ours(hooks_map, "/opt/lea/bin/lea") == 1
    restantes = [e["command"] for g in hooks_map["PostToolUse"] for e in g["hooks"]]
    assert restantes == [OUTRA_FERRAMENTA["hooks"][0]["command"]]


def test_json_quebrado_levanta_em_vez_de_virar_dicionario_vazio(tmp_path: Path) -> None:
    """Seguir com {} faria o merge apagar a config por um erro de digitação."""
    ruim = tmp_path / "settings.json"
    ruim.write_text('{"hooks": {', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        hooks_file.read_json(ruim)


def test_write_json_preserva_permissao(tmp_path: Path) -> None:
    alvo = tmp_path / "settings.json"
    alvo.write_text("{}", encoding="utf-8")
    alvo.chmod(0o600)
    hooks_file.write_json(alvo, {"a": 1})
    assert alvo.stat().st_mode & 0o777 == 0o600


def test_backup_nao_sobrescreve_o_anterior(tmp_path: Path) -> None:
    alvo = tmp_path / "settings.json"
    alvo.write_text("v1", encoding="utf-8")
    primeiro = hooks_file.backup(alvo)
    alvo.write_text("v2", encoding="utf-8")
    segundo = hooks_file.backup(alvo)

    assert primeiro and segundo
    # Mesmo criados no mesmo segundo, os dois arquivos precisam existir.
    assert primeiro.exists() and segundo.exists()


def test_backup_de_arquivo_inexistente_e_none(tmp_path: Path) -> None:
    assert hooks_file.backup(tmp_path / "nao-existe.json") is None


# ── Claude Code ──────────────────────────────────────────────────────────


def test_claude_instala_hooks_sem_perder_config(claude_home: Path) -> None:
    from lea.harnesses.base import InstallReport

    report = InstallReport(harness="claude-code")
    ClaudeCode(home=claude_home)._install_hooks(HOOK_CMD, report)

    settings = json.loads((claude_home / "settings.json").read_text())

    # O que era do usuário continua lá.
    assert settings["model"] == "sonnet"
    assert settings["statusLine"]["command"] == "meu-statusline"
    comandos = [
        e["command"] for g in settings["hooks"]["PostToolUse"] for e in g["hooks"]
    ]
    assert any("ai-memory" in c for c in comandos)

    # E os nossos entraram, um por evento.
    for event in LEA_HOOK_EVENTS:
        presentes = [
            e["command"] for g in settings["hooks"][event] for e in g["hooks"]
        ]
        assert f"{HOOK_CMD} {event}" in presentes

    assert report.backups, "instalar sem backup é inaceitável"
    assert not report.problems


def test_claude_instalar_duas_vezes_nao_duplica(claude_home: Path) -> None:
    from lea.harnesses.base import InstallReport

    adaptador = ClaudeCode(home=claude_home)
    for _ in range(2):
        adaptador._install_hooks(HOOK_CMD, InstallReport(harness="claude-code"))

    settings = json.loads((claude_home / "settings.json").read_text())
    entradas = [
        e["command"] for g in settings["hooks"]["Stop"] for e in g["hooks"]
    ]
    assert entradas.count(f"{HOOK_CMD} Stop") == 1


def test_claude_settings_quebrado_nao_e_sobrescrito(tmp_path: Path) -> None:
    from lea.harnesses.base import InstallReport

    home = tmp_path / "claude"
    home.mkdir()
    original = '{"model": "sonnet", "hooks": {'
    (home / "settings.json").write_text(original, encoding="utf-8")

    report = InstallReport(harness="claude-code")
    ClaudeCode(home=home)._install_hooks(HOOK_CMD, report)

    assert report.problems, "deveria relatar o problema"
    assert (home / "settings.json").read_text() == original, "o arquivo foi alterado"


def test_claude_saude_sem_hooks_e_missing(tmp_path: Path) -> None:
    from lea.harnesses.base import HarnessStatus

    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text("{}", encoding="utf-8")

    state = HarnessStatus(id="claude-code", code="CC", name="Claude Code", installed=True)
    assert ClaudeCode(home=home)._hooks_health(HOOK_CMD, state) is Health.MISSING


def test_claude_saude_parcial_e_error(claude_home: Path) -> None:
    """Metade instalada é ◐, não ●: o usuário precisa ver que falta algo."""
    from lea.harnesses.base import HarnessStatus

    settings = json.loads((claude_home / "settings.json").read_text())
    settings["hooks"]["Stop"] = [
        {"matcher": "", "hooks": [{"type": "command", "command": f"{HOOK_CMD} Stop"}]}
    ]
    (claude_home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    state = HarnessStatus(id="claude-code", code="CC", name="Claude Code", installed=True)
    assert ClaudeCode(home=claude_home)._hooks_health(HOOK_CMD, state) is Health.ERROR
    assert state.notes


def test_claude_saude_completa_e_ok(claude_home: Path) -> None:
    from lea.harnesses.base import HarnessStatus, InstallReport

    adaptador = ClaudeCode(home=claude_home)
    adaptador._install_hooks(HOOK_CMD, InstallReport(harness="claude-code"))

    state = HarnessStatus(id="claude-code", code="CC", name="Claude Code", installed=True)
    assert adaptador._hooks_health(HOOK_CMD, state) is Health.OK


# ── Codex ────────────────────────────────────────────────────────────────


def test_codex_instala_hooks_preservando_os_alheios(codex_home: Path) -> None:
    from lea.harnesses.base import InstallReport

    report = InstallReport(harness="codex")
    Codex(home=codex_home)._install_hooks(HOOK_CMD, report)

    document = json.loads((codex_home / "hooks.json").read_text())
    comandos = [e["command"] for g in document["hooks"]["SessionStart"] for e in g["hooks"]]
    assert any("ai-memory" in c for c in comandos)
    assert f"{HOOK_CMD} SessionStart" in comandos


def test_codex_avisa_que_precisa_de_confianca(codex_home: Path) -> None:
    """O Codex não roda hook novo até o usuário confiar nele."""
    from lea.harnesses.base import InstallReport

    report = InstallReport(harness="codex")
    Codex(home=codex_home)._install_hooks(HOOK_CMD, report)

    assert report.manual_steps, "instalar sem avisar deixaria o usuário achando que funciona"
    aviso = report.manual_steps[0]
    assert "confiar" in aviso.lower()
    assert "dangerously" in aviso.lower(), "precisa desaconselhar explicitamente o bypass"


def test_codex_hook_sem_confianca_e_error(codex_home: Path) -> None:
    from lea.harnesses.base import HarnessStatus, InstallReport

    adaptador = Codex(home=codex_home)
    adaptador._install_hooks(HOOK_CMD, InstallReport(harness="codex"))

    state = HarnessStatus(id="codex", code="CX", name="Codex CLI", installed=True)
    assert adaptador._hooks_health(HOOK_CMD, state) is Health.ERROR
    assert any("confiança" in n for n in state.notes)


def test_codex_hook_confiado_e_ok(codex_home: Path) -> None:
    from lea.harnesses.base import HarnessStatus, InstallReport

    adaptador = Codex(home=codex_home)
    adaptador._install_hooks(HOOK_CMD, InstallReport(harness="codex"))

    # Simula a confiança que o Codex grava depois que o usuário aceita.
    hooks_path = adaptador.hooks_file_path
    linhas = ["[hooks.state]"]
    for snake in ("session_start", "post_tool_use", "pre_compact", "stop"):
        linhas.append(f'[hooks.state."{hooks_path}:{snake}:0:0"]')
        linhas.append('trusted_hash = "sha256:abc"')
    (codex_home / "config.toml").write_text("\n".join(linhas) + "\n", encoding="utf-8")

    state = HarnessStatus(id="codex", code="CX", name="Codex CLI", installed=True)
    assert adaptador._hooks_health(HOOK_CMD, state) is Health.OK


def test_codex_config_toml_quebrado_nao_derruba_a_saude(codex_home: Path) -> None:
    (codex_home / "config.toml").write_text("isto [ nao e toml", encoding="utf-8")
    assert Codex(home=codex_home)._read_config() == {}
    assert Codex(home=codex_home)._mcp_in_config() is False


def test_fixture_do_codex_reflete_o_formato_real(codex_home: Path) -> None:
    """Guarda-costas: se o formato mudar, o teste conta o porquê."""
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert "projects" in config, "config.toml do Codex usa [projects.\"<path>\"]"
