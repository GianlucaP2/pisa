"""The gate every unprompted word goes through, and its record.

- `gate` reads the moment (their rules from Settings and from their own words, the calendar around now, the
  phone's last fixes, their last words, the last stay, the kind's decay), asks `guardian.attend` for the
  verdict and keeps it in `guardian_items` (ids, kinds, numbers and reasons; never the words). A verdict that
  lets the item out marks it released at once; a hold is released by `GuardianService.tick`
  (`guardian_tick.py`) or by a re-check when its `not_before` comes.
- The callers: `notifications.Dispatcher.deliver` (every notice, so every sense, the knock, the briefing,
  the rituals, the talk's ask to open and Pisa speaking inside a talk), `ApprovalService._deliver` (the
  approval push), `LiveActivityService.tick` (the lock screen's alert). Nothing else sends a notice or a push.
- No model call; logs carry ids, kinds and numbers only.
"""

import itertools
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.domain import app_events as app_event_rules
from pisa.domain import geo_rules as geo
from pisa.domain import guardian as rules
from pisa.domain import walks as walk_rules
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.domain.settings import is_quiet
from pisa.domain.text import people_name_key
from pisa.logging import get_logger

_log = get_logger("pisa.guardian")
MOVING_LOOK = timedelta(minutes=5)  # the phone's fixes this recent tell whether they are on the move
DECAY_LOOK = 30  # a kind's outcomes read for its decay


async def his_rules(conn: AsyncConnection, settings: dict[str, Any]) -> list[rules.Rule]:
    """Their rules: Settings' first, then the ones they said (`word_rules`)."""
    return rules.settings_rules(settings.get("guardian")) + [rule for _, rule in await word_rules(conn)]


async def word_rules(conn: AsyncConnection) -> list[tuple[uuid.UUID, rules.Rule]]:
    """The rules they said, each with its claim (a claim of their own words, through the memory tools:
    provenance said or confirmed, never one Pisa inferred or observed); forgetting the claim forgets the
    rule."""
    claims = await (
        await conn.execute(
            "SELECT id, statement FROM profile_claims WHERE state <> 'retired' "
            "AND provenance IN ('said', 'confirmed') ORDER BY updated_at DESC LIMIT 200"
        )
    ).fetchall()
    found: list[tuple[uuid.UUID, rules.Rule]] = []
    if claims:
        people = [
            (str(pid), str(key))
            for pid, key in await (
                await conn.execute("SELECT id, name_key FROM people WHERE NOT hidden")
            ).fetchall()
        ]
        for claim_id, statement in claims:
            rule = rules.rule_from_words(str(statement), people)
            if rule is not None:
                found.append((claim_id, rule))
    return found


async def events_near(conn: AsyncConnection, now: datetime) -> list[rules.Event]:
    rows = await (
        await conn.execute(
            "SELECT starts_at, ends_at, title, coalesce(attendees, 0) FROM calendar_events "
            "WHERE status <> 'cancelled' AND NOT all_day AND coalesce(accepted, true) "
            "AND starts_at < %s AND ends_at > %s AND ends_at - starts_at <= %s ORDER BY starts_at",
            (now + timedelta(hours=12), now - timedelta(hours=12), rules.MEETING_MAX),
        )
    ).fetchall()
    return [rules.event(start, end, str(title), int(others)) for start, end, title, others in rows]


async def phone(conn: AsyncConnection, now: datetime) -> rules.Phone:
    """The phone's calls, motion and Focus as of now, by each event's own `at` (a state
    older than `HOLD_MAX` has expired; `rules.phone_state`), from their devices not revoked."""
    rows = await (
        await conn.execute(
            "SELECT e.event, e.site, e.state, e.at FROM device_app_events e "
            "JOIN devices d ON d.id = e.device_id AND d.revoked_at IS NULL "
            "WHERE e.event = ANY(%s) AND e.at > %s AND e.at <= %s ORDER BY e.at DESC LIMIT 200",
            (sorted(app_event_rules.BREAKPOINTS), now - rules.HOLD_MAX, now),
        )
    ).fetchall()
    return rules.phone_state([rules.PhoneEvent(str(e), s, st, at) for e, s, st, at in rows], now)


async def motion(conn: AsyncConnection, now: datetime) -> tuple[bool, bool]:
    """(moving, driving) from the phone's last fixes: on foot or faster in the last pair, faster than a run in
    the last two (`walks.FAST_MPS`); or driving from the phone's own motion state (`activity=automotive`)."""
    rows = await (
        await conn.execute(
            "SELECT lat, lon, accuracy_m, measured_at FROM device_locations WHERE measured_at > %s "
            "AND measured_at <= %s AND accuracy_m <= %s ORDER BY measured_at DESC LIMIT 3",
            (now - MOVING_LOOK, now, geo.PACE_ACCURACY_MAX_M),
        )
    ).fetchall()
    speeds = []
    for (lat_b, lon_b, _, at_b), (lat_a, lon_a, _, at_a) in itertools.pairwise(rows):
        seconds = (at_b - at_a).total_seconds()
        if seconds > 0:
            speeds.append(distance_m((lat_a, lon_a), (lat_b, lon_b)) / seconds)
    moving = bool(speeds) and speeds[0] >= 2 * geo.FOOT_MIN_MPS
    driving = len(speeds) >= 2 and all(s >= walk_rules.FAST_MPS for s in speeds[:2])
    if (await phone(conn, now)).driving is not None:
        return True, True
    return moving, driving


async def _decay(conn: AsyncConnection, kind: str) -> tuple[int, int]:
    outcomes = [
        str(o)
        for (o,) in await (
            await conn.execute(
                "SELECT outcome FROM guardian_items WHERE kind = %s AND outcome IS NOT NULL "
                "ORDER BY outcome_at DESC LIMIT %s",
                (kind, DECAY_LOOK),
            )
        ).fetchall()
    ]
    level = rules.decay_level(outcomes)
    if not level:
        return 0, 0
    row = await (
        await conn.execute(
            "SELECT count(*) FROM guardian_items WHERE kind = %s AND reason LIKE 'decay%%' "
            "AND decided_at > coalesce((SELECT max(decided_at) FROM guardian_items WHERE kind = %s "
            "AND released_at IS NOT NULL AND reason NOT LIKE 'decay%%'), '-infinity')",
            (kind, kind),
        )
    ).fetchone()
    return level, int(row[0]) if row else 0


async def moment_for(
    conn: AsyncConnection, kind: str, now: datetime, owner: Owner, *, talking: bool | None = None,
    settings: dict[str, Any] | None = None, quiet_holds: bool = True,
) -> rules.Moment:  # fmt: skip
    """The moment as the gate sees it (code's facts only). `quiet_holds`: quiet hours are no breakpoint (a
    release, a re-check); a first decision has none of it: what reaches it in quiet hours was let through by
    `enqueue` on purpose (urgent, or its sense checked quiet hours itself)."""
    tz = owner.tz
    settings = settings if settings is not None else await owner.settings(conn)
    events = await events_near(conn, now)
    moving, driving = await motion(conn, now)
    words = await (
        await conn.execute(
            "SELECT max(created_at) FROM messages WHERE role = 'user' AND created_at <= %s", (now,)
        )
    ).fetchone()
    arrived = await (
        await conn.execute("SELECT max(arrived_at) FROM stays WHERE arrived_at <= %s", (now,))
    ).fetchone()
    said = await phone(conn, now)
    found = rules.busy(events, now, driving=driving, talking=bool(talking), phone=said)
    point = rules.at_breakpoint(
        events, now, moving=moving, talking=talking, last_words_at=words[0] if words else None,
        arrived_at=arrived[0] if arrived else None, phone=said,
    )  # fmt: skip
    quiet = quiet_holds and is_quiet(now, settings, tz)
    if quiet:
        point = None  # quiet hours are no breakpoint (floors and their "always" still go)
    level, skipped = await _decay(conn, kind)
    return rules.Moment(
        now, await his_rules(conn, settings), found, point, rules.next_check(events, now, found), level,
        skipped, rules.next_digest(now, tz), quiet,
    )  # fmt: skip


async def record(
    conn: AsyncConnection, ref: str, item: rules.Item, verdict: rules.Verdict, now: datetime
) -> None:
    """The record: the first verdict of an item is kept; a later one only releases it."""
    row = await (
        await conn.execute(
            "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, override, "
            "rule_source, counted, decided_at, released_at, released_how) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (ref) DO NOTHING RETURNING id",
            (
                uuid.uuid4(), ref, item.path.name, item.kind, verdict.decision, verdict.reason[:60],
                Jsonb(verdict.score.parts()), verdict.override,
                verdict.rule.source if verdict.rule is not None else None,
                rules.counted(verdict.reason, item.kind), now,
                now if verdict.released else None, verdict.reason[:60] if verdict.released else None,
            ),
        )
    ).fetchone()  # fmt: skip
    if row is None and verdict.released:
        await release(conn, ref, verdict.reason, now)
    if verdict.override is not None:
        _log.info("guardian_override", ref=ref, kind=item.kind, override=verdict.override)
    _log.info(
        "guardian_released" if verdict.released else "guardian_held",
        ref=ref, kind=item.kind, decision=verdict.decision, reason=verdict.reason, score=verdict.score.total,
    )  # fmt: skip


async def release(conn: AsyncConnection, ref: str, how: str, now: datetime) -> None:
    await conn.execute(
        "UPDATE guardian_items SET released_at = %s, released_how = %s "
        "WHERE ref = %s AND released_at IS NULL",
        (now, how[:60], ref),
    )


async def delivered(conn: AsyncConnection, ref: str, via: str, now: datetime) -> None:
    """An item is out only when the dispatcher delivered it (`ws`, `apns`); one it dropped
    (expired, its offer lapsed, no channel) never got out: the neglect report's."""
    if via in ("ws", "apns"):
        await conn.execute(
            "UPDATE guardian_items SET released_at = coalesce(released_at, %s) WHERE ref = %s", (now, ref)
        )
    else:
        await conn.execute(
            "UPDATE guardian_items SET released_at = NULL, lapsed_at = coalesce(lapsed_at, %s) "
            "WHERE ref = %s",
            (now, ref),
        )


async def gate(
    conn: AsyncConnection, item: rules.Item, ref: str, now: datetime, owner: Owner, *,
    talking: bool | None = None, keep: bool = True, recheck: bool = False,
) -> rules.Verdict:  # fmt: skip
    """The one gate: the verdict for this item now, kept in the record (`keep` false: a silent state push,
    decided and never counted). `talking`: the live sockets' word where known (the dispatcher's)."""
    moment = await moment_for(conn, item.kind, now, owner, talking=talking, quiet_holds=recheck)
    verdict = rules.attend(item, moment)
    if keep:
        await record(conn, ref, item, verdict, now)
    return verdict


async def person_of(conn: AsyncConnection, emails: Sequence[str]) -> uuid.UUID | None:
    """Which of their people these addresses are (a mail's sender, an event's guests)."""
    wanted = sorted({e.strip().lower() for e in emails if e and "@" in e})
    if not wanted:
        return None
    row = await (
        await conn.execute(
            "SELECT id FROM people WHERE NOT hidden AND emails && %s::text[] ORDER BY created_at LIMIT 1",
            (wanted,),
        )
    ).fetchone()
    return row[0] if row else None


async def person_named(conn: AsyncConnection, name: str) -> uuid.UUID | None:
    """One of their people by the name Pisa wrote ("Marta", "Marta Rossi"), as `rule_from_words` reads
    names."""
    key = people_name_key(name or "")
    if not key:
        return None
    row = await (
        await conn.execute(
            "SELECT id FROM people WHERE NOT hidden AND (name_key = %s OR split_part(name_key, ' ', 1) = %s) "
            "ORDER BY created_at LIMIT 1",
            (key, key),
        )
    ).fetchone()
    return row[0] if row else None


async def event_person(conn: AsyncConnection, event_id: str) -> uuid.UUID | None:
    """One of their people among an event's guests."""
    row = await (
        await conn.execute(
            "SELECT guests FROM calendar_events WHERE id = %s AND guests IS NOT NULL LIMIT 1", (event_id,)
        )
    ).fetchone()
    guests = row[0] if row else None
    emails = [str(g.get("email") or "") for g in guests or [] if isinstance(g, dict)]
    return await person_of(conn, emails)


def always(his: Sequence[rules.Rule], path: str, person_id: str | None) -> bool:
    """Their "always" names this item (attend's `at`, fold and drop give way to it)."""
    found, outside = rules.path_of(path)
    rule = rules.rule_for(rules.Item("-", found, None, False, person_id, outside), his)
    return rule is not None and rule.mode == "always"


async def ask_deadline(conn: AsyncConnection, ask_id: uuid.UUID | None) -> datetime | None:
    if ask_id is None:
        return None
    row = await (await conn.execute("SELECT expires_at FROM ritual_asks WHERE id = %s", (ask_id,))).fetchone()
    return row[0] if row else None


def notice_item(
    notification_id: uuid.UUID, path: str | None, *, floor: bool, deadline: datetime | None,
    person_id: uuid.UUID | None, kind: str | None = None,
) -> rules.Item:  # fmt: skip
    found, outside = rules.path_of(path, kind)
    return rules.Item(
        str(notification_id),
        found,
        deadline,
        floor,
        str(person_id) if person_id else None,
        outside,
        "shown" in rules.path_marks(path),
    )
