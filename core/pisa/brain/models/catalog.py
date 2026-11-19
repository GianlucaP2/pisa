"""Model catalog: which models exist, what they can do and what they cost (stored in model_catalog).

Sources, merged in this order:
1. genai-prices (bundled, offline): models, names, context sizes and base-tier prices per provider.
2. OpenRouter's public model list: every `openrouter:` model with prices and capabilities, and the
   capabilities
   of other providers' own models, matched by OpenRouter id "<vendor>/<model_id>".
3. Each provider's own model list, for providers with a key.
4. Each of the user's OpenAI-compatible servers (compat.py): its model list, at no price, with the
   capabilities the probe found and the user's overrides.

Capabilities come from the sources where they're known. Otherwise conservative rules apply (see
`conservative_capabilities`): a model is never credited with a capability no source confirms.

What Pisa sets per model (its reasoning effort) and the voice connectors' default models are the catalog's
shipped data, which the operator can change (`pisa.domain.model_catalog`).
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.brain.models.compat import LOCAL_PRICE_SOURCE, http_for, list_servers, stored_caps
from pisa.brain.models.pricing import base_prices, genai_model
from pisa.brain.models.providers import PROVIDERS, ListedModel, ProviderCallError, ProviderSpec, list_models
from pisa.db.settings import get_setting, set_setting
from pisa.domain.ai import Capability
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import ModelCaps
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# A row no source listed in this many consecutive refreshes is removed (failed sources don't count as misses).
STALE_AFTER_MISSES = 3
_log = get_logger("pisa.models.catalog")


@dataclass
class CatalogRow:
    ref: str
    provider: str
    model_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    context_tokens: int | None = None
    input_usd_per_mtok: Decimal | None = None
    output_usd_per_mtok: Decimal | None = None
    price_source: str | None = None
    refreshed_at: datetime | None = None

    @property
    def price_known(self) -> bool:
        return self.input_usd_per_mtok is not None and self.output_usd_per_mtok is not None


@dataclass
class RefreshReport:
    models: int
    errors: list[str]


def conservative_capabilities(model_id: str) -> list[str]:
    """Used when no source describes the model: an id naming embeddings → embeddings; otherwise nothing.

    So an unknown chat model can serve slots without needs (briefing.compose, understand.confirm)
    but is never offered for tools, structured output, vision or audio until a source confirms it.
    """
    return ["embeddings"] if "embed" in model_id.lower() else []


def openrouter_capabilities(item: dict[str, Any]) -> list[str]:
    params = item.get("supported_parameters") or []
    arch = item.get("architecture") or {}
    inputs = arch.get("input_modalities") or []
    outputs = arch.get("output_modalities") or []
    caps: list[Capability] = []
    if "tools" in params:
        caps.append("tools")
    if "structured_outputs" in params:  # response_format alone is JSON mode, not schema-checked output
        caps.append("structured_output")
    if "image" in inputs:
        caps.append("vision")
    if "embeddings" in outputs:
        caps.append("embeddings")
    if "audio" in inputs:
        caps.append("audio_in")
    if "audio" in outputs:
        caps.append("audio_out")
    return list(caps)


def _per_mtok(per_token: Any) -> Decimal | None:
    try:
        value = Decimal(str(per_token))
    except (InvalidOperation, ValueError):
        return None
    return None if value < 0 else value * 1_000_000  # OpenRouter uses -1 for "variable"


def builtin_rows(now: datetime) -> dict[str, CatalogRow]:
    """genai-prices' models for every provider in the registry (no network)."""
    from genai_prices import data_snapshot

    rows: dict[str, CatalogRow] = {}
    snapshot = data_snapshot.get_snapshot()
    for spec in PROVIDERS:
        for gp_id in spec.genai_prices_ids:
            for gp_provider in snapshot.providers:
                if gp_provider.id != gp_id:
                    continue
                for model in gp_provider.models:
                    if model.deprecated or (
                        spec.builtin_prefixes and not model.id.startswith(spec.builtin_prefixes)
                    ):
                        continue
                    ref = f"{spec.id}:{model.id}"
                    if ref in rows:
                        continue
                    prices = base_prices(model, now)
                    rows[ref] = CatalogRow(
                        ref,
                        spec.id,
                        model.id,
                        model.name or model.id,
                        context_tokens=model.context_window,
                        input_usd_per_mtok=prices[0] if prices else None,
                        output_usd_per_mtok=prices[1] if prices else None,
                        price_source="genai-prices" if prices else None,
                    )
    return rows


async def fetch_openrouter_models(http: httpx.AsyncClient, key: str | None) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = await http.get(OPENROUTER_MODELS_URL, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json().get("data")
    return (
        [d for d in data if isinstance(d, dict) and isinstance(d.get("id"), str)]
        if isinstance(data, list)
        else []
    )


def merge_catalog(
    builtin: dict[str, CatalogRow],
    openrouter: list[dict[str, Any]],
    listed: dict[str, list[ListedModel]],
) -> dict[str, CatalogRow]:
    rows = {ref: replace(row) for ref, row in builtin.items()}
    or_by_id = {item["id"]: item for item in openrouter}
    for item in openrouter:
        pricing = item.get("pricing") or {}
        ref = f"openrouter:{item['id']}"
        inp, out = _per_mtok(pricing.get("prompt")), _per_mtok(pricing.get("completion"))
        rows[ref] = CatalogRow(
            ref,
            "openrouter",
            item["id"],
            str(item.get("name") or item["id"]),
            context_tokens=item.get("context_length")
            if isinstance(item.get("context_length"), int)
            else None,
            input_usd_per_mtok=inp if out is not None else None,
            output_usd_per_mtok=out if inp is not None else None,
            price_source="openrouter" if inp is not None and out is not None else None,
        )
    listed_caps: dict[str, tuple[Capability, ...] | None] = {}
    for provider_id, models in listed.items():
        spec = next(p for p in PROVIDERS if p.id == provider_id)
        for m in models:
            ref = f"{provider_id}:{m.model_id}"
            listed_caps[ref] = m.capabilities
            row = rows.get(ref)
            if row is None:
                prices = _genai_base(spec, m.model_id)
                row = rows[ref] = CatalogRow(
                    ref,
                    provider_id,
                    m.model_id,
                    m.name or m.model_id,
                    input_usd_per_mtok=prices[0] if prices else None,
                    output_usd_per_mtok=prices[1] if prices else None,
                    price_source="genai-prices" if prices else None,
                )
            if m.name and row.name == row.model_id:
                row.name = m.name
            row.context_tokens = row.context_tokens or m.context_tokens
    for row in rows.values():
        spec = next(p for p in PROVIDERS if p.id == row.provider)
        if spec.aggregator:
            row.capabilities = (
                openrouter_capabilities(or_by_id[row.model_id]) if row.model_id in or_by_id else []
            )
            continue
        or_item = or_by_id.get(f"{spec.openrouter_vendor}/{row.model_id}") if spec.openrouter_vendor else None
        from_list = listed_caps.get(row.ref)
        if from_list is not None:
            row.capabilities = list(from_list)
        elif or_item is not None:
            row.capabilities = openrouter_capabilities(or_item)
        else:
            row.capabilities = conservative_capabilities(row.model_id)
        if not row.price_known and or_item is not None:
            pricing = or_item.get("pricing") or {}
            inp, out = _per_mtok(pricing.get("prompt")), _per_mtok(pricing.get("completion"))
            if inp is not None and out is not None:
                row.input_usd_per_mtok, row.output_usd_per_mtok, row.price_source = inp, out, "openrouter"
        if (
            row.context_tokens is None
            and or_item is not None
            and isinstance(or_item.get("context_length"), int)
        ):
            row.context_tokens = or_item["context_length"]
    return rows


async def _keep_stored_when_openrouter_down(conn: AsyncConnection, rows: dict[str, CatalogRow]) -> None:
    """OpenRouter supplies most capabilities and many prices. When it is down, the merge falls back to the
    conservative capability list (often empty) and unknown prices; keep what the last good refresh stored
    instead of making models look incompatible until tomorrow."""
    stored = await (
        await conn.execute(
            "SELECT ref, capabilities, input_usd_per_mtok, output_usd_per_mtok, price_source, context_tokens "
            "FROM model_catalog"
        )
    ).fetchall()
    for ref, caps, inp, out, source, context in stored:
        row = rows.get(ref)
        if row is None:
            continue
        if caps and set(row.capabilities) < set(caps):
            row.capabilities = list(caps)
        if not row.price_known and inp is not None and out is not None:
            row.input_usd_per_mtok, row.output_usd_per_mtok, row.price_source = inp, out, source
        row.context_tokens = row.context_tokens or context


def server_rows(
    spec: ProviderSpec, models: list[ListedModel], caps: dict[str, ModelCaps]
) -> dict[str, CatalogRow]:
    """A server's models: no price (the server is the user's), and only the capabilities the probe found or
    the user set (a model never probed has none)."""
    rows: dict[str, CatalogRow] = {}
    for m in models:
        ref = f"{spec.id}:{m.model_id}"
        rows[ref] = CatalogRow(
            ref,
            spec.id,
            m.model_id,
            m.name or m.model_id,
            capabilities=list(caps.get(m.model_id, ModelCaps()).capabilities()),
            context_tokens=m.context_tokens,
            input_usd_per_mtok=Decimal(0),
            output_usd_per_mtok=Decimal(0),
            price_source=LOCAL_PRICE_SOURCE,
        )
    return rows


def _genai_base(spec: ProviderSpec, model_id: str) -> tuple[Decimal, Decimal] | None:
    model = genai_model(spec, model_id)
    return base_prices(model) if model else None


async def refresh_catalog(
    conn: AsyncConnection,
    http: httpx.AsyncClient,
    keys: KeyStore,
    *,
    ollama_base_url: str | None,
    now: datetime | None = None,
) -> RefreshReport:
    """Rebuild the catalog from every source. A failing source is logged and skipped, never fatal."""
    now = now or datetime.now(UTC)
    errors: list[str] = []
    failed: set[str] = set()  # providers whose source failed: their rows are not counted as missed
    builtin = builtin_rows(now)
    keyed = set((await keys.statuses(conn)).keys()) if keys.can_store else set()

    openrouter: list[dict[str, Any]] = []
    openrouter_ok = False
    try:
        or_key = await keys.get(conn, "openrouter") if "openrouter" in keyed else None
        openrouter = await fetch_openrouter_models(http, or_key)
        openrouter_ok = True
    except (httpx.HTTPError, ValueError, PisaError) as exc:
        errors.append(f"The OpenRouter model list failed ({exc.__class__.__name__}).")
        failed.add("openrouter")

    listed: dict[str, list[ListedModel]] = {}
    for spec in PROVIDERS:
        if spec.models_path is None or spec.aggregator:  # the aggregator's list is fetched above
            continue
        if spec.needs_key and spec.id not in keyed:
            continue
        try:
            key = await keys.get(conn, spec.id) if spec.needs_key else None
            listed[spec.id] = await list_models(spec, key, http)
        except (ProviderCallError, PisaError) as exc:
            errors.append(exc.message)
            failed.add(spec.id)

    rows = merge_catalog(builtin, openrouter, listed)
    if not openrouter_ok:
        await _keep_stored_when_openrouter_down(conn, rows)
    for server in await list_servers(conn, ollama_base_url):
        spec = server.spec()
        try:
            key = await keys.get(conn, spec.id) if spec.id in keyed else None
            models = await list_models(spec, key, http_for(spec, http))
        except (ProviderCallError, PisaError) as exc:
            errors.append(exc.message)
            failed.add(spec.id)
            continue
        rows.update(server_rows(spec, models, await stored_caps(conn, spec.id)))
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO model_catalog (ref, provider, model_id, name, capabilities, context_tokens, "
                "input_usd_per_mtok, output_usd_per_mtok, price_source, refreshed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ref) DO UPDATE SET provider = EXCLUDED.provider, model_id = EXCLUDED.model_id, "
                "name = EXCLUDED.name, capabilities = EXCLUDED.capabilities, "
                "context_tokens = EXCLUDED.context_tokens, input_usd_per_mtok = EXCLUDED.input_usd_per_mtok, "
                "output_usd_per_mtok = EXCLUDED.output_usd_per_mtok, price_source = EXCLUDED.price_source, "
                "refreshed_at = EXCLUDED.refreshed_at, missed_refreshes = 0",
                [
                    (
                        r.ref,
                        r.provider,
                        r.model_id,
                        r.name,
                        r.capabilities,
                        r.context_tokens,
                        r.input_usd_per_mtok,
                        r.output_usd_per_mtok,
                        r.price_source,
                        now,
                    )
                    for r in rows.values()
                ],
            )
        await conn.execute(
            "UPDATE model_catalog SET missed_refreshes = missed_refreshes + 1 "
            "WHERE refreshed_at < %s AND NOT (provider = ANY(%s))",
            (now, sorted(failed)),
        )
        removed = [
            r[0]
            for r in await (
                await conn.execute(
                    "DELETE FROM model_catalog WHERE missed_refreshes >= %s RETURNING ref",
                    (STALE_AFTER_MISSES,),
                )
            ).fetchall()
        ]
        await set_setting(conn, "catalog_refreshed_at", Jsonb(now.isoformat()))
    if removed:
        _log.info("catalog_rows_removed", count=len(removed), refs=removed[:50])
    for error in errors:
        _log.warning("catalog_source_failed", cause=error)
    _log.info("catalog_refreshed", models=len(rows), failed_sources=len(errors))
    return RefreshReport(len(rows), errors)


_COLUMNS = (
    "ref, provider, model_id, name, capabilities, context_tokens, input_usd_per_mtok, output_usd_per_mtok, "
    "price_source, refreshed_at"
)


async def get_model(conn: AsyncConnection, ref: str) -> CatalogRow | None:
    row = await (
        await conn.execute(f"SELECT {_COLUMNS} FROM model_catalog WHERE ref = %s", (ref,))  # noqa: S608
    ).fetchone()
    return CatalogRow(*row) if row else None


async def all_models(conn: AsyncConnection) -> list[CatalogRow]:
    rows = await (
        await conn.execute(f"SELECT {_COLUMNS} FROM model_catalog ORDER BY provider, name")  # noqa: S608
    ).fetchall()
    return [CatalogRow(*r) for r in rows]


async def catalog_refreshed_at(conn: AsyncConnection) -> datetime | None:
    value = await get_setting(conn, "catalog_refreshed_at")
    return datetime.fromisoformat(value) if isinstance(value, str) else None
