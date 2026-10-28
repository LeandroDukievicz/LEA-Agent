"""Tokens locais: um para a interface, outro para os hooks dos harnesses.

O daemon escuta em 127.0.0.1, mas isso sozinho não autentica nada -- qualquer
processo da máquina alcança a porta. O token é o que separa "a janela do LEA"
de "uma página web aberta no navegador tentando falar com a porta 7420".

São dois segredos distintos de propósito (ver ``paths.hook_token_file``).
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from lea import paths

#: 32 bytes em base64url ~ 43 caracteres. Sobra entropia para o uso.
TOKEN_BYTES = 32


def _read_or_create(path: Path) -> str:
    """Lê o token de ``path``, criando-o na primeira vez.

    A escrita é feita com ``O_EXCL`` num arquivo temporário e depois renomeada,
    de modo que dois processos subindo ao mesmo tempo não produzam um arquivo
    pela metade -- e para o segredo nunca existir em disco com permissão larga,
    nem por um instante.
    """
    existing = _read(path)
    if existing is not None:
        return existing

    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, paths.SECRET_MODE)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(token)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

    # Perdeu a corrida com outro processo? Quem escreveu por último venceu, e é
    # o conteúdo em disco que vale -- devolvemos ele, não o nosso.
    return _read(path) or token


def _read(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def ui_token() -> str:
    """Token que a interface apresenta ao daemon."""
    return _read_or_create(paths.ui_token_file())


def hook_token() -> str:
    """Token que ``lea hook`` apresenta ao daemon."""
    return _read_or_create(paths.hook_token_file())


def rotate(path: Path) -> str:
    """Descarta o token de ``path`` e emite outro.

    Quem estava usando o antigo passa a tomar 401 -- é o efeito desejado quando
    o segredo vazou.
    """
    path.unlink(missing_ok=True)
    return _read_or_create(path)


def matches(expected: str, presented: str | None) -> bool:
    """Compara tokens em tempo constante.

    ``==`` em string sai no primeiro byte diferente, e a diferença de tempo
    entre uma comparação que falha no byte 1 e uma que falha no byte 40 é
    medível pela rede local. ``compare_digest`` não tem esse vazamento.
    """
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)
