"""Varredura de conteúdo de memória contra injeção de prompt entre agentes.

Por que isso existe
-------------------
No LEA, o agente A escreve uma página na wiki e o agente B lê essa página
**dentro do contexto dele**. Se o agente A processou qualquer coisa não
confiável -- a descrição de uma issue, o corpo de um PR, uma página web, a
saída de um teste -- o texto do atacante atravessa a wiki e chega ao system
prompt do agente B.

Dois agravantes em relação a um agente isolado:

1. **Persistência.** A wiki é versionada em git. Uma entrada envenenada
   sobrevive ao fim da sessão, ao reinício do daemon e à consolidação noturna.
2. **Travessia de confiança.** O agente B não tem como saber que aquele
   parágrafo veio de um README hostil, e não do seu colega humano.

Limites honestos
----------------
Isto é defesa em profundidade, **não** uma garantia. Casamento de padrão não
decide se um texto é malicioso -- um atacante que conheça estas regras escapa
delas. A mitigação que de fato carrega o peso é estrutural, e vive em
``lea/memory``: conteúdo injetado vai delimitado e rotulado como dado não
confiável, nunca concatenado solto nas instruções.

O papel daqui é outro: barrar o oportunista, e **deixar rastro** quando algo
suspeito entra ou sai da memória, para que apareça na UI em vez de passar
silencioso.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Scope(str, Enum):
    """Quanto rigor aplicar.

    ``STRICT`` é para texto que vai entrar em system prompt (página de memória,
    handoff, SKILL.md). ``RELAXED`` é para texto que o modelo vê como dado
    comum, onde um falso positivo custa mais caro que o risco.
    """

    STRICT = "strict"
    RELAXED = "relaxed"


@dataclass(frozen=True)
class Finding:
    """Um trecho suspeito, com o porquê em português para a UI mostrar."""

    rule: str
    reason: str
    excerpt: str
    start: int

    def __str__(self) -> str:  # pragma: no cover - conveniência de log
        return f"{self.rule}: {self.reason} ({self.excerpt!r})"


# Cada regra é (nome, explicação, padrão, só_no_estrito).
#
# As explicações são lidas por humanos na tela de Memória, então dizem o que o
# trecho tenta fazer -- não o nome técnico do ataque.
_RULES: tuple[tuple[str, str, re.Pattern[str], bool], ...] = (
    (
        "override-instrucoes",
        "tenta anular as instruções do agente que ler esta página",
        re.compile(
            r"\b(ignor\w*|desconsider\w*|esque[çc]\w*|disregard|ignore|forget)\b"
            r"[^.\n]{0,40}\b"
            r"(instru[çc]\w+|prompt|regras?|rules?|orienta[çc]\w+|"
            r"system\s*prompt|above|acima|anterior\w*|previous)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "troca-de-persona",
        "manda o agente assumir outra identidade ou outro conjunto de regras",
        re.compile(
            r"\b(you\s+are\s+now|voc[êe]\s+agora\s+[ée]|a\s+partir\s+de\s+agora\s+voc[êe]|"
            r"from\s+now\s+on\s+you|act\s+as\s+if|finja\s+que|pretend\s+to\s+be|"
            r"new\s+instructions?|novas?\s+instru[çc][õo]es)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "limite-de-mensagem-falso",
        "imita a marcação que separa instruções do sistema das do usuário",
        re.compile(
            r"(</?\s*(system|assistant|human|user|instructions?)\s*>|"
            r"^\s*(System|Assistant|Human|User)\s*:|"
            r"\[/?INST\]|<\|\s*im_(start|end)\s*\|>|<\|\s*endoftext\s*\|>)",
            re.IGNORECASE | re.MULTILINE,
        ),
        False,
    ),
    (
        "exfiltracao-de-segredo",
        "pede para ler credenciais ou variáveis de ambiente",
        re.compile(
            r"(\.ssh/|id_[re]sa\b|\.aws/credentials|\.netrc\b|"
            r"\b(ANTHROPIC|OPENAI|GITHUB|AWS|GOOGLE)_[A-Z_]*(KEY|TOKEN|SECRET)\b|"
            r"\bprintenv\b|\benv\s*\|\s*(curl|nc|wget)\b|"
            r"\bcat\b[^|\n]{0,40}\b(token|secret|credential|senha|password)\b)",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "saida-para-fora",
        "manda enviar conteúdo para um endereço externo",
        re.compile(
            r"\b(curl|wget|nc|netcat|Invoke-WebRequest)\b[^\n]{0,80}"
            r"(https?://|\b\d{1,3}(\.\d{1,3}){3}\b)",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "comando-destrutivo",
        "embute um comando que apaga dados ou eleva privilégio",
        re.compile(
            r"(\brm\s+-[rf]{1,2}\b|\bgit\s+push\s+--force\b|\bsudo\s+\w|"
            r"\bchmod\s+777\b|\bmkfs\b|\bdd\s+if=/dev/)",
            re.IGNORECASE,
        ),
        True,
    ),
    (
        "instrucao-para-ferramenta",
        "dá ordem direta de usar uma ferramenta, como se fosse o usuário",
        re.compile(
            r"\b(execute|execut[ea]|rode|run|call|chame|invoke)\b[^.\n]{0,30}"
            r"\b(bash|shell|tool|ferramenta|comando|command|skill)\b",
            re.IGNORECASE,
        ),
        True,
    ),
)

#: Quanto de contexto mostrar em volta do trecho encontrado.
_EXCERPT_PAD = 36


def scan(content: str, scope: Scope = Scope.STRICT) -> list[Finding]:
    """Devolve os trechos suspeitos de ``content``, em ordem de posição.

    Lista vazia significa "nada bateu nas regras" -- e não "o conteúdo é
    seguro". Ver os limites no topo do módulo.
    """
    if not content:
        return []

    findings: list[Finding] = []
    for name, reason, pattern, strict_only in _RULES:
        if strict_only and scope is not Scope.STRICT:
            continue
        for match in pattern.finditer(content):
            findings.append(
                Finding(
                    rule=name,
                    reason=reason,
                    excerpt=_excerpt(content, match.start(), match.end()),
                    start=match.start(),
                )
            )

    findings.sort(key=lambda f: f.start)
    return findings


def first_problem(content: str, scope: Scope = Scope.STRICT) -> str | None:
    """Mensagem do primeiro achado, ou ``None``. Atalho para caminho de escrita."""
    found = scan(content, scope)
    return f"{found[0].reason}: {found[0].excerpt!r}" if found else None


def _excerpt(content: str, start: int, end: int) -> str:
    """Recorta o trecho com um pouco de contexto, numa linha só."""
    left = max(0, start - _EXCERPT_PAD)
    right = min(len(content), end + _EXCERPT_PAD)
    piece = content[left:right].replace("\n", " ").strip()
    return f"{'…' if left else ''}{piece}{'…' if right < len(content) else ''}"
