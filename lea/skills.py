"""Skills portáveis em ``SKILL.md``, com registro e histórico no SQLite.

O arquivo Markdown é a fonte da verdade do conteúdo. O banco guarda o índice,
as versões aplicadas e as execuções para a interface poder consultar sem varrer
o diretório a cada tela.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from lea import paths

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_SKILL_BYTES = 500_000


class SkillError(ValueError):
    """Documento de skill inválido ou não encontrado."""


@dataclass(frozen=True)
class SkillDocument:
    id: int
    name: str
    description: str
    version: str
    triggers: list[str]
    origin: str
    body: str
    path: str
    updated_at: str
    content: str

    def as_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "triggers": self.triggers,
            "origin": self.origin,
            "path": self.path,
            "updated_at": self.updated_at,
        }
        if include_body:
            item["body"] = self.body
            item["content"] = self.content
        return item


def parse(content: str, *, directory_name: str | None = None) -> SkillDocument:
    """Valida o frontmatter Agent Skills e lê os campos próprios do LEA."""
    match = _FRONTMATTER.match(content)
    if not match:
        raise SkillError("SKILL.md precisa começar com frontmatter YAML entre ---")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillError(f"frontmatter YAML inválido: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillError("frontmatter precisa ser um objeto YAML")

    name = metadata.get("name")
    description = metadata.get("description", "")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise SkillError("name precisa usar letras minúsculas, números e hífens (até 64 caracteres)")
    if directory_name and directory_name != name:
        raise SkillError(f"o diretório {directory_name!r} precisa corresponder a name={name!r}")
    if not isinstance(description, str) or len(description) > 1000:
        raise SkillError("description precisa ser texto com até 1000 caracteres")

    lea_meta = metadata.get("metadata", {})
    if not isinstance(lea_meta, dict):
        lea_meta = {}
    lea_meta = lea_meta.get("lea", {})
    if not isinstance(lea_meta, dict):
        lea_meta = {}
    version = str(lea_meta.get("version", "1"))
    if not version or len(version) > 40:
        raise SkillError("metadata.lea.version precisa ter entre 1 e 40 caracteres")
    triggers = lea_meta.get("triggers", [])
    if isinstance(triggers, str):
        triggers = [triggers]
    if not isinstance(triggers, list) or any(not isinstance(t, str) for t in triggers):
        raise SkillError("metadata.lea.triggers precisa ser uma lista de textos")

    return SkillDocument(
        id=0,  # preenchido pelo registro após a leitura do banco
        name=name,
        description=description,
        version=version,
        triggers=triggers,
        origin="manual",
        body=match.group(2).strip(),
        path=f"{name}/SKILL.md",
        updated_at="",
        content=content,
    )


def _with_id(skill: SkillDocument, skill_id: int, origin: str, updated_at: str) -> SkillDocument:
    return SkillDocument(
        id=skill_id,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        triggers=skill.triggers,
        origin=origin,
        body=skill.body,
        path=skill.path,
        updated_at=updated_at,
        content=skill.content,
    )


async def list_skills(conn: aiosqlite.Connection) -> list[SkillDocument]:
    async with conn.execute(
        "SELECT id, name, description, version, triggers, origin, path, updated_at "
        "FROM skills ORDER BY name"
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        SkillDocument(
            id=int(row["id"]),
            name=row["name"],
            description=row["description"],
            version=row["version"],
            triggers=_json_list(row["triggers"]),
            origin=row["origin"],
            body="",
            path=row["path"],
            updated_at=row["updated_at"],
            content="",
        )
        for row in rows
    ]


async def get_skill(conn: aiosqlite.Connection, name: str) -> SkillDocument:
    async with conn.execute("SELECT * FROM skills WHERE name = ?", (name,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise SkillError(f"skill desconhecida: {name}")
    path = paths.skills_dir() / row["path"]
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"não consegui ler {path}: {exc}") from exc
    parsed = parse(content, directory_name=name)
    return _with_id(parsed, int(row["id"]), row["origin"], row["updated_at"])


async def save_skill(
    conn: aiosqlite.Connection,
    content: str,
    *,
    origin: str = "manual",
    reason: str = "",
    created_by: str | None = None,
    apply: bool = True,
) -> SkillDocument:
    """Cria ou atualiza uma skill e guarda uma revisão do conteúdo."""
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillError(f"SKILL.md excede o limite de {MAX_SKILL_BYTES} bytes")
    if origin not in {"manual", "auto"}:
        raise SkillError("origin precisa ser manual ou auto")
    skill = parse(content)
    root = paths.skills_dir().resolve()
    folder = (root / skill.name).resolve()
    if not folder.is_relative_to(root):
        raise SkillError("caminho da skill inválido")
    path = folder / "SKILL.md"

    async with conn.execute(
        "SELECT id, version, origin, updated_at FROM skills WHERE name = ?", (skill.name,)
    ) as cursor:
        current = await cursor.fetchone()

    old_content = ""
    if current is not None:
        try:
            old_content = path.read_text(encoding="utf-8")
        except OSError:
            pass
        if old_content == content and current["origin"] == origin:
            return _with_id(
                parse(content, directory_name=skill.name),
                int(current["id"]),
                origin,
                current["updated_at"],
            )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    version = skill.version
    if current is not None:
        async with conn.execute(
            "SELECT 1 FROM skill_versions WHERE skill_id = ? AND version = ? LIMIT 1",
            (int(current["id"]), version),
        ) as cursor:
            version_exists = await cursor.fetchone() is not None
        if version == current["version"] or version_exists:
            try:
                version = str(int(current["version"]) + 1)
            except ValueError:
                version = f"{current['version']}+{hashlib.sha256(content.encode()).hexdigest()[:8]}"
        content = _write_version(content, version)
        skill = parse(content)

    folder.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    relative = path.relative_to(root).as_posix()

    if current is None:
        cursor = await conn.execute(
            "INSERT INTO skills (name, version, origin, description, triggers, path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                skill.name, version, origin, skill.description,
                json.dumps(skill.triggers, ensure_ascii=False), relative, now, now,
            ),
        )
        skill_id = int(cursor.lastrowid or 0)
        diff = content
    else:
        skill_id = int(current["id"])
        await conn.execute(
            "UPDATE skills SET version = ?, origin = ?, description = ?, triggers = ?, path = ?, updated_at = ? "
            "WHERE id = ?",
            (
                version, origin, skill.description,
                json.dumps(skill.triggers, ensure_ascii=False), relative, now, skill_id,
            ),
        )
        diff = _unified_diff(old_content, content)

    await conn.execute(
        "INSERT OR IGNORE INTO skill_versions (skill_id, version, diff, reason, created_by, created_at, applied) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (skill_id, version, diff, reason, created_by, now, int(apply)),
    )
    await conn.commit()
    return _with_id(parse(content, directory_name=skill.name), skill_id, origin, now)


async def record_run(
    conn: aiosqlite.Connection,
    skill_id: int,
    version: str,
    *,
    session_id: str,
    success: bool,
    duration_ms: int,
) -> None:
    await conn.execute(
        "INSERT INTO skill_runs (skill_id, version, session_id, success, duration_ms, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            skill_id, version, session_id, int(success), duration_ms,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    await conn.commit()


async def history(conn: aiosqlite.Connection, skill_id: int, limit: int = 50) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT version, diff, reason, created_by, created_at, applied FROM skill_versions "
        "WHERE skill_id = ? ORDER BY created_at DESC LIMIT ?",
        (skill_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def runs(conn: aiosqlite.Connection, skill_id: int, limit: int = 50) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT version, session_id, success, duration_ms, at FROM skill_runs "
        "WHERE skill_id = ? ORDER BY at DESC LIMIT ?",
        (skill_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def sync_from_disk(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Indexa skills portáveis em disco e recolhe edições feitas pelo usuário."""
    root = paths.skills_dir().resolve()
    imported = updated = 0
    problems: list[str] = []
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                problems.append(f"{path}: destino fora do diretório de skills")
                continue
            content = resolved.read_text(encoding="utf-8")
            if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
                raise SkillError(f"SKILL.md excede o limite de {MAX_SKILL_BYTES} bytes")
            parsed = parse(content, directory_name=path.parent.name)
        except (OSError, UnicodeDecodeError, SkillError) as exc:
            problems.append(f"{path}: {exc}")
            continue

        async with conn.execute("SELECT id, path FROM skills WHERE name = ?", (parsed.name,)) as cursor:
            current = await cursor.fetchone()
        if current is not None:
            try:
                known = (root / current["path"]).read_text(encoding="utf-8")
            except OSError:
                known = ""
            if known == content:
                continue

        try:
            await save_skill(
                conn,
                content,
                origin="manual",
                reason="importada ou editada em disco",
                created_by="disk-sync",
            )
            if current is None:
                imported += 1
            else:
                updated += 1
        except (OSError, SkillError, aiosqlite.Error) as exc:
            problems.append(f"{path}: {exc}")
    return {"imported": imported, "updated": updated, "problems": problems}


def _write_version(content: str, version: str) -> str:
    match = _FRONTMATTER.match(content)
    assert match is not None
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:  # parse() already checked; defensive for future callers
        raise SkillError(f"frontmatter YAML inválido: {exc}") from exc
    if not isinstance(metadata.get("metadata"), dict):
        metadata["metadata"] = {}
    if not isinstance(metadata["metadata"].get("lea"), dict):
        metadata["metadata"]["lea"] = {}
    lea = metadata["metadata"]["lea"]
    lea["version"] = version
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{frontmatter}\n---\n{match.group(2)}"


def _unified_diff(before: str, after: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="before/SKILL.md", tofile="after/SKILL.md",
        )
    )


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
