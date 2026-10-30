# Avatar LEA

O elemento `<lea-avatar>` reconstrói a arte de referência (`docs/reference/lea-portrait.jpg`) como um relevo de voxels em WebGL, com Three.js. Funciona localmente, sem conexão ou serviços externos.

A referência corta o topo da cabeça — e não só acima da borda: a cunha preta no canto superior esquerdo também é crânio, deixada de fora porque o enquadramento corta ali. O gerador reconstrói isso antes de tudo: ajusta uma elipse à linha mais larga de cabelo que o desenho mostra, leva até um ápice e preenche cada célula dentro dela que ainda seja fundo, tirando a cor das mechas que já estão logo abaixo — a calota sai da paleta que o cabelo dela já tem. O mapa final tem 96×105.

Cada pixel vira uma caixa instanciada. A profundidade de cada caixa vem da silhueta borrada da própria arte, então a cabeça ganha volume, o implante óptico flutua à frente do rosto e as trilhas de neon pairam sobre a pele. As cores são as da referência: as classes de neon (implante, circuitos, luzes de fundo) são desenhadas sem iluminação para manter o croma original, e o resto recebe uma luz suave que só modula o relevo.

## Uso

Carregue, nesta ordem, `assets/vendor/three.min.js` (Three.js, licença MIT em `assets/vendor/three.LICENSE`), `assets/lea-portrait.js` e `assets/lea-avatar.js`. Em seguida, adicione o elemento à página e defina suas dimensões por CSS:

```html
<lea-avatar expression="neutral" style="width: 100%; height: 280px"></lea-avatar>
```

O atributo `expression` aceita quatro valores:

| Valor | O que muda |
| --- | --- |
| `neutral` | Repouso. Pulsos saem do implante pelas trilhas. |
| `listening` | As trilhas ficam em ciano mais forte e os pulsos **invertem o sentido**, correndo de volta para dentro da lente, que pisca a cada onda que chega — ela absorvendo o que você fala ou digita. |
| `thinking` | A lente passa de ciano a **vermelho** e volta ao ciano quando a expressão sai; as pálpebras baixam e os pulsos desaceleram. |
| `speaking` | A boca é conduzida pela fala (veja abaixo). |

Cada mudança de cor é amortecida, nunca abrupta. A animação inclui piscadas, oscilação sutil da cabeça e paralaxe conforme o ponteiro passa sobre o elemento.

Na prática você não escolhe a expressão: `assets/lea-presence.js` escolhe por ela (veja abaixo).

## Sincronia labial

A boca tem seis formas (visemas) traçadas a partir da própria arte, além da posição de repouso: `ajar`, `open`, `wide`, `round`, `narrow` e `gape`. Os pixels trocam de forma de uma vez, como pixel art faz, mas o maxilar embaixo desce de forma contínua e amortecida — é essa combinação que faz a fala parecer um movimento só, e não um flipbook.

Há duas maneiras de alimentar a boca.

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

## Regenerar o mapa de pixels

`app/assets/lea-portrait.js` é gerado; não edite à mão. Ele sai de `tools/build_portrait.py`, que amostra a referência em 96×96, reconstrói a calota do crânio, separa as classes (pele, cabelo, circuito, implante, olho, lábio, fundo), monta uma paleta por classe e desenha as variações de pálpebra e de boca a partir dos próprios pixels:

```bash
python tools/build_portrait.py                       # usa docs/reference/lea-portrait.jpg
LEA_PORTRAIT_PREVIEW=1 python tools/build_portrait.py  # grava PNGs de conferência
```

Depende de `pillow` e `numpy`. Para trocar a arte, aponte `LEA_REFERENCE` para outra imagem quadrada — as caixas do olho e da boca (`EYE_BOX`, `MOUTH_BOX`), o centro do implante e a altura da calota (`CROWN`) precisarão ser reajustados.

## Comportamento

O elemento libera a cena, o renderizador, as texturas, o contexto de áudio e o observador de tamanho quando é removido do documento. Respeita a preferência de movimento reduzido do sistema. Caso WebGL não esteja disponível, exibe uma mensagem alternativa no lugar do canvas.

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
