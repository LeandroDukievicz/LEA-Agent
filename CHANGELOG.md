# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Enquanto o LEA não tem release, cada entrada é uma fase da especificação.

## [Não publicado]

### Interface e orquestração

- **A LEA ganhou janela própria** (`lea avatar`) e saiu do dashboard. É um
  webview nativo sobre `/app/avatar.html`, servido pelo próprio daemon. As duas
  interfaces não conversam entre si: assinam o mesmo `/ws`, então fechar uma não
  afeta a outra. Extra opcional `desktop` (pywebview), escolhido no lugar de
  Electron e Tauri para não trazer Chromium nem a cadeia do Node/Rust.
- **Frota** (`lea fleet up/status/down`): sobe gerente (Codex), implementador
  (Claude Code) e publicador (agy) de uma vez, cada um com um papel só. O papel
  chega como prompt de abertura — o Launcher não injeta skill, isso é mecanismo
  do `lea run`.
- **Harness do Antigravity CLI** (`agy`). Não expõe hooks de ciclo de vida, e o
  adaptador reporta a ausência em vez de fingir que estão lá.
- Travas dos CLIs (`--yolo`, `--dangerously-skip-permissions`) em
  `[fleet.flags]`, lidas pelo servidor e nunca aceitas no corpo da requisição.
  Coleira do publicador ligada por padrão.
- Persona da LEA em `agent.persona`, aplicada em toda execução e inserida
  **depois** das regras de escopo, para tom não poder reescrever permissão.
- Memória de registro passa a ser o **ai-memory**, por MCP.

### Fase 5 concluída, Fase 7 avançando

- **Aprovação humana nos backends de CLI**, pelo hook `PreToolUse` que Codex e
  Claude Code expõem e o LEA não instalava. Funciona **mesmo com as travas
  soltas**: verificado contra o CLI real que `permissionDecision: "deny"` barra
  a ferramenta com `--dangerously-skip-permissions` ativo.
- **Coleira do publicador** (`lea fleet guard`): um `pre-push` para o agy, que
  não expõe hook nenhum. Quem executa é o git, antes de qualquer byte sair.
- **Memória no backend `anthropic-api`**, que era o único sem nenhuma — os de
  CLI alcançam o ai-memory por MCP, este não fala MCP. Ferramentas `recall` e
  `remember` por JSON-RPC direto, sem dependência nova.
- **O wheel passa a levar a interface** (`lea/app`). Antes, `pip install` dava
  um daemon servindo `/app` de um diretório inexistente.
- **O portão passou a distinguir executar de mencionar.** Casava por substring,
  então barrava um `echo` que citasse o padrão, um `grep` procurando por ele na
  documentação ou um CLI recebendo a expressão como prompt. Agora compara token
  a token, no começo de cada comando da linha.
- **A frota é autônoma.** `lea fleet up` sobe os três papéis em modo
  `autonomous`, e o portão respeita isso: dado o prompt de papel, cada um
  resolve o que for necessário sem parar para pedir. O padrão que casou vai para
  o log e para o feed, mas nada é enfileirado — numa sessão em que ninguém está
  olhando no instante da ação, enfileirar não produz decisão a tempo, produz
  agente sem a ferramenta e parado. O controle muda de lugar, para a borda onde
  o trabalho sai da máquina.
- **`fleet up` instala a coleira**, em vez de só anunciá-la. A instalação era um
  comando separado, então o aviso "Publicador com coleira" podia ser verdadeiro
  sobre o prompt e falso sobre o mecanismo — e era: neste repositório não havia
  `pre-push` nenhum. Quando não dá para instalar, o aviso agora diz isso em
  vermelho.
- `curl` e `wget` saíram de `require_approval_for`. Buscar documentação não é
  fronteira de segurança; era pedágio, e pedágio ensina a aprovar sem ler.
- **`Stop` não encerra mais a sessão; `SessionEnd` encerra.** Nos dois CLIs o
  `Stop` dispara quando o agente termina de *responder*. O LEA instalava só ele
  e o tratava como fim, então toda sessão de CLI era marcada como encerrada
  depois da primeira resposta, com o agente vivo e trabalhando — a tela mentia,
  o handoff saía no meio do trabalho e a fila de aprovação daquela sessão ficava
  indecidível. Visto na frota: o implementador rodou oito ferramentas,
  respondeu, e virou `handoff` no banco enquanto o processo seguia de pé.
- **A aprovação do portão passou a poder ser aprovada.** `resolve_approval`
  exigia sessão `paused`, condição que vem do backend `anthropic-api`, onde o
  laço de ferramentas pausa e espera. O portão dos CLIs não pausa nada, então
  `lea approvals approve` respondia "não encontrada" para toda aprovação vinda
  de Codex ou Claude Code: metade da Fase 5 — a da liberação — nunca funcionou.
- **A fila de aprovação mostra o comando** e sai com as linhas de `approve`
  prontas para colar. Vinda do portão, toda aprovação aparecia como
  `(inválido)`: a tela só entendia a forma `argv` do `anthropic-api`, e pedia
  uma decisão sobre um comando que não mostrava.
- **Pendência de sessão encerrada expira.** No `anthropic-api` quem pede também
  limpa; o portão não tem esse dono, e a pendência sobrevivia à sessão.
- **O portão vale para as sessões do LEA** (`agent.gate_external_sessions`,
  padrão `false`). Os hooks são de usuário, então instalá-los põe o portão na
  frente de todo terminal da máquina — e para sessão que o LEA não lançou não
  existe fila de aprovação, porque ela referencia `sessions`. Era bloqueio sem
  saída. `true` para quem quer a máquina inteira sob trava.

### Corrigido

- Sessões ficavam `active` para sempre quando o daemon morria; o PTY ia junto e
  `fleet status` listava agentes que não existiam. Varredura na subida.
- `AI_MEMORY_*` não passava pela lista branca de ambiente dos CLIs, então o MCP
  de memória falhava com 401 **em silêncio** no `lea run`.
- Flags de CLI entravam depois do prompt posicional, onde vários CLIs já pararam
  de ler flags.
- `lea avatar` quebrava com traceback cru quando faltava o backend gráfico; agora
  explica o pacote da distribuição, o `PYTHONPATH` e a saída pelo navegador.
- Contraste da interface contra o fundo preto: 231 trocas de cor, molduras de
  card de 1,38 para 3,03 de razão.

### Removido

- Pipeline de voxels do avatar (`lea-portrait.js`, `tools/build_portrait.py`),
  órfão desde a reescrita do retrato.
- Seletor de expressão e teste de fala do dashboard: a expressão é comandada
  pelo agente, e um seletor manual seria um botão mentindo sobre quem decide.


### Fase 5 — base do agente, skills e cron (em andamento)

- Agente próprio com backends Codex CLI, Claude Code e Anthropic API; sessão,
  eventos e contexto da wiki/skills são compartilhados entre os modos.
- Codex usa `codex exec --json`, sandbox `workspace-write` e revisão automática;
  Claude Code usa `claude -p`, modo de permissão `auto` e prompts de permissão
  negados em execução sem terminal.
- Autenticação dos backends CLI fica no próprio programa; chaves de API de
  modelos não são repassadas aos processos locais.
- `lea run --backend` escolhe o backend por tarefa. Automações persistem a
  escolha e as que já existiam são migradas para Codex.
- O agente via API mantém limites de duração, tokens, rodadas de ferramenta e
  concorrência. Para os CLIs, o LEA aplica limite de duração; controle de
  tokens e custo monetário ainda não está integrado.
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
