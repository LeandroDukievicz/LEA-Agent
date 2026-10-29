"""Cálculo e registro de custo das chamadas Anthropic do agente próprio."""

from __future__ import annotations

from typing import Any

import aiosqlite

from lea.config import CostsConfig, ModelTokenPricing

TOKENS_PER_MILLION = 1_000_000


def pricing_for_model(model: str, config: CostsConfig) -> ModelTokenPricing | None:
    """Localiza o preço exato ou o prefixo configurado para um id versionado."""
    prices = config.model_prices_usd_per_million
    if model in prices:
        return prices[model]
    matches = [name for name in prices if model.startswith(name + "-")]
    return prices[max(matches, key=len)] if matches else None


def anthropic_usage_cost_usd(
    model: str,
    usage: Any,
    config: CostsConfig,
) -> tuple[int, int, float] | None:
    """Calcula tokens faturáveis e custo de uma resposta do Messages API."""
    pricing = pricing_for_model(model, config)
    if pricing is None:
        return None

    input_tokens = _integer(usage, "input_tokens")
    output_tokens = _integer(usage, "output_tokens")
    cache_read = _integer(usage, "cache_read_input_tokens")
    cache_write_total = _integer(usage, "cache_creation_input_tokens")
    cache_creation = _value(usage, "cache_creation")
    cache_write_5m = _integer(cache_creation, "ephemeral_5m_input_tokens")
    cache_write_1h = _integer(cache_creation, "ephemeral_1h_input_tokens")
    cache_write_total = max(cache_write_total, cache_write_5m + cache_write_1h)
    cache_write_5m += max(0, cache_write_total - cache_write_5m - cache_write_1h)

    input_price = pricing.input_usd_per_million
    cache_5m_price = pricing.cache_write_5m_usd_per_million
    cache_1h_price = pricing.cache_write_1h_usd_per_million
    cache_read_price = pricing.cache_read_usd_per_million
    cost = (
        input_tokens * input_price
        + output_tokens * pricing.output_usd_per_million
        + cache_write_5m * (cache_5m_price if cache_5m_price is not None else input_price * 1.25)
        + cache_write_1h * (cache_1h_price if cache_1h_price is not None else input_price * 2.0)
        + cache_read * (cache_read_price if cache_read_price is not None else input_price * 0.1)
    ) / TOKENS_PER_MILLION
    return input_tokens + cache_write_total + cache_read, output_tokens, cost


async def record_call(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    usd_brl: float,
    at: str,
) -> float:
    cost_brl = cost_usd * usd_brl
    await conn.execute(
        "INSERT INTO costs (session_id, harness, model, tokens_in, tokens_out, "
        "cost_usd, cost_brl, at) "
        "VALUES (?, 'anthropic-api', ?, ?, ?, ?, ?, ?)",
        (session_id, model, tokens_in, tokens_out, cost_usd, cost_brl, at),
    )
    await conn.execute(
        "UPDATE sessions SET cost_brl = cost_brl + ? WHERE id = ?",
        (cost_brl, session_id),
    )
    await conn.commit()
    return cost_brl


async def daily_total_brl(conn: aiosqlite.Connection) -> float:
    """Soma o gasto de hoje no fuso local, sem arredondar antes do limite."""
    async with conn.execute(
        "SELECT COALESCE(SUM(cost_brl), 0.0) FROM costs "
        "WHERE date(at, 'localtime') = date('now', 'localtime')"
    ) as cursor:
        row = await cursor.fetchone()
    return float(row[0]) if row else 0.0


def _value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _integer(value: Any, name: str) -> int:
    try:
        return max(0, int(_value(value, name) or 0))
    except (TypeError, ValueError):
        return 0
