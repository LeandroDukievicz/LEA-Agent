"""Rotas HTTP da memória, para a tela Memória e o launcher."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from lea.memory import GitError, Memory, PageError

router = APIRouter(prefix="/api", tags=["memoria"])


def get_memory(request: Request) -> Memory:
    return request.app.state.memory


MemoryDep = Annotated[Memory, Depends(get_memory)]


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


@router.get("/pages")
async def list_pages(
    memory: MemoryDep,
    q: Annotated[str, Query(description="Texto a buscar.")] = "",
    project: Annotated[int | None, Query(description="Id do projeto.")] = None,
    tier: Annotated[str | None, Query(pattern="^(hot|warm|cold)$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Busca páginas. Sem ``q``, devolve a árvore do projeto."""
    if not q.strip():
        return {"tree": await memory.tree(project), "results": []}
    hits = await memory.search(q, project_id=project, tier=tier, limit=limit)
    return {"results": [hit.as_dict() for hit in hits], "count": len(hits)}


@router.get("/tree")
async def page_tree(
    memory: MemoryDep,
    project: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    return await memory.tree(project)


@router.get("/pages/{page_id}")
async def get_page(
    memory: MemoryDep,
    page_id: str,
    agent: Annotated[str, Query(description="Quem está lendo.")] = "ui",
) -> dict[str, Any]:
    """Lê a página. Registra o acesso, que reforça a força dela."""
    try:
        page = await memory.read(page_id, agent=agent)
        projection = await memory.projection(page_id)
    except (GitError, PageError) as exc:
        raise _not_found(exc) from exc

    return {
        "id": page.id,
        "path": page.path,
        "slug": page.qualified_slug,
        "title": page.title,
        "body": page.body,
        "tier": page.tier,
        "strength": round(page.strength, 4),
        "tags": page.tags,
        "created_by": page.created_by,
        "updated_by": page.updated_by,
        "links": page.links(),
        "backlinks": await memory.backlinks(page_id),
        "projection": projection.as_dict(),
    }


@router.put("/pages/{page_id}")
async def update_page(
    memory: MemoryDep,
    page_id: str,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Salva a página com commit no git."""
    try:
        current = await memory.peek(page_id)
    except (GitError, PageError) as exc:
        raise _not_found(exc) from exc

    result = await memory.write(
        path=current.path,
        title=payload.get("title", current.title),
        body=payload.get("body", current.body),
        tags=payload.get("tags", current.tags),
        tier=payload.get("tier", current.tier),
        agent=payload.get("agent", "ui"),
        message=payload.get("message"),
    )
    return result.as_dict()


@router.post("/pages", status_code=201)
async def create_page(
    memory: MemoryDep,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Cria uma página nova."""
    for required in ("path", "title"):
        if not payload.get(required):
            raise HTTPException(status_code=422, detail=f"campo obrigatório: {required}")

    result = await memory.write(
        path=str(payload["path"]),
        title=str(payload["title"]),
        body=str(payload.get("body") or ""),
        tags=list(payload.get("tags") or []),
        tier=payload.get("tier") or "warm",
        agent=payload.get("agent", "ui"),
        message=payload.get("message"),
    )
    return result.as_dict()


@router.get("/pages/{page_id}/versions")
async def page_versions(
    memory: MemoryDep,
    page_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    try:
        versions = await memory.versions(page_id, limit)
    except (GitError, PageError) as exc:
        raise _not_found(exc) from exc
    return {"versions": [v.as_dict() for v in versions]}


@router.get("/pages/{page_id}/diff")
async def page_diff(
    memory: MemoryDep,
    page_id: str,
    sha: Annotated[str, Query(min_length=4, max_length=64)],
) -> dict[str, Any]:
    try:
        return {"sha": sha, "diff": await memory.diff(page_id, sha)}
    except (GitError, PageError) as exc:
        raise _not_found(exc) from exc


@router.get("/pages/{page_id}/graph")
async def page_graph(
    memory: MemoryDep,
    page_id: str,
    depth: Annotated[int, Query(ge=1, le=4)] = 2,
) -> dict[str, Any]:
    return (await memory.graph(page_id, depth)).as_dict()


@router.get("/memory-suggestions")
async def memory_suggestions(
    request: Request,
    memory: MemoryDep,
    task: Annotated[str, Query(description="A tarefa da sessão.")] = "",
    project: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """Páginas sugeridas para injetar numa sessão nova, com o custo em tokens.

    É o que a lista com checkbox do launcher mostra.
    """
    config = request.app.state.config
    selection = await memory.context_for(
        project_id=project,
        task=task,
        budget_tokens=config.memory.inject_budget_tokens,
    )
    return selection.as_dict()


@router.post("/memory/reindex")
async def reindex(memory: MemoryDep) -> dict[str, Any]:
    """Reconstrói o índice a partir do git."""
    return await memory.reindex()


@router.post("/memory/decay")
async def apply_decay(memory: MemoryDep) -> dict[str, Any]:
    """Aplica decaimento e ajusta tiers. Nenhuma página é apagada."""
    return await memory.apply_decay()
