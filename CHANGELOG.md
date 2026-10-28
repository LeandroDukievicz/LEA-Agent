# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Enquanto o LEA não tem release, cada entrada é uma fase da especificação.

## [Não publicado]

### Fase 5 — base do agente, skills e cron (em andamento)

- Agente próprio via Anthropic, com sessão, eventos, memória e limites de
  duração, tokens, rodadas de ferramentas e concorrência.
- Ferramentas para listar, ler e escrever arquivos do projeto e executar
  programas sem shell; caminhos de arquivo ficam dentro do projeto e comandos
  configurados para aprovação encerram a tarefa sem execução.
- Opt-in de autonomia por projeto e endpoint para editar os limites e a lista
  de comandos que exigem revisão.
- Skills no formato Agent Skills em `~/.lea/skills`, importação de arquivos,
  histórico de versões e execuções.
- Agendador cron com cinco campos, fuso IANA, histórico de runs e recuperação
  de execuções interrompidas pelo encerramento do daemon.
- CLI `lea run` e rotas HTTP para iniciar tarefas, manter skills e agendar
  instruções.
- Revisão automática de skills, aprovação interativa, cálculo de custos e
  canais de mensagem permanecem para etapas posteriores.

### Fase 4 — Handoffs

- Geração de handoff no hook `Stop` e pelo `/handoff` do console, com
  **onde parou · o que falhou · em aberto · páginas citadas**.
- Handoff persiste em dois lugares: linha em `handoffs` e
  `wiki/handoffs/<id>.md` versionado, que conta como página citável.
- `POST /api/handoffs/{id}/claim` inicia sessão em modo `handoff` com o
  documento como **primeira página do contexto**.
- Camada de modelo (`lea/llm.py`) com protocolo neutro e backend Anthropic
  pelo SDK oficial.
- **Funciona sem credencial**: o handoff é montado dos eventos registrados
  quando não há modelo disponível.
- Claim condicionado a `status = 'pending'`, para dois cliques não darem o
  mesmo trabalho a dois agentes.

### Fase 3 — Projetos, launcher e sessões

- Abrir projeto com detecção de linguagem, branch e remote; indexação
  opcional de `README`/`docs` na wiki.
- Adaptadores de Claude Code e Codex, escritos contra instalações reais
  (2.1.278 e 0.164.0): hooks por merge com backup, MCP pelo CLI da própria
  ferramenta.
- Sessões sob PTY real, com worktree git por sessão e controles de pausar,
  retomar, parar e enviar entrada.
- WebSocket de terminal com histórico para quem conecta depois.
- `lea harnesses [--install]`.

**Corrigido:** a task que lia o PTY sobrevivia ao fim do `lifespan` e gravava
num banco fechado — `no active connection` em todo Ctrl-C.

### Fase 2 — Memória

- Wiki em Markdown versionada em git como fonte da verdade; SQLite como
  índice descartável (`lea reindex`).
- Busca FTS5 com boost por força e por projeto; árvore, grafo e backlinks.
- Força com meia-vida por tier, promoção por uso e rebaixamento por
  decaimento. **Nada é apagado.**
- Injeção de contexto com prefixo estável (para o prompt cache) e conteúdo
  delimitado como dado.
- Servidor MCP `lea.memory` por stdio: `memory_search`, `memory_read`,
  `memory_write`.

**Corrigido:** YAML lia `id: 0x7a3f19` como inteiro hexadecimal, encurtando
ids com zero à esquerda. **Corrigido:** `reindex` apagava `page_access` por
cascata, zerando a força da wiki.

**Desvio da especificação:** ela escrevia `exp(-Δt/h)` mas chamava o
parâmetro de meia-vida. Implementada a meia-vida de verdade (`0.5**(Δt/h)`).

### Fase 1 — Núcleo

- Daemon headless com API local, WebSocket de eventos e receptor de hooks.
- Configuração em `~/.lea/config.toml`, com precedência para `LEA_*`.
- Banco SQLite com migrações versionadas e esquema num arquivo só.
- Barramento de eventos com filas limitadas.
- Dois tokens locais em `0600`: um da interface, outro dos hooks.
- Scanner de injeção de prompt para conteúdo de memória.
- Dependências com pin exato.
- CLI: `daemon`, `hook`, `doctor`.

### Antes das fases

- Protótipo visual desempacotado em `app/`, com assets byte a byte idênticos
  ao bundle original.
- Licença MIT.
