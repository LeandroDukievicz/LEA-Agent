"""Processo filho com terminal de verdade (PTY).

Os agentes de código são programas interativos de terminal: eles desenham
com códigos ANSI, perguntam coisas e mudam de comportamento quando a saída
não é um TTY (desligam cor, viram modo "pipe", às vezes recusam o modo
interativo). Rodar com ``subprocess.PIPE`` daria um console morto.

Portanto: alocamos um pseudo-terminal, o filho enxerga um TTY, e o LEA lê o
outro lado e transmite os bytes crus por WebSocket para o terminal da
interface desenhar.

Limite conhecido: isto é POSIX (``os.openpty``). Windows precisa de
``pywinpty``, que entra na Fase 7 junto com o empacotamento -- até lá o
launcher recusa com mensagem clara em vez de falhar de um jeito obscuro.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import signal
import struct
import termios
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Quanto ler do PTY por vez.
READ_CHUNK = 65536

#: Tamanho inicial do terminal, até a interface informar o dela.
DEFAULT_COLS = 120
DEFAULT_ROWS = 32

#: Quanto esperar entre SIGTERM e SIGKILL ao parar uma sessão.
TERM_GRACE_SECONDS = 5.0


class PtyUnsupported(RuntimeError):
    """Esta plataforma não tem PTY POSIX."""


def supported() -> bool:
    return hasattr(os, "openpty")


@dataclass
class Spawned:
    """Um processo rodando sob PTY."""

    process: asyncio.subprocess.Process
    master_fd: int
    command: list[str]
    cwd: str

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.returncode is None


async def spawn(
    command: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None = None,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
) -> Spawned:
    """Inicia ``command`` sob um PTY novo.

    O filho vira líder da própria sessão (``start_new_session``), o que cria
    um grupo de processos separado. Isso importa na hora de parar: sinal
    mandado ao grupo alcança também os netos -- o agente que abriu um
    ``npm test`` não deixa órfão rodando quando a sessão é encerrada.
    """
    if not supported():
        raise PtyUnsupported(
            "terminal interativo exige PTY POSIX; no Windows isso chega na Fase 7"
        )

    master_fd, slave_fd = os.openpty()
    _set_size(master_fd, cols, rows)

    environment = {**os.environ, **(env or {})}
    # Sem isso o agente pode achar que está num terminal burro e desligar a
    # cor -- justamente o que o console da interface quer mostrar.
    environment.setdefault("TERM", "xterm-256color")

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=environment,
            start_new_session=True,
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        # O filho tem a cópia dele; manter a nossa aberta impediria o EOF
        # quando ele morresse, e a leitura ficaria pendurada para sempre.
        os.close(slave_fd)

    os.set_blocking(master_fd, False)
    return Spawned(process=process, master_fd=master_fd, command=list(command), cwd=str(cwd))


async def read_stream(spawned: Spawned):
    """Gera os bytes que o processo escreve, até ele terminar.

    Bytes crus, não texto: a saída carrega sequências ANSI e pode partir um
    caractere UTF-8 no meio entre duas leituras. Decodificar aqui produziria
    lixo; quem desenha o terminal junta e decodifica.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(spawned.master_fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError as exc:
            # EIO é como o PTY sinaliza que o outro lado fechou: fim normal,
            # não erro.
            if exc.errno not in (errno.EIO, errno.EBADF):
                logger.warning("erro lendo pty: %s", exc)
            queue.put_nowait(None)
            return
        queue.put_nowait(data or None)

    loop.add_reader(spawned.master_fd, on_readable)
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
    finally:
        with_suppress = getattr(loop, "remove_reader", None)
        if with_suppress:
            try:
                loop.remove_reader(spawned.master_fd)
            except Exception:  # pragma: no cover - loop já fechando
                pass


def write(spawned: Spawned, data: str | bytes) -> None:
    """Manda entrada para o processo, como se digitada no terminal."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    try:
        os.write(spawned.master_fd, payload)
    except OSError as exc:
        raise RuntimeError(f"não consegui escrever no terminal da sessão: {exc}") from exc


def resize(spawned: Spawned, cols: int, rows: int) -> None:
    """Informa o novo tamanho do terminal ao processo.

    Sem isso o agente continua quebrando linha na largura antiga quando o
    usuário redimensiona a janela.
    """
    _set_size(spawned.master_fd, cols, rows)
    _signal(spawned, signal.SIGWINCH)


def pause(spawned: Spawned) -> None:
    """Congela o processo e os filhos dele (SIGSTOP)."""
    _signal(spawned, signal.SIGSTOP)


def resume(spawned: Spawned) -> None:
    _signal(spawned, signal.SIGCONT)


async def stop(spawned: Spawned, grace: float = TERM_GRACE_SECONDS) -> int | None:
    """Encerra com educação e depois à força. Devolve o código de saída.

    Um processo parado (SIGSTOP) não reage a SIGTERM -- ele nem é escalado.
    Por isso mandamos SIGCONT antes: sem esse passo, parar uma sessão
    pausada ficaria pendurado até o SIGKILL.
    """
    if not spawned.running:
        return spawned.process.returncode

    _signal(spawned, signal.SIGCONT)
    _signal(spawned, signal.SIGTERM)
    try:
        return await asyncio.wait_for(spawned.process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        logger.warning("pid %s ignorou SIGTERM; mandando SIGKILL", spawned.pid)
        _signal(spawned, signal.SIGKILL)
        try:
            return await asyncio.wait_for(spawned.process.wait(), timeout=grace)
        except asyncio.TimeoutError:  # pragma: no cover - processo zumbi
            return None
    finally:
        close(spawned)


def close(spawned: Spawned) -> None:
    """Fecha o nosso lado do PTY. Idempotente."""
    try:
        os.close(spawned.master_fd)
    except OSError:
        pass


def _signal(spawned: Spawned, sig: int) -> None:
    """Manda o sinal ao grupo do processo, pegando também os netos."""
    if not spawned.running:
        return
    try:
        os.killpg(os.getpgid(spawned.pid), sig)
    except ProcessLookupError:
        pass
    except OSError as exc:
        logger.warning("não consegui sinalizar %s: %s", spawned.pid, exc)


def _set_size(fd: int, cols: int, rows: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError as exc:  # pragma: no cover - fd já fechado
        logger.debug("TIOCSWINSZ falhou: %s", exc)
