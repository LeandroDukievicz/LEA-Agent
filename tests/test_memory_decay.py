"""Decaimento: a regra que decide o que continua entrando em contexto."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lea.memory import decay


def test_sem_tempo_decorrido_a_forca_se_mantem() -> None:
    assert decay.strength_at(0.8, "warm", 0.0) == pytest.approx(0.8)


def test_meia_vida_corta_pela_metade() -> None:
    """O nome promete metade: em 3 dias uma hot vai de 0,8 a 0,4.

    A spec escreveu exp(-Δt/h), que daria 0,294. Seguimos o nome, não a
    fórmula literal -- ver o docstring de lea/memory/decay.py.
    """
    assert decay.strength_at(0.8, "hot", 3.0) == pytest.approx(0.4)
    assert decay.strength_at(0.8, "warm", 21.0) == pytest.approx(0.4)
    assert decay.strength_at(0.8, "cold", 120.0) == pytest.approx(0.4)


def test_duas_meias_vidas_cortam_a_um_quarto() -> None:
    assert decay.strength_at(0.8, "hot", 6.0) == pytest.approx(0.2)


def test_tier_frio_decai_mais_devagar() -> None:
    passados = 10.0
    assert decay.strength_at(0.9, "cold", passados) > decay.strength_at(0.9, "warm", passados)
    assert decay.strength_at(0.9, "warm", passados) > decay.strength_at(0.9, "hot", passados)


def test_acesso_recente_reforca() -> None:
    sem = decay.strength_at(0.5, "warm", 10.0)
    com = decay.strength_at(0.5, "warm", 10.0, accesses_7d=5)
    assert com > sem


def test_reforco_de_acesso_satura() -> None:
    """Um laço lendo a mesma página não pode prendê-la no topo."""
    muitos = decay.strength_at(0.1, "warm", 0.0, accesses_7d=1000)
    teto = 0.1 + decay.ACCESS_WEIGHT
    assert muitos <= teto + 1e-9


def test_citacao_em_handoff_e_o_sinal_mais_forte() -> None:
    base = decay.strength_at(0.3, "warm", 5.0)
    citada = decay.strength_at(0.3, "warm", 5.0, cited_in_handoff=True)
    assert citada - base == pytest.approx(decay.HANDOFF_WEIGHT)


def test_forca_fica_entre_zero_e_um() -> None:
    alta = decay.strength_at(1.0, "cold", 0.0, accesses_7d=99, cited_in_handoff=True)
    assert alta == 1.0
    assert decay.strength_at(0.9, "hot", 10_000.0) >= 0.0


def test_relogio_para_tras_nao_aumenta_forca() -> None:
    """NTP, fuso ou container podem produzir Δt negativo."""
    assert decay.strength_at(0.5, "warm", -30.0) == pytest.approx(0.5)


def test_meia_vida_zero_e_erro_e_nao_perda_silenciosa() -> None:
    with pytest.raises(ValueError, match="maior que zero"):
        decay.strength_at(0.5, "warm", 1.0, half_lives={"warm": 0})


def test_projecao_cai_ao_longo_do_tempo() -> None:
    agora = datetime(2026, 10, 28, tzinfo=timezone.utc)
    p = decay.project(0.9, "warm", agora, now=agora)
    assert p.now > p.in_7d > p.in_30d
    assert p.now == pytest.approx(0.9)


def test_projecao_sem_acesso_registrado_parte_do_agora() -> None:
    p = decay.project(0.6, "hot", None)
    assert p.now == pytest.approx(0.6)


def test_elapsed_days_conta_do_ultimo_acesso() -> None:
    agora = datetime(2026, 10, 28, tzinfo=timezone.utc)
    antes = agora - timedelta(days=3, hours=12)
    assert decay.elapsed_days(antes, agora) == pytest.approx(3.5)


def test_elapsed_days_trata_data_sem_fuso_como_utc() -> None:
    agora = datetime(2026, 10, 28, tzinfo=timezone.utc)
    ingenua = datetime(2026, 10, 27)
    assert decay.elapsed_days(ingenua, agora) == pytest.approx(1.0)


def test_promocao_e_rebaixamento_respeitam_os_extremos() -> None:
    assert decay.promote("cold") == "warm"
    assert decay.promote("warm") == "hot"
    assert decay.promote("hot") == "hot"
    assert decay.demote("hot") == "warm"
    assert decay.demote("warm") == "cold"
    assert decay.demote("cold") == "cold"


def test_limiares_de_promocao_e_rebaixamento() -> None:
    assert decay.should_promote(3)
    assert not decay.should_promote(2)
    assert decay.should_demote(0.2)
    assert not decay.should_demote(0.25)
