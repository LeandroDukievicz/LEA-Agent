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

NO_PYWEBVIEW = (
    "A janela da LEA precisa do extra 'desktop':\n"
    "    uv sync --extra dev --extra desktop"
)

#: No Linux o pywebview não traz o backend gráfico: ele usa o PyGObject da
#: distribuição, que não existe no PyPI. Um venv comum não enxerga pacote de
#: sistema, então instalar o extra não basta -- e o erro que o pywebview levanta
#: nesse caso não diz nada disso.
NO_GUI_BACKEND = (
    "O pywebview está instalado, mas não achou backend gráfico.\n"
    "No Linux ele usa o WebKitGTK pelo PyGObject, que vem da distribuição e não "
    "do pip:\n"
    "    sudo apt install gir1.2-webkit2-4.1 python3-gi\n"
    "Um venv comum não enxerga pacote de sistema. Aponte o PyGObject para ele:\n"
    "    PYTHONPATH=/usr/lib/python3/dist-packages lea avatar\n"
    "ou recrie o ambiente com 'uv venv --system-site-packages'.\n"
    "Saída imediata, sem nada disso: 'lea avatar --browser'."
)


def page_url(page: str, token: str | None = None) -> str:
    """URL de uma página da interface no daemon, com o token na query.

    O token viaja na query e some da URL no primeiro carregamento: a página o
    guarda e reescreve o endereço. Mesma decisão que o websocket do daemon já
    toma, e pelo mesmo motivo -- a URL não sai desta máquina.
    """
    config = load()
    base = f"http://{config.server.host}:{config.server.port}/app/{page}"
    token = token if token is not None else tokens.ui_token()
    return f"{base}?{urlencode({'token': token})}" if token else base


def avatar_url(token: str | None = None) -> str:
    """URL da janela da LEA."""
    return page_url("avatar.html", token)


def dashboard_url(token: str | None = None) -> str:
    """URL do dashboard.

    Sem isto o dashboard era inalcançável como cliente do daemon. Ele é servido
    em `/app/`, e abrir esse endereço à mão entrega uma página SEM token: o
    `lea-presence.js` não acha configuração, não abre o websocket e o chat
    responde localmente. A interface parecia funcionar -- é um protótipo
    completo, tudo renderiza -- e não falava com o daemon em nenhum momento.

    A janela da LEA tinha o caminho desde o começo, porque `lea avatar` monta a
    URL com o token. O dashboard não tinha equivalente nenhum.
    """
    return page_url("", token)


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
        raise RuntimeError(NO_PYWEBVIEW) from exc

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
    try:
        webview.start(private_mode=False)
    except Exception as exc:  # pragma: no cover - depende do ambiente gráfico
        # O pywebview levanta WebViewException genérica quando não acha GTK nem
        # QT. Traduzir importa: o traceback cru não diz que falta um pacote da
        # distribuição nem que o venv não o enxerga.
        if "QT or GTK" in str(exc):
            raise RuntimeError(NO_GUI_BACKEND) from exc
        raise
    return window
