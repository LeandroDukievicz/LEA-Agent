"""Cliente do ai-memory para o backend ``anthropic-api``.

Nos backends de CLI a LEA já alcança o ai-memory: Codex, Claude Code e agy têm
o servidor registrado como MCP e o chamam sozinhos. O ``anthropic-api`` é o
único que não fala MCP -- quem roda o laço de ferramentas ali é o próprio LEA.
Resultado: era o único modo em que a LEA trabalhava **sem memória nenhuma**.

Isto resolve sem trazer dependência: o servidor do ai-memory responde JSON-RPC
direto no ``/mcp``, autenticado por Bearer, e o ``httpx`` já está no núcleo.
Dois métodos chegam ao agente como ferramentas, ``recall`` e ``remember``.

Onde mora o token, verificado numa instalação real em 2026-11-08: o arquivo
``~/.local/share/ai-memory/auth-token``, com a variável ``AI_MEMORY_AUTH_TOKEN``
tendo precedência quando existe.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:49374/mcp"
TOKEN_FILE = Path.home() / ".local" / "share" / "ai-memory" / "auth-token"
TIMEOUT = 20.0


class MemoryUnavailable(RuntimeError):
    """O ai-memory não respondeu. Nunca derruba a tarefa: vira texto para o agente."""


def server_url() -> str:
    return os.environ.get("AI_MEMORY_SERVER_URL") or DEFAULT_URL


def auth_token() -> str:
    token = os.environ.get("AI_MEMORY_AUTH_TOKEN")
    if token:
        return token.strip()
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def configured() -> bool:
    """Há token para falar com o ai-memory? Sem ele, nem oferecemos as ferramentas."""
    return bool(auth_token())


async def call(tool: str, arguments: dict[str, Any]) -> str:
    """Chama uma ferramenta do ai-memory e devolve o texto da resposta."""
    token = auth_token()
    if not token:
        raise MemoryUnavailable(
            "ai-memory sem token; esperado em AI_MEMORY_AUTH_TOKEN ou "
            f"{TOKEN_FILE}"
        )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                server_url(),
                json=payload,
                headers={
                    "authorization": f"Bearer {token}",
                    "content-type": "application/json",
                    # O transporte aceita resposta única ou fluxo; pedir os dois
                    # evita 406 quando o servidor escolhe SSE.
                    "accept": "application/json, text/event-stream",
                },
            )
    except httpx.HTTPError as exc:
        raise MemoryUnavailable(f"ai-memory inalcançável: {exc}") from exc

    if response.status_code >= 400:
        raise MemoryUnavailable(f"ai-memory respondeu {response.status_code}")
    return _text_of(_decode(response.text))


def _decode(raw: str) -> dict[str, Any]:
    """Lê a resposta, que pode vir como JSON puro ou como evento SSE."""
    texto = raw.strip()
    if texto.startswith("event:") or texto.startswith("data:"):
        for linha in texto.splitlines():
            if linha.startswith("data:"):
                texto = linha[5:].strip()
                break
    try:
        body = json.loads(texto)
    except ValueError as exc:
        raise MemoryUnavailable(f"resposta ilegível do ai-memory: {exc}") from exc
    if not isinstance(body, dict):
        raise MemoryUnavailable("resposta do ai-memory não é um objeto")
    if body.get("error"):
        raise MemoryUnavailable(str(body["error"].get("message", body["error"]))[:300])
    return body


def _text_of(body: dict[str, Any]) -> str:
    """Extrai o texto de um resultado MCP, que vem como lista de blocos."""
    result = body.get("result") or {}
    blocks = result.get("content") or []
    partes = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    texto = "\n".join(parte for parte in partes if parte).strip()
    return texto or json.dumps(result, ensure_ascii=False)[:4000]


#: Ferramentas que o agente enxerga. Só entram quando há token -- oferecer uma
#: ferramenta que sempre falha ensina o modelo a parar de usá-la.
TOOLS = [
    {
        "name": "recall",
        "description": (
            "Consulta a memória compartilhada (ai-memory) antes de supor. "
            "Outros agentes desta máquina gravam nela."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "O que você quer lembrar."},
                "limit": {"type": "integer", "description": "Máximo de páginas; padrão 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Grava na memória compartilhada o que a próxima sessão não deduziria "
            "do código: o porquê de uma decisão, o que falhou, o que ficou em "
            "aberto. Não grave o que o diff já conta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Caminho da página, ex.: 'decisoes/cache-de-sessao'."},
                "title": {"type": "string", "description": "Título curto."},
                "body": {"type": "string", "description": "Conteúdo em Markdown."},
            },
            "required": ["path", "body"],
        },
    },
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


async def dispatch(name: str, arguments: dict[str, Any], *, project: str = "") -> str:
    """Executa ``recall`` ou ``remember``. Erro vira texto, nunca exceção."""
    try:
        if name == "recall":
            query = str(arguments.get("query") or "").strip()
            if not query:
                return "Error: query vazia."
            limit = arguments.get("limit")
            args: dict[str, Any] = {"query": query}
            if isinstance(limit, int) and 0 < limit <= 50:
                args["limit"] = limit
            if project:
                args["project"] = project
            return await call("memory_query", args)

        if name == "remember":
            path = str(arguments.get("path") or "").strip()
            body = str(arguments.get("body") or "").strip()
            if not path or not body:
                return "Error: path e body são obrigatórios."
            args = {"path": path, "body": body}
            if arguments.get("title"):
                args["title"] = str(arguments["title"])
            if project:
                args["project"] = project
            return await call("memory_write_page", args)
    except MemoryUnavailable as exc:
        # Texto, e não exceção: a tarefa continua, e o agente fica sabendo que
        # a memória está fora em vez de achar que não há nada gravado.
        return f"Error: {exc}"
    return f"Error: ferramenta de memória desconhecida: {name}"
