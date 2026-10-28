"""Caminhos de leitura sobre o índice: busca, árvore e grafo.

``index.py`` escreve; este módulo só lê. A separação evita que uma consulta
cara acabe, por descuido, mexendo em força ou tier -- quem altera estado é
sempre ``record_access``, explicitamente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

#: Peso da força da página na ordenação da busca.
STRENGTH_WEIGHT = 0.35

#: Bônus para páginas do projeto em foco.
PROJECT_BONUS = 0.25

#: Caracteres com significado na sintaxe do FTS5.
_FTS_SPECIAL = re.compile(r'["*()^:{}\[\]~]')


@dataclass
class Hit:
    """Uma página encontrada, com o porquê da posição dela."""

    id: str
    path: str
    title: str
    tier: str
    strength: float
    score: float
    snippet: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "tier": self.tier,
            "strength": round(self.strength, 4),
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }


@dataclass
class GraphNode:
    id: str
    title: str
    path: str
    tier: str
    depth: int


@dataclass
class Graph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [
                {"id": n.id, "title": n.title, "path": n.path, "tier": n.tier, "depth": n.depth}
                for n in self.nodes
            ],
            "edges": [{"from": a, "to": b} for a, b in self.edges],
        }


def escape_query(raw: str) -> str:
    """Transforma texto livre numa expressão FTS5 segura.

    O usuário digita na busca global; o agente passa a tarefa inteira pelo
    MCP. Nenhum dos dois escreve sintaxe de FTS, e um parêntese solto faria o
    SQLite levantar erro de sintaxe no meio de uma busca.

    Cada termo vira string entre aspas, e os termos são unidos por OR para que
    uma frase longa ainda encontre alguma coisa.
    """
    cleaned = _FTS_SPECIAL.sub(" ", raw)
    terms = [t for t in cleaned.split() if len(t) > 1]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


async def search(
    conn: aiosqlite.Connection,
    raw_query: str,
    *,
    project_id: int | None = None,
    tier: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    """Busca por texto, ordenada por relevância, força e projeto em foco.

    A relevância bruta vem do bm25 do FTS5. Somamos a força -- uma página viva
    vale mais que uma esquecida com as mesmas palavras -- e um bônus para o
    projeto aberto, que é quase sempre o que o usuário quer.
    """
    expression = escape_query(raw_query)
    if not expression:
        return []

    sql = """
        SELECT p.id, p.path, p.title, p.tier, p.strength, p.project_id,
               bm25(pages_fts) AS rank,
               snippet(pages_fts, 1, '[', ']', '…', 12) AS snippet
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.page_id
        WHERE pages_fts MATCH ?
    """
    params: list[Any] = [expression]
    if tier:
        sql += " AND p.tier = ?"
        params.append(tier)
    sql += " ORDER BY rank LIMIT ?"
    # Buscamos com folga porque a reordenação abaixo pode trocar as posições.
    params.append(limit * 4)

    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError:
        # Expressão que o FTS5 recusou mesmo depois da limpeza. Buscar não é
        # operação crítica: devolver vazio é melhor que estourar na UI.
        return []

    hits = []
    for row in rows:
        # bm25 é negativo e quanto menor, melhor. Invertemos para 0..1.
        relevance = 1.0 / (1.0 + abs(row["rank"]))
        score = relevance + STRENGTH_WEIGHT * row["strength"]
        if project_id is not None and row["project_id"] == project_id:
            score += PROJECT_BONUS
        hits.append(
            Hit(
                id=row["id"],
                path=row["path"],
                title=row["title"],
                tier=row["tier"],
                strength=row["strength"],
                score=score,
                snippet=row["snippet"] or "",
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


async def tree(conn: aiosqlite.Connection, project_id: int | None = None) -> dict[str, Any]:
    """Árvore de pastas da wiki, para o painel esquerdo da tela Memória."""
    sql = "SELECT id, path, title, tier, strength FROM pages"
    params: list[Any] = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY path"

    async with conn.execute(sql, params) as cursor:
        rows = await cursor.fetchall()

    root: dict[str, Any] = {"name": "", "folders": {}, "pages": []}
    for row in rows:
        parts = row["path"].split("/")
        node = root
        for folder in parts[:-1]:
            node = node["folders"].setdefault(folder, {"name": folder, "folders": {}, "pages": []})
        node["pages"].append(
            {
                "id": row["id"],
                "path": row["path"],
                "title": row["title"],
                "tier": row["tier"],
                "strength": round(row["strength"], 4),
            }
        )
    return root


async def backlinks(conn: aiosqlite.Connection, page_id: str) -> list[dict[str, Any]]:
    """Páginas que apontam para esta."""
    async with conn.execute(
        """
        SELECT p.id, p.path, p.title FROM page_links l
        JOIN pages p ON p.id = l.src_id
        WHERE l.dst_id = ? ORDER BY p.title
        """,
        (page_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"id": r["id"], "path": r["path"], "title": r["title"]} for r in rows]


async def graph(conn: aiosqlite.Connection, page_id: str, depth: int = 2) -> Graph:
    """Vizinhança da página até ``depth`` saltos, seguindo links nos dois sentidos.

    A aba GRAFO pinta o nó atual em magenta, os diretos em ciano e o segundo
    grau em cinza -- por isso cada nó carrega a profundidade em que foi achado.
    """
    depth = max(1, min(depth, 4))
    result = Graph()
    seen: dict[str, int] = {}
    frontier = [page_id]

    for level in range(depth + 1):
        if not frontier:
            break
        novos = [pid for pid in frontier if pid not in seen]
        for pid in novos:
            seen[pid] = level
        if not novos:
            break

        placeholders = ",".join("?" * len(novos))
        async with conn.execute(
            f"SELECT id, title, path, tier FROM pages WHERE id IN ({placeholders})", novos
        ) as cursor:
            for row in await cursor.fetchall():
                result.nodes.append(
                    GraphNode(
                        id=row["id"],
                        title=row["title"],
                        path=row["path"],
                        tier=row["tier"],
                        depth=seen[row["id"]],
                    )
                )

        if level == depth:
            break

        async with conn.execute(
            f"""
            SELECT src_id, dst_id FROM page_links
            WHERE dst_id IS NOT NULL
              AND (src_id IN ({placeholders}) OR dst_id IN ({placeholders}))
            """,
            novos + novos,
        ) as cursor:
            edges = await cursor.fetchall()

        frontier = []
        for row in edges:
            edge = (row["src_id"], row["dst_id"])
            if edge not in result.edges:
                result.edges.append(edge)
            frontier.extend(e for e in edge if e not in seen)

    return result
