"""Fixtures compartilhadas.

Todo teste roda contra um ``LEA_HOME`` temporário. Um teste que escrevesse no
``~/.lea`` real poderia apagar a wiki de quem está rodando a suíte.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def lea_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Aponta ``LEA_HOME`` para um diretório temporário."""
    home = tmp_path / "lea-home"
    monkeypatch.setenv("LEA_HOME", str(home))

    # config.py resolve o padrão de wiki_path na importação do módulo, via
    # default_factory. Recarregar garante que ele enxergue o LEA_HOME do teste.
    import lea.config
    import lea.paths

    importlib.reload(lea.paths)
    importlib.reload(lea.config)

    yield home


@pytest.fixture
def db_path(lea_home: Path) -> Path:
    return lea_home / "lea.db"
