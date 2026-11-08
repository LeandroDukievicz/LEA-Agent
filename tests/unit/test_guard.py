"""A coleira do publicador: um pre-push que barra push vindo da frota."""

import subprocess
from pathlib import Path

import pytest

from lea import guard
from lea.sessions.launcher import ENV_SESSION


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**base, **(env or {})},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origem, trabalho = tmp_path / "remoto", tmp_path / "local"
    origem.mkdir(); trabalho.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(origem)], check=True)
    _git(trabalho, "init", "-q")
    _git(trabalho, "remote", "add", "origin", str(origem))
    (trabalho / "a.txt").write_text("oi\n")
    _git(trabalho, "add", "-A")
    _git(trabalho, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "inicial")
    return trabalho


def test_hook_usa_a_variavel_que_o_launcher_realmente_exporta():
    """O nome é contrato entre quem abre a sessão e quem a reconhece.

    Escrito à mão, divergiu: o launcher exporta LEA_SESSION_ID e o hook lia
    LEA_SESSION, então ele nunca disparava -- uma coleira que parecia instalada
    e não prendia nada.
    """
    assert ENV_SESSION in guard.HOOK_BODY
    assert "${LEA_SESSION:-}" not in guard.HOOK_BODY


def test_instala_e_marca_como_nosso(repo: Path):
    report = guard.install(repo)
    assert report.ok and report.action == "instalado"
    assert guard.status(repo) == "instalada"
    assert report.path.stat().st_mode & 0o111            # precisa ser executável


def test_reinstalar_nao_duplica_nem_cria_backup_de_si(repo: Path):
    guard.install(repo)
    de_novo = guard.install(repo)
    assert de_novo.action == "atualizado"
    assert de_novo.backup is None


def test_hook_de_outra_ferramenta_e_preservado(repo: Path):
    """Nunca sobrescrever sem guardar: a config é do usuário, não nossa."""
    alheio = guard.hook_path(repo)
    alheio.parent.mkdir(parents=True, exist_ok=True)
    alheio.write_text("#!/bin/sh\necho outro\n")

    report = guard.install(repo)
    assert report.backup and report.backup.is_file()
    assert "echo outro" in report.backup.read_text()

    guard.remove(repo)                                   # devolve o que estava lá
    assert "echo outro" in alheio.read_text()


def test_remover_nao_toca_em_hook_que_nao_e_nosso(repo: Path):
    alheio = guard.hook_path(repo)
    alheio.parent.mkdir(parents=True, exist_ok=True)
    alheio.write_text("#!/bin/sh\necho alheio\n")
    report = guard.remove(repo)
    assert not report.ok
    assert "não é do LEA" in (report.problem or "")
    assert alheio.is_file()


def test_push_da_frota_e_barrado_e_o_seu_passa(repo: Path):
    """O teste que importa: git de verdade, push de verdade."""
    guard.install(repo)
    ramo = _git(repo, "branch", "--show-current").stdout.strip()

    seu = _git(repo, "push", "origin", ramo)
    assert seu.returncode == 0, seu.stderr

    (repo / "a.txt").write_text("mais\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "segundo")

    da_frota = _git(repo, "push", "origin", ramo, env={ENV_SESSION: "s-teste"})
    assert da_frota.returncode != 0
    assert "push bloqueado" in da_frota.stderr
