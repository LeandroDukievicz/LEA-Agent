"""A página de memória: frontmatter YAML + corpo Markdown.

O arquivo ``.md`` no repositório git é a **fonte da verdade**. O SQLite é
índice, e pode ser jogado fora e reconstruído a partir do git a qualquer
momento. Essa ordem importa: significa que o usuário pode editar a wiki com o
editor dele, fazer merge, dar `git revert`, e o LEA se reconcilia.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml

Tier = Literal["hot", "warm", "cold"]

#: Tiers válidos, na ordem de quente para frio.
TIERS: tuple[Tier, ...] = ("hot", "warm", "cold")

#: Delimitador do frontmatter.
_FENCE = "---"

#: ``[[slug]]`` ou ``[[projeto/slug]]``. O ``[^\[\]|]`` impede que um link
#: atravesse o fechamento do outro numa linha com vários.
_WIKILINK = re.compile(r"\[\[([^\[\]|]+?)\]\]")

#: Caracteres aceitos num slug de arquivo.
_SLUG_SAFE = re.compile(r"[^a-z0-9/-]+")


class PageError(ValueError):
    """Frontmatter ausente, inválido ou incoerente."""


def new_id() -> str:
    """Gera um id curto no formato do protótipo (``0x7a3f19``).

    Seis dígitos hex dão 16,7 milhões de valores. Para uma wiki pessoal é
    folgado, e a unicidade real é garantida pela PRIMARY KEY do índice -- quem
    insere trata a colisão gerando outro.
    """
    return f"0x{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Page:
    """Uma página da wiki, já separada em metadados e corpo."""

    id: str
    title: str
    body: str = ""
    tier: Tier = "warm"
    strength: float = 0.5
    tags: list[str] = field(default_factory=list)
    created_by: str | None = None
    updated_by: str | None = None

    #: Caminho relativo à raiz da wiki, com barra normal (ex.:
    #: ``lea-core/memoria/decaimento.md``). Não vai para o frontmatter: é
    #: a própria localização do arquivo, e duplicá-lo criaria duas verdades.
    path: str = ""

    @property
    def slug(self) -> str:
        """Nome sem projeto nem extensão (``decaimento``)."""
        return PurePosixPath(self.path).stem if self.path else ""

    @property
    def project(self) -> str | None:
        """Primeiro segmento do caminho, ou ``None`` se a página é global.

        ``_global/preferencias.md`` não pertence a projeto nenhum -- é
        preferência do usuário, injetada em toda sessão.
        """
        if not self.path:
            return None
        head = PurePosixPath(self.path).parts[0]
        return None if head == "_global" else head

    @property
    def qualified_slug(self) -> str:
        """``projeto/slug``, ou só ``slug`` para páginas globais."""
        return f"{self.project}/{self.slug}" if self.project else self.slug

    def links(self) -> list[str]:
        """Destinos dos ``[[wikilinks]]`` do corpo, sem repetição e em ordem.

        Ordem estável importa: o índice grava isso, e ordem instável faria
        cada reindexação produzir um diff falso.
        """
        seen: dict[str, None] = {}
        for match in _WIKILINK.finditer(self.body):
            target = match.group(1).strip()
            if target:
                seen.setdefault(normalize_slug(target), None)
        return list(seen)

    def to_markdown(self) -> str:
        """Serializa de volta para o formato de arquivo."""
        meta: dict[str, Any] = {
            # Aspas obrigatórias: sem elas o YAML lê `0x7a3f19` como inteiro
            # hexadecimal, e um id com zero à esquerda (`0x07a3f1`, que o
            # token_hex gera em ~6% dos casos) voltaria encurtado -- trocando
            # silenciosamente a identidade da página e órfãnando o índice.
            "id": _QuotedStr(self.id),
            "title": self.title,
            "tier": self.tier,
            "strength": round(self.strength, 4),
        }
        if self.tags:
            meta["tags"] = list(self.tags)
        if self.created_by:
            meta["created_by"] = self.created_by
        if self.updated_by:
            meta["updated_by"] = self.updated_by

        front = yaml.safe_dump(
            meta,
            sort_keys=False,
            allow_unicode=True,  # acento no título não vira \uXXXX
            default_flow_style=False,
        ).strip()

        body = self.body.strip()
        return f"{_FENCE}\n{front}\n{_FENCE}\n\n{body}\n" if body else f"{_FENCE}\n{front}\n{_FENCE}\n"


def parse(text: str, path: str = "") -> Page:
    """Lê o conteúdo de um ``.md`` da wiki.

    Levanta :class:`PageError` com mensagem em português -- ela aparece na UI
    quando o usuário edita o arquivo à mão e erra o frontmatter.
    """
    meta, body = split_frontmatter(text)

    page_id = _coerce_id(meta.get("id"))
    if not page_id:
        raise PageError("frontmatter sem `id`")

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PageError(f"página {page_id} sem `title`")

    tier = meta.get("tier", "warm")
    if tier not in TIERS:
        raise PageError(f"tier inválido em {page_id}: {tier!r} (use hot, warm ou cold)")

    try:
        strength = float(meta.get("strength", 0.5))
    except (TypeError, ValueError):
        raise PageError(f"strength não numérico em {page_id}: {meta.get('strength')!r}") from None
    if not 0.0 <= strength <= 1.0:
        raise PageError(f"strength fora de 0..1 em {page_id}: {strength}")

    raw_tags = meta.get("tags") or []
    if isinstance(raw_tags, str):
        # Tolerado porque é o erro mais comum de quem edita à mão.
        raw_tags = [t.strip() for t in raw_tags.split(",")]
    if not isinstance(raw_tags, list):
        raise PageError(f"tags deve ser uma lista em {page_id}")

    return Page(
        id=page_id,
        title=title.strip(),
        body=body,
        tier=tier,
        strength=strength,
        tags=[str(t).strip() for t in raw_tags if str(t).strip()],
        created_by=_optional_str(meta.get("created_by")),
        updated_by=_optional_str(meta.get("updated_by")),
        path=path,
    )


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Separa o bloco YAML do corpo Markdown."""
    stripped = text.lstrip("﻿")  # BOM, quando o arquivo veio do Windows
    if not stripped.startswith(_FENCE):
        raise PageError("arquivo sem frontmatter (deve começar com ---)")

    # O fechamento é a próxima linha que contém exatamente ---
    lines = stripped.splitlines()
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == _FENCE), None)
    if end is None:
        raise PageError("frontmatter não fechado (falta o --- final)")

    raw_meta = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()

    try:
        # safe_load: o conteúdo pode ter sido escrito por um agente a partir de
        # entrada não confiável, e yaml.load executaria construtores Python.
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError as exc:
        raise PageError(f"frontmatter YAML inválido: {exc}") from exc

    if not isinstance(meta, dict):
        raise PageError("frontmatter deve ser um mapa de chave: valor")

    return meta, body


def make(
    title: str,
    body: str = "",
    *,
    path: str,
    tier: Tier = "warm",
    tags: list[str] | None = None,
    author: str | None = None,
) -> Page:
    """Cria uma página nova, com id gerado e força inicial do tier."""
    from lea.memory import decay

    return Page(
        id=new_id(),
        title=title,
        body=body,
        tier=tier,
        strength=decay.INITIAL_STRENGTH,
        tags=tags or [],
        created_by=author,
        updated_by=author,
        path=path,
    )


def normalize_slug(value: str) -> str:
    """Normaliza o destino de um wikilink para comparação.

    ``[[Memória/Decaimento]]`` e ``[[memoria/decaimento]]`` apontam para a
    mesma página: quem escreve à mão não acerta caixa nem acento.
    """
    lowered = _strip_accents(value.strip().lower())
    lowered = lowered.removesuffix(".md")
    lowered = lowered.replace(" ", "-").replace("_", "-")
    lowered = _SLUG_SAFE.sub("", lowered)
    return lowered.strip("/-")


def _strip_accents(value: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class _QuotedStr(str):
    """String que o YAML sempre escreve entre aspas."""


def _represent_quoted(dumper: yaml.Dumper, data: _QuotedStr) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


yaml.SafeDumper.add_representer(_QuotedStr, _represent_quoted)


def _coerce_id(value: Any) -> str:
    """Normaliza o id lido do frontmatter.

    ``id: 0x7a3f19`` sem aspas é lido pelo YAML como o **inteiro** 8011545 --
    a notação hexadecimal de YAML 1.1. Arquivo escrito pelo LEA sempre vem
    com aspas, mas um arquivo editado à mão pode cair nisso, então aceitamos
    o inteiro e devolvemos à forma canônica de seis dígitos.
    """
    if isinstance(value, bool):  # bool é subclasse de int; não é id
        return ""
    if isinstance(value, int):
        return f"0x{value:06x}"
    if isinstance(value, str):
        return value.strip()
    return ""
