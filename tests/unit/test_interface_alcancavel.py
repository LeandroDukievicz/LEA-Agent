"""A interface precisa ter como chegar ligada ao daemon.

O dashboard é servido em `/app/`, e abrir esse endereço à mão entrega uma página
**sem token**: o `lea-presence.js` não acha configuração, não abre o websocket e
o chat responde localmente. Como o protótipo renderiza inteiro, a interface
*parecia* funcionar sem falar com o daemon em momento nenhum -- não havia erro à
vista, só ausência de ligação. A janela da LEA sempre teve esse caminho, porque
`lea avatar` monta a URL com o token; o dashboard não tinha equivalente.
"""

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def test_as_duas_paginas_tem_url_com_token(lea_home, monkeypatch):
    from lea import desktop

    monkeypatch.setattr(desktop.tokens, "ui_token", lambda: "TOKEN")
    assert desktop.dashboard_url().endswith("/app/?token=TOKEN")
    assert desktop.avatar_url().endswith("/app/avatar.html?token=TOKEN")


def test_sem_token_a_url_sai_sem_query(lea_home, monkeypatch):
    """Melhor endereço honesto que `?token=` vazio, que a página guardaria."""
    from lea import desktop

    monkeypatch.setattr(desktop.tokens, "ui_token", lambda: "")
    assert desktop.dashboard_url().endswith("/app/")
    assert "token" not in desktop.dashboard_url()


def test_existe_comando_para_abrir_o_dashboard():
    from lea.cli import app

    nomes = {
        comando.name or getattr(comando.callback, "__name__", "")
        for comando in app.registered_commands
    }
    assert "dashboard" in nomes


def test_as_paginas_declaram_icone():
    """Sem `rel=icon` o navegador pede `/favicon.ico`, que o daemon não serve --
    e cada carregamento deixava um 404 no console. Um data URI resolve sem
    requisição a mais e sem binário no repositório."""
    for pagina in ("index.html", "avatar.html"):
        html = (APP / pagina).read_text(encoding="utf-8")
        assert 'rel="icon"' in html, pagina
        assert "data:image/svg+xml" in html, pagina


def test_o_indicador_de_ligacao_nao_espera_um_segundo():
    """A janela abria anunciando o texto do HTML por um segundo inteiro.

    O indicador existe para não confundir desconectada com ociosa; atualizar só
    no primeiro `setInterval` fazia dele a própria fonte da confusão. E a
    atualização imediata precisa esperar o `defer` do `lea-presence.js`, ou
    leria um `window.LeaPresence` que ainda não existe.
    """
    html = (APP / "avatar.html").read_text(encoding="utf-8")
    assert "DOMContentLoaded" in html
    assert "setInterval(mostrar" in html
    assert "◐ ligando</span>" in html
