"""A wiki: repositório git onde as páginas de memória vivem.

Usamos o **binário do git por subprocess**, não uma ligação nativa. Motivos:
a Fase 3 precisa de ``git worktree`` de qualquer forma, então o git já é
requisito duro; e empacotar ``libgit2`` com PyInstaller é uma dor conhecida.
Menos dependência nativa, menor superfície de ataque na cadeia de suprimentos.

Todo comando roda com lista de argumentos, nunca por shell: não há string de
comando para um título de página malicioso escapar.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from lea.memory.page import Page, PageError, parse

logger = logging.getLogger(__name__)

#: Identidade usada quando o autor não é informado.
DEFAULT_AUTHOR = "lea"

#: Domínio das identidades sintéticas dos agentes.
AUTHOR_DOMAIN = "lea.local"

#: Nome do diretório de páginas globais, que não pertencem a projeto.
GLOBAL_DIR = "_global"

#: Onde os handoffs viram páginas citáveis.
HANDOFF_DIR = "handoffs"


class GitError(RuntimeError):
    """Um comando git falhou."""


@dataclass(frozen=True)
class Version:
    """Um commit que tocou a página."""

    sha: str
    author: str
    at: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"sha": self.sha, "author": self.author, "at": self.at, "message": self.message}


class Wiki:
    """Operações de leitura e escrita sobre o repositório da memória."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ── infraestrutura ───────────────────────────────────────────────────

    async def ensure_repo(self) -> None:
        """Cria o repositório na primeira vez. Idempotente."""
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / ".git").exists():
            return

        await self._git("init", "--initial-branch=main")
        # Identidade local: sem isto, o commit falha em máquina onde o usuário
        # nunca configurou user.email -- e a wiki é nossa, não dele.
        await self._git("config", "user.name", DEFAULT_AUTHOR)
        await self._git("config", "user.email", f"{DEFAULT_AUTHOR}@{AUTHOR_DOMAIN}")

        for folder in (GLOBAL_DIR, HANDOFF_DIR):
            (self.root / folder).mkdir(exist_ok=True)

        gitignore = self.root / ".gitignore"
        gitignore.write_text("*.tmp\n.DS_Store\n", encoding="utf-8")
        readme = self.root / "README.md"
        readme.write_text(_WIKI_README, encoding="utf-8")

        await self._git("add", "-A")
        await self._commit("wiki inicial", DEFAULT_AUTHOR)

    @property
    def exists(self) -> bool:
        return (self.root / ".git").exists()

    # ── leitura ──────────────────────────────────────────────────────────

    def page_path(self, relative: str) -> Path:
        """Resolve um caminho relativo para dentro da wiki, com segurança.

        Barra a travessia de diretório: um ``path`` vindo de página ou de
        chamada MCP não pode escrever em ``../../.ssh/authorized_keys``.
        """
        candidate = (self.root / relative).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise GitError(f"caminho fora da wiki: {relative}")
        return candidate

    def read(self, relative: str) -> Page:
        """Lê e interpreta uma página. Levanta ``PageError`` se o formato quebrou."""
        path = self.page_path(relative)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise GitError(f"página não encontrada: {relative}") from None
        return parse(text, path=relative)

    def iter_paths(self) -> list[str]:
        """Caminhos relativos de todo ``.md`` da wiki, em ordem estável."""
        if not self.root.exists():
            return []
        found = [
            str(PurePosixPath(p.relative_to(self.root)))
            for p in self.root.rglob("*.md")
            if ".git" not in p.parts and p.name != "README.md"
        ]
        return sorted(found)

    def load_all(self) -> tuple[list[Page], list[tuple[str, str]]]:
        """Lê toda a wiki. Devolve (páginas, erros) -- erro não interrompe.

        Uma página com frontmatter quebrado não pode impedir a reindexação das
        outras; ela vira um aviso que o ``doctor`` mostra.
        """
        pages: list[Page] = []
        problems: list[tuple[str, str]] = []
        for relative in self.iter_paths():
            try:
                pages.append(self.read(relative))
            except (PageError, GitError) as exc:
                problems.append((relative, str(exc)))
        return pages, problems

    # ── escrita ──────────────────────────────────────────────────────────

    async def write(self, page: Page, *, message: str, author: str | None = None) -> str:
        """Grava a página e faz commit. Devolve o sha.

        Toda escrita é um commit: é isso que dá histórico, diff e `git revert`
        de graça, e o que permite reconstruir o índice a partir do git.
        """
        if not page.path:
            raise GitError("página sem caminho; não dá para gravar")

        await self.ensure_repo()
        target = self.page_path(page.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page.to_markdown(), encoding="utf-8")

        await self._git("add", "--", page.path)
        return await self._commit(message, author or page.updated_by or DEFAULT_AUTHOR)

    async def delete(self, relative: str, *, message: str, author: str | None = None) -> str:
        """Apaga uma página com commit.

        Só o usuário chega aqui: decaimento nunca apaga nada.
        """
        self.page_path(relative)  # valida que está dentro da wiki
        await self._git("rm", "--quiet", "--", relative)
        return await self._commit(message, author or DEFAULT_AUTHOR)

    # ── histórico ────────────────────────────────────────────────────────

    async def versions(self, relative: str, limit: int = 50) -> list[Version]:
        """Commits que tocaram a página, do mais novo para o mais antigo."""
        if not self.exists:
            return []
        # %x1f separa campos e %x1e separa registros: são bytes de controle que
        # não aparecem em mensagem de commit, ao contrário de | ou tab.
        out = await self._git(
            "log",
            f"--max-count={limit}",
            "--format=%H%x1f%an%x1f%aI%x1f%s%x1e",
            "--",
            relative,
        )
        versions = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, author, at, message = record.split("\x1f")
            versions.append(Version(sha=sha, author=author, at=at, message=message))
        return versions

    async def diff(self, relative: str, sha: str) -> str:
        """Diff da página naquele commit."""
        _validate_sha(sha)
        return await self._git("show", f"{sha}", "--format=", "--", relative)

    async def content_at(self, relative: str, sha: str) -> str:
        """Conteúdo da página como estava naquele commit."""
        _validate_sha(sha)
        return await self._git("show", f"{sha}:{relative}")

    async def head_sha(self) -> str | None:
        """Sha do HEAD, ou ``None`` numa wiki ainda sem commit."""
        if not self.exists:
            return None
        try:
            return (await self._git("rev-parse", "HEAD")).strip()
        except GitError:
            return None

    # ── internos ─────────────────────────────────────────────────────────

    async def _commit(self, message: str, author: str) -> str:
        """Faz commit atribuído ao agente, se houver algo em stage."""
        status = await self._git("status", "--porcelain")
        if not status.strip():
            # Reescrever o mesmo conteúdo não gera commit vazio.
            return (await self.head_sha()) or ""

        identity = _sanitize_author(author)
        await self._git(
            "-c",
            f"user.name={identity}",
            "-c",
            f"user.email={identity}@{AUTHOR_DOMAIN}",
            "commit",
            "--quiet",
            "--message",
            message,
        )
        return (await self.head_sha()) or ""

    async def _git(self, *args: str) -> str:
        """Roda git na raiz da wiki e devolve o stdout."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args[:2])} falhou: {detail}")
        return stdout.decode("utf-8", "replace")


def _sanitize_author(author: str) -> str:
    """Reduz o nome do agente a algo seguro para identidade de commit.

    Vem de configuração de harness e de payload de hook, então é entrada
    externa. Sem shell no caminho não há injeção de comando, mas uma quebra de
    linha aqui corromperia o objeto de commit.
    """
    cleaned = "".join(ch for ch in author if ch.isalnum() or ch in "-_.").strip("-_.")
    return cleaned[:64] or DEFAULT_AUTHOR


def _validate_sha(sha: str) -> None:
    """Aceita só hexadecimal: o sha chega por URL e vira argumento do git."""
    if not sha or len(sha) > 64 or any(ch not in "0123456789abcdefABCDEF" for ch in sha):
        raise GitError(f"sha inválido: {sha!r}")


def utc_from_git(value: str) -> datetime:
    """Converte a data ISO do git para datetime com fuso."""
    return datetime.fromisoformat(value)


_WIKI_README = """\
# Wiki de memória do LEA

Cada `.md` aqui é uma página de memória: frontmatter YAML com `id`, `title`,
`tier`, `strength` e `tags`, seguido do corpo em Markdown. Links internos usam
`[[slug]]` ou `[[projeto/slug]]`.

Pode editar à mão. O LEA reindexa a partir dos arquivos -- eles são a fonte da
verdade, e o banco é só índice.

- `_global/` preferências e fatos que valem para todo projeto
- `handoffs/` passagens de bastão entre agentes
- `<projeto>/` memória daquele projeto
"""
