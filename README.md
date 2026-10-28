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

## Estado: Fase 1 (núcleo) concluída

O núcleo é um **daemon headless** com API local, WebSocket de eventos e receptor
de hooks. A interface é um cliente dele — é o que permite rodar sem janela num
homelab e trocar a camada visual sem mexer no produto.

| | |
|---|---|
| ✅ Fase 1 | núcleo: config, banco com migrações, barramento de eventos, CLI, tokens locais, scanner de injeção |
| ⬜ Fase 2 | memória: wiki git, FTS, decaimento, grafo, servidor MCP |
| ⬜ Fase 3 | projetos, launcher, harnesses, PTY, worktrees |
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
uv run pytest          # 48 testes
```

`lea doctor` numa instalação limpa cria `~/.lea/` (config, banco, wiki, skills,
worktrees, logs), com os diretórios em `0700` e os tokens em `0600`.

### Comandos

```
lea daemon    sobe o núcleo, sem janela (serve para homelab)
lea hook      ponte chamada de dentro dos agentes de código
lea doctor    diagnostica a instalação
lea mcp       servidor MCP de memória (Fase 2)
```

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
  faz isso persistir. `lea/security/injection.py` varre conteúdo que entra e sai
  da memória, com os limites documentados no módulo.
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
