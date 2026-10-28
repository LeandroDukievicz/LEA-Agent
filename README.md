# LEA-Agent

Wireframe de um painel de orquestração de agentes de código — uma página única,
sem build e sem dependências de servidor.

## O que tem aqui

`wireframe.html` é um bundle autocontido (HTML + CSS + JS + fontes embutidos em
base64). Abrir o arquivo no navegador já exibe o protótipo:

```bash
xdg-open wireframe.html
```

## Telas prototipadas

- **Overview** — agentes online, custo do dia, handoffs pendentes, tokens/hora
- **Projetos** — sessões rodando, fixadas, aguardando resposta e recentes
- **Canais & Agentes** — Slack, Telegram, WhatsApp e Discord ligados a
  claude-code, codex, cursor, aider e gemini-cli
- **Console** — comandos de sessão (`/compress`, `/memory`, …)
- **Memória** — páginas de memória injetadas por sessão
- **Automações** — tarefas em cron e seus próximos disparos
- **Histórico & Handoffs** — sessões encerradas e handoffs transferidos
- **Skills** — skills disponíveis por agente

## Status

Protótipo visual. Todos os números, sessões, custos e logs são dados fictícios
escritos no próprio arquivo — não há backend, autenticação nem chamada de API.
