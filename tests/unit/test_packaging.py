"""O que o wheel precisa levar para o LEA funcionar instalado."""

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]

#: Sem estes, `pip install lea-agent` dá um daemon que serve /app de um
#: diretório inexistente: o dashboard some e `lea avatar` abre em branco.
ESSENCIAIS = (
    "lea/app/index.html",
    "lea/app/avatar.html",
    "lea/app/assets/lea-avatar.js",
    "lea/app/assets/lea-presence.js",
    "lea/app/assets/lea-retrato.png",
    "lea/app/assets/vendor/three.min.js",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    destino = tmp_path_factory.mktemp("dist")
    if shutil.which("uv") is None:
        pytest.skip("uv não está no PATH")
    build = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(destino)],
        cwd=RAIZ, capture_output=True, text=True, timeout=180,
    )
    if build.returncode != 0:
        pytest.skip(f"não consegui construir o wheel: {build.stderr[-200:]}")
    produzidos = sorted(destino.glob("*.whl"))
    if not produzidos:
        pytest.skip("nenhum wheel produzido")
    return produzidos[-1]


def test_wheel_leva_o_prototipo(wheel: Path):
    nomes = set(zipfile.ZipFile(wheel).namelist())
    faltando = [alvo for alvo in ESSENCIAIS if alvo not in nomes]
    assert not faltando, f"fora do wheel: {faltando}"


def test_prototipo_e_encontrado_dentro_do_pacote():
    """O daemon procura `lea/app` antes da raiz do repositório.

    Instalado, só o primeiro existe; em checkout, só o segundo. Antes isto
    olhava apenas para a raiz, que no site-packages aponta para lugar nenhum.
    """
    from lea.server.app import _prototype_dir

    encontrado = _prototype_dir()
    assert encontrado is not None
    assert (encontrado / "avatar.html").is_file()


def test_pyproject_mapeia_o_app_para_dentro_do_pacote():
    """Checagem rápida que sempre roda, mesmo sem poder construir o wheel.

    O que mais provavelmente se perde numa refatoração é esta linha de
    configuração, e o sintoma dela é tardio: só aparece em quem instalou.
    """
    config = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    incluidos = (
        config["tool"]["hatch"]["build"]["targets"]["wheel"]
        .get("force-include", {})
    )
    assert incluidos.get("app") == "lea/app", incluidos
