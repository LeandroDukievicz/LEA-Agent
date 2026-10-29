"""CLI do LEA.

``lea daemon``  sobe o núcleo (com ou sem janela)
``lea hook``    ponte chamada de dentro dos agentes de código
``lea doctor``  diagnostica a instalação
``lea mcp``     servidor MCP de memória (Fase 2)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from lea import __version__, paths, status
from lea.config import load
from lea.db import schema
from lea.db import session as db
from lea.security import tokens

app = typer.Typer(
    name="lea",
    help="Centro de comando self-hosted para agentes de IA com memória persistente.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

approval_commands = typer.Typer(
    name="approvals",
    help="Revisa ações do lea-agent que estão aguardando uma decisão humana.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(approval_commands, name="approvals")

skill_commands = typer.Typer(
    name="skills",
    help="Consulta skills e revisa propostas do agente.",
    no_args_is_help=True,
    add_completion=False,
)
skill_proposal_commands = typer.Typer(
    name="proposals",
    help="Lista, inspeciona, aplica ou recusa propostas de skill.",
    no_args_is_help=True,
    add_completion=False,
)
channel_commands = typer.Typer(
    name="channels",
    help="Configura bots Telegram e Discord para o agente próprio.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(skill_commands, name="skills")
skill_commands.add_typer(skill_proposal_commands, name="proposals")
app.add_typer(channel_commands, name="channels")


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log detalhado.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def version() -> None:
    """Mostra a versão."""
    console.print(f"lea {__version__}")


@app.command("costs")
def show_costs(days: int = typer.Option(30, help="Dias de histórico (1 a 365).")) -> None:
    """Mostra o gasto estimado pela API e o orçamento diário."""
    if not 1 <= days <= 365:
        raise typer.BadParameter("days precisa ficar entre 1 e 365")

    async def load_costs() -> tuple[list[dict[str, Any]], float]:
        paths.ensure_layout()
        async with db.connection() as conn:
            async with conn.execute(
                "SELECT date(at, 'localtime') AS day, SUM(cost_usd) AS cost_usd, "
                "SUM(cost_brl) AS cost_brl FROM costs "
                "WHERE date(at, 'localtime') >= date('now', 'localtime', ?) "
                "GROUP BY day ORDER BY day DESC",
                (f"-{days - 1} days",),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
            today = await status.cost_today_brl(conn)
        return rows, today

    config = load()
    rows, today = asyncio.run(load_costs())
    table = Table(title=f"Custos Anthropic API · últimos {days} dias")
    table.add_column("Dia")
    table.add_column("USD", justify="right")
    table.add_column("BRL", justify="right")
    for row in rows:
        table.add_row(
            str(row["day"]),
            f"${float(row['cost_usd'] or 0):.4f}",
            f"R$ {float(row['cost_brl'] or 0):.2f}",
        )
    if rows:
        console.print(table)
    else:
        console.print("Nenhum custo de API registrado nesse período.")
    console.print(
        f"Hoje: R$ {today:.2f} / R$ {config.costs.daily_budget_brl:.2f} "
        "de orçamento diário."
    )


@channel_commands.command("add")
def add_channel_route(
    platform: Annotated[str, typer.Argument(help="Plataforma: telegram ou discord.")],
    chat_id: Annotated[
        str,
        typer.Option("--chat-id", help="ID numérico do chat/canal; * para DMs autorizadas."),
    ],
    project_id: Annotated[int, typer.Option("--project", help="Projeto com autonomia habilitada.")],
    user_ids: Annotated[
        list[str],
        typer.Option("--user-id", help="ID de usuário autorizado; repita a opção."),
    ],
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="Backend: codex, claude-code ou anthropic-api."),
    ] = None,
) -> None:
    """Autoriza usuários específicos a iniciar tarefas em um projeto."""
    from lea import channels

    async def configure() -> dict[str, Any]:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await channels.configure_route(
                conn,
                platform=platform,
                remote_id=chat_id,
                project_id=project_id,
                allowed_user_ids=user_ids,
                backend=backend,
            )

    try:
        route = asyncio.run(configure())
    except channels.ChannelError as exc:
        err_console.print(f"[red]não foi possível configurar o canal:[/] {exc}")
        raise typer.Exit(2) from exc
    token_env = channels.PLATFORMS[platform][1]
    console.print(
        f"Rota [cyan]{platform}:{route['remote_id']}[/] → projeto {route['project_id']} salva. "
        f"Defina {token_env} no ambiente do daemon e reinicie-o para conectar."
    )


@channel_commands.command("remove")
def remove_channel_route(
    platform: Annotated[str, typer.Argument(help="Plataforma: telegram ou discord.")],
    chat_id: Annotated[
        str,
        typer.Option("--chat-id", help="ID da rota, ou * para DMs autorizadas."),
    ],
) -> None:
    """Remove uma rota autorizada de um canal."""
    from lea import channels

    async def remove() -> bool:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await channels.remove_route(conn, platform=platform, remote_id=chat_id)

    try:
        removed = asyncio.run(remove())
    except channels.ChannelError as exc:
        err_console.print(f"[red]não foi possível remover a rota:[/] {exc}")
        raise typer.Exit(2) from exc
    if not removed:
        err_console.print(f"[red]rota não encontrada:[/] {platform}:{chat_id}")
        raise typer.Exit(1)
    console.print(
        f"Rota [cyan]{platform}:{chat_id}[/] removida. Reinicie o daemon para aplicar."
    )


@channel_commands.command("list")
def list_channel_routes() -> None:
    """Lista as rotas e o estado de conexão dos bots."""
    from lea import channels

    async def load_channels() -> list[dict[str, Any]]:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await channels.list_channels(conn)

    items = asyncio.run(load_channels())
    if not items:
        console.print("Nenhum canal configurado. Use `lea channels add` para criar uma rota.")
        return
    table = Table(title="Canais de mensagem")
    table.add_column("Plataforma")
    table.add_column("Estado")
    table.add_column("Chat → projeto")
    table.add_column("Usuários autorizados")
    for item in items:
        routes = item["routes"]
        if routes:
            for route in routes:
                table.add_row(
                    item["platform"],
                    item["status"],
                    f"{route['remote_id']} → {route['project_id']}",
                    ", ".join(route["allowed_user_ids"]),
                )
        else:
            table.add_row(item["platform"], item["status"], "—", "—")
        if item.get("last_error"):
            err_console.print(f"{item['platform']}: {item['last_error']}")
    console.print(table)


@approval_commands.command("list")
def list_pending_approvals() -> None:
    """Lista ações aguardando aprovação."""
    from lea.agent import list_approvals as fetch_approvals

    async def load_approvals() -> list[dict[str, Any]]:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await fetch_approvals(conn, "pending")

    approvals = asyncio.run(load_approvals())
    if not approvals:
        console.print("Nenhuma aprovação pendente.")
        return
    table = Table(title="Aprovações pendentes")
    table.add_column("ID", style="cyan")
    table.add_column("Sessão")
    table.add_column("Projeto")
    table.add_column("Tarefa")
    table.add_column("Comando")
    table.add_column("Motivo")
    for approval in approvals:
        command_input = approval.get("input") or {}
        argv = command_input.get("argv")
        command = (
            shlex.join(argv)
            if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
            else "(inválido)"
        )
        cwd = command_input.get("cwd")
        if cwd:
            command = f"{command} · cwd={cwd}"
        table.add_row(
            str(approval["id"]),
            str(approval["session_id"]),
            str(approval.get("project_slug") or approval.get("project_id") or "—"),
            str(approval.get("task") or "—"),
            command,
            str(approval["reason"]),
        )
    console.print(table)


@approval_commands.command("approve")
def approve_pending_action(
    approval_id: Annotated[str, typer.Argument(help="Id exibido por `lea approvals list`.")],
) -> None:
    """Aprova uma única execução de comando."""
    _decide_pending_action(approval_id, "approve")


@approval_commands.command("deny")
def deny_pending_action(
    approval_id: Annotated[str, typer.Argument(help="Id exibido por `lea approvals list`.")],
) -> None:
    """Recusa a ação e encerra a tarefa que a solicitou."""
    _decide_pending_action(approval_id, "deny")


def _decide_pending_action(approval_id: str, decision: str) -> None:
    from lea.agent import resolve_approval

    async def resolve() -> str | None:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await resolve_approval(conn, approval_id, decision, decided_by="cli")

    session_id = asyncio.run(resolve())
    if session_id is None:
        err_console.print(
            f"[red]aprovação pendente não encontrada ou sessão encerrada:[/] {approval_id}"
        )
        raise typer.Exit(1)
    decision_label = "aprovada" if decision == "approve" else "recusada"
    console.print(f"Ação {decision_label}; sessão [cyan]{session_id}[/] retomada.")


@skill_proposal_commands.command("list")
def list_skill_proposals_cli() -> None:
    """Lista propostas de skill aguardando revisão."""
    from lea import skills

    async def load_proposals() -> list[dict[str, Any]]:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await skills.list_proposals(conn, "pending")

    proposals = asyncio.run(load_proposals())
    if not proposals:
        console.print("Nenhuma proposta de skill pendente.")
        return
    table = Table(title="Propostas de skill")
    table.add_column("ID", style="cyan")
    table.add_column("Skill")
    table.add_column("Versão")
    table.add_column("Motivo")
    table.add_column("Sessão")
    for proposal in proposals:
        table.add_row(
            str(proposal["id"]),
            str(proposal["skill_name"]),
            f"{proposal['base_version']} → {proposal['proposed_version']}",
            str(proposal["reason"]),
            str(proposal.get("session_id") or "—"),
        )
    console.print(table)


@skill_proposal_commands.command("show")
def show_skill_proposal(
    proposal_id: Annotated[int, typer.Argument(help="Id exibido na lista de propostas.")],
) -> None:
    """Exibe o diff de uma proposta para revisão."""
    from lea import skills

    async def load_proposal() -> dict[str, Any] | None:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await skills.get_proposal(conn, proposal_id)

    proposal = asyncio.run(load_proposal())
    if proposal is None:
        err_console.print(f"[red]proposta desconhecida:[/] {proposal_id}")
        raise typer.Exit(1)
    console.print(
        f"[cyan]{proposal['skill_name']}[/] v{proposal['base_version']} → "
        f"v{proposal['proposed_version']} · {proposal['status']}"
    )
    console.print(f"Motivo: {proposal['reason']}")
    console.print(proposal["diff"] or "(sem diff)", markup=False, highlight=False)


@skill_proposal_commands.command("apply")
def apply_skill_proposal_cli(
    proposal_id: Annotated[int, typer.Argument(help="Id exibido na lista de propostas.")],
) -> None:
    """Aplica uma proposta pendente após revisão."""
    from lea import skills

    async def apply() -> skills.SkillDocument:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await skills.apply_proposal(conn, proposal_id)

    try:
        updated = asyncio.run(apply())
    except skills.SkillError as exc:
        err_console.print(f"[red]não foi possível aplicar:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Skill [cyan]{updated.name}[/] aplicada na versão {updated.version}.")


@skill_proposal_commands.command("reject")
def reject_skill_proposal_cli(
    proposal_id: Annotated[int, typer.Argument(help="Id exibido na lista de propostas.")],
) -> None:
    """Recusa uma proposta pendente."""
    from lea import skills

    async def reject() -> bool:
        paths.ensure_layout()
        async with db.connection() as conn:
            return await skills.reject_proposal(conn, proposal_id)

    if not asyncio.run(reject()):
        err_console.print(f"[red]proposta pendente não encontrada:[/] {proposal_id}")
        raise typer.Exit(1)
    console.print(f"Proposta [cyan]#{proposal_id}[/] recusada.")


@app.command()
def daemon(
    host: Annotated[str | None, typer.Option(help="Sobrepõe server.host.")] = None,
    port: Annotated[int | None, typer.Option(help="Sobrepõe server.port.")] = None,
) -> None:
    """Sobe o daemon (API local, WebSocket e receptor de hooks), sem janela."""
    import uvicorn

    paths.ensure_layout()
    config = load()
    bind_host = host or config.server.host
    bind_port = port or config.server.port

    if bind_host not in ("127.0.0.1", "::1", "localhost"):
        err_console.print(
            f"[yellow]aviso:[/] escutando em {bind_host}, alcançável de fora desta "
            "máquina. As ferramentas do agente executam comandos aqui."
        )

    console.print(f"[cyan]lea[/] {__version__} · http://{bind_host}:{bind_port}")
    uvicorn.run(
        "lea.server.app:app",
        host=bind_host,
        port=bind_port,
        log_level="warning",
        access_log=False,
    )


@app.command()
def hook(
    event: Annotated[str, typer.Argument(help="SessionStart, PostToolUse, PreCompact ou Stop.")],
) -> None:
    """Repassa um evento de ciclo de vida ao daemon. Chamado pelos harnesses.

    O JSON do evento vem pelo stdin, que é como Claude Code e Codex entregam
    payload de hook. A saída no stdout volta para o agente, então nada é
    impresso aqui além da resposta do daemon.

    Falhar aqui não pode derrubar a sessão do usuário: se o daemon estiver
    fora do ar, saímos em silêncio com código 0. Um hook que quebra o agente
    de código é pior que um evento perdido.
    """
    import httpx

    if event not in {"SessionStart", "PostToolUse", "PreCompact", "Stop"}:
        err_console.print(f"[red]evento desconhecido:[/] {event}")
        raise typer.Exit(2)

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"raw": payload}

    # O harness manda o id de sessão *dele*. O nosso viaja pelo ambiente que
    # o launcher injetou no processo do agente, e o hook herda daí -- é o que
    # liga um evento de tool use à sessão que a interface está mostrando.
    lea_session = os.environ.get("LEA_SESSION_ID")
    if lea_session:
        payload["harness_session_id"] = payload.get("session_id")
        payload["session_id"] = lea_session
    if os.environ.get("LEA_PROJECT"):
        payload.setdefault("project", os.environ["LEA_PROJECT"])

    config = load()
    url = f"http://{config.server.host}:{config.server.port}/hooks/{event}"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-Lea-Hook-Token": tokens.hook_token()},
            timeout=5.0,
        )
        response.raise_for_status()
    except Exception as exc:
        logging.getLogger(__name__).debug("hook %s não entregue: %s", event, exc)
        raise typer.Exit(0) from None

    console.print_json(data=response.json())


@app.command()
def mcp() -> None:
    """Servidor MCP ``lea.memory`` por stdio, para os agentes de código.

    O stdout é o canal do protocolo: nada pode ser impresso nele além das
    mensagens MCP, ou o cliente do outro lado perde o enquadramento.
    """
    from lea.mcp_server import run

    paths.ensure_layout()
    logging.getLogger().handlers.clear()
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


@app.command()
def reindex() -> None:
    """Reconstrói o índice da memória a partir do git.

    Use depois de editar a wiki à mão, de um `git pull` ou se a busca
    parecer desatualizada. É seguro: o git é a fonte da verdade, e o
    histórico de acessos é preservado.
    """
    asyncio.run(_reindex())


@app.command("run")
def run_agent(
    project_id: Annotated[int, typer.Option("--project", help="Id do projeto com autonomia habilitada.")],
    task: Annotated[str, typer.Option("--task", help="Tarefa para o lea-agent.")],
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="Backend: codex, claude-code ou anthropic-api.", metavar="BACKEND"),
    ] = None,
    skill: Annotated[str | None, typer.Option("--skill", help="Skill pelo nome.")] = None,
) -> None:
    """Executa uma tarefa via Codex CLI, Claude Code ou Anthropic API."""
    result = asyncio.run(_run_agent(project_id, task, skill, backend))
    console.print(f"[cyan]{result['session_id']}[/] · {result['backend']} · {result['status']}")
    console.print(result["summary"])
    if result["tokens_in"] or result["tokens_out"]:
        console.print(f"tokens: {result['tokens_in']} entrada · {result['tokens_out']} saída")
    if result.get("cost_brl"):
        console.print(f"custo estimado: US$ {result['cost_usd']:.4f} · R$ {result['cost_brl']:.2f}")
    if result["status"] != "ended":
        raise typer.Exit(1)


async def _run_agent(
    project_id: int,
    task: str,
    skill_name: str | None,
    backend: str | None = None,
) -> dict[str, Any]:
    from lea.agent import AgentService
    from lea.events import EventBus
    from lea.memory import Memory
    from lea import skills

    paths.ensure_layout()
    config = load()
    async with db.connection() as conn:
        memory = Memory(
            conn,
            config.memory.wiki_path,
            half_lives=config.memory.half_life_days.model_dump(),
        )
        await memory.setup()
        await skills.sync_from_disk(conn)
        skill_id = None
        if skill_name:
            async with conn.execute("SELECT id FROM skills WHERE name = ?", (skill_name,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise typer.BadParameter(f"skill desconhecida: {skill_name}")
            skill_id = int(row["id"])
        agent = AgentService(conn, memory, config, EventBus())
        try:
            session, running = await agent.start(
                project_id=project_id,
                task=task,
                skill_id=skill_id,
                backend=backend,
            )
            console.print(f"sessão [cyan]{session['id']}[/] iniciada")
            return await running
        except Exception as exc:
            from lea.agent import AgentError

            if isinstance(exc, AgentError):
                raise typer.BadParameter(str(exc)) from exc
            raise
        finally:
            await agent.shutdown()


async def _reindex() -> None:
    from lea.memory import Memory

    paths.ensure_layout()
    config = load()
    async with db.connection() as conn:
        memory = Memory(conn, config.memory.wiki_path)
        result = await memory.reindex()

    console.print(f"[green]{result['indexed']}[/] página(s) indexada(s).")
    problems = result["problems"]
    if isinstance(problems, list) and problems:
        err_console.print(f"\n[yellow]{len(problems)} página(s) com problema:[/]")
        for item in problems:
            err_console.print(f"  {item}")


@app.command(name="harnesses")
def harnesses_(
    install: Annotated[
        str | None,
        typer.Option("--install", help="Instala no harness indicado (ex.: claude-code)."),
    ] = None,
) -> None:
    """Mostra a matriz de integração dos agentes de código, ou instala num deles.

    Instalar **altera a configuração de outra ferramenta** na sua máquina.
    Por isso é um comando explícito e nunca acontece sozinho: fazemos backup
    antes, mesclamos em vez de sobrescrever, e listamos o que mudou.
    """
    asyncio.run(_harnesses(install))


async def _harnesses(install: str | None) -> None:
    from lea import harnesses as hz

    if install:
        harness = hz.get(install)
        if harness is None:
            conhecidos = ", ".join(h.id for h in hz.all_harnesses())
            err_console.print(f"[red]harness desconhecido:[/] {install} (use: {conhecidos})")
            raise typer.Exit(2)

        console.print(f"instalando em [cyan]{harness.name}[/]…")
        report = await harness.install(hz.hook_command(), hz.mcp_command())

        for item in report.changed:
            console.print(f"  [green]+[/] {item}")
        for item in report.unchanged:
            console.print(f"  [dim]=[/] {item} (já estava)")
        for item in report.backups:
            console.print(f"  [dim]backup:[/] {item}")
        for item in report.manual_steps:
            console.print(f"  [yellow]ação sua:[/] {item}")
        for item in report.problems:
            err_console.print(f"  [red]![/] {item}")

        raise typer.Exit(0 if report.ok else 1)

    hook_command = hz.hook_command()
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    for column in ("agente", "versão", "hooks", "mcp", "observação"):
        table.add_column(column, overflow="fold")

    for harness in hz.all_harnesses():
        state = await harness.status(hook_command)
        if not state.installed:
            table.add_row(f"{state.code} {state.name}", "—", "—", "—", "não instalado")
            continue
        table.add_row(
            f"{state.code} {state.name}",
            state.version,
            hz.Health(state.hooks).glyph,
            hz.Health(state.mcp).glyph,
            "; ".join(state.notes) or "",
        )

    console.print(table)
    console.print("\n[dim]● ok  ◐ incompleto  ○ ausente[/]")
    console.print("[dim]instalar:[/] lea harnesses --install claude-code")


@app.command()
def doctor() -> None:
    """Confere instalação, permissões e banco."""
    asyncio.run(_doctor())


async def _doctor() -> None:
    root = paths.ensure_layout()
    config = load()

    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("verificação")
    table.add_column("estado")
    table.add_column("detalhe", overflow="fold")

    ok, warn, bad = "[green]ok[/]", "[yellow]atenção[/]", "[red]falha[/]"
    problems = 0

    table.add_row("versão", ok, f"lea {__version__} · python {sys.version.split()[0]}")
    table.add_row("diretório", ok, str(root))
    table.add_row("config", ok, str(paths.config_file()))

    # Segredos precisam estar em 0600: eles autorizam quem fala com o daemon.
    # Chamar o getter cria o token se ainda não existir, então o doctor também
    # serve para inicializar uma instalação nova.
    for label, path, ensure in (
        ("token da ui", paths.ui_token_file(), tokens.ui_token),
        ("token de hook", paths.hook_token_file(), tokens.hook_token),
    ):
        ensure()
        if paths.is_secret_tight(path):
            table.add_row(label, ok, "0600")
        else:
            problems += 1
            table.add_row(label, bad, f"permissão frouxa em {path}")

    if config.server.is_exposed:
        problems += 1
        table.add_row(
            "rede", warn, f"escutando em {config.server.host}, alcançável de fora da máquina"
        )
    else:
        table.add_row("rede", ok, f"{config.server.host}:{config.server.port}")

    try:
        async with db.connection() as conn:
            version = await db.current_version(conn)
            table.add_row(
                "banco",
                ok if version == schema.LATEST_VERSION else warn,
                f"esquema v{version} de v{schema.LATEST_VERSION} · {paths.database_file()}",
            )
            issues = await db.integrity_problems(conn)
            if issues:
                problems += 1
                table.add_row("integridade", bad, "; ".join(issues[:3]))
            else:
                table.add_row("integridade", ok, "sem problemas")
            counters = await status.counters(conn)
            table.add_row(
                "conteúdo",
                ok,
                f"{counters['projects']} projetos · {counters['pages']} páginas · "
                f"{counters['sessions_active']} sessões ativas",
            )
    except Exception as exc:
        problems += 1
        table.add_row("banco", bad, str(exc))

    status.prime()
    stats = status.system_stats()
    table.add_row(
        "máquina",
        ok,
        f"mem {stats.memory_used_gb:.1f}/{stats.memory_total_gb:.1f} GB "
        f"({stats.memory_percent:.0f}%)",
    )

    console.print(table)
    if problems:
        err_console.print(f"\n[red]{problems} problema(s).[/]")
        raise typer.Exit(1)
    console.print("\n[green]tudo certo.[/]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
