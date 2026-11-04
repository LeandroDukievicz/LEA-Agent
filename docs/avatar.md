# Avatar LEA

O elemento `<lea-avatar>` aplica `app/assets/lea-retrato.png` a uma malha subdividida no Three.js. O arquivo é a imagem transparente fornecida para o avatar, preservada em alta resolução sobre o fundo preto. A geometria forma um relevo suave, com profundidade no rosto e no implante; o movimento da cabeça responde ao ponteiro e aos estados do agente.

O relevo é uma representação animada a partir de uma única imagem, não uma cabeça 3D integral com visão de todos os ângulos. Os olhos e a boca são deformações aproximadas da imagem original.

## Onde ela aparece

A LEA tem **janela própria** e saiu do dashboard:

```bash
lea avatar                 # janela nativa
lea avatar --on-top        # sempre sobre as outras
lea avatar --browser       # mesma página, no navegador (depuração)
```

A janela é um webview nativo apontado para `/app/avatar.html`, servido pelo
próprio daemon. Ela **não conversa com o dashboard**: as duas assinam o mesmo
websocket de eventos (`/ws`), e é de lá que vem a expressão. Fechar uma não
afeta a outra, e abrir dois dashboards não duplica nada — o daemon é o eixo.

Requer o extra `desktop` (`uv sync --extra desktop`), que traz o pywebview. Ele
usa o webview que já existe no sistema em vez de empacotar um navegador; no
Linux isso depende do PyGObject da distribuição, que não vem do pip — no Ubuntu,
`gir1.2-webkit2-4.1`. Sem o extra, `lea avatar --browser` resolve.

O cabeçalho da janela mostra se o websocket está mesmo ligado (`● ligada`,
`◐ ligando`, `○ sozinha`). Sem esse sinal, uma janela desconectada fica idêntica
a uma conectada e ociosa.

## Uso

Carregue, nesta ordem, `assets/vendor/three.min.js` (Three.js, licença MIT em `assets/vendor/three.LICENSE`) e `assets/lea-avatar.js`; mantenha `lea-retrato.png` no mesmo diretório do último. Em seguida, adicione o elemento à página e defina suas dimensões por CSS:

```html
<lea-avatar expression="neutral" style="width: 100%; height: 280px"></lea-avatar>
```

O atributo `expression` aceita quatro valores:

| Valor | O que muda |
| --- | --- |
| `neutral` | Repouso. Pulsos percorrem as regiões luminosas dos circuitos. |
| `listening` | Os pulsos ficam mais intensos e **invertem o sentido**. |
| `thinking` | A lente passa de ciano a **vermelho** e volta ao ciano quando a expressão sai; as pálpebras baixam e os pulsos desaceleram. |
| `speaking` | A boca é conduzida pela fala (veja abaixo). |

Cada mudança de cor é amortecida, nunca abrupta. A animação inclui piscadas, oscilação sutil da cabeça e paralaxe conforme o ponteiro passa sobre o elemento.

Na prática `assets/lea-presence.js` escolhe a expressão conforme o agente (veja abaixo). O botão **HABILITAR FALA**, no painel do avatar, reproduz uma frase localmente usando a voz do navegador; se a voz estiver indisponível, mostra uma simulação visual sem áudio.

## Sincronia labial

A boca usa a abertura e o formato dos visemas para deformar os lábios e o maxilar suavemente. A abertura acompanha a fala com deslocamento discreto do maxilar; os formatos arredondados estreitam os lábios apenas um pouco, preservando a expressão do retrato.

Há três maneiras de alimentar a boca.

**1. Voz do navegador.** `speak()` usa `SpeechSynthesis` e cronometra os visemas pelos eventos `boundary` da própria síntese, ou seja, pela fala real e não por um temporizador fixo. Vogais seguram mais tempo que consoantes, `m`, `b` e `p` fecham os lábios, e a boca volta ao repouso no intervalo entre palavras:

```js
const avatar = document.querySelector('lea-avatar');
await avatar.speak('Olá, eu sou a LEA.');            // retorna uma Promise
await avatar.speak('Pronta.', {lang: 'pt-BR', rate: 1.1, voice: minhaVoz});
```

A expressão vira `speaking` durante a fala e volta sozinha ao terminar. A Promise é rejeitada se o navegador não tiver `SpeechSynthesis` ou se a fala for interrompida.

**2. Áudio real.** `attachAudio()` liga um analisador a qualquer fonte de áudio — um `<audio>`, um `MediaStream` do microfone ou um `AudioNode` — e a abertura da boca passa a seguir o volume do sinal:

```js
const player = new Audio('/tts/resposta.mp3');
avatar.attachAudio(player);                          // mantém o áudio audível
await player.play();
// ...
avatar.detachAudio();
```

As duas fontes se combinam: com `speak()` e `attachAudio()` ativos ao mesmo tempo, a voz decide **se** e **quanto** a boca abre e o texto decide **qual** forma ela faz.

**3. Controle manual.** O atributo `mouth` (0 a 1) tem prioridade sobre tudo e é útil para ligar uma fonte de voz própria. Remova-o para devolver o controle às fontes acima:

```js
avatar.setAttribute('mouth', '0.5');
avatar.removeAttribute('mouth');
```

Sem nenhuma fonte conectada, `expression="speaking"` roda uma fala simulada — movimento plausível, sem sincronia com áudio nenhum.

## Imagem e efeitos

O relevo usa diretamente a imagem transparente fornecida. Um shader ilumina as regiões coloridas dos circuitos com pulsos em movimento; em escuta, o sentido se inverte. A versão anterior, em voxels gerados por script, foi removida — está no histórico do git se um dia fizer falta.

## Comportamento

O elemento libera a cena, o renderizador, as texturas, o contexto de áudio que ele criou e o observador de tamanho quando é removido do documento. Respeita a preferência de movimento reduzido do sistema. Caso WebGL não esteja disponível, exibe uma mensagem alternativa no lugar do canvas.

## Gráfico de onda

`<lea-waveform>` desenha a onda sonora de linhas aninhadas que fica abaixo do retrato. É uma única senoide modulada por uma fileira de gaussianas, repetida em cópias escaladas: todas cruzam o zero juntas e se abrem nas cristas, que é o que produz o aspecto de folhas encaixadas. A cor é um degradê de magenta a ciano.

```html
<lea-waveform avatar="#lea-face" style="height:92px"></lea-waveform>
```

Aponte `avatar` para o seletor do `<lea-avatar>`: a onda fica quase parada em repouso e se move enquanto a expressão for `speaking`, com a amplitude seguindo `avatar.speechLevel` (0 a 1, a abertura atual da boca). Sem o atributo, ela procura o primeiro `<lea-avatar>` da página; sem nenhum, use a propriedade `level`.

Não use o atributo `for`: runtimes no estilo React o reescrevem e o elemento perde a referência.

## Expressão automática e o motor Codex

`assets/lea-presence.js` é quem decide a cara dela. Ele junta duas coisas:

**O que está acontecendo.** Os eventos que o agente já publica no barramento (`lea/agent.py`) mapeiam direto para as expressões:

| Evento do agente | Expressão |
| --- | --- |
| você digitando no chat | `listening` |
| `session_start`, `tool_call`, `tool_result`, `alert` | `thinking` |
| `message` | `speaking`, e ela fala o texto em voz alta |
| `session_end` | `neutral` |

**Quem responde.** Ao apertar Enter no chat, o módulo manda a tarefa para `POST /api/agent/runs` com `backend: "codex"` e acompanha a sessão pelo websocket `/ws`. O Codex já é o backend padrão em `lea/config.py`; o que faltava era a interface conversar com ele.

Para ligar, abra o protótipo **pelo próprio daemon**, passando o token uma vez:

```
http://127.0.0.1:7420/app/index.html?token=<conteúdo de ~/.lea/ui_token>&project=1
```

O módulo guarda o token em `localStorage` e o apaga da URL, para ele não ficar no histórico nem numa captura de tela. Nas próximas vezes basta `http://127.0.0.1:7420/app/index.html`.

O daemon serve `app/` em `/app` justamente para a página ficar na **mesma origem** da API. Servir o protótipo de outra porta exigiria abrir CORS no daemon, e aí qualquer página que você visitasse poderia tentar falar com ele — escutar em 127.0.0.1 não autentica ninguém.

Também dá para configurar à mão com `window.LEA_API = {base, token, projectId}`. Sem token o módulo fica inerte: a página segue como demonstração e só os sinais locais (digitação, teste de fala) mexem o rosto dela.

Não há seletor no painel: quem manda é o agente. Um controle manual ali seria um botão mentindo sobre quem decide. Para forçar uma expressão em teste, use a API:

```js
LeaPresence.signal('working');                  // força um estado pelo contexto
LeaPresence.onAgentEvent({type: 'message', payload: {text: 'Pronto.'}});
LeaPresence.setAuto(false, 'thinking');         // trava numa expressão
LeaPresence.setAuto(true);                      // devolve ao automático
```

O módulo nunca guarda referência ao elemento: o runtime da página recria o nó do avatar a cada render, então ele reconsulta o DOM a cada escrita.
