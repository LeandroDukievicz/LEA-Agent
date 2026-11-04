"""A janela própria da LEA, fora do navegador.

Um webview nativo apontado para ``/app/avatar.html``, servido pelo próprio
daemon. A janela não fala com o dashboard: os dois assinam o mesmo websocket de
eventos, e é de lá que sai a expressão dela. O daemon é o eixo.

Por que pywebview e não Electron ou Tauri: o ``pyproject.toml`` diz, com todas
as letras, que menos dependência no núcleo é menos raio de explosão no próximo
ataque de cadeia de suprimentos. Electron traz um Chromium inteiro e a cadeia
do Node; Tauri traz a do Rust. O pywebview usa o webview que já existe no
sistema -- WebKitGTK no Linux, WebView2 no Windows, WKWebView no macOS -- e é
Python, como o resto. Fica em ``[project.optional-dependencies]``, do lado do
discord: o daemon continua subindo sem janela num homelab.
"""

from __future__ import annotations

import logging
import webbrowser
from urllib.parse import urlencode

from lea.config import load
from lea.security import tokens

logger = logging.getLogger(__name__)

TITLE = "LEA"
#: Retrato em pé: a arte é mais alta que larga, e a onda mora embaixo.
SIZE = (520, 760)
MIN_SIZE = (360, 520)

INSTALL_HINT = (
    "A janela da LEA precisa do extra `desktop`:\n"
    "    uv sync --extra desktop\n"
    "No Linux ela usa o WebKitGTK do sistema pelo PyGObject, que vem da "
    "distribuição e não do pip (no Ubuntu: gir1.2-webkit2-4.1)."
)


def avatar_url(token: str | None = None) -> str:
    """URL da página da LEA no daemon.

    O token viaja na query e some da URL no primeiro carregamento: a página o
    guarda e reescreve o endereço. Mesma decisão que o websocket do daemon já
    toma, e pelo mesmo motivo -- a URL não sai desta máquina.
    """
    config = load()
    base = f"http://{config.server.host}:{config.server.port}/app/avatar.html"
    token = token if token is not None else tokens.ui_token()
    return f"{base}?{urlencode({'token': token})}" if token else base


def open_in_browser() -> str:
    """Abre a página da LEA no navegador padrão. Serve de saída quando não há
    webview nativo disponível -- e para depurar a janela."""
    url = avatar_url()
    webbrowser.open(url)
    return url


def run(*, fullscreen: bool = False, on_top: bool = False) -> None:
    """Abre a janela nativa e bloqueia até fechá-la."""
    try:
        import webview
    except ModuleNotFoundError as exc:  # pragma: no cover - depende do extra
        raise RuntimeError(INSTALL_HINT) from exc

    window = webview.create_window(
        TITLE,
        avatar_url(),
        width=SIZE[0],
        height=SIZE[1],
        min_size=MIN_SIZE,
        background_color="#000000",
        on_top=on_top,
        fullscreen=fullscreen,
        frameless=False,
    )
    # `private_mode` vem ligado por padrão e desliga o localStorage. A página
    # guarda ali o token do daemon, então com ele ligado o websocket nunca
    # conecta: a LEA abre, renderiza e fica muda, sem erro nenhum à vista.
    webview.start(private_mode=False)
    return window
