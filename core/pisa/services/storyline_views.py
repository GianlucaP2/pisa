"""What reads the storylines: the cards the chat model reads and the morning's diff (LIFE-GRAPH section 3).

Both are built in code from the replay (`storylines.read`): no model call, nothing written. The cards are
Pisa's own list, but the other side's words in them (a quote, a name, a key) are returned apart
(`Cards.foreign`), so the day context that holds them counts as outside text exactly as an email-made task
title does (`AssistantDataService.foreign_titles`, `foreign_in`).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from pisa.domain import cards as rules
from pisa.domain.cards import Card, CardFacts, Cards, Move
from pisa.domain.playbooks import ALL_TERMINAL, Ev, replay
from pisa.services import storylines
from pisa.services.storylines import OPEN_DAYS, Read

REF_CHARS = 6  # a card's ref is "s" and this many characters of the storyline's id
RECENT_DAYS = 14  # a storyline that waits on someone else is on a card while it moved this recently


def ref_of(storyline_id: str) -> str:
    return "s" + storyline_id.replace("-", "")[:REF_CHARS]


def _day(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{local.day} {local:%b}"


async def key_of(conn: AsyncConnection, entity_id: str | None) -> str | None:
    """The storyline's own key as the mail wrote it: an order, booking or invoice number, else a parcel's."""
    if entity_id is None:
        return None
    row = await (
        await conn.execute(
            "SELECT verbatim_source FROM entity_key WHERE entity_id = %s ORDER BY "
            "(key_kind = ANY(%s)) DESC, created_at LIMIT 1",
            (entity_id, list(storylines.SOURCE_REF_KINDS)),
        )
    ).fetchone()
    return str(row[0]) if row else None


async def facts_of(conn: AsyncConnection, got: Read, tz: ZoneInfo) -> CardFacts:
    """What a card says about a storyline, from its replay."""
    pl, rep = got.plan, got.replay
    who = await storylines.opener(conn, got.storyline_id)
    task = next((a for a in pl.actions if a.status == "task" and a.kind == pl.next_action), None) or next(
        (a for a in pl.actions if a.status == "task"), None
    )
    dates: list[str] = []
    for a in pl.actions:
        if a.after is not None:
            dates.append(f"check not before {_day(a.after, tz)}")
        if a.before is not None:
            dates.append(f"window closes {_day(a.before, tz)}")
    if rep.last_when is not None:
        dates.append(f"appointment {_day(rep.last_when, tz)} {rep.last_when.astimezone(tz):%H:%M}")
    shown = rep.steps[-1].event_id if rep.steps else (got.events[-1].id if got.events else None)
    row = got.rows.get(shown or "")
    last = max((e.at for e in got.events), default=None)
    by_owner = bool(row and (row.get("actor") == "owner" or row.get("owner_authored")))
    return CardFacts(
        ref=ref_of(got.storyline_id),
        playbook=got.playbook,
        state=rep.state or "open",
        waiting_on=pl.waiting_on,
        next_task=task.key if task else None,
        who=who,
        key=await key_of(conn, got.entity_id),
        last_day=_day(last, tz) if last else "-",
        dates=tuple(dates),
        quote=(row or {}).get("quote"),
        quote_by="the user"
        if by_owner
        else (row or {}).get("from_name") or (row or {}).get("from_address") or who,
        quote_day=_day(row["at"], tz) if row and row.get("at") else None,
        quote_is_strangers=not by_owner,
    )


async def build_cards(
    conn: AsyncConnection, now: datetime, tz: ZoneInfo, cap: int = rules.CARDS_MAX
) -> Cards:
    """The cards for the open storylines that need the user or moved lately, the user's own moves first, at
    most `cap`, each cut to its limit. Nothing is written."""
    rows = await (
        await conn.execute(
            "SELECT id::text FROM storyline WHERE state IN ('open', 'waiting') AND updated_at > %s "
            "AND (next_actor = 'owner' OR updated_at > %s) "
            "ORDER BY (next_actor = 'owner') DESC, updated_at DESC, id LIMIT %s",
            (now - timedelta(days=OPEN_DAYS), now - timedelta(days=RECENT_DAYS), cap),
        )
    ).fetchall()
    out: list[Card] = []
    for (sid,) in rows:
        got = await storylines.read(conn, sid)
        if got is None or not got.events:
            continue
        out.append(rules.render(await facts_of(conn, got, tz)))
    return Cards(out)


# ---- the daily diff ----------------------------------------------------------------------------------------


async def daily_diff(conn: AsyncConnection, since: datetime, tz: ZoneInfo) -> list[Move]:
    """What moved in the storylines since `since` (when Pisa learned it, not when it happened): a storyline
    opened, a state changed, one closed, a task made or closed by its evidence, a closing held for the
    user's yes. Undone events don't count. Each line has its evidence: the last step's quote, who wrote it,
    the day."""
    ids = [
        r[0]
        for r in await (
            await conn.execute(
                "SELECT DISTINCT se.storyline_id::text FROM storyline_event se WHERE se.created_at > %s "
                "AND se.undone_at IS NULL "
                "UNION SELECT subject_id FROM assertion WHERE subject_kind = 'storyline' "
                "AND predicate = 'proposed_transition' AND valid_to IS NULL AND recorded_at > %s "
                "UNION SELECT e.dst_id FROM task_evidence te JOIN life_edge e ON e.src_kind = 'task' "
                "AND e.src_id = te.task_id::text AND e.dst_kind = 'storyline' "
                "AND e.relation = 'projection_of' "
                "WHERE te.created_at > %s AND te.undone_at IS NULL",
                (since, since, since),
            )
        ).fetchall()
    ]
    states: list[Move] = []
    todo: list[Move] = []
    yes: list[Move] = []
    for sid in sorted(ids):
        got = await storylines.read(conn, sid)
        if got is None or not got.events:
            continue
        who = await storylines.opener(conn, sid)
        key = await key_of(conn, got.entity_id)
        before_events: list[Ev] = [e for e in got.events if got.rows[e.id]["created"] <= since]
        before = replay(got.playbook, before_events).state if before_events else None
        after = got.replay.state
        if after is not None and after != before:
            step = got.replay.steps[-1] if got.replay.steps else None
            row = got.rows.get(step.event_id) if step else None
            by_owner = bool(row and (row.get("actor") == "owner" or row.get("owner_authored")))
            kind = "new" if not before_events else ("closed" if after in ALL_TERMINAL else "moved")
            states.append(
                Move(
                    kind,
                    got.playbook,
                    who,
                    key,
                    before=before,
                    after=after,
                    evidence=(row or {}).get("quote"),
                    by="the user"
                    if by_owner
                    else (row or {}).get("from_name") or (row or {}).get("from_address") or who,
                    day=_day(row["at"], tz) if row and row.get("at") else None,
                )
            )
        for held in got.replay.held:
            exists = await (
                await conn.execute(
                    "SELECT 1 FROM assertion WHERE subject_kind = 'storyline' AND subject_id = %s "
                    "AND predicate = 'proposed_transition' AND valid_to IS NULL AND recorded_at > %s "
                    "AND value->>'to' = %s",
                    (sid, since, held.to),
                )
            ).fetchone()
            if exists:
                yes.append(Move("yes", got.playbook, who, key, held=held.to))
        made = await (
            await conn.execute(
                "SELECT t.intent, te.transition FROM task_evidence te JOIN tasks t ON t.id = te.task_id "
                "JOIN life_edge e ON e.src_kind = 'task' AND e.src_id = t.id::text "
                "AND e.dst_kind = 'storyline' "
                "AND e.dst_id = %s AND e.relation = 'projection_of' WHERE te.created_at > %s "
                "AND te.undone_at IS NULL AND te.transition IN ('created', 'done') AND t.intent IS NOT NULL "
                "ORDER BY te.created_at",
                (sid, since),
            )
        ).fetchall()
        for intent, transition in made:
            todo.append(
                Move("task" if transition == "created" else "done", got.playbook, who, key, task=intent)
            )
    return [*states, *todo, *yes]
