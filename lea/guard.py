"""A coleira do publicador: um hook de git que barra push de dentro da frota.

O portão (``lea/gate.py``) resolve Codex e Claude Code, porque os dois expõem
``PreToolUse``. O **agy não expõe hook nenhum** -- verificado na 1.4.10, sem
menção em ``agy --help`` e sem a chave no ``settings.json``. E o agy é
justamente o publicador, o único papel da frota cujo trabalho sai da máquina.

Para ele a trava precisa viver num lugar que não dependa do CLI cooperar. O
``pre-push`` do git é esse lugar: quem executa é o próprio git, antes de
qualquer byte sair, e nenhuma flag de CLI o desliga.

Como ele sabe que o push vem da frota: o ``Launcher`` exporta ``LEA_SESSION_ID``
no ambiente do processo que abre. O hook lê essa variável. Push que você der
no seu terminal não tem ela e passa direto -- a coleira é para o agente, não
para você.

Escrever em ``.git/hooks`` é mexer no repositório de quem chama, então isso
nunca roda sozinho: é ``lea fleet guard --install``, explícito, com backup de
qualquer hook que já esteja lá.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

#: Importado do launcher, nunca redigitado: o nome da variável é o contrato
#: entre quem abre a sessão e quem a reconhece. Escrevê-lo à mão aqui já me
#: custou um hook que nunca disparava, porque o launcher exporta
#: LEA_SESSION_ID e eu tinha escrito LEA_SESSION.
from lea.sessions.launcher import ENV_SESSION

#: Linha que identifica o hook como nosso, para reinstalar sem duplicar e para
#: nunca remover o hook de outra ferramenta por engano.
MARKER = "# lea-fleet-guard"

HOOK_BODY = f"""#!/usr/bin/env bash
{MARKER}
# Coleira da frota do LEA. Barra push vindo de uma sessão que o LEA abriu.
#
# O agy (publicador) não expõe hooks de ciclo de vida, então a trava dele não
# pode viver no CLI. Aqui ela vive no git: quem executa é o próprio git, antes
# de qualquer byte sair, e nenhuma flag de CLI desliga isto.
#
# Push que você der no seu terminal não tem essa variável e passa direto.
set -euo pipefail

if [ -z "${{{ENV_SESSION}:-}}" ]; then
  exit 0
fi

cat >/dev/null || true   # drena a lista de refs; o git fecha o pipe se não lermos

echo "LEA: push bloqueado — esta sessão é da frota ({ENV_SESSION}=${{{ENV_SESSION}}})." >&2
echo "O publicador prepara o commit; quem publica é você." >&2
echo "Para publicar: rode 'git push' no seu terminal, fora da sessão." >&2
exit 1
"""


@dataclass
class GuardReport:
    """O que aconteceu ao instalar ou remover."""

    path: Path
    action: str                 # "instalado" | "atualizado" | "removido" | "intacto"
    backup: Path | None = None
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def hook_path(repo: Path) -> Path:
    """Caminho do ``pre-push``, respeitando ``core.hooksPath`` e worktree.

    Num worktree, ``.git`` é um arquivo apontando para o diretório real. Sem
    resolver isso o hook seria escrito num lugar que o git nunca lê.
    """
    git = repo / ".git"
    if git.is_file():
        texto = git.read_text(encoding="utf-8", errors="replace").strip()
        if texto.startswith("gitdir:"):
            alvo = Path(texto.split(":", 1)[1].strip())
            git = alvo if alvo.is_absolute() else (repo / alvo).resolve()
            # Em worktree os hooks ficam no repositório principal.
            commondir = git / "commondir"
            if commondir.is_file():
                comum = commondir.read_text(encoding="utf-8").strip()
                git = (git / comum).resolve() if not Path(comum).is_absolute() else Path(comum)
    return git / "hooks" / "pre-push"


def is_ours(path: Path) -> bool:
    if not path.is_file():
        return False
    return MARKER in path.read_text(encoding="utf-8", errors="replace")


def install(repo: Path) -> GuardReport:
    """Instala o ``pre-push``, preservando qualquer hook que já exista."""
    path = hook_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return GuardReport(path=path, action="falhou", problem=f"não consegui criar {path.parent}: {exc}")

    backup: Path | None = None
    if path.is_file() and not is_ours(path):
        # Hook de outra ferramenta: nunca sobrescrever sem guardar uma cópia.
        backup = path.with_suffix(path.suffix + ".lea-backup")
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            return GuardReport(path=path, action="falhou", problem=f"não consegui fazer backup: {exc}")

    acao = "atualizado" if is_ours(path) else "instalado"
    try:
        path.write_text(HOOK_BODY, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        return GuardReport(path=path, action="falhou", problem=f"não consegui gravar {path}: {exc}")
    return GuardReport(path=path, action=acao, backup=backup)


def remove(repo: Path) -> GuardReport:
    """Remove o hook, mas só se for o nosso."""
    path = hook_path(repo)
    if not path.is_file():
        return GuardReport(path=path, action="intacto")
    if not is_ours(path):
        return GuardReport(
            path=path,
            action="intacto",
            problem="o pre-push existente não é do LEA; não vou mexer nele",
        )
    backup = path.with_suffix(path.suffix + ".lea-backup")
    try:
        path.unlink()
        if backup.is_file():
            shutil.move(str(backup), str(path))     # devolve o que estava antes
    except OSError as exc:
        return GuardReport(path=path, action="falhou", problem=str(exc))
    return GuardReport(path=path, action="removido")


def status(repo: Path) -> str:
    path = hook_path(repo)
    if is_ours(path):
        return "instalada"
    if path.is_file():
        return "outro pre-push no lugar"
    return "ausente"


def session_env() -> str | None:
    """A sessão da frota que este processo pertence, se houver."""
    return os.environ.get(ENV_SESSION) or None
