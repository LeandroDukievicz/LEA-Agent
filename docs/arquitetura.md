# Arquitetura

## A decisão que organiza tudo

O núcleo é um **daemon headless**; a interface é cliente dele. Isso veio do
estudo do Hermes Agent ([docs/estudo-hermes.md](estudo-hermes.md)) e resolve
três coisas de uma vez:

1. `lea daemon` roda sem janela num homelab.
2. Os hooks são processos de terceiros (Claude Code, Codex) que precisam
   alcançar o LEA de fora — eles não chamam função Python nossa, mas sabem
   fazer um POST.
3. Trocar a camada visual depois não mexe no produto.

```
agentes de código                      interface
(claude, codex, …)                    (ainda a definir)
      │                                     │
      │ lea hook <evento>                   │ HTTP + WebSocket
      │ lea mcp (stdio)                     │ X-Lea-Token
      ▼                                     ▼
┌─────────────────────────────────────────────────────┐
│  daemon  ·  127.0.0.1:7420                          │
│                                                      │
│  server/     rotas, /ws, receptor de hooks          │
│  sessions/   launcher, PTY, worktrees               │
│  memory/     wiki git + índice SQLite + injeção     │
│  handoffs    passagem de bastão entre agentes       │
│  harnesses/  adaptadores (hooks + MCP)              │
│  security/   tokens locais, scanner de injeção      │
│  events      barramento pub/sub                      │
└─────────────────────────────────────────────────────┘
      │                      │
      ▼                      ▼
  ~/.lea/lea.db         ~/.lea/wiki (git)
  índice, descartável   fonte da verdade
```

## Fonte da verdade

| Dado | Onde vive | Reconstruível? |
|---|---|---|
| Páginas de memória | `~/.lea/wiki/**.md` (git) | — é a fonte |
| Índice, busca, grafo | `pages`, `pages_fts`, `page_links` | sim, `lea reindex` |
| Histórico de acesso | `page_access` | **não** — só existe no banco |
| Sessões e eventos | `sessions`, `events` | não |
| Handoffs | `handoffs` **e** `wiki/handoffs/*.md` | parcialmente |

A linha do `page_access` é a que mais importa na prática: ela não está no git,
e apagá-la zeraria a força de toda a wiki. Foi por isso que `reindex` faz
upsert em vez de `DELETE FROM pages` — o `ON DELETE CASCADE` levaria o
histórico junto.

## API

Tudo sob `/api`, autenticado por `X-Lea-Token`. O WebSocket autentica por
query string, porque a API do navegador não deixa definir cabeçalho no
handshake.

### Núcleo
```
GET  /api/health                      sem token: a UI descobre se o daemon subiu
GET  /api/overview                    contadores, custo do dia, CPU/mem
WS   /ws                              eventos globais (feed, alertas)
POST /hooks/{evento}                  token próprio; SessionStart devolve contexto
```

### Memória
```
GET  /api/pages?q=&project=&tier=     busca; sem q, devolve a árvore
GET  /api/tree?project=
POST /api/pages                       cria (commit)
GET  /api/pages/{id}                  lê e reforça; traz projeção e backlinks
PUT  /api/pages/{id}                  edita (commit)
GET  /api/pages/{id}/versions
GET  /api/pages/{id}/diff?sha=
GET  /api/pages/{id}/graph?depth=
GET  /api/memory-suggestions?task=    o que injetar, com custo em tokens
POST /api/memory/reindex
POST /api/memory/decay
```

### Projetos, harnesses e sessões
```
GET  /api/projects
POST /api/projects/inspect            prévia, sem registrar nada
POST /api/projects                    abre (opcionalmente indexa docs)
GET  /api/harnesses                   matriz hooks × mcp
POST /api/harnesses/{id}/install      altera config de terceiro: backup + merge
GET  /api/sessions?status=&project=
POST /api/sessions                    inicia um agente
GET  /api/sessions/{id}
GET  /api/sessions/{id}/events
POST /api/sessions/{id}/{pause|resume|stop|input}
WS   /api/sessions/{id}/pty           terminal, bytes crus nos dois sentidos
```

### Handoffs
```
GET  /api/handoffs?status=
GET  /api/handoffs/{id}
POST /api/sessions/{id}/handoff       gera (o /handoff do console)
POST /api/handoffs/{id}/claim         assume: inicia sessão em modo handoff
```

> Ordem de registro importa: as rotas de handoff entram **antes** das de
> sessão, senão `/api/sessions/{id}/{action}` captura `handoff` como ação
> desconhecida.

## Invariantes que o código assume

- **Horário é sempre UTC no banco.** A conversão acontece na borda, ao exibir.
  Guardar horário local faz o relatório semanal errar no horário de verão.
- **Conteúdo de memória é dado, nunca instrução.** Vai delimitado e rotulado;
  ver `memory/inject.py`.
- **Decaimento não apaga nada.** Decide o que entra em contexto, não o que
  existe.
- **Escrita em config de terceiro faz backup e merge.** Nunca sobrescreve.
- **O hook nunca derruba a sessão do agente.** Daemon fora do ar = sai 0.
- **Toda escrita na wiki é um commit.** É o que dá histórico e `git revert`.

## Testes

```
tests/                    unidade: decaimento, página, eventos, tokens, harnesses
tests/acceptance/         borda: HTTP, WebSocket, MCP por stdio, PTY real
```

Os testes de aceite verificam o critério de cada fase da especificação. Foi
neles que apareceram os defeitos que mais custariam em produção — a task de
PTY gravando num banco já fechado, e a colisão de rota do handoff.

```bash
uv run pytest                      # tudo
uv run pytest tests/acceptance -q  # só os de aceite
```
