# LEA-Agent

Centro de comando **self-hosted** para agentes de IA com memória persistente.
É um **programa desktop em Python**, que roda na máquina do usuário — não é um
site nem um serviço hospedado.

O que ele une:

1. **Memória compartilhada entre agentes de código** (Claude Code, Codex CLI,
   Gemini CLI, Cursor, Aider) — uma wiki em Markdown versionada em git, com
   força/decaimento por página, busca, backlinks e handoffs entre agentes.
2. **Um agente autônomo próprio (lea-agent)** — aprende e melhora skills, roda
   tarefas agendadas escritas em linguagem natural, delega para subagentes e
   conversa por Telegram, Discord, Slack e WhatsApp.

Diferente de um agente que quer substituir os outros, o LEA deixa cada agente
continuar sendo ele mesmo e fica **entre** eles, como camada de memória e
supervisão. Ver [docs/estudo-hermes.md](docs/estudo-hermes.md).

## Estado: Fases 1 a 3 concluídas

O núcleo é um **daemon headless** com API local, WebSocket de eventos e receptor
de hooks. A interface é um cliente dele — é o que permite rodar sem janela num
homelab e trocar a camada visual sem mexer no produto.

| | |
|---|---|
| ✅ Fase 1 | núcleo: config, banco com migrações, barramento de eventos, CLI, tokens locais, scanner de injeção |
| ✅ Fase 2 | memória: wiki git, busca FTS, decaimento, grafo, injeção de contexto, servidor MCP |
| ✅ Fase 3 | projetos, launcher, adaptadores de harness, hooks, PTY, worktrees |
| ⬜ Fase 4 | handoffs |
| ⬜ Fase 5 | lea-agent, skills, cron |
| ⬜ Fase 6 | canais de mensagem |
| ⬜ Fase 7 | custos, empacotamento |

A camada de interface ainda não foi escolhida — ver [Protótipo](#protótipo).

## Rodar

Requer Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run lea doctor      # confere instalação, permissões e banco
uv run lea daemon      # sobe o núcleo em 127.0.0.1:7420
uv run pytest          # 156 testes
```

`lea doctor` numa instalação limpa cria `~/.lea/` (config, banco, wiki, skills,
worktrees, logs), com os diretórios em `0700` e os tokens em `0600`.

### Comandos

```
lea daemon    sobe o núcleo, sem janela (serve para homelab)
lea hook      ponte chamada de dentro dos agentes de código
lea mcp        servidor MCP lea.memory, por stdio
lea harnesses  matriz de integração dos agentes; --install para configurar
lea reindex    reconstrói o índice da memória a partir do git
lea doctor     diagnostica a instalação
```

## Agentes de código

```
$ lea harnesses
agente          versão                 hooks  mcp  observação
CC Claude Code  2.1.278 (Claude Code)  ●      ●
CX Codex CLI    codex-cli 0.164.0      ◐      ●    1 hook aguardando confiança
```

`lea harnesses --install claude-code` registra os hooks de ciclo de vida
(`SessionStart`, `PostToolUse`, `PreCompact`, `Stop`) e o servidor MCP de
memória.

**Isso altera a configuração de outra ferramenta na sua máquina**, então é um
comando explícito e nunca roda sozinho. Antes de tocar em qualquer arquivo o
LEA faz backup, e a escrita é sempre **merge**: hooks de outras ferramentas que
já estavam ali continuam funcionando. Rodar duas vezes não duplica nada.

Formatos conferidos contra instalações reais em 2026-10-28, porque mudam com
frequência:

| | Hooks | MCP |
|---|---|---|
| Claude Code | `~/.claude/settings.json`, chave `hooks` | `claude mcp add --scope user` |
| Codex CLI | `~/.codex/hooks.json` | `codex mcp add` |

O Codex exige **confiar** em cada hook novo antes de executá-lo (há um
`trusted_hash` por hook em `config.toml`). O LEA não forja esse hash nem sugere
`--dangerously-bypass-hook-trust`: ele instala e avisa que o Codex vai perguntar
na próxima execução.

## Sessões

`POST /api/sessions` inicia um agente e devolve a sequência de boot que o
console desenha linha a linha:

```
✓ comando:   claude "padronizar os testes"
✓ worktree:  ~/.lea/worktrees/s-7d06 (lea/s-7d06)
✓ hooks:     ok · mcp ok
✓ memória:   3 página(s) · 1.204 tokens
✓ sessão:    ativa · pid 2422616
```

O agente roda sob **PTY de verdade** — sem TTY eles desligam cor e modo
interativo. A saída vai por WebSocket em `/api/sessions/{id}/pty` como bytes
crus, e quem conecta depois recebe o histórico para não abrir num terminal em
branco.

Controles: `pause` (SIGSTOP), `resume`, `stop` (SIGTERM → SIGKILL) e `input`.
Os sinais vão ao **grupo** de processos, então o `npm test` que o agente abriu
não fica órfão. Com `use_worktree`, cada sessão ganha diretório e branch
próprios — e a worktree **não é apagada** no fim, porque pode haver trabalho não
commitado lá dentro.

## Memória

Uma wiki em Markdown versionada em git, em `~/.lea/wiki`. **Os arquivos são a
fonte da verdade**; o SQLite é só índice e pode ser reconstruído a qualquer
momento com `lea reindex`. Isso significa que você pode editar a wiki com seu
editor, dar `git revert`, fazer merge — o LEA se reconcilia.

```markdown
---
id: '0x7a3f19'
title: Por que SQLite
tier: warm
strength: 0.71
tags: [arquitetura]
---

Zero infraestrutura, FTS5 embutido. Ver [[lea-core/tiers]].
```

**Força e decaimento.** Cada página tem uma força que cai com o tempo e sobe
com o uso. Ler reforça; ser citada num handoff reforça mais. Três acessos em
48 h promovem o tier; força abaixo de 0,25 rebaixa. **Decaimento nunca apaga
nada** — ele decide o que entra no contexto, não o que existe.

Meia-vida por tier: `hot` 3 dias, `warm` 21, `cold` 120 (configurável).

**MCP.** Os agentes de código falam com a memória por `lea mcp`, que expõe
`memory_search`, `memory_read` e `memory_write`. Três ferramentas só:
superfície pequena é superfície auditável.

### Configuração

`~/.lea/config.toml`, criado com padrões no primeiro uso e feito para ser
editado à mão. Precedência: `LEA_*` no ambiente > arquivo > padrão do código.
Aninhado usa duplo sublinhado (`LEA_SERVER__PORT=7421`).

`LEA_HOME` aponta a raiz para outro lugar — útil para perfis separados e usado
pelos testes.

## Segurança

- O daemon escuta **só em 127.0.0.1**. Mudar `server.host` o expõe à rede, e com
  ele vão as ferramentas do agente, que executam comandos; a CLI e o `doctor`
  avisam.
- Escutar em loopback não autentica nada — qualquer processo da máquina alcança a
  porta. Há **dois tokens** locais em `0600`: um para a interface, outro para os
  hooks, que rodam dentro de processos de terceiros e precisam poder ser
  rotacionados em separado.
- **Memória é superfície de ataque.** O agente A escreve na wiki e o agente B lê
  aquilo dentro do contexto dele: se A processou uma issue ou página web hostil,
  o texto do atacante atravessa a wiki e chega ao system prompt de B — e o git
  faz isso persistir. Duas defesas, nesta ordem de importância: todo conteúdo
  injetado vai **delimitado e rotulado como dado**, nunca concatenado solto nas
  instruções; e `lea/security/injection.py` varre o que entra e sai da memória.
  Página marcada é gravada, mas **não é injetada** em contexto de outro agente —
  bloquear a escrita perderia trabalho num falso positivo, enquanto o estrago
  real aconteceria na injeção.
- Dependências com **pin exato**, para que nenhuma versão nova chegue sem revisão.

## Protótipo

A fonte da verdade visual, ainda sem backend — números, sessões e custos são
dados fictícios escritos no próprio arquivo.

```
wireframe.html     protótipo, página única autocontida
app/               o mesmo protótipo desempacotado em arquivos
```

Para ver, sirva o diretório (os assets carregam por caminho relativo):

```bash
python3 -m http.server 8731
# http://127.0.0.1:8731/app/index.html
```

Telas: Overview, Projetos, Console, Memória, Skills, Histórico & Handoffs,
Automações, Canais & Agentes.

Interface em português do Brasil; código, tabelas e APIs em inglês.

## Licença

MIT — veja [LICENSE](LICENSE).
