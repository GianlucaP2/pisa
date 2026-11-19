"""What happened to Pisa's recommendations (signals S1, S3, S7): the places card shown, the app's actions
on it, a visit within 3 hours (from stays), and the user's critique of a card ("something less touristy").
The night's learning (`pisa.services.learning`) turns them into evidence. Nothing is kept while learning is
paused.

An app action counts only for a card Pisa showed this device with that place on it, once
per card, place and action. A critique is kept only when it reads like one (not a new request), says nothing
sensitive and touches no off-limits topic; forgetting a claim scrubs the critiques that named it."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from pisa.db.settings import get_setting
from pisa.domain.errors import PisaError
from pisa.domain.profile import plain
from pisa.logging import get_logger
from pisa.services.profile import PAUSE_KEY

_log = get_logger("pisa.outcomes")
WENT_WITHIN = timedelta(hours=3)
CRITIQUE_WITHIN = timedelta(minutes=30)  # the user's next words after a card are about it
REASON_MAX = 300
# Words that compare with the card just shown: "più tranquillo", "qualcosa di meno turistico", "another one".
CRITIQUE_MARKERS = frozenset({
    "piu", "meno", "altro", "altra", "altri", "altre", "diverso", "diversa", "invece", "troppo", "troppa",
    "troppi", "troppe", "anziche", "questi", "quelli", "nessuno", "more", "less", "another", "other",
    "different", "instead", "too", "rather", "these", "those", "none", "cheaper", "closer", "quieter",
})  # fmt: skip


def is_critique(words: str) -> bool:
    """The user's words compare with the card just shown, rather than ask for something new."""
    return bool(CRITIQUE_MARKERS & set(plain(words).split()))


async def _paused(conn: AsyncConnection, now: datetime) -> bool:
    value = await get_setting(conn, PAUSE_KEY)
    return bool(value) and datetime.fromisoformat(value) > now


def _uuid(value: str | None, what: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PisaError(
            "validation_failed", f"The {what} must be a uuid.", "Send the id from the card"
        ) from None


async def app_action(
    conn: AsyncConnection, *, card_id: str, place_id: str, action: str, device_id: uuid.UUID, now: datetime
) -> bool:
    """POST /v1/places/outcomes. False when nothing is kept: learning is paused (checked first, so a paused
    log answers alike for any card), or the same action was already reported. 404 unless Pisa showed this
    device that card with that place on it."""
    card, place = _uuid(card_id, "card_id"), _uuid(place_id, "place_id")
    if await _paused(conn, now):
        return False
    shown_here = await (
        await conn.execute(
            "SELECT 1 FROM recommendation_outcomes WHERE action = 'shown' AND card_id = %s "
            "AND device_id = %s AND %s = ANY(shown)",
            (card, device_id, place),
        )
    ).fetchone()
    if shown_here is None:
        raise PisaError(
            "not_found",
            "Pisa showed this device no card with that id and that place.",
            "Report the card_id and place_id of a places card this device received",
        )
    cur = await conn.execute(
        "INSERT INTO recommendation_outcomes (id, card_id, place_id, action, device_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (card_id, place_id, action) WHERE place_id IS NOT NULL "
        "DO NOTHING",
        (uuid.uuid4(), card, place, action, device_id, now),
    )
    _log.info("recommendation_outcome", card_id=card_id, action=action, kept=cur.rowcount == 1)
    return cur.rowcount == 1


async def shown(
    conn: AsyncConnection, *, card_id: uuid.UUID, places: list[str], device_id: uuid.UUID | None, words: str,
    now: datetime, critique_allowed: bool = True,
) -> None:  # fmt: skip
    """A places card went to the phone. When another card was shown in the last 30 minutes and what the user
    said now compares with it, it is their critique of it (S7, kept as data), unless the caller found it
    sensitive or off-limits (`critique_allowed`)."""
    if await _paused(conn, now):
        return
    previous = await (
        await conn.execute(
            "SELECT card_id FROM recommendation_outcomes WHERE action = 'shown' AND created_at > %s "
            "AND device_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT 1",
            (now - CRITIQUE_WITHIN, device_id),
        )
    ).fetchone()
    if previous is not None and critique_allowed and is_critique(words):
        await conn.execute(
            "INSERT INTO recommendation_outcomes (id, card_id, action, reason, device_id, created_at) "
            "VALUES (%s, %s, 'critique', %s, %s, %s)",
            (uuid.uuid4(), previous[0], " ".join(words.split())[:REASON_MAX], device_id, now),
        )
    await conn.execute(
        "INSERT INTO recommendation_outcomes (id, card_id, action, shown, device_id, created_at) "
        "VALUES (%s, %s, 'shown', %s, %s, %s)",
        (uuid.uuid4(), card_id, [uuid.UUID(p) for p in places], device_id, now),
    )


async def visits(conn: AsyncConnection, stays: list[tuple[str, datetime]], now: datetime) -> int:
    """A stay at a place a card showed, starting within 3 hours of the card: "went", once per card and
    place."""
    if not stays or await _paused(conn, now):
        return 0
    made = 0
    for place_id, arrived in stays:
        cur = await conn.execute(
            "INSERT INTO recommendation_outcomes (id, card_id, place_id, action, created_at) "
            "SELECT gen_random_uuid(), o.card_id, %s, 'went', %s FROM recommendation_outcomes o "
            "WHERE o.action = 'shown' AND %s = ANY(o.shown) AND o.created_at <= %s AND o.created_at > %s "
            "ON CONFLICT (card_id, place_id, action) WHERE place_id IS NOT NULL DO NOTHING",
            (place_id, now, place_id, arrived, arrived - WENT_WITHIN),
        )
        made += cur.rowcount
    return made


def view(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}
