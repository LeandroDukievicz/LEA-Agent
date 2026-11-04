"""A janela própria da LEA: URL, token e a saída quando não há webview."""

import pytest

from lea import desktop


def test_url_aponta_para_a_pagina_da_lea_no_daemon(lea_home, monkeypatch):
    url = desktop.avatar_url(token="tok3n")
    assert "/app/avatar.html" in url
    assert "token=tok3n" in url
    # A janela é cliente do daemon como qualquer outra interface.
    assert url.startswith("http://127.0.0.1:")


def test_sem_token_a_url_nao_carrega_query_vazia(lea_home):
    assert "?" not in desktop.avatar_url(token="")


def test_sem_pywebview_a_mensagem_ensina_a_instalar(monkeypatch):
    """Sem o extra, a falha tem que dizer qual extra instalar."""
    import builtins

    real_import = builtins.__import__

    def sem_webview(name, *args, **kwargs):
        if name == "webview":
            raise ModuleNotFoundError("No module named 'webview'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", sem_webview)
    with pytest.raises(RuntimeError) as erro:
        desktop.run()
    assert "--extra desktop" in str(erro.value)


def test_sem_backend_grafico_a_mensagem_cobre_a_pegadinha_do_linux():
    """O pywebview instala sem backend: no Linux ele depende do PyGObject da
    distribuição, que não existe no PyPI e que um venv comum não enxerga.

    O traceback dele não diz nada disso, então a tradução é o que separa
    "resolvo em um minuto" de "por que a janela não abre".
    """
    assert "gir1.2-webkit2-4.1" in desktop.NO_GUI_BACKEND
    assert "PYTHONPATH" in desktop.NO_GUI_BACKEND
    assert "--browser" in desktop.NO_GUI_BACKEND
