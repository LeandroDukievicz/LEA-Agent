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

## Estado atual

Só o **protótipo visual**, que serve de fonte da verdade de layout e estética
para a implementação. Ainda não há backend: todos os números, sessões, custos
e logs são dados fictícios escritos no próprio arquivo.

```
wireframe.html     protótipo, página única autocontida
app/               o mesmo protótipo desempacotado em arquivos
├─ index.html      documento (template dc-runtime)
└─ assets/
   ├─ dc-runtime.js        runtime declarativo que monta a UI
   ├─ vendor/              react 18.3.1 + react-dom (UMD)
   └─ fonts/               JetBrains Mono e Rajdhani (woff2, offline)
```

`wireframe.html` embute tudo em base64 num único arquivo; `app/` é a mesma
página com os assets em disco. Os dois renderizam igual — a extração é byte a
byte nos assets e não reescreve o HTML, só troca as referências internas por
caminhos de arquivo.

## Ver o protótipo

Os assets carregam por caminho relativo, então sirva o diretório:

```bash
python3 -m http.server 8731
# http://127.0.0.1:8731/app/index.html
```

O bundle de arquivo único abre direto no navegador:

```bash
xdg-open wireframe.html
```

Isso é só para inspecionar o protótipo. O produto final abre numa **janela
nativa**, sem navegador e sem porta exposta.

## Telas

- **Overview** — agentes online, custo do dia, handoffs pendentes, tokens/hora
- **Projetos** — sessões rodando, fixadas, aguardando resposta e recentes
- **Console** — stream da sessão, tool calls colapsáveis, slash commands
- **Memória** — wiki em Markdown, força/decaimento, versões git e grafo
- **Skills** — biblioteca versionada, taxa de sucesso e auto-melhoria
- **Histórico & Handoffs** — linha do tempo e transferência entre agentes
- **Automações** — tarefas em cron escritas em linguagem natural
- **Canais & Agentes** — matriz de harnesses × hooks e gateways de mensagem

Interface em português do Brasil; código, tabelas e APIs em inglês.

## Licença

MIT — veja [LICENSE](LICENSE).
