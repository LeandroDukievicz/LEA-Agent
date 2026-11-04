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
    """Sem o extra, a falha tem que dizer o que fazer -- inclusive o pacote da
    distribuição, que é a parte que ninguém adivinha no Linux."""
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
    assert "gir1.2-webkit2-4.1" in str(erro.value)
