"""Força da página: decaimento no tempo, reforço por uso, promoção de tier.

A ideia: memória que ninguém consulta perde relevância sozinha, sem que
ninguém precise fazer faxina. O que é lido e citado sobe; o que é esquecido
desce e para de ser injetado em contexto -- mas **continua existindo**.

Nenhuma página é apagada por decaimento. Só por commit manual. Decaimento
decide o que entra no contexto, não o que existe.

Sobre a fórmula
---------------
A especificação escreve::

    strength = base * exp(-Δt / half_life[tier])

mas chama o parâmetro de *meia-vida*. Os dois não combinam: com
``exp(-Δt/h)``, em ``Δt = h`` sobra ``e⁻¹ ≈ 0,368``, não metade. Implementamos
a **meia-vida de verdade** (``0,5 ** (Δt/h)``), porque é o que o nome promete e
é como o usuário vai raciocinar ao editar ``half_life_days`` no config: "página
hot cai pela metade em 3 dias". A diferença é material -- em 21 dias, uma
página warm fica em 0,50 pela nossa fórmula e em 0,37 pela literal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lea.memory.page import TIERS, Tier

#: Força de uma página recém-criada.
INITIAL_STRENGTH = 0.5

#: Quantos acessos em 7 dias saturam o bônus de uso. Acima disso o reforço
#: para de crescer: senão um laço que lê a mesma página prenderia ela em 1.0.
ACCESS_SATURATION = 10

#: Peso do uso recente.
ACCESS_WEIGHT = 0.15

#: Peso de ter sido citada num handoff. É o sinal mais forte que existe: um
#: agente achou a página relevante o bastante para passá-la adiante.
HANDOFF_WEIGHT = 0.30

#: Abaixo disto, a consolidação rebaixa o tier.
DEMOTE_BELOW = 0.25

#: Acessos nesta janela promovem o tier.
PROMOTE_ACCESSES = 3
PROMOTE_WINDOW = timedelta(hours=48)

#: Meia-vida padrão por tier, em dias. O config pode sobrepor.
DEFAULT_HALF_LIFE: dict[Tier, float] = {"hot": 3.0, "warm": 21.0, "cold": 120.0}


@dataclass(frozen=True)
class Projection:
    """Força agora e no futuro, para a barra de decaimento da UI."""

    now: float
    in_7d: float
    in_30d: float

    def as_dict(self) -> dict[str, float]:
        return {"now": self.now, "in_7d": self.in_7d, "in_30d": self.in_30d}


def half_life_for(tier: Tier, half_lives: dict[str, float] | None = None) -> float:
    """Meia-vida do tier, em dias."""
    table = half_lives or DEFAULT_HALF_LIFE
    value = table.get(tier, DEFAULT_HALF_LIFE[tier])
    if value <= 0:
        # Meia-vida zero faria a força sumir na primeira consulta. Config
        # ruim não deve virar perda de memória silenciosa.
        raise ValueError(f"meia-vida de {tier} deve ser maior que zero, veio {value}")
    return float(value)


def strength_at(
    base: float,
    tier: Tier,
    elapsed_days: float,
    *,
    accesses_7d: int = 0,
    cited_in_handoff: bool = False,
    half_lives: dict[str, float] | None = None,
) -> float:
    """Força após ``elapsed_days`` sem acesso, já com os reforços.

    ``elapsed_days`` negativo é tratado como zero: relógio que andou para trás
    (fuso, NTP, container) não pode *aumentar* a força de uma página.
    """
    elapsed = max(0.0, elapsed_days)
    half_life = half_life_for(tier, half_lives)

    decayed = base * (0.5 ** (elapsed / half_life))
    reinforcement = ACCESS_WEIGHT * _normalized_accesses(accesses_7d)
    if cited_in_handoff:
        reinforcement += HANDOFF_WEIGHT

    return _clamp(decayed + reinforcement)


def project(
    base: float,
    tier: Tier,
    last_access: datetime | None,
    *,
    accesses_7d: int = 0,
    cited_in_handoff: bool = False,
    half_lives: dict[str, float] | None = None,
    now: datetime | None = None,
) -> Projection:
    """Força agora, em 7 e em 30 dias.

    A projeção supõe que **nada mais acontece** com a página: nenhum acesso
    novo, nenhuma citação. É uma projeção de esquecimento, não previsão de
    uso -- a UI mostra para o usuário entender o que vai sumir do contexto se
    ele não fizer nada.
    """
    moment = now or datetime.now(timezone.utc)
    elapsed = elapsed_days(last_access, moment)

    def at(extra: float) -> float:
        return strength_at(
            base,
            tier,
            elapsed + extra,
            accesses_7d=accesses_7d,
            cited_in_handoff=cited_in_handoff,
            half_lives=half_lives,
        )

    return Projection(now=at(0.0), in_7d=at(7.0), in_30d=at(30.0))


def elapsed_days(last_access: datetime | None, now: datetime | None = None) -> float:
    """Dias desde o último acesso. Sem acesso registrado conta como zero."""
    if last_access is None:
        return 0.0
    moment = now or datetime.now(timezone.utc)
    if last_access.tzinfo is None:
        # Gravamos sempre com fuso; um valor sem fuso veio de edição manual e
        # é lido como UTC, que é o fuso de gravação.
        last_access = last_access.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - last_access).total_seconds() / 86400.0)


def should_promote(accesses_in_window: int) -> bool:
    """True se o uso recente justifica subir o tier."""
    return accesses_in_window >= PROMOTE_ACCESSES


def promote(tier: Tier) -> Tier:
    """Sobe um nível. ``hot`` já é o topo."""
    index = TIERS.index(tier)
    return TIERS[max(0, index - 1)]


def demote(tier: Tier) -> Tier:
    """Desce um nível. ``cold`` já é o fundo."""
    index = TIERS.index(tier)
    return TIERS[min(len(TIERS) - 1, index + 1)]


def should_demote(strength: float) -> bool:
    return strength < DEMOTE_BELOW


def _normalized_accesses(count: int) -> float:
    """Acessos em 7 dias, de 0 a 1, com saturação suave.

    Logarítmico e não linear: a diferença entre 1 e 2 acessos diz mais sobre
    relevância do que a diferença entre 9 e 10.
    """
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(ACCESS_SATURATION))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
