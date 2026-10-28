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
│  agent.py    agente autônomo e ferramentas locais   │
│  skills      SKILL.md + versões e execuções          │
│  cron        agenda e dispara tarefas autônomas     │
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

### Agente próprio, skills e automações
```
POST  /api/agent/runs                    inicia tarefa em modo autônomo
POST  /api/agent/runs/{id}/stop          interrompe a tarefa
GET   /api/skills                        lista skills indexadas
POST  /api/skills                        cria SKILL.md
GET   /api/skills/{name}                 lê skill
PUT   /api/skills/{name}                 grava nova versão
GET   /api/skills/{name}/history         histórico de versões
GET   /api/skills/{name}/runs            histórico de execução
GET   /api/automations                   lista tarefas agendadas
POST  /api/automations                   cria cron
PATCH /api/automations/{id}              edita ou pausa cron
GET   /api/automations/{id}/runs         histórico de execução
PATCH /api/projects/{id}/autonomy        opt-in e limites do projeto
```

`lea-agent` só inicia em projeto com `allow_autonomous = 1`. O backend padrão é
Codex CLI; cada tarefa ou automação pode escolher `codex`, `claude-code` ou
`anthropic-api`. Os CLIs recebem o contexto da wiki e a skill selecionada e
usam suas autenticações locais. O Codex roda em sandbox `workspace-write` com
revisão automática de permissões. O Claude Code roda no modo `auto`, e prompts
que exigiriam interação são negados. As regras `require_approval_for` são
aplicadas diretamente às ferramentas do modo API e também incluídas como
instruções nos modos CLI, cujas permissões locais continuam sendo a autoridade.
`POST /api/agent/runs` e `POST /api/automations` aceitam `backend`; se ausente,
valem `agent.default_backend`.

O modo API recebe ferramentas de leitura e escrita limitadas ao projeto, além
de execução de programas sem shell. Comandos que correspondem à lista global
ou do projeto são recusados antes da execução. Comandos permitidos e os CLIs
locais rodam sob o usuário local; habilite autonomia apenas para projetos em
que confia. O LEA impõe limite de duração a todos os backends; os limites de
tokens são aplicados pelo modo API.

Skills ficam em `~/.lea/skills/<nome>/SKILL.md`; ao iniciar, o daemon importa
novos arquivos e indexa edições. O Markdown guarda o conteúdo e o SQLite guarda
versões e execuções. O scanner estrito impede injetar uma skill marcada. O cron aceita
cinco campos numéricos e fuso IANA; o daemon
calcula a próxima execução em UTC, faz claim antes de disparar e marca execuções
inacabadas como interrompidas após reiniciar. O backend fica salvo com cada
automação; as anteriores à migração passam a usar Codex.

Esta é a base da Fase 5. Revisão automática de skills, fluxo interativo de
aprovação, custo monetário e canais ainda não estão ligados ao agente próprio.

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
