"""The owner's chat moves storylines (LIFE-GRAPH section 7: chat episodes).

"Ho già spedito" and "l'ho pagato" are events from the owner, as a courier's mail is one from the courier.
This writes one as a `storyline_event` with no message and a reference to the chat turn; the evidence is the
owner's own words of that turn (an assertion `chat_evidence`, source chat), and the storyline is replayed the
same way mail moves it. Nothing else changes the engine: the same rules close nothing without an exact key,
and here the owner's naming of the storyline is the exact key (`domain/chat_events.names`).

Code decides, never the model (`progress`):
- the quote is the owner's own words of this turn and says the kind (`chat_events.in_words`, `says`);
- the storyline is one that the event would move; when more than one would move and the words name none of
  them, nothing is written (the user is asked);
- a closing the words don't name stays a proposal (the engine holds it), as any closing without an exact key;
- an undo (`kind` "undo", the owner's words taking it back) undoes the last chat event of that storyline.
Nothing here reads the clock to close anything. Run it inside the caller's transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.domain import chat_events, linking
from pisa.domain.cards import state_word
from pisa.domain.playbooks import ACCEPTS, Ev, replay
from pisa.services import storyline_views, storylines
from pisa.services.storylines import OwnerContext, Read

OPEN_MAX = 30  # open storylines looked at for one event


@dataclass(frozen=True)
class Progress:
    """What the tool says. `done`: an event was written (or taken back)."""

    done: bool
    text: str
    event_id: str | None = None


def _no(text: str) -> Progress:
    return Progress(False, text)


async def _open(conn: AsyncConnection) -> list[Read]:
    rows = await (
        await conn.execute(
            "SELECT id::text FROM storyline WHERE state IN ('open', 'waiting') "
            "ORDER BY updated_at DESC, id LIMIT %s",
            (OPEN_MAX,),
        )
    ).fetchall()
    out: list[Read] = []
    for (sid,) in rows:
        got = await storylines.read(conn, sid)
        if got is not None and got.events:
            out.append(got)
    return out


async def _keys(conn: AsyncConnection, entity_id: str | None) -> list[str]:
    if entity_id is None:
        return []
    rows = await (
        await conn.execute(
            "SELECT verbatim_source, value FROM entity_key WHERE entity_id = %s", (uuid.UUID(entity_id),)
        )
    ).fetchall()
    return [str(x) for row in rows for x in row if x]


async def _names_of_other_side(conn: AsyncConnection, storyline_id: str) -> list[str]:
    rows = await (
        await conn.execute(
            "SELECT DISTINCT m.from_name, m.from_address FROM storyline_event se "
            "JOIN email_messages m ON m.id = se.message_id WHERE se.storyline_id = %s "
            "AND se.undone_at IS NULL AND NOT m.owner_authored",
            (uuid.UUID(storyline_id),),
        )
    ).fetchall()
    out: list[str] = []
    for name, address in rows:
        if name:
            out.append(str(name))
        if address and (brand := linking.brand_of(address)):
            out.append(brand)
    return out


def _moves(got: Read, kind: str, now: datetime) -> bool:
    """The owner's event would change this storyline: its state, what is held for his yes, a payment made, a
    request settled."""
    if kind not in ACCEPTS[got.playbook]:
        return False
    new = Ev("chat-new", kind, now, actor="owner", debtor="none", hard=True)
    after = replay(got.playbook, [*got.events, new])
    before = got.replay
    return (
        after.state != before.state
        or after.paid != before.paid
        or after.request_open != before.request_open
        or len(after.held) != len(before.held)
    )


async def resolve(conn: AsyncConnection, ref: str, open_: list[Read]) -> Read | None:
    """The open storyline a card's ref or a key names: `s` and the start of its id, or one of its keys as
    the mail wrote it (case aside). None when none or more than one does."""
    text = ref.strip().strip("[]«»\"'` ")
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("s") and len(lowered) > 3:
        hits = [g for g in open_ if g.storyline_id.replace("-", "").startswith(lowered[1:])]
        if hits:
            return hits[0] if len(hits) == 1 else None
    found: list[Read] = []
    for g in open_:
        if lowered in [k.lower() for k in await _keys(conn, g.entity_id)]:
            found.append(g)
    return found[0] if len(found) == 1 else None


async def progress(
    conn: AsyncConnection,
    *,
    ref: str,
    kind: str,
    quote: str,
    words: str,
    turn: str,
    now: datetime,
    owner: OwnerContext,
) -> Progress:
    """Record what the owner said happened (or take it back), or say why not. `words`: the owner's words this
    turn; `turn`: the chat turn's reference; `ref`: a card's ref or a key."""
    if not chat_events.in_words(quote, words):
        return _no("Not recorded: the quote is not the user's own words this turn.")
    open_ = await _open(conn)
    chosen = await resolve(conn, ref, open_)
    if chosen is None:
        return _no("Not recorded: no one open storyline has that ref or key. Use a ref from the cards.")
    if kind == chat_events.UNDO:
        return await _undo(conn, chosen, words, now, owner)
    if kind not in chat_events.CHAT_KINDS:
        return _no("Not recorded: that is not something the user's words can say about a storyline.")
    if not chat_events.says(kind, quote, words):
        return _no("Not recorded: the user's words don't say it happened (a question, a plan or a no).")
    movers = [g for g in open_ if _moves(g, kind, now)]
    if chosen not in movers:
        return _no(f"Not recorded: it changes nothing in [{storyline_views.ref_of(chosen.storyline_id)}].")
    same = [g for g in open_ if g.playbook == chosen.playbook]
    named = chat_events.names(
        words,
        keys=await _keys(conn, chosen.entity_id),
        who=await _names_of_other_side(conn, chosen.storyline_id),
        playbook=chosen.playbook,
        only_of_kind=len(same) == 1,
    )
    if len(movers) > 1 and not named.exact:
        refs = ", ".join(f"[{storyline_views.ref_of(g.storyline_id)}]" for g in movers)
        return _no(f"Not recorded: more than one storyline could be this ({refs}). Ask the user which one.")
    row = await (
        await conn.execute(
            "INSERT INTO storyline_event (storyline_id, kind, message_id, mail_event_id, occurred_at, "
            "confidence) VALUES (%s, %s, NULL, NULL, %s, 1.0) RETURNING id::text",
            (uuid.UUID(chosen.storyline_id), kind, now),
        )
    ).fetchone()
    assert row is not None
    event_id = row[0]
    await conn.execute(
        "INSERT INTO assertion (subject_kind, subject_id, predicate, value, valid_from, provenance, "
        "confidence, source, source_ref, quote) VALUES ('storyline_event', %s, 'chat_evidence', %s, %s, "
        "'owner', 1.0, 'chat', %s, %s)",
        (
            event_id,
            Jsonb({"turn": turn, "kind": kind, "hard": named.exact, "named": named.how, "debtor": "none"}),
            now,
            turn,
            quote,
        ),
    )
    done = await storylines.recompute(conn, chosen.storyline_id, now, owner)
    assert done is not None
    rep = done[0]
    ref_text = f"[{storyline_views.ref_of(chosen.storyline_id)}]"
    if any(h.event_id == event_id for h in rep.held):
        return Progress(
            True,
            f"Recorded on {ref_text}: {kind}. It would close the storyline, but the user's words don't name "
            "it, so it stays a proposal: ask them to confirm which one they mean.",
            event_id,
        )
    return Progress(
        True,
        f"Recorded on {ref_text}: {kind}. Now: {state_word(rep.state or 'open', False)}. "
        'To take it back, call storyline_progress with kind "undo" when the user says so.',
        event_id,
    )


async def _undo(
    conn: AsyncConnection, chosen: Read, words: str, now: datetime, owner: OwnerContext
) -> Progress:
    if not chat_events.undoes(words):
        return _no("Not undone: the user's words this turn don't take anything back.")
    row = await (
        await conn.execute(
            "SELECT se.id::text FROM storyline_event se WHERE se.storyline_id = %s AND se.message_id IS NULL "
            "AND se.undone_at IS NULL ORDER BY se.created_at DESC, se.id DESC LIMIT 1",
            (uuid.UUID(chosen.storyline_id),),
        )
    ).fetchone()
    if row is None:
        return _no("Not undone: the user has said nothing about that storyline in chat.")
    await storylines.undo_event(conn, row[0], now, owner, "owner")
    return Progress(True, f"Taken back on [{storyline_views.ref_of(chosen.storyline_id)}].", row[0])
