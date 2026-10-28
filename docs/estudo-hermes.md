# Estudo: Hermes Agent como referência de arquitetura

Levantamento feito em 2026-10-28 sobre [NousResearch/hermes-agent][repo] (MIT),
para decidir o desenho do LEA. Leitura direta do repositório — clone esparso de
`agent/`, `tools/`, `gateway/`, `cron/`, `skills/`, `acp_adapter/`, `plugins/` e
`website/docs/`. Não foi leitura exaustiva: são ~908 mil linhas de Python em
2.516 arquivos, fora testes.

> Aviso ao pesquisar: boa parte do que aparece em busca sobre "Hermes Agent" é
> conteúdo de SEO gerado, em domínios que não são da Nous Research
> (`hermes-ai.net`, `hermesaiagent.app`, `hermesdesktop.app`, `hermes-agent.ai`).
> As fontes reais são o repositório e `hermes-agent.nousresearch.com`.

## O que o Hermes é

Núcleo Python headless (`hermes serve`) expondo **JSON-RPC sobre WebSocket**
(`tui_gateway`). Toda interface é cliente desse servidor:

| Superfície | Stack |
|---|---|
| `apps/desktop` | Electron + React + TypeScript + Vite; electron-builder → dmg/zip, MSIX, AppImage/deb/rpm |
| `ui-tui/` + `tui_gateway/` | TUI de terminal |
| `web/` + `website/` | painel no navegador |
| `apps/shared` | transporte compartilhado entre o desktop e o painel web |

O app Electron **empacota o payload Python junto** (`dist:bundled` →
`scripts/bundles/stage.py` → `build/agent-payload`), com Python 3.14 e
dependências dentro do pacote.

## Onde o LEA é diferente

O Hermes tem integração com agentes de terceiros, mas em duas formas que não são
a do LEA:

- **`hermes import-agent`** — importa configuração de `~/.claude`, `~/.codex`,
  `~/.cursor` (instruções, allowlists, MCP, skills, memórias). É **migração**:
  trazer o usuário para dentro e aposentar o resto.
- **Codex App-Server Runtime** — entrega o turno ao `codex app-server` e "vira a
  casca em volta dele". É **substituição do próprio loop**, um agente por vez.

Nos dois casos, o Hermes quer **ser** o seu agente.

O LEA faz o oposto: **N sessões simultâneas de harnesses diferentes, cada uma na
sua worktree, continuando sendo elas mesmas**, com o LEA como camada de memória e
supervisão *entre* elas. O handoff do Codex para o Claude Code, citando páginas da
wiki, não existe no Hermes.

### Memória: a diferença que mais importa

A memória embutida do Hermes é **dois arquivos markdown achatados** —
`MEMORY.md` e `USER.md` (`tools/memory_tool_store.py`), entradas separadas por
delimitador, orçamento em **caracteres**. Sem versionamento, sem decaimento, sem
grafo.

Memória rica no Hermes é **provider externo** (`plugins/memory/byterover`,
`plugins/memory/retaindb`), com OAuth — ou seja, nuvem.

A wiki git com força/decaimento, backlinks, diff e grafo **é melhor que a do
Hermes para uso local**, que é o caso de uso do LEA. É o coração do produto.

## O que reaproveitamos

| O quê | Por quê | Onde já foi aplicado |
|---|---|---|
| Formato `SKILL.md` | frontmatter compatível com Agent Skills (Claude Code e Hermes). Torna skill portável entre as ferramentas e permite consumir a biblioteca MIT deles. O que é só do LEA vai sob `metadata.lea`. | `lea/db/schema.py`, tabela `skills` |
| Núcleo como servidor + clientes | dá desktop, TUI e acesso remoto do notebook para o homelab | `lea/server/app.py` |
| `features/hooks.md` | quatro sistemas de hook com modelo de confiança explícito | referência para a Fase 3 |
| `kanban-worker-lanes.md`, `delegation.md` | arte prévia de orquestração multi-sessão | referência para a Fase 5 |

## O que fazemos diferente

### a) Revisão em background, não só consolidação noturna

`agent/background_review.py`: depois de cada turno, o Hermes forka o agente numa
thread daemon, replica o snapshot da conversa e pergunta *"alguma skill ou memória
deve ser salva ou atualizada?"*. O fork **herda o runtime vivo do pai, então
acerta o mesmo prefix cache** — a revisão sai quase de graça.

A spec original do LEA só tinha consolidação às 23h, que paga contexto cheio.
Fazemos os dois: revisão incremental barata por turno, e consolidação noturna para
decaimento e promoção de tier. Chave: `memory.background_review`.

### b) Prompt cache é decisão de arquitetura

O LEA injeta páginas de memória no contexto. Se o bloco injetado muda a cada
sessão, perde-se o cache toda vez. O bloco estável vai num prefixo fixo; o volátil
no fim.

### c) A memória é superfície de ataque — e no LEA é pior

O Hermes tem `_scan_memory_content()` com escopo `strict`, e o comentário no
código explica o porquê: *"memória entra no system prompt, então uma entrada
envenenada persiste entre sessões"*.

No LEA é pior por dois motivos:

1. O agente A **escreve** na wiki e o agente B **lê** aquilo direto no contexto.
   Se A processou algo não confiável (issue, PR, página web), o texto do atacante
   atravessa a wiki e chega ao system prompt de B.
2. A wiki é versionada em git: a entrada envenenada sobrevive ao fim da sessão,
   ao reinício do daemon e à consolidação.

Implementado em `lea/security/injection.py`, com os limites documentados lá: é
defesa em profundidade, não garantia. A mitigação que carrega o peso é estrutural
— conteúdo injetado vai delimitado e rotulado como dado não confiável.

### d) Pin exato das dependências desde o primeiro commit

O `pyproject.toml` do Hermes fixa toda dependência direta em `==X.Y.Z` e justifica
citando o worm Mini Shai-Hulud, que atingiu o `mistralai` 2.4.6 no PyPI: um range
teria instalado o pacote comprometido nas horas antes da quarentena. Adotado.

### Antipadrão a não copiar

Mais de **setenta módulos `hermes_state_*.py`** na raiz do pacote. Ninguém
responde "quais tabelas existem?" sem um grep. O esquema do LEA mora inteiro em
`lea/db/schema.py`.

## Escopo

O Hermes tem **35 plataformas de mensagem**, catálogo de plugins, credential
vault, kanban com worker lanes, browser automation, computer-use, TTS, batch
runner e evals. O LEA não compete nisso: dois canais (Telegram e Discord) e uma
interface de plugin.

[repo]: https://github.com/NousResearch/hermes-agent
