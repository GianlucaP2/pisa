"""Prices and costs. Costs use genai-prices' own calculation (tiered prices included), never a flat rate.

Price lookup order for one model:
1. A model on one of the user's OpenAI-compatible servers: €0 (tokens are still counted). No price is made up.
2. genai-prices for the provider (exact, tiered).
3. The catalog's list price (OpenRouter's live list) when genai-prices doesn't know the model.
4. Unknown: the most expensive known price on the same provider (or in the whole catalog when the provider has
   none), flagged as not known.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import LiteralString

from genai_prices import Usage, calc_price, data_snapshot
from genai_prices.types import ModelInfo, TieredPrices
from psycopg import AsyncConnection

from pisa.brain.models.providers import ProviderSpec
from pisa.domain.ai import MTOK, ModelRef
from pisa.domain.errors import PisaError


@dataclass(frozen=True)
class Cost:
    usd: Decimal
    known: bool


def genai_model(spec: ProviderSpec, model_id: str) -> ModelInfo | None:
    snapshot = data_snapshot.get_snapshot()
    for gp_id in spec.genai_prices_ids:
        for provider in snapshot.providers:
            if provider.id == gp_id and (model := provider.find_model(model_id)) is not None:
                return model
    return None


def _base(value: Decimal | TieredPrices | None) -> Decimal | None:
    if isinstance(value, TieredPrices):
        return value.base
    return value


def base_prices(model: ModelInfo, at: datetime | None = None) -> tuple[Decimal, Decimal] | None:
    """Base-tier USD per 1M input/output tokens (what the picker shows)."""
    prices = model.get_prices(at or datetime.now(UTC))
    inp, out = _base(prices.input_mtok), _base(prices.output_mtok)
    if inp is None or out is None:
        return None
    return inp, out


def _genai_cost(
    spec: ProviderSpec,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> Decimal | None:
    usage = Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read or None,
        cache_write_tokens=cache_write or None,
    )
    for gp_id in spec.genai_prices_ids:
        try:
            result = calc_price(usage, model_id, provider_id=gp_id)
        except LookupError:
            continue
        return result.total_price
    return None


async def call_cost_usd(
    conn: AsyncConnection,
    spec: ProviderSpec,
    ref: ModelRef,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Cost:
    """Cached input tokens are priced at the model's cache rates (genai-prices). The flat fallbacks below have
    no cache rates and charge every input token at the input price (conservative)."""
    if spec.compatible:
        return Cost(Decimal(0), True)
    exact = _genai_cost(
        spec, ref.model_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    )
    if exact is not None:
        return Cost(exact, True)
    row = await (
        await conn.execute(
            "SELECT input_usd_per_mtok, output_usd_per_mtok FROM model_catalog WHERE ref = %s", (str(ref),)
        )
    ).fetchone()
    if row and row[0] is not None and row[1] is not None:
        return Cost((row[0] * input_tokens + row[1] * output_tokens) / MTOK, True)
    worst = await most_expensive_price(conn, spec.id)
    if worst is None:
        raise PisaError(
            "internal",
            f"No price is known for {ref} and the model catalog has no prices yet, so spend caps "
            f"can't be enforced.",
            "Run `pisa catalog refresh` on the server, or wait for the daily refresh at 03:00",
        )
    return Cost((worst[0] * input_tokens + worst[1] * output_tokens) / MTOK, False)


_MAX_PRICE_ANY = (
    "SELECT max(input_usd_per_mtok), max(output_usd_per_mtok) FROM model_catalog "
    "WHERE input_usd_per_mtok IS NOT NULL AND output_usd_per_mtok IS NOT NULL"
)
_MAX_PRICE_PROVIDER = (
    "SELECT max(input_usd_per_mtok), max(output_usd_per_mtok) FROM model_catalog "
    "WHERE provider = %s AND input_usd_per_mtok IS NOT NULL AND output_usd_per_mtok IS NOT NULL"
)


async def most_expensive_price(conn: AsyncConnection, provider: str) -> tuple[Decimal, Decimal] | None:
    """Highest known input and output price on this provider, else in the whole catalog."""
    queries: tuple[tuple[LiteralString, tuple[str, ...]], ...] = (
        (_MAX_PRICE_PROVIDER, (provider,)),
        (_MAX_PRICE_ANY, ()),
    )
    for query, params in queries:
        row = await (await conn.execute(query, params)).fetchone()
        if row and row[0] is not None and row[1] is not None:
            return row[0], row[1]
    return None
