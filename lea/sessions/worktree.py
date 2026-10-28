"""Worktrees git: cada sessão autônoma no seu próprio diretório de trabalho.

Dois agentes editando o mesmo checkout ao mesmo tempo é receita para um
sobrescrever o outro. Uma worktree por sessão dá a cada um um diretório e um
branch próprios, com o mesmo histórico git por baixo.

Worktree **não é apagada** quando a sessão acaba: pode haver trabalho não
commitado lá dentro. Remover é ação explícita do usuário.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from lea import paths

logger = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    """Falha ao criar ou remover uma worktree."""


def path_for(session_id: str) -> Path:
    return paths.worktrees_dir() / session_id


async def create(repo: str | Path, target: str | Path, branch: str) -> Path:
    """Cria a worktree em ``target`` com um branch novo.

    Se o branch já existir, faz checkout dele em vez de falhar -- é o caso de
    retomar um handoff que já tinha branch.
    """
    repo_path = Path(repo)
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        raise WorktreeError(f"já existe algo em {target_path}")

    if await _branch_exists(repo_path, branch):
        code, err = await _git(repo_path, "worktree", "add", str(target_path), branch)
    else:
        code, err = await _git(repo_path, "worktree", "add", "-b", branch, str(target_path))

    if code != 0:
        raise WorktreeError(f"git worktree add falhou: {err.strip()[:300]}")
    return target_path


async def remove(repo: str | Path, target: str | Path, force: bool = False) -> None:
    """Remove a worktree. Sem ``force``, o git recusa se houver mudança solta."""
    args = ["worktree", "remove", str(target)]
    if force:
        args.append("--force")
    code, err = await _git(Path(repo), *args)
    if code != 0:
        raise WorktreeError(f"git worktree remove falhou: {err.strip()[:300]}")


async def listing(repo: str | Path) -> list[dict[str, str]]:
    """Worktrees do repositório, como o git as vê."""
    code, out = await _git_out(Path(repo), "worktree", "list", "--porcelain")
    if code != 0:
        return []

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


async def _branch_exists(repo: Path, branch: str) -> bool:
    code, _ = await _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return code == 0


async def _git(repo: Path, *args: str) -> tuple[int, str]:
    code, _, err = await _run("git", "-C", str(repo), *args)
    return code, err


async def _git_out(repo: Path, *args: str) -> tuple[int, str]:
    code, out, _ = await _run("git", "-C", str(repo), *args)
    return code, out


async def _run(*args: str) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except (FileNotFoundError, asyncio.TimeoutError) as exc:
        return 1, "", str(exc)
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
