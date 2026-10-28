"""Tokens locais, permissões de arquivo e carga de configuração."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from lea import paths
from lea.security import tokens


def test_token_e_criado_com_0600(lea_home: Path) -> None:
    paths.ensure_layout()
    token = tokens.ui_token()
    assert token
    arquivo = paths.ui_token_file()
    assert stat.S_IMODE(arquivo.stat().st_mode) == 0o600
    assert paths.is_secret_tight(arquivo)


def test_token_e_estavel_entre_chamadas(lea_home: Path) -> None:
    paths.ensure_layout()
    assert tokens.ui_token() == tokens.ui_token()


def test_ui_e_hook_tem_tokens_distintos(lea_home: Path) -> None:
    """Os hooks rodam dentro de processos de terceiros; o segredo deles
    circula mais e precisa poder ser trocado sem derrubar a janela."""
    paths.ensure_layout()
    assert tokens.ui_token() != tokens.hook_token()


def test_rotate_invalida_o_anterior(lea_home: Path) -> None:
    paths.ensure_layout()
    antigo = tokens.hook_token()
    novo = tokens.rotate(paths.hook_token_file())
    assert novo != antigo
    assert tokens.hook_token() == novo


def test_matches_rejeita_ausente_e_errado() -> None:
    assert tokens.matches("abc", "abc")
    assert not tokens.matches("abc", "abd")
    assert not tokens.matches("abc", None)
    assert not tokens.matches("abc", "")


def test_ensure_layout_cria_tudo_privado(lea_home: Path) -> None:
    root = paths.ensure_layout()
    assert root.exists()
    for factory in (paths.wiki_dir, paths.skills_dir, paths.worktrees_dir, paths.logs_dir):
        caminho = factory()
        assert caminho.is_dir()
        assert stat.S_IMODE(caminho.stat().st_mode) == 0o700


def test_ensure_layout_corrige_permissao_frouxa(lea_home: Path) -> None:
    paths.ensure_layout()
    paths.wiki_dir().chmod(0o755)
    paths.ensure_layout()
    assert stat.S_IMODE(paths.wiki_dir().stat().st_mode) == 0o700


def test_lea_home_isola_do_diretorio_real(lea_home: Path) -> None:
    assert paths.home() == lea_home
    assert str(Path.home() / ".lea") not in str(paths.database_file())


# ── configuração ─────────────────────────────────────────────────────────


def test_load_cria_config_com_padroes(lea_home: Path) -> None:
    from lea import config as config_module

    paths.ensure_layout()
    config = config_module.load()

    assert paths.config_file().exists()
    assert config.server.port == 7420
    assert config.server.host == "127.0.0.1"
    assert not config.server.is_exposed
    assert config.ui.language == "pt-BR"
    assert config.memory.inject_budget_tokens == 8000
    assert config.memory.half_life_days.hot == 3


def test_host_fora_do_loopback_e_considerado_exposto(lea_home: Path) -> None:
    from lea.config import ServerConfig

    assert ServerConfig(host="0.0.0.0").is_exposed
    assert ServerConfig(host="192.168.1.10").is_exposed
    assert not ServerConfig(host="127.0.0.1").is_exposed
    assert not ServerConfig(host="localhost").is_exposed


def test_ambiente_sobrepoe_arquivo(lea_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lea import config as config_module

    paths.ensure_layout()
    config_module.load()
    monkeypatch.setenv("LEA_SERVER__PORT", "7999")
    assert config_module.Config().server.port == 7999


def test_porta_invalida_e_rejeitada(lea_home: Path) -> None:
    from pydantic import ValidationError

    from lea.config import ServerConfig

    with pytest.raises(ValidationError):
        ServerConfig(port=99999)


def test_modelo_padrao_e_da_familia_atual(lea_home: Path) -> None:
    from lea.config import AgentConfig

    assert AgentConfig().default_model == "anthropic/claude-sonnet-5"
