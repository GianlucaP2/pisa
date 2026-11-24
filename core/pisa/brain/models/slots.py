"""Feature slots: which model (and optional fallback) serves each feature."""

from dataclasses import dataclass

from psycopg import AsyncConnection

from pisa.brain.models.catalog import get_model
from pisa.brain.models.compat import get_caps, server_exists
from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.domain.ai import SLOTS, SLOTS_BY_ID, ModelRef, SlotSpec, missing_capabilities
from pisa.domain.errors import PisaError


@dataclass(frozen=True)
class SlotState:
    spec: SlotSpec
    model: str | None
    fallback: str | None


def slot_spec(slot_id: str) -> SlotSpec:
    spec = SLOTS_BY_ID.get(slot_id)
    if spec is None:
        raise PisaError("not_found", f"There is no slot {slot_id!r}.", "Use a slot id from GET /v1/ai/slots")
    return spec


async def get_slot(conn: AsyncConnection, slot_id: str) -> SlotState:
    spec = slot_spec(slot_id)
    row = await (
        await conn.execute("SELECT model_ref, fallback_ref FROM model_slots WHERE id = %s", (slot_id,))
    ).fetchone()
    return SlotState(spec, row[0], row[1]) if row else SlotState(spec, None, None)  # no default model


async def all_slots(conn: AsyncConnection) -> list[SlotState]:
    rows = dict(
        (r[0], (r[1], r[2]))
        for r in await (await conn.execute("SELECT id, model_ref, fallback_ref FROM model_slots")).fetchall()
    )
    return [SlotState(s, *rows.get(s.id, (None, None))) for s in SLOTS]


async def _check_compatible(conn: AsyncConnection, spec: SlotSpec, ref: str, role: str) -> None:
    """The model must be able to serve the slot. A model on an OpenAI-compatible server that was never
    probed (its server could not be reached when the slot was set) and has no override is taken: what it can
    do is unknown, not missing, and its first use probes it."""
    parsed = ModelRef.parse(ref)
    builtin = parsed.provider in PROVIDERS_BY_ID
    if not builtin and not await server_exists(conn, parsed.provider):
        raise PisaError(
            "validation_failed", f"The {role} {ref!r} names an unknown provider {parsed.provider!r}."
        )
    model = await get_model(conn, ref)
    if model is None:
        raise PisaError(
            "validation_failed",
            f"The {role} {ref!r} is not in the model catalog.",
            "Pick a model from GET /v1/ai/models (add the provider's key first to see all its models)",
        )
    if not builtin:
        caps = await get_caps(conn, parsed.provider, parsed.model_id)
        if caps.probed_at is None and not caps.overrides:
            return
    missing = missing_capabilities(spec.needs, model.capabilities)
    if missing:
        raise PisaError(
            "model_incompatible",
            f"{model.name} ({ref}) can't be used for {spec.phrase}: it lacks {', '.join(missing)}.",
            "Choose a model marked compatible for this slot",
        )


async def update_slot(conn: AsyncConnection, slot_id: str, model: str, fallback: str | None) -> SlotState:
    spec = slot_spec(slot_id)
    await _check_compatible(conn, spec, model, "model")
    if fallback is not None:
        await _check_compatible(conn, spec, fallback, "fallback")
    await conn.execute(
        "INSERT INTO model_slots (id, model_ref, fallback_ref, updated_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (id) DO UPDATE SET model_ref = EXCLUDED.model_ref, fallback_ref = "
        "EXCLUDED.fallback_ref, "
        "updated_at = now()",
        (slot_id, model, fallback),
    )
    return SlotState(spec, model, fallback)
