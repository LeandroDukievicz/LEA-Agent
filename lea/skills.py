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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from lea import paths
from lea.security import injection

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_SKILL_BYTES = 500_000


class SkillError(ValueError):
    """Documento de skill inválido ou não encontrado."""


class SkillConflictError(SkillError):
    """A proposta usa uma versão base que já foi alterada."""


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
            "SELECT version FROM skill_versions WHERE skill_id = ?",
            (int(current["id"]),),
        ) as cursor:
            used_versions = {str(row["version"]) for row in await cursor.fetchall()}
        if version == current["version"] or version in used_versions:
            version = _next_version(current["version"], content, used_versions)
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


async def propose_update(
    conn: aiosqlite.Connection,
    skill_id: int,
    content: str,
    *,
    reason: str,
    created_by: str,
    session_id: str,
    auto_apply: bool = False,
) -> dict[str, Any]:
    """Aplica ou registra para revisão uma atualização feita pelo agente."""
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillError(f"SKILL.md excede o limite de {MAX_SKILL_BYTES} bytes")
    reason = reason.strip()
    if not reason or len(reason) > 2000:
        raise SkillError("reason precisa ter entre 1 e 2000 caracteres")
    async with conn.execute(
        "SELECT id, name, version, origin FROM skills WHERE id = ?",
        (skill_id,),
    ) as cursor:
        current = await cursor.fetchone()
    if current is None:
        raise SkillError(f"skill desconhecida: {skill_id}")

    current_skill = await get_skill(conn, current["name"])
    if current_skill.content == content:
        raise SkillError("a proposta não altera o conteúdo atual da skill")
    parse(content, directory_name=current_skill.name)
    async with conn.execute(
        "SELECT version FROM skill_versions WHERE skill_id = ?",
        (skill_id,),
    ) as cursor:
        used_versions = {str(row["version"]) for row in await cursor.fetchall()}
    proposed_version = _next_version(current_skill.version, content, used_versions)
    proposed_content = _write_version(content, proposed_version)
    parse(proposed_content, directory_name=current_skill.name)
    finding = injection.first_problem(proposed_content, injection.Scope.STRICT)
    if finding:
        raise SkillError(f"a proposta contém uma instrução suspeita e foi bloqueada: {finding}")

    if auto_apply:
        updated = await save_skill(
            conn,
            proposed_content,
            origin=current["origin"],
            reason=reason,
            created_by=created_by,
        )
        return {"status": "applied", "skill": updated.as_dict(include_body=True)}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = await conn.execute(
        "INSERT INTO skill_proposals "
        "(skill_id, session_id, base_version, base_sha256, proposed_version, content, diff, reason, created_by, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            skill_id,
            session_id,
            current_skill.version,
            hashlib.sha256(current_skill.content.encode()).hexdigest(),
            proposed_version,
            proposed_content,
            _unified_diff(current_skill.content, proposed_content),
            reason,
            created_by,
            now,
        ),
    )
    await conn.commit()
    return {
        "id": int(cursor.lastrowid or 0),
        "skill_id": skill_id,
        "skill_name": current_skill.name,
        "base_version": current_skill.version,
        "proposed_version": proposed_version,
        "status": "pending",
    }


async def list_proposals(
    conn: aiosqlite.Connection,
    status: str | None = "pending",
    limit: int = 100,
) -> list[dict[str, Any]]:
    await recover_applying_proposals(conn)
    query = (
        "SELECT p.id, p.skill_id, s.name AS skill_name, p.session_id, p.base_version, "
        "p.proposed_version, p.reason, p.created_by, p.status, p.created_at, "
        "p.reviewed_at, s.version AS current_version "
        "FROM skill_proposals p JOIN skills s ON s.id = p.skill_id"
    )
    params: tuple[Any, ...] = ()
    if status is not None:
        query += " WHERE p.status = ?"
        params = (status,)
    query += " ORDER BY p.created_at DESC, p.id DESC LIMIT ?"
    params += (limit,)
    async with conn.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_proposal(conn: aiosqlite.Connection, proposal_id: int) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT p.*, s.name AS skill_name, s.version AS current_version "
        "FROM skill_proposals p JOIN skills s ON s.id = p.skill_id WHERE p.id = ?",
        (proposal_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def apply_proposal(conn: aiosqlite.Connection, proposal_id: int) -> SkillDocument:
    """Aplica uma proposta pendente se a skill ainda estiver na versão-base."""
    await recover_applying_proposals(conn)
    cursor = await conn.execute(
        "UPDATE skill_proposals SET status = 'applying', reviewed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (_now(), proposal_id),
    )
    await conn.commit()
    if cursor.rowcount != 1:
        raise SkillError(f"proposta pendente não encontrada: {proposal_id}")

    saved = False
    try:
        proposal = await get_proposal(conn, proposal_id)
        if proposal is None:
            raise SkillError(f"proposta desconhecida: {proposal_id}")
        current = await get_skill(conn, proposal["skill_name"])
        current_hash = hashlib.sha256(current.content.encode()).hexdigest()
        if current.version != proposal["base_version"] or current_hash != proposal["base_sha256"]:
            await conn.execute(
                "UPDATE skill_proposals SET status = 'conflict', reviewed_at = ? "
                "WHERE id = ? AND status = 'applying'",
                (_now(), proposal_id),
            )
            await conn.commit()
            raise SkillConflictError(
                f"a skill mudou de {proposal['base_version']} para {current.version}; gere uma nova proposta"
            )
        parse(proposal["content"], directory_name=current.name)
        finding = injection.first_problem(proposal["content"], injection.Scope.STRICT)
        if finding:
            raise SkillError(f"a proposta contém uma instrução suspeita e foi bloqueada: {finding}")
        updated = await save_skill(
            conn,
            proposal["content"],
            origin=current.origin,
            reason=proposal["reason"],
            created_by=proposal["created_by"],
        )
        saved = True
        await conn.execute(
            "UPDATE skill_proposals SET status = 'applied', reviewed_at = ? "
            "WHERE id = ? AND status = 'applying'",
            (_now(), proposal_id),
        )
        await conn.commit()
        return updated
    except SkillConflictError:
        raise
    except Exception:
        if saved:
            await conn.execute(
                "UPDATE skill_proposals SET status = 'applied', reviewed_at = ? "
                "WHERE id = ? AND status = 'applying'",
                (_now(), proposal_id),
            )
        else:
            await conn.execute(
                "UPDATE skill_proposals SET status = 'pending', reviewed_at = NULL "
                "WHERE id = ? AND status = 'applying'",
                (proposal_id,),
            )
        await conn.commit()
        raise


async def reject_proposal(conn: aiosqlite.Connection, proposal_id: int) -> bool:
    cursor = await conn.execute(
        "UPDATE skill_proposals SET status = 'rejected', reviewed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (_now(), proposal_id),
    )
    await conn.commit()
    return cursor.rowcount == 1


async def recover_applying_proposals(conn: aiosqlite.Connection) -> None:
    """Reconcilia uma aplicação interrompida entre o arquivo e o registro."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    async with conn.execute(
        "SELECT p.id, p.skill_id, p.base_version, p.base_sha256, p.proposed_version, "
        "p.content, p.reviewed_at, s.name FROM skill_proposals p "
        "JOIN skills s ON s.id = p.skill_id WHERE p.status = 'applying'"
    ) as cursor:
        proposals = await cursor.fetchall()
    for proposal in proposals:
        try:
            current = await get_skill(conn, proposal["name"])
        except SkillError:
            await conn.execute(
                "UPDATE skill_proposals SET status = 'conflict', reviewed_at = ? "
                "WHERE id = ? AND status = 'applying'",
                (_now(), proposal["id"]),
            )
            continue
        current_hash = hashlib.sha256(current.content.encode()).hexdigest()
        proposal_hash = hashlib.sha256(proposal["content"].encode()).hexdigest()
        if current.version == proposal["proposed_version"] and current_hash == proposal_hash:
            status = "applied"
            reviewed_at = proposal["reviewed_at"] or _now()
        elif current.version != proposal["base_version"] or current_hash != proposal["base_sha256"]:
            status = "conflict"
            reviewed_at = _now()
        elif not proposal["reviewed_at"] or proposal["reviewed_at"] < cutoff:
            status = "pending"
            reviewed_at = None
        else:
            continue
        await conn.execute(
            "UPDATE skill_proposals SET status = ?, reviewed_at = ? "
            "WHERE id = ? AND status = 'applying'",
            (status, reviewed_at, proposal["id"]),
        )
    await conn.commit()


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


def _next_version(current: str, content: str, used_versions: set[str]) -> str:
    if current.isdigit():
        numbers = [int(version) for version in used_versions if version.isdigit()]
        return str(max([int(current), *numbers]) + 1)
    base = f"{current[:25]}+{hashlib.sha256(content.encode()).hexdigest()[:8]}"
    candidate = base
    suffix = 2
    while candidate in used_versions:
        marker = f"-{suffix}"
        candidate = f"{base[:40 - len(marker)]}{marker}"
        suffix += 1
    return candidate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
