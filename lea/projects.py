"""Projetos: abrir um caminho local, detectar o que dá para detectar.

Um projeto é um diretório + uma seção na wiki. Abrir um projeto não muda
nada dentro dele -- a não ser que o usuário peça para indexar a documentação,
e mesmo aí o que é escrito vai para a wiki do LEA, nunca para o repositório
dele.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from lea.memory import Memory

logger = logging.getLogger(__name__)

#: Arquivo indicador -> linguagem. A ordem importa: o primeiro que casar
#: ganha, então o mais específico vem antes.
LANGUAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "Python"),
    ("uv.lock", "Python"),
    ("requirements.txt", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java"),
    ("build.gradle", "Java"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
    ("pubspec.yaml", "Dart"),
    ("package.json", "JavaScript/TypeScript"),
)

#: O que indexar como página inicial quando o usuário pede.
DOC_PATTERNS: tuple[str, ...] = ("README*", "docs/**/*.md", "adr/**/*.md", "doc/**/*.md")

#: Teto de páginas indexadas na abertura. Um monorepo com centenas de
#: markdowns encheria a wiki de ruído e afogaria o que importa.
MAX_INDEXED_DOCS = 40

_SLUG_UNSAFE = re.compile(r"[^a-z0-9-]+")


class ProjectError(ValueError):
    """Caminho inválido ou projeto já existente."""


@dataclass
class ProjectInfo:
    """O que foi detectado no diretório."""

    slug: str
    name: str
    path: str
    remote: str | None = None
    default_branch: str | None = None
    language: str | None = None
    is_git: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "path": self.path,
            "remote": self.remote,
            "default_branch": self.default_branch,
            "language": self.language,
            "is_git": self.is_git,
        }


@dataclass
class OpenResult:
    """O que a abertura produziu."""

    project_id: int
    info: ProjectInfo
    indexed_pages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.project_id,
            **self.info.as_dict(),
            "indexed_pages": self.indexed_pages,
            "notes": self.notes,
        }


def slugify(value: str) -> str:
    cleaned = _SLUG_UNSAFE.sub("-", value.strip().lower())
    return cleaned.strip("-") or "projeto"


async def inspect(path: str | Path) -> ProjectInfo:
    """Olha o diretório e descobre o que der, sem alterar nada."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ProjectError(f"não é um diretório: {root}")

    info = ProjectInfo(
        slug=slugify(root.name),
        name=root.name,
        path=str(root),
        language=_detect_language(root),
        is_git=(root / ".git").exists(),
    )

    if info.is_git:
        info.default_branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        info.remote = await _git(root, "remote", "get-url", "origin")

    return info


async def open_project(
    conn: aiosqlite.Connection,
    memory: Memory,
    *,
    path: str | Path,
    name: str | None = None,
    index_docs: bool = False,
    allow_autonomous: bool = False,
) -> OpenResult:
    """Registra o projeto e, se pedido, indexa a documentação na wiki."""
    info = await inspect(path)
    if name:
        info.name = name
        info.slug = slugify(name)

    async with conn.execute(
        "SELECT id FROM projects WHERE path = ? OR slug = ?", (info.path, info.slug)
    ) as cursor:
        if await cursor.fetchone():
            raise ProjectError(f"já existe um projeto com este caminho ou slug: {info.slug}")

    cursor = await conn.execute(
        """
        INSERT INTO projects
            (slug, name, path, remote, default_branch, language, created_at, allow_autonomous)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            info.slug,
            info.name,
            info.path,
            info.remote,
            info.default_branch,
            info.language,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            int(allow_autonomous),
        ),
    )
    await conn.commit()
    project_id = int(cursor.lastrowid or 0)

    result = OpenResult(project_id=project_id, info=info)
    if not info.is_git:
        result.notes.append("não é um repositório git; worktrees ficam indisponíveis")

    if index_docs:
        result.indexed_pages = await _index_docs(memory, info)
        if not result.indexed_pages:
            result.notes.append("nenhuma documentação encontrada para indexar")

    return result


async def _index_docs(memory: Memory, info: ProjectInfo) -> list[str]:
    """Traz README e docs para a wiki como páginas iniciais, em tier warm.

    O conteúdo vem do repositório do usuário, que pode ter vindo de um clone
    de terceiro -- então passa pelo mesmo caminho de escrita de sempre, que
    varre e marca conteúdo suspeito.
    """
    root = Path(info.path)
    seen: set[Path] = set()
    created: list[str] = []

    for pattern in DOC_PATTERNS:
        for found in sorted(root.glob(pattern)):
            if len(created) >= MAX_INDEXED_DOCS:
                return created
            if not found.is_file() or found in seen:
                continue
            seen.add(found)

            try:
                body = found.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not body.strip():
                continue

            relative = found.relative_to(root)
            slug = slugify(relative.with_suffix("").as_posix().replace("/", "-"))
            result = await memory.write(
                path=f"{info.slug}/docs/{slug}",
                title=_title_of(body, found.stem),
                body=body,
                tags=["doc", "importado"],
                tier="warm",
                agent="lea",
                message=f"importa {relative} de {info.slug}",
            )
            created.append(result.page.qualified_slug)

    return created


def _title_of(body: str, fallback: str) -> str:
    """Primeiro ``# título`` do markdown, ou o nome do arquivo."""
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return fallback


def _detect_language(root: Path) -> str | None:
    for marker, language in LANGUAGE_MARKERS:
        if (root / marker).exists():
            return language
    return None


async def _git(root: Path, *args: str) -> str | None:
    """Roda git no projeto. Devolve ``None`` em qualquer falha.

    Falha aqui é esperada e inofensiva: repositório sem remote, HEAD solto,
    diretório que nem é git. Nenhuma delas impede abrir o projeto.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (FileNotFoundError, asyncio.TimeoutError):
        return None
    if process.returncode != 0:
        return None
    value = stdout.decode("utf-8", "replace").strip()
    return value or None


async def listing(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Projetos cadastrados, com a contagem de sessões ativas."""
    async with conn.execute(
        """
        SELECT p.*, (
            SELECT COUNT(*) FROM sessions s
            WHERE s.project_id = p.id AND s.status IN ('active', 'starting')
        ) AS active_sessions
        FROM projects p ORDER BY p.name
        """
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "id": row["id"],
            "slug": row["slug"],
            "name": row["name"],
            "path": row["path"],
            "remote": row["remote"],
            "default_branch": row["default_branch"],
            "language": row["language"],
            "allow_autonomous": bool(row["allow_autonomous"]),
            "require_approval_for": json.loads(row["require_approval_for"] or "[]"),
            "active_sessions": row["active_sessions"],
        }
        for row in rows
    ]
