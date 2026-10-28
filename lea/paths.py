"""Layout de ``~/.lea`` e criação dos diretórios de dados do usuário.

Um único módulo resolve todo caminho. Se cada parte do código montasse o seu,
dois call sites acabariam discordando sobre onde a wiki mora -- é assim que
nasce "a memória sumiu depois que troquei de perfil".

``LEA_HOME`` sobrepõe a raiz, o que mantém os testes fora do diretório real do
usuário e permite perfis separados.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Permissão de arquivo que guarda segredo: só o dono lê e escreve.
SECRET_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600

#: Permissão de diretório privado: só o dono entra.
PRIVATE_DIR_MODE = stat.S_IRWXU  # 0o700


def home() -> Path:
    """Raiz dos dados do usuário (``~/.lea``, ou ``LEA_HOME`` se definido)."""
    override = os.environ.get("LEA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".lea"


def config_file() -> Path:
    return home() / "config.toml"


def database_file() -> Path:
    return home() / "lea.db"


def wiki_dir() -> Path:
    """Repositório git da memória."""
    return home() / "wiki"


def skills_dir() -> Path:
    return home() / "skills"


def worktrees_dir() -> Path:
    """Worktrees criadas pelo launcher ao iniciar uma sessão isolada."""
    return home() / "worktrees"


def logs_dir() -> Path:
    return home() / "logs"


def ui_token_file() -> Path:
    """Token que a interface usa para falar com o daemon."""
    return home() / "ui_token"


def hook_token_file() -> Path:
    """Token que o comando ``lea hook`` usa. Separado do da UI de propósito:
    os hooks rodam dentro de processos de terceiros (Claude Code, Codex), então
    o segredo deles circula por mais lugares e precisa poder ser rotacionado
    sem derrubar a janela aberta do usuário."""
    return home() / "hook_token"


#: Diretórios criados no primeiro uso, todos privados.
DATA_DIRS = (home, wiki_dir, skills_dir, worktrees_dir, logs_dir)


def ensure_layout() -> Path:
    """Cria ``~/.lea`` e os subdiretórios, com permissão restrita. Idempotente.

    Em diretório que já existe a permissão é corrigida: uma instalação antiga
    (ou um ``umask`` frouxo) pode ter deixado a wiki legível para todo mundo da
    máquina, e ela guarda o que os agentes aprenderam sobre os seus projetos.
    """
    root = home()
    for factory in DATA_DIRS:
        path = factory()
        path.mkdir(parents=True, exist_ok=True)
        _harden_dir(path)
    return root


def _harden_dir(path: Path) -> None:
    """Aplica 0700, ignorando sistemas de arquivos que não suportam chmod."""
    try:
        if stat.S_IMODE(path.stat().st_mode) != PRIVATE_DIR_MODE:
            path.chmod(PRIVATE_DIR_MODE)
    except (OSError, NotImplementedError):
        # Montagens tipo FAT/exFAT não têm modo POSIX. Não é motivo para o LEA
        # se recusar a iniciar; o `lea doctor` reporta o que ficou frouxo.
        pass


def is_secret_tight(path: Path) -> bool:
    """True se ``path`` existe e só o dono pode lê-lo."""
    try:
        return stat.S_IMODE(path.stat().st_mode) == SECRET_MODE
    except OSError:
        return False
