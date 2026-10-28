"""Merge seguro de arquivos de hook no formato que Claude Code e Codex usam.

Os dois convergiram para a mesma forma::

    {"hooks": {"SessionStart": [{"matcher": "",
                                 "hooks": [{"type": "command",
                                            "command": "..."}]}]}}

A diferença é onde o mapa mora: no Claude Code ele é a chave ``hooks`` de
``~/.claude/settings.json``, que tem muita outra coisa dentro; no Codex é o
arquivo ``~/.codex/hooks.json`` inteiro.

Este módulo existe porque **a máquina do usuário já tem hooks de outra
ferramenta**. Toda operação aqui preserva o que existe: nunca escrevemos um
arquivo inteiro por cima, nunca removemos entrada que não seja nossa, e
sempre deixamos um backup antes de tocar em qualquer coisa.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Sufixo dos backups que criamos.
BACKUP_SUFFIX = "lea-bak"


def backup(path: Path) -> Path | None:
    """Copia o arquivo antes de alterá-lo. ``None`` se ele não existia.

    O nome carrega o instante, então instalações sucessivas não sobrescrevem
    o backup da anterior -- se algo der errado duas vezes, as duas versões
    anteriores continuam lá.
    """
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.{BACKUP_SUFFIX}-{int(time.time())}")
    shutil.copy2(path, target)
    logger.info("backup de %s em %s", path, target)
    return target


def read_json(path: Path) -> dict[str, Any]:
    """Lê JSON, devolvendo ``{}`` se não existir.

    JSON quebrado **levanta**, de propósito: seguir em frente com ``{}``
    faria o merge gravar um arquivo novo por cima de uma config que o usuário
    só digitou errado, e aí a perda seria nossa culpa.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} não contém um objeto JSON")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Grava JSON de forma atômica, preservando a permissão do arquivo.

    Escrita direta pode deixar o arquivo truncado se o processo morrer no
    meio -- e aqui o arquivo é a configuração do agente de código do usuário.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600

    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def has_entry(hooks_map: dict[str, Any], event: str, command: str) -> bool:
    """True se este comando exato já está registrado no evento."""
    for group in hooks_map.get(event) or []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict) and entry.get("command") == command:
                return True
    return False


def find_ours(hooks_map: dict[str, Any], event: str, marker: str) -> str | None:
    """Comando registrado por nós naquele evento, se houver.

    Identificamos pelo ``marker`` (o caminho do nosso executável). Serve para
    detectar uma instalação antiga apontando para outro lugar -- caso em que
    a saúde é ``ERROR``, não ``MISSING``: a entrada existe, mas está errada.
    """
    for group in hooks_map.get(event) or []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict):
                command = entry.get("command")
                if isinstance(command, str) and marker in command:
                    return command
    return None


def upsert(hooks_map: dict[str, Any], event: str, command: str, marker: str) -> bool:
    """Garante que ``command`` esteja registrado em ``event``.

    Devolve ``True`` se alterou alguma coisa. Entradas de outras ferramentas
    são preservadas; só uma entrada **nossa** desatualizada é substituída.
    """
    if has_entry(hooks_map, event, command):
        return False

    groups = hooks_map.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"hooks[{event}] deveria ser uma lista, veio {type(groups).__name__}")

    # Instalação nossa apontando para o lugar errado: atualiza no lugar.
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("command"), str)
                and marker in entry["command"]
            ):
                entry["command"] = command
                return True

    groups.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
    return True


def remove_ours(hooks_map: dict[str, Any], marker: str) -> int:
    """Tira só as entradas nossas. Devolve quantas saíram."""
    removed = 0
    for event, groups in list(hooks_map.items()):
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            kept = [
                e
                for e in entries
                if not (
                    isinstance(e, dict)
                    and isinstance(e.get("command"), str)
                    and marker in e["command"]
                )
            ]
            removed += len(entries) - len(kept)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks_map[event] = kept_groups
        else:
            del hooks_map[event]
    return removed
