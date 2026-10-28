"""PTY, worktrees e ciclo de vida de sessão.

Os testes rodam processos de verdade (``cat``, ``sh``) sob PTY real, porque
é justamente a interação com o terminal que não dá para simular com fé.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from lea.sessions import pty, worktree

pytestmark = pytest.mark.skipif(not pty.supported(), reason="PTY POSIX indisponível")


async def _collect(spawned: pty.Spawned, until: str, timeout: float = 5.0) -> str:
    """Acumula a saída até encontrar ``until``."""
    buffer = bytearray()

    async def read() -> None:
        async for chunk in pty.read_stream(spawned):
            buffer.extend(chunk)
            if until.encode() in bytes(buffer):
                return

    try:
        await asyncio.wait_for(read(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return bytes(buffer).decode("utf-8", "replace")


# ── PTY ──────────────────────────────────────────────────────────────────


async def test_processo_ve_um_tty_de_verdade(tmp_path: Path) -> None:
    """Sem TTY, os agentes de código desligam cor e modo interativo."""
    spawned = await pty.spawn(
        ["python3", "-c", "import sys; print('TTY', sys.stdout.isatty())"],
        cwd=tmp_path,
    )
    saida = await _collect(spawned, "TTY")
    await spawned.process.wait()
    pty.close(spawned)

    assert "TTY True" in saida


async def test_variaveis_de_ambiente_chegam_ao_filho(tmp_path: Path) -> None:
    """É assim que o hook descobre a qual sessão do LEA ele pertence."""
    spawned = await pty.spawn(
        ["python3", "-c", "import os; print('SID', os.environ.get('LEA_SESSION_ID'))"],
        cwd=tmp_path,
        env={"LEA_SESSION_ID": "s-7f3a"},
    )
    saida = await _collect(spawned, "SID")
    await spawned.process.wait()
    pty.close(spawned)

    assert "SID s-7f3a" in saida


async def test_term_tem_cor_por_padrao(tmp_path: Path) -> None:
    spawned = await pty.spawn(
        ["python3", "-c", "import os; print('TERM', os.environ['TERM'])"], cwd=tmp_path
    )
    saida = await _collect(spawned, "TERM")
    await spawned.process.wait()
    pty.close(spawned)

    assert "xterm-256color" in saida


async def test_entrada_chega_ao_processo(tmp_path: Path) -> None:
    spawned = await pty.spawn(["cat"], cwd=tmp_path)
    pty.write(spawned, "oi console\n")
    saida = await _collect(spawned, "oi console")
    await pty.stop(spawned)

    assert "oi console" in saida


async def test_cwd_e_respeitado(tmp_path: Path) -> None:
    alvo = tmp_path / "sub"
    alvo.mkdir()
    spawned = await pty.spawn(["pwd"], cwd=alvo)
    saida = await _collect(spawned, "sub")
    await spawned.process.wait()
    pty.close(spawned)

    assert str(alvo) in saida


async def test_stop_encerra_processo_teimoso(tmp_path: Path) -> None:
    """Um agente que ignora SIGTERM ainda precisa morrer."""
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    spawned = await pty.spawn(["python3", "-c", script], cwd=tmp_path)
    await asyncio.sleep(0.3)

    code = await pty.stop(spawned, grace=0.5)
    assert code is not None
    assert not spawned.running


async def test_stop_funciona_em_sessao_pausada(tmp_path: Path) -> None:
    """Processo parado não reage a SIGTERM; precisa de SIGCONT antes."""
    spawned = await pty.spawn(["sleep", "60"], cwd=tmp_path)
    await asyncio.sleep(0.2)
    pty.pause(spawned)

    code = await asyncio.wait_for(pty.stop(spawned, grace=1.0), timeout=5.0)
    assert code is not None


async def test_pausar_congela_a_saida(tmp_path: Path) -> None:
    script = "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.05)\n"
    spawned = await pty.spawn(["python3", "-c", script], cwd=tmp_path)
    await _collect(spawned, "tick", timeout=3.0)

    pty.pause(spawned)
    await asyncio.sleep(0.2)
    # Drena o que ficou no buffer do PTY.
    try:
        os.read(spawned.master_fd, 65536)
    except BlockingIOError:
        pass
    await asyncio.sleep(0.3)

    try:
        parado = os.read(spawned.master_fd, 65536)
    except BlockingIOError:
        parado = b""

    pty.resume(spawned)
    await pty.stop(spawned, grace=1.0)
    assert parado == b"", "processo pausado não deveria produzir saída"


async def test_grupo_de_processo_leva_os_netos(tmp_path: Path) -> None:
    """O agente abre subprocessos; parar a sessão não pode deixar órfão."""
    script = "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"
    spawned = await pty.spawn(["python3", "-c", script], cwd=tmp_path)
    await asyncio.sleep(0.5)

    grupo = os.getpgid(spawned.pid)
    await pty.stop(spawned, grace=1.0)
    await asyncio.sleep(0.3)

    restantes = subprocess.run(
        ["pgrep", "-g", str(grupo)], capture_output=True, text=True
    ).stdout.strip()
    assert restantes == "", f"sobraram processos no grupo: {restantes}"


async def test_fluxo_termina_quando_o_processo_morre(tmp_path: Path) -> None:
    spawned = await pty.spawn(["echo", "fim"], cwd=tmp_path)
    pedacos = [chunk async for chunk in pty.read_stream(spawned)]
    pty.close(spawned)

    assert b"fim" in b"".join(pedacos)


async def test_resize_nao_quebra(tmp_path: Path) -> None:
    spawned = await pty.spawn(["cat"], cwd=tmp_path)
    pty.resize(spawned, 200, 50)
    await pty.stop(spawned, grace=1.0)


# ── worktrees ────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "projeto"
    root.mkdir()
    for args in (
        ["init", "--initial-branch=main"],
        ["config", "user.email", "teste@lea.local"],
        ["config", "user.name", "teste"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "README.md").write_text("# projeto\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "inicial"], check=True, capture_output=True
    )
    return root


async def test_cria_worktree_com_branch_novo(repo: Path, tmp_path: Path) -> None:
    alvo = tmp_path / "wt" / "s-1234"
    await worktree.create(repo, alvo, "lea/s-1234")

    assert (alvo / "README.md").exists()
    branches = await worktree.listing(repo)
    assert any("s-1234" in e.get("worktree", "") for e in branches)


async def test_reaproveita_branch_existente(repo: Path, tmp_path: Path) -> None:
    """Retomar um handoff reusa o branch que a sessão anterior criou."""
    subprocess.run(
        ["git", "-C", str(repo), "branch", "lea/antigo"], check=True, capture_output=True
    )
    alvo = tmp_path / "wt" / "s-2222"
    await worktree.create(repo, alvo, "lea/antigo")
    assert alvo.exists()


async def test_recusa_destino_ocupado(repo: Path, tmp_path: Path) -> None:
    alvo = tmp_path / "wt" / "s-3333"
    alvo.mkdir(parents=True)
    with pytest.raises(worktree.WorktreeError, match="já existe"):
        await worktree.create(repo, alvo, "lea/x")


async def test_erro_de_git_vira_mensagem_util(tmp_path: Path) -> None:
    nao_e_repo = tmp_path / "vazio"
    nao_e_repo.mkdir()
    with pytest.raises(worktree.WorktreeError, match="worktree add falhou"):
        await worktree.create(nao_e_repo, tmp_path / "wt", "x")
