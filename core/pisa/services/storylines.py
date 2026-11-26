"""Storylines: the linker, the engine and the tasks that project from them (LIFE-GRAPH sections 2 and 4).

One message at a time, in this order:
1. `store`: its events (what the events call said, checked in code) go to `mail_event`;
2. `link`: its keys go to `entity`/`entity_key` (kept only as the message wrote them), its sender to an org;
   an exact key found on an entity is a **hard** edge, the same thread and subject a **structural** edge;
   anything softer is a `life_edge` in state `proposed` and never acts;
3. `attach`: each event joins the storyline its links point to, or opens one (only an event that may start
   a playbook does), as a `storyline_event` with the message as its evidence;
4. `recompute`: the storyline's events are replayed through the playbook (`domain/playbooks.py`), in the
   order they happened, never the order they arrived. The state, the next action and the effects are written
   as assertions (with two time axes, nothing erased); a transition the rules held back (no exact key, an
   injected message) is written as a proposed transition;
5. the tasks: a pending owner action that the playbook says is a task becomes a `tasks` row (entity, intent,
   state); what is no longer pending closes or waits, with a `task_evidence` row. Everything else is a
   proposal. `undo` reverses an event or a task change; the owner's undo stays undone.

Retention: the quotes are on `mail_event`, where the 30-day purge reaches them; ids and keys stay as long as
a task needs them. Nothing here reads the clock to close anything.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pisa.domain import debtor_lint, linking, mail_events, task_machine
from pisa.domain.mail_events import Key, Understood
from pisa.domain.owner import Owner
from pisa.domain.playbooks import (
    ACCEPTS,
    KEYLESS,
    PLAYBOOKS,
    START_EVENTS,
    TERMINAL,
    Ev,
    Plan,
    Replay,
    plan,
    replay,
    task_title,
)
from pisa.domain.tasks import end_of_day
from pisa.logging import get_logger

_log = get_logger("pisa.storylines")
OPEN_DAYS = 45  # a storyline with no event this long is not a candidate for a new message
_ENTITY_KIND = {
    "order_id": "order",
    "tracking": "parcel",
    "booking_ref": "booking",
    "invoice_no": "invoice",
    "other": "other",
}
_PLAYBOOK_ENTITY = {
    "marketplace_sale": "order",
    "purchase_delivery": "order",
    "refund_support": "case",
    "booking_appointment": "appointment",
    "conversation_request": "case",
}
LABEL_MAX = 120
SOURCE_REF_KINDS = ("order_id", "booking_ref", "invoice_no")


@dataclass(frozen=True)
class Msg:
    """A message as the linker reads it."""

    id: str
    thread_id: str
    from_name: str | None
    from_address: str | None
    subject: str
    sent_at: datetime
    owner_authored: bool

    @property
    def brand(self) -> str:
        return linking.brand_of(self.from_address)

    @property
    def sender(self) -> str:
        return (self.from_name or self.from_address or "").strip()


@dataclass
class Outcome:
    """What one message did, for the logs and the tests: counts and ids, never text."""

    events: int = 0
    storylines: list[str] = field(default_factory=list)
    tasks_created: int = 0
    tasks_changed: int = 0
    proposals: int = 0
    risks: int = 0
    task_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OwnerContext:
    """What the engine needs to know about the owner to write his tasks."""

    names: tuple[str, ...] = ()  # display-name variants and address local parts, for the debtor lint
    tz: str = "UTC"
    italian: bool = False


async def owner_context(conn: AsyncConnection, owner: Owner) -> OwnerContext:
    """What the engine needs to know about the owner: the names his addresses and his display names give (the
    debtor lint), his zone and whether his language is Italian."""
    settings = await owner.settings(conn)
    lang = str(settings["language"])
    names: set[str] = set()
    rows = await (
        await conn.execute(
            "SELECT a.account, i.alias, i.display_name FROM connector_accounts a "
            "LEFT JOIN owner_identity i ON i.account = lower(a.account) WHERE a.connector = 'google'"
        )
    ).fetchall()
    for account, alias, display in rows:
        for address in (account, alias):
            if address and "@" in address:
                local = address.split("@", 1)[0]
                names |= {local, " ".join(local.replace(".", " ").replace("_", " ").split())}
        if display:
            names.add(display)
    return OwnerContext(
        names=tuple(sorted(n for n in names if len(n) >= 2)), tz=owner.tz, italian=lang.startswith("it")
    )


# ---- small writers -----------------------------------------------------------------------------------------


async def _edge(
    conn: AsyncConnection,
    grade: str,
    src: tuple[str, str],
    dst: tuple[str, str],
    relation: str,
    *,
    state: str = "accepted",
    reason: str | None = None,
    confidence: float | None = None,
    decided_by: str = "code",
) -> None:
    await conn.execute(
        "INSERT INTO life_edge (grade, state, src_kind, src_id, dst_kind, dst_id, relation, reason, "
        "confidence, decided_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (grade, state, src[0], src[1], dst[0], dst[1], relation, reason, confidence, decided_by),
    )


async def _assert(
    conn: AsyncConnection,
    subject: tuple[str, str],
    predicate: str,
    value: dict[str, Any],
    *,
    valid_from: datetime,
    provenance: str,
    source_ref: str | None,
    quote: str | None = None,
    confidence: float = 0.9,
) -> None:
    await conn.execute(
        "INSERT INTO assertion (subject_kind, subject_id, predicate, value, valid_from, provenance, "
        "confidence, source, source_ref, quote) VALUES (%s, %s, %s, %s, %s, %s, %s, 'mail', %s, %s)",
        (
            subject[0],
            subject[1],
            predicate,
            Jsonb(value),
            valid_from,
            provenance,
            confidence,
            source_ref,
            quote,
        ),
    )


# ---- 1. store ----------------------------------------------------------------------------------------------


async def store(conn: AsyncConnection, msg: Msg, found: Understood) -> list[tuple[str, mail_events.Event]]:
    """The message's events as `mail_event` rows: (row id, event). Once per message: a message that has
    events is not stored again."""
    row = await (
        await conn.execute("SELECT 1 FROM mail_event WHERE message_id = %s LIMIT 1", (msg.id,))
    ).fetchone()
    if row is not None:
        return []
    stored: list[tuple[str, mail_events.Event]] = []
    for ev in found.events:
        got = await (
            await conn.execute(
                "INSERT INTO mail_event (message_id, thread_id, kind, actor, debtor, occurred_at, quote) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
                (msg.id, msg.thread_id, ev.kind, ev.actor, ev.debtor, ev.occurred_at, ev.quote),
            )
        ).fetchone()
        assert got is not None
        stored.append((got[0], ev))
    return stored


# ---- 2. link -----------------------------------------------------------------------------------------------


async def _org(conn: AsyncConnection, msg: Msg) -> str | None:
    """The sender's org (a merchant, a carrier, a clinic), made once per brand: sender plus org is a
    structural edge. A person on a shared mail provider has no brand and no org."""
    brand = msg.brand
    if not brand or msg.owner_authored:
        return None
    domain = (msg.from_address or "").rsplit("@", 1)[-1].lower()
    row = await (
        await conn.execute("SELECT id::text FROM orgs WHERE %s = ANY(domains) LIMIT 1", (domain,))
    ).fetchone()
    if row is None:
        row = await (
            await conn.execute(
                "INSERT INTO orgs (name, domains) VALUES (%s, %s) RETURNING id::text", (brand, [domain])
            )
        ).fetchone()
    assert row is not None
    await _edge(conn, "structural", ("message", msg.id), ("org", row[0]), "sent_by")
    return row[0]


def _key_ok(kind: str, issuer: str, other: str) -> bool:
    """A held key is this key's: a tracking number names its parcel whoever wrote it; any other number only
    for a compatible issuer (the merchant's own other address)."""
    return kind == "tracking" or linking.compatible(issuer, other) or (not issuer and not other)


async def _entities_of(
    conn: AsyncConnection, keys: Sequence[Key], issuer: str, thread_id: str
) -> dict[str, list[Key]]:
    """The entities that already hold one of these keys: the same kind, the same value, a compatible issuer.
    A key the owner wrote himself (no issuer yet) is claimed by the reply in its own thread."""
    found: dict[str, list[Key]] = {}
    for key in keys:
        rows = await (
            await conn.execute(
                "SELECT k.entity_id::text, k.issuer, m.thread_id FROM entity_key k "
                "LEFT JOIN email_messages m ON m.id = k.message_id WHERE k.key_kind = %s AND k.value = %s",
                (key.kind, key.value),
            )
        ).fetchall()
        issuers: dict[str, set[str]] = {}
        for entity_id, held, _ in rows:
            issuers.setdefault(entity_id, set()).add(held)
        for entity_id, held, held_thread in rows:
            # a key the owner wrote has no issuer: the reply in its own thread claims it, and so does the
            # first issuer's mail anywhere (a shop's reply that quotes nothing), until an issuer holds it;
            # and what he writes names the thing whoever issued its key
            unclaimed = not held and not any(issuers[entity_id])
            if (
                _key_ok(key.kind, issuer, held)
                or (not held and held_thread == thread_id)
                or unclaimed
                or not issuer
            ):
                found.setdefault(entity_id, []).append(key)
    return found


async def _has_storyline(conn: AsyncConnection, entity_id: str) -> bool:
    row = await (
        await conn.execute("SELECT 1 FROM storyline WHERE entity_id = %s LIMIT 1", (entity_id,))
    ).fetchone()
    return row is not None


async def _merge(conn: AsyncConnection, keep: str, drop: str) -> None:
    """An entity no storyline holds yet (a courier's mail read first) joins the one it turned out to be."""
    await conn.execute(
        "DELETE FROM entity_key d USING entity_key k WHERE d.entity_id = %s AND k.entity_id = %s "
        "AND k.key_kind = d.key_kind AND k.issuer = d.issuer AND k.value = d.value",
        (drop, keep),
    )
    await conn.execute("UPDATE entity_key SET entity_id = %s WHERE entity_id = %s", (keep, drop))
    await conn.execute("UPDATE mail_event SET entity_id = %s WHERE entity_id = %s", (keep, drop))
    await conn.execute("UPDATE tasks SET entity_id = %s WHERE entity_id = %s", (keep, drop))
    await conn.execute(
        "UPDATE life_edge SET dst_id = %s WHERE dst_kind = 'entity' AND dst_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM life_edge o WHERE o.src_kind = life_edge.src_kind "
        "AND o.src_id = life_edge.src_id AND o.dst_kind = 'entity' AND o.dst_id = %s "
        "AND o.relation = life_edge.relation)",
        (keep, drop, keep),
    )
    await conn.execute("DELETE FROM life_edge WHERE dst_kind = 'entity' AND dst_id = %s", (drop,))
    await conn.execute("DELETE FROM entity WHERE id = %s", (drop,))


async def link(
    conn: AsyncConnection,
    msg: Msg,
    found: Understood,
    stored: Sequence[tuple[str, mail_events.Event]],
) -> str | None:
    """The entity this message is about, by its keys: the hard edge. Keys are kept as the message wrote them.
    None when it carries no key, or its keys name two things that each have a storyline (then no hard edge:
    a soft one proposes, and nothing acts on it)."""
    await _org(conn, msg)
    if not found.keys:
        return None
    issuer = "" if msg.owner_authored else msg.brand  # what the owner wrote has no issuer: it is his
    held = await _entities_of(conn, found.keys, issuer, msg.thread_id)
    entity_id: str | None = None
    if len(held) == 1:
        entity_id = next(iter(held))
    elif len(held) > 1:
        with_story = [e for e in held if await _has_storyline(conn, e)]
        if len(with_story) > 1:
            first, *rest = sorted(with_story)
            for other in rest:
                await _edge(
                    conn,
                    "soft",
                    ("entity", first),
                    ("entity", other),
                    "same_thing",
                    state="proposed",
                    reason="one message names both",
                    confidence=0.3,
                    decided_by="code",
                )
            return None
        entity_id = with_story[0] if with_story else sorted(held)[0]
        for other in held:
            if other != entity_id and not await _has_storyline(conn, other):
                await _merge(conn, entity_id, other)
    if entity_id is None:
        if not stored:
            return None  # no event, no thing: a newsletter's order number makes no entity
        kind = _ENTITY_KIND.get(found.keys[0].kind, "other")
        row = await (
            await conn.execute(
                "INSERT INTO entity (kind, issuer) VALUES (%s, %s) RETURNING id::text", (kind, issuer)
            )
        ).fetchone()
        assert row is not None
        entity_id = row[0]
    assert entity_id is not None
    for key in found.keys:
        await _write_key(conn, entity_id, key, issuer, msg.id)
    await _edge(
        conn,
        "hard",
        ("message", msg.id),
        ("entity", entity_id),
        "names",
        confidence=1.0,
        reason="the message names a key the entity holds",
    )
    if stored:
        await conn.execute(
            "UPDATE mail_event SET entity_id = %s WHERE id = ANY(%s::uuid[])",
            (entity_id, [i for i, _ in stored]),
        )
    return entity_id


async def _write_key(conn: AsyncConnection, entity_id: str, key: Key, issuer: str, message_id: str) -> None:
    """A key the entity does not hold yet (no compatible issuer holds it)."""
    rows = await (
        await conn.execute(
            "SELECT issuer FROM entity_key WHERE entity_id = %s AND key_kind = %s AND value = %s",
            (entity_id, key.kind, key.value),
        )
    ).fetchall()
    if any(_key_ok(key.kind, issuer, held) for (held,) in rows):
        return
    await conn.execute(
        "INSERT INTO entity_key (entity_id, key_kind, issuer, value, verbatim_source, message_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (entity_id, key.kind, issuer, key.value, key.verbatim, message_id),
    )


# ---- 3. attach ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cand:
    id: str
    playbook: str
    state: str
    entity_id: str | None
    via: str  # key | thread


async def _storylines_of(conn: AsyncConnection, entity_id: str | None, msg: Msg) -> list[_Cand]:
    """The storylines this message may join: the entity's (a hard link), and those of its thread with the same
    subject (a structural one). Old, quiet ones don't count."""
    out: dict[str, _Cand] = {}
    horizon = msg.sent_at - timedelta(days=OPEN_DAYS)
    if entity_id is not None:
        rows = await (
            await conn.execute(
                "SELECT s.id::text, s.kind, s.entity_id::text, s.state FROM storyline s "
                "WHERE s.entity_id = %s ORDER BY s.created_at",
                (entity_id,),
            )
        ).fetchall()
        for sid, kind, ent, st in rows:
            out[sid] = _Cand(sid, kind, st, ent, "key")
    rows = await (
        await conn.execute(
            "SELECT DISTINCT s.id::text, s.kind, s.entity_id::text, s.state, m.subject FROM storyline s "
            "JOIN storyline_event se ON se.storyline_id = s.id AND se.undone_at IS NULL "
            "JOIN email_messages m ON m.id = se.message_id WHERE m.thread_id = %s AND m.id <> %s "
            "AND s.updated_at > %s",
            (msg.thread_id, msg.id, horizon),
        )
    ).fetchall()
    mine = linking.norm_subject(msg.subject)
    for sid, kind, ent, st, subject in rows:
        if sid not in out and linking.norm_subject(subject) == mine:
            out[sid] = _Cand(sid, kind, st, ent, "thread")
    return list(out.values())


_START_ANY = frozenset().union(*START_EVENTS.values())


def _playbook_for(ev: mail_events.Event) -> str | None:
    """The playbook a starting event opens: the model's pick when the event may start it, else the one the
    kind says. A request to the owner opens a conversation; an order opens a sale when the owner owes the
    next move (he must ship) and a purchase when the shop does."""
    if ev.playbook in PLAYBOOKS and ev.kind in START_EVENTS[ev.playbook]:
        return ev.playbook
    if ev.kind == "order_placed":
        return "marketplace_sale" if ev.debtor == "owner" else "purchase_delivery"
    for pb in PLAYBOOKS:
        if ev.kind in START_EVENTS[pb]:
            return pb
    return None


def _pick(ev: mail_events.Event, cands: Sequence[_Cand]) -> _Cand | None:
    usable = [c for c in cands if ev.kind in ACCEPTS[c.playbook]]
    if not usable:
        return None

    def rank(c: _Cand) -> tuple[int, int, int]:
        hinted = 0 if ev.playbook == c.playbook else 1
        closed = 1 if c.state in ("done", "cancelled") else 0
        return (hinted, closed, 0 if c.via == "key" else 1)

    best = min(usable, key=rank)
    if ev.kind in START_EVENTS.get(best.playbook, ()) and best.state in ("done", "cancelled"):
        return None  # a new order, a new request after a closed one: a new storyline
    return best


async def _new_storyline(conn: AsyncConnection, msg: Msg, playbook: str, entity_id: str | None) -> _Cand:
    if entity_id is None:
        row = await (
            await conn.execute(
                "INSERT INTO entity (kind, issuer) VALUES (%s, %s) RETURNING id::text",
                (_PLAYBOOK_ENTITY[playbook], msg.brand),
            )
        ).fetchone()
        assert row is not None
        entity_id = row[0]
    label = " ".join((msg.subject or "").split())[:LABEL_MAX] or None
    row = await (
        await conn.execute(
            "INSERT INTO storyline (kind, label, entity_id, thread_id) VALUES (%s, %s, %s, %s) "
            "RETURNING id::text",
            (playbook, label, entity_id, msg.thread_id),
        )
    ).fetchone()
    assert row is not None
    return _Cand(row[0], playbook, "open", entity_id, "key")


async def attach(
    conn: AsyncConnection,
    msg: Msg,
    stored: Sequence[tuple[str, mail_events.Event]],
    entity_id: str | None,
    *,
    risky: bool,
) -> set[str]:
    """Each event joins the storyline its links point to, or opens one. The ids of the storylines touched.
    `risky`: the sender imitates another (nothing of it joins or opens a storyline)."""
    touched: set[str] = set()
    if risky:
        return touched
    # the events that open things first (an order), so the message's request (pay) finds that storyline
    ordered = sorted(stored, key=lambda i: (i[1].kind == "request_to_owner", i[1].kind != "order_placed"))
    for event_id, ev in ordered:
        cand = _pick(ev, await _storylines_of(conn, entity_id, msg))
        if cand is None and ev.kind == "request_to_owner" and touched:
            # the message's ask belongs to the story it already moved (a shop's "ship it in 3 days" on a
            # sale): never a conversation of its own beside it (§37 S003)
            continue
        if (
            cand is None
            and ev.kind in _START_ANY
            and not (ev.kind == "request_to_owner" and ev.actor == "owner")
        ):
            pb = _playbook_for(ev)
            if pb is not None:
                cand = await _new_storyline(conn, msg, pb, entity_id)
                entity_id = cand.entity_id
                await conn.execute(
                    "UPDATE mail_event SET entity_id = %s WHERE id = %s", (entity_id, event_id)
                )
        if cand is None:
            continue
        if cand.via == "thread":
            await _edge(
                conn,
                "structural",
                ("message", msg.id),
                ("storyline", cand.id),
                "same_thread",
                reason="same thread and subject",
                confidence=0.7,
            )
            if entity_id is not None and cand.entity_id and cand.entity_id != entity_id:
                await _edge(  # its own keys name another thing: a proposal, never a merge
                    conn,
                    "soft",
                    ("entity", entity_id),
                    ("entity", cand.entity_id),
                    "same_thing",
                    state="proposed",
                    reason="a thread-linked message brought keys the storyline does not hold",
                    confidence=0.3,
                )
        if cand.playbook in KEYLESS and cand.entity_id:  # its identity is the thread: that counts as exact
            await _edge(
                conn,
                "structural",
                ("message", msg.id),
                ("entity", cand.entity_id),
                "same_thread",
                confidence=0.7,
            )
        await conn.execute(
            "INSERT INTO storyline_event (storyline_id, kind, message_id, mail_event_id, occurred_at, "
            "confidence) VALUES (%s, %s, %s, %s, %s, %s)",
            (cand.id, ev.kind, msg.id, event_id, msg.sent_at, 1.0 if cand.via == "key" else 0.7),
        )
        await _edge(conn, "hard", ("message", msg.id), ("storyline", cand.id), "evidence_of", confidence=1.0)
        touched.add(cand.id)
    return touched


async def adopt(conn: AsyncConnection, entity_id: str) -> set[str]:
    """Events read before their storyline existed (a courier's mail before the order's) join it now, by the
    entity they share. The ids of the storylines they joined."""
    touched: set[str] = set()
    rows = await (
        await conn.execute(
            "SELECT me.id::text, me.kind, me.message_id, m.sent_at FROM mail_event me "
            "JOIN email_messages m ON m.id = me.message_id WHERE me.entity_id = %s AND NOT EXISTS "
            "(SELECT 1 FROM storyline_event se WHERE se.mail_event_id = me.id) ORDER BY m.sent_at",
            (entity_id,),
        )
    ).fetchall()
    stories = await (
        await conn.execute(
            "SELECT id::text, kind FROM storyline WHERE entity_id = %s ORDER BY created_at", (entity_id,)
        )
    ).fetchall()
    for event_id, kind, message_id, sent_at in rows:
        for sid, pb in stories:
            if kind in ACCEPTS[pb] and kind not in START_EVENTS[pb]:
                await conn.execute(
                    "INSERT INTO storyline_event (storyline_id, kind, message_id, mail_event_id, "
                    "occurred_at, confidence) VALUES (%s, %s, %s, %s, %s, 1.0)",
                    (sid, kind, message_id, event_id, sent_at),
                )
                touched.add(sid)
                break
    return touched


# ---- soft links and risks ---------------------------------------------------------------------------------


async def look_alike(conn: AsyncConnection, msg: Msg) -> list[str]:
    """The open storylines whose sender this message's sender imitates (a brand one letter away, not the same
    issuer). The look-alike's mail never joins a storyline; each of these gets a risk effect."""
    brand = msg.brand
    if not brand or msg.owner_authored:
        return []
    rows = await (
        await conn.execute(
            "SELECT DISTINCT s.id::text, m.from_address FROM storyline s "
            "JOIN storyline_event se ON se.storyline_id = s.id AND se.undone_at IS NULL "
            "JOIN email_messages m ON m.id = se.message_id WHERE s.state IN ('open', 'waiting')"
        )
    ).fetchall()
    return sorted({sid for sid, addr in rows if linking.lookalike(brand, linking.brand_of(addr))})


async def propose_soft(conn: AsyncConnection, msg: Msg, found: Understood) -> None:
    """A message with events that joined no storyline may be about an open one of the same sender's brand:
    that is a soft edge, state `proposed`. It never acts: nothing joins, closes or changes because of it."""
    brand = msg.brand
    if not brand or msg.owner_authored or not found.events:
        return
    rows = await (
        await conn.execute(
            "SELECT DISTINCT s.id::text, s.kind, m.from_address FROM storyline s "
            "JOIN storyline_event se ON se.storyline_id = s.id AND se.undone_at IS NULL "
            "JOIN email_messages m ON m.id = se.message_id WHERE s.state IN ('open', 'waiting') LIMIT 50"
        )
    ).fetchall()
    for sid, kind, addr in rows:
        if linking.compatible(brand, linking.brand_of(addr)) and any(
            e.kind in ACCEPTS[kind] for e in found.events
        ):
            await _edge(
                conn,
                "soft",
                ("message", msg.id),
                ("storyline", sid),
                "same_story",
                state="proposed",
                reason="same sender, no key or thread in common",
                confidence=0.3,
            )


async def note_risk(
    conn: AsyncConnection,
    msg: Msg,
    key: str,
    quote: str | None,
    storylines: Sequence[str] = (),
) -> None:
    """A risk effect, with the message as its evidence: on the message, and on the storylines it concerns."""
    when = msg.sent_at
    await _assert(
        conn,
        ("message", msg.id),
        "risk",
        {"kind": "risk", "key": key},
        valid_from=when,
        provenance="inferred",
        source_ref=msg.id,
        quote=quote,
    )
    for sid in storylines:
        await _assert(
            conn,
            ("storyline", sid),
            "effect",
            {"kind": "risk", "key": key},
            valid_from=when,
            provenance="inferred",
            source_ref=msg.id,
            quote=quote,
        )


async def note_effects(conn: AsyncConnection, msg: Msg, found: Understood, storylines: Sequence[str]) -> int:
    """The model's effects (each with its quote), kept on the storylines the message joined. Its reactions are
    proposals: they never become a task here."""
    n = 0
    for sid in storylines:
        for eff in found.effects:
            if (eff.kind, eff.key) == ("risk", "injection_attempt"):
                continue  # `note_risk` has it
            await _assert(
                conn,
                ("storyline", sid),
                "effect",
                {"kind": eff.kind, "key": eff.key},
                valid_from=msg.sent_at,
                provenance="extracted",
                source_ref=msg.id,
                quote=eff.evidence,
            )
            n += 1
            if eff.reaction is not None:
                kind, key = eff.reaction
                await _assert(
                    conn,
                    ("storyline", sid),
                    "action",
                    {"kind": kind, "key": key, "status": "proposal", "evidence": msg.id, "by": "model"},
                    valid_from=msg.sent_at,
                    provenance="inferred",
                    source_ref=msg.id,
                    quote=eff.evidence,
                    confidence=0.5,
                )
    return n


# ---- 4. recompute: replay, plan, write ---------------------------------------------------------------------


@dataclass
class _Loaded:
    events: list[Ev]
    rows: dict[str, dict[str, Any]]  # event id (storyline_event) -> row


async def _load(conn: AsyncConnection, storyline_id: str, playbook: str, entity_id: str | None) -> _Loaded:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT se.id::text AS id, se.kind, se.message_id, se.mail_event_id::text AS mail_event_id, "
        "se.occurred_at AS at, se.created_at AS created, me.actor, me.debtor, me.occurred_at AS happened, "
        "me.quote, "
        "m.owner_authored, m.from_name, m.from_address, m.subject, ce.value AS chat, ce.quote AS chat_quote "
        "FROM storyline_event se LEFT JOIN mail_event me ON me.id = se.mail_event_id "
        "LEFT JOIN email_messages m ON m.id = se.message_id "
        "LEFT JOIN assertion ce ON ce.subject_kind = 'storyline_event' AND ce.subject_id = se.id::text "
        "AND ce.predicate = 'chat_evidence' AND ce.valid_to IS NULL "
        "WHERE se.storyline_id = %s AND se.undone_at IS NULL ORDER BY se.occurred_at, se.id",
        (storyline_id,),
    )
    rows = await cur.fetchall()
    messages = [r["message_id"] for r in rows if r["message_id"]]
    hard: set[str] = set()
    new_key: set[str] = set()
    if entity_id is not None and messages:
        grades = ("hard", "structural") if playbook in KEYLESS else ("hard",)
        hard = {
            r[0]
            for r in await (
                await conn.execute(
                    "SELECT src_id FROM life_edge WHERE dst_kind = 'entity' AND dst_id = %s "
                    "AND src_kind = 'message' "
                    "AND state = 'accepted' AND grade = ANY(%s) AND src_id = ANY(%s)",
                    (entity_id, list(grades), messages),
                )
            ).fetchall()
        }
        new_key = {
            r[0]
            for r in await (
                await conn.execute(
                    "SELECT DISTINCT k.message_id FROM entity_key k WHERE k.entity_id = %s "
                    "AND k.message_id = ANY(%s) "
                    "AND EXISTS (SELECT 1 FROM entity_key o WHERE o.entity_id = k.entity_id "
                    "AND o.key_kind = k.key_kind AND o.message_id <> k.message_id)",
                    (entity_id, messages),
                )
            ).fetchall()
        }
    injected = {
        r[0]
        for r in await (
            await conn.execute(
                "SELECT subject_id FROM assertion WHERE subject_kind = 'message' AND predicate = 'risk' "
                "AND value->>'key' = 'injection_attempt' AND subject_id = ANY(%s)",
                (messages,),
            )
        ).fetchall()
    }
    events: list[Ev] = []
    by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        mid = r["message_id"]
        if mid is None:
            # an episode that is not mail (a chat turn): the owner's own words, whose exactness was judged
            # when it was written (`chat.record`): an exact key or his naming makes it hard
            chat = r["chat"] or {}
            hard_here = bool(chat.get("hard"))
            r["actor"], r["debtor"], r["quote"] = "owner", chat.get("debtor", "none"), r["chat_quote"]
            r["owner_authored"] = True
        else:
            hard_here = mid in hard
        events.append(
            Ev(
                id=r["id"],
                kind=r["kind"],
                at=r["at"] or datetime.now(UTC),
                actor=r["actor"] or ("owner" if r["owner_authored"] else "other"),
                debtor=r["debtor"] or "none",
                hard=hard_here,
                when=r["happened"],
                new_key=mid in new_key,
                injected=mid in injected,
            )
        )
        by_id[r["id"]] = r
    return _Loaded(events, by_id)


@dataclass
class Read:
    """A storyline as its events replay it: read only, nothing written (the cards and the chat read this)."""

    storyline_id: str
    playbook: str
    entity_id: str | None
    events: list[Ev]
    rows: dict[str, dict[str, Any]]
    replay: Replay
    plan: Plan


async def read(conn: AsyncConnection, storyline_id: str) -> Read | None:
    """The storyline's replay and plan now, from its events that are not undone. None: no such storyline."""
    head = await (
        await conn.execute(
            "SELECT kind, entity_id::text FROM storyline WHERE id = %s", (uuid.UUID(storyline_id),)
        )
    ).fetchone()
    if head is None:
        return None
    playbook, entity_id = head
    loaded = await _load(conn, storyline_id, playbook, entity_id)
    rep = replay(playbook, loaded.events)
    return Read(storyline_id, playbook, entity_id, loaded.events, loaded.rows, rep, plan(rep, loaded.events))


def _terminal_state(state: str | None, playbook: str) -> str:
    if state in TERMINAL.get(playbook, ()):
        return "cancelled" if state in ("cancelled", "refused") else "done"
    return "open"


async def _sync(
    conn: AsyncConnection,
    storyline_id: str,
    predicate: str,
    wanted: dict[str, dict[str, Any]],
    *,
    provenance: str,
    valid_from: datetime,
    source_ref: str | None,
    now: datetime,
) -> None:
    """Make the current rows of one predicate what the playbook says now: a row that no longer holds ends
    (`valid_to`), one that is new starts; nothing is erased. `wanted`: identity -> value."""
    rows = await (
        await conn.execute(
            "SELECT id::text, value FROM assertion WHERE subject_kind = 'storyline' AND subject_id = %s "
            "AND predicate = %s AND provenance = %s AND valid_to IS NULL",
            (storyline_id, predicate, provenance),
        )
    ).fetchall()
    have: dict[str, str] = {}
    for aid, value in rows:
        ident = _ident(predicate, value)
        if ident not in wanted or ident in have:
            await conn.execute(
                "UPDATE assertion SET valid_to = GREATEST(valid_from, %s) WHERE id = %s",
                (now, uuid.UUID(aid)),
            )
        else:
            have[ident] = aid
            if wanted[ident] != value:  # the same thing, said differently now (its evidence moved)
                await conn.execute(
                    "UPDATE assertion SET valid_to = GREATEST(valid_from, %s) WHERE id = %s",
                    (now, uuid.UUID(aid)),
                )
                del have[ident]
    for ident, value in wanted.items():
        if ident not in have:
            await _assert(
                conn,
                ("storyline", storyline_id),
                predicate,
                value,
                valid_from=valid_from,
                provenance=provenance,
                source_ref=source_ref,
            )


def _ident(predicate: str, value: dict[str, Any]) -> str:
    if predicate == "state":
        return "state"
    if predicate == "proposed_transition":
        return f"{value.get('event')}|{value.get('to')}"
    return f"{value.get('kind')}|{value.get('key')}"


@dataclass
class Tally:
    """What a recompute did to tasks: ids and counts."""

    created: list[str] = field(default_factory=list)
    changed: int = 0
    proposals: int = 0


async def recompute(
    conn: AsyncConnection,
    storyline_id: str,
    now: datetime,
    owner: OwnerContext,
    *,
    tasks: bool = True,
) -> tuple[Replay, Plan, Tally] | None:
    """Replay the storyline's events through its playbook and write what follows: state, next action, effects,
    proposals, and (unless `tasks` is False, the re-triage) its tasks."""
    head = await (
        await conn.execute(
            "SELECT kind, entity_id::text FROM storyline WHERE id = %s", (uuid.UUID(storyline_id),)
        )
    ).fetchone()
    if head is None:
        return None
    playbook, entity_id = head
    loaded = await _load(conn, storyline_id, playbook, entity_id)
    rep = replay(playbook, loaded.events)
    pl = plan(rep, loaded.events)
    last = loaded.events[-1] if loaded.events else None
    valid_from = last.at if last else now
    source = loaded.rows[last.id]["message_id"] if last else None
    await _sync(
        conn,
        storyline_id,
        "state",
        {"state": {"state": rep.state, "next": pl.next_action, "waiting_on": pl.waiting_on}},
        provenance="playbook",
        valid_from=valid_from,
        source_ref=source,
        now=now,
    )
    actions: dict[str, dict[str, Any]] = {}
    for a in pl.actions:
        ev = loaded.rows.get(a.evidence or "")
        actions[f"{a.kind}|{a.key}"] = {
            "kind": a.kind,
            "key": a.key,
            "status": a.status,
            "evidence": (ev["message_id"] or (ev["chat"] or {}).get("turn")) if ev else None,
            "after": a.after.isoformat() if a.after else None,
            "before": a.before.isoformat() if a.before else None,
            "by": "playbook",
        }
    await _sync(
        conn,
        storyline_id,
        "action",
        actions,
        provenance="playbook",
        valid_from=valid_from,
        source_ref=source,
        now=now,
    )
    effects = {f"{k}|{v}": {"kind": k, "key": v} for k, v in pl.effects}
    await _sync(
        conn,
        storyline_id,
        "effect",
        effects,
        provenance="playbook",
        valid_from=valid_from,
        source_ref=source,
        now=now,
    )
    if rep.steps:
        # what a message said it changes holds until the storyline moves on from it: an effect the model read
        # in an earlier message ends at the latest transition (a ship deadline once shipped, the appointment
        # once cancelled); the state's own effects are the playbook's above, and a risk stays (§38)
        moved = next((e.at for e in loaded.events if e.id == rep.steps[-1].event_id), None)
        if moved is not None:
            await conn.execute(
                "UPDATE assertion SET valid_to = GREATEST(valid_from, %s) WHERE subject_kind = 'storyline' "
                "AND subject_id = %s AND predicate = 'effect' AND provenance = 'extracted' "
                "AND valid_to IS NULL AND value->>'kind' <> 'risk' AND valid_from < %s",
                (now, storyline_id, moved),
            )
    held = {}
    for h in rep.held:
        row = loaded.rows[h.event_id]
        held[f"{h.event}|{h.to}"] = {
            "event": h.event,
            "to": h.to,
            "why": h.why,
            "message": row["message_id"],
            "storyline_event": h.event_id,
            "quote": row["quote"],
        }
    await _sync(
        conn,
        storyline_id,
        "proposed_transition",
        held,
        provenance="playbook",
        valid_from=valid_from,
        source_ref=source,
        now=now,
    )
    next_actor = (
        "owner"
        if any(a.status == "task" for a in pl.actions)
        else ("other" if pl.waiting_on != "none" else "none")
    )
    coarse = _terminal_state(rep.state, playbook)
    if coarse == "open" and next_actor != "owner" and pl.waiting_on != "none":
        coarse = "waiting"
    await conn.execute(
        "UPDATE storyline SET state = %s, next_actor = %s, updated_at = %s WHERE id = %s",
        (coarse, next_actor, max(now, valid_from), uuid.UUID(storyline_id)),
    )
    tally = Tally()
    if tasks and entity_id is not None:
        await _project(conn, storyline_id, entity_id, playbook, rep, pl, loaded, now, owner, tally)
    tally.proposals += len(rep.held)
    return rep, pl, tally


# ---- 5. the tasks ------------------------------------------------------------------------------------------


async def _counterpart(conn: AsyncConnection, storyline_id: str) -> str:
    row = await (
        await conn.execute(
            "SELECT coalesce(m.from_name, m.from_address, '') FROM storyline_event se "
            "JOIN email_messages m ON m.id = se.message_id WHERE se.storyline_id = %s "
            "AND se.undone_at IS NULL AND NOT m.owner_authored ORDER BY se.occurred_at DESC LIMIT 1",
            (uuid.UUID(storyline_id),),
        )
    ).fetchone()
    return (row[0] if row else "").strip()


async def opener(conn: AsyncConnection, storyline_id: str) -> str:
    """The other side who opened the storyline (the shop, the clinic, the person who asked): what the cards
    and the diff call it. The courier's later mail does not rename it."""
    row = await (
        await conn.execute(
            "SELECT coalesce(m.from_name, m.from_address, '') FROM storyline_event se "
            "JOIN email_messages m ON m.id = se.message_id WHERE se.storyline_id = %s "
            "AND se.undone_at IS NULL AND NOT m.owner_authored ORDER BY se.occurred_at, se.id LIMIT 1",
            (uuid.UUID(storyline_id),),
        )
    ).fetchone()
    return (row[0] if row else "").strip()


async def _primary_key(conn: AsyncConnection, entity_id: str) -> str | None:
    row = await (
        await conn.execute(
            "SELECT verbatim_source FROM entity_key WHERE entity_id = %s AND key_kind = ANY(%s) "
            "ORDER BY created_at LIMIT 1",
            (uuid.UUID(entity_id), list(SOURCE_REF_KINDS)),
        )
    ).fetchone()
    return row[0] if row else None


def _due(after: datetime | None, before: datetime | None, tz: str) -> tuple[datetime | None, str]:
    day = after or before
    return (end_of_day(day.astimezone(UTC).date(), tz), "date") if day else (None, "none")


def _since(playbook: str, events: Sequence[Ev]) -> dict[str, str]:
    """For each action the playbook wants now, the event it has been wanted since without a break (the
    storyline's own history replayed event by event). It names the need: an order's "ship" is the same need
    at "sold" and at "label"; a second request after an answer is a new one."""
    since: dict[str, str] = {}
    ordered = sorted(events, key=lambda e: (e.at, e.id))
    for i in range(1, len(ordered) + 1):
        prefix = ordered[:i]
        now_wanted = {
            a.key: a.evidence for a in plan(replay(playbook, prefix), prefix).actions if a.status == "task"
        }
        since = {k: since.get(k) or v for k, v in now_wanted.items() if v}
    return since


async def _project(
    conn: AsyncConnection,
    storyline_id: str,
    entity_id: str,
    playbook: str,
    rep: Replay,
    pl: Plan,
    loaded: _Loaded,
    now: datetime,
    owner: OwnerContext,
    tally: Tally,
) -> None:
    """The storyline's pending owner actions as tasks, and the tasks that are no longer pending closed or
    waiting. One task per (entity, intent). The owner's own changes to a task are never undone by the
    engine."""
    existing = await (
        await conn.execute(
            "SELECT t.id::text, t.intent, t.status, t.state, t.source_ref, "
            "EXISTS (SELECT 1 FROM life_edge e WHERE e.src_kind = 'task' AND e.src_id = t.id::text "
            "AND e.dst_kind = 'storyline' AND e.dst_id = %s AND e.relation = 'projection_of') "
            "FROM tasks t WHERE t.entity_id = %s AND t.source_kind = 'email' AND t.intent IS NOT NULL "
            "ORDER BY t.created_at",
            (storyline_id, uuid.UUID(entity_id)),
        )
    ).fetchall()
    latest: dict[str, tuple[str, str, str | None, str | None]] = {}
    mine: set[str] = set()
    for tid, intent, status, state, ref, own in existing:
        latest[intent] = (tid, status, state, ref)
        if own:
            mine.add(tid)
    wanted = {a.key: a for a in pl.actions if a.status == "task"}
    last_id = loaded.events[-1].id if loaded.events else None
    origin = _since(playbook, loaded.events)
    for intent, action in wanted.items():
        ev = loaded.rows.get(origin.get(intent) or action.evidence or "")
        ref = ev["message_id"] if ev else None
        cur = latest.get(intent)
        later = action.after is not None and action.after > now
        if cur is not None and cur[1] == "open":
            state = "waiting" if later else "open"
            if cur[2] != state:
                await _set_state(conn, cur[0], state, now, action.after if later else None, ev, "asked_again")
                tally.changed += 1
            continue
        if cur is not None and cur[3] == ref:
            continue  # the same need, already met or closed: the owner's own close stays
        if ev is None or ref is None:
            continue
        made = await _make_task(conn, storyline_id, entity_id, intent, action, ev, ref, later, now, owner)
        if made is not None:
            tally.created.append(made)
    for intent, (tid, status, state, _ref) in latest.items():
        if intent in wanted or status != "open" or tid not in mine:
            continue
        if not loaded.events:
            continue
        reason = "void" if _terminal_state(rep.state, playbook) == "cancelled" else "fulfilled"
        last = loaded.rows[last_id] if last_id else None
        hard = bool(loaded.events[-1].hard)
        by_owner = bool(last and (last["actor"] == "owner" or last["owner_authored"]))
        to = task_machine.next_state(state or "open", reason, hard=hard, by_owner=by_owner)
        if to is None:
            await _propose_change(conn, storyline_id, tid, reason, last, now)
            tally.proposals += 1
            continue
        await _set_state(conn, tid, to, now, None, last, to)
        tally.changed += 1


async def _make_task(
    conn: AsyncConnection,
    storyline_id: str,
    entity_id: str,
    intent: str,
    action: Any,
    ev: dict[str, Any],
    ref: str,
    later: bool,
    now: datetime,
    owner: OwnerContext,
) -> str | None:
    """One task for one pending owner action; its id, or None when it stays a proposal (the debtor lint
    refused its words, or the thread already has a task from before the storylines)."""
    from pisa.services.tasks import insert_task  # the one writer of tasks

    who = await _counterpart(conn, storyline_id)
    title = task_title(intent, who=who, ref=await _primary_key(conn, entity_id), italian=owner.italian)
    findings = debtor_lint.lint(title, debtor="owner", owner_names=owner.names)
    if findings:
        await _proposal(conn, storyline_id, action, ref, f"lint:{findings[0].code}", ev.get("quote"), now)
        return None
    if await _legacy_task_in_thread(conn, ref):
        await _proposal(conn, storyline_id, action, ref, "legacy_task_in_thread", ev.get("quote"), now)
        return None
    due_at, due_kind = _due(action.after, action.before, owner.tz)
    if intent == "attend" and ev.get("happened") is not None:
        due_at, due_kind = ev["happened"], "datetime"
    sender = (ev.get("from_name") or ev.get("from_address") or "unknown sender").strip()
    subject = " ".join((ev.get("subject") or "(no subject)").split())
    label = f"Email from {sender}: {subject[:60]}"
    task = await insert_task(
        conn,
        title=title,
        now=now,
        source_kind="email",
        due_at=due_at,
        due_kind=due_kind,  # type: ignore[arg-type]
        priority=2,
        source_ref=ref,
        source_label=label,
        confidence=0.9,
    )
    if task is None:
        return None
    state = "waiting" if later else "open"
    await conn.execute(
        "UPDATE tasks SET entity_id = %s, intent = %s, state = %s, waiting_until = %s WHERE id = %s",
        (uuid.UUID(entity_id), intent, state, action.after if later else None, uuid.UUID(task.id)),
    )
    await conn.execute(
        "INSERT INTO task_evidence (task_id, message_id, mail_event_id, transition, quote, confidence) "
        "VALUES (%s, %s, %s, 'created', %s, 0.9)",
        (
            uuid.UUID(task.id),
            ref,
            uuid.UUID(ev["mail_event_id"]) if ev.get("mail_event_id") else None,
            ev.get("quote"),
        ),
    )
    await _edge(conn, "hard", ("task", task.id), ("storyline", storyline_id), "projection_of", confidence=1.0)
    return task.id


async def _legacy_task_in_thread(conn: AsyncConnection, message_id: str) -> bool:
    """An open task made before the storylines (no state) from this message's thread: the storyline does not
    add a second one beside it; the re-triage proposes what becomes of it."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM tasks t JOIN email_messages m ON m.id = t.source_ref JOIN email_messages me "
            "ON me.thread_id = m.thread_id WHERE me.id = %s AND t.source_kind = 'email' "
            "AND t.status = 'open' AND t.state IS NULL LIMIT 1",
            (message_id,),
        )
    ).fetchone()
    return row is not None


def chat_ref(storyline_event_id: str) -> str:
    """What a chat turn's event is called in `task_evidence.message_id` (it has no message)."""
    return f"chat:{storyline_event_id}"


def _evidence_ref(ev: dict[str, Any] | None) -> str | None:
    """What a task change is recorded against: the message, or for the owner's chat turn its event."""
    if ev is None:
        return None
    if ev.get("message_id"):
        return str(ev["message_id"])
    return chat_ref(ev["id"]) if ev.get("chat") is not None and ev.get("id") else None


async def _set_state(
    conn: AsyncConnection,
    task_id: str,
    state: str,
    now: datetime,
    waiting_until: datetime | None,
    ev: dict[str, Any] | None,
    transition: str,
) -> None:
    await conn.execute(
        "UPDATE tasks SET state = %s, status = %s, waiting_until = %s, updated_at = %s WHERE id = %s",
        (state, task_machine.status_of(state), waiting_until, now, uuid.UUID(task_id)),
    )
    ref = _evidence_ref(ev)
    if ev is not None and ref is not None:
        await conn.execute(
            "INSERT INTO task_evidence (task_id, message_id, mail_event_id, transition, quote, confidence) "
            "VALUES (%s, %s, %s, %s, %s, 0.9)",
            (
                uuid.UUID(task_id),
                ref,
                uuid.UUID(ev["mail_event_id"]) if ev.get("mail_event_id") else None,
                transition,
                ev.get("quote"),
            ),
        )


async def _proposal(
    conn: AsyncConnection,
    storyline_id: str,
    action: Any,
    message_id: str,
    why: str,
    quote: str | None,
    now: datetime,
) -> None:
    """An action that should have been a task and was refused (the debtor lint): kept as a proposal."""
    await _assert(
        conn,
        ("storyline", storyline_id),
        "action",
        {"kind": action.kind, "key": action.key, "status": "proposal", "evidence": message_id, "by": why},
        valid_from=now,
        provenance="inferred",
        source_ref=message_id,
        quote=quote,
        confidence=0.5,
    )


async def _propose_change(
    conn: AsyncConnection,
    storyline_id: str,
    task_id: str,
    reason: str,
    ev: dict[str, Any] | None,
    now: datetime,
) -> None:
    """The storyline says a task is over, but not with an exact key or his own word: a proposal for him."""
    exists = await (
        await conn.execute(
            "SELECT 1 FROM assertion WHERE subject_kind = 'task' AND subject_id = %s AND predicate = "
            "'proposed_change' AND valid_to IS NULL AND value->>'reason' = %s",
            (task_id, reason),
        )
    ).fetchone()
    if exists is not None:
        return
    await _assert(
        conn,
        ("task", task_id),
        "proposed_change",
        {"reason": reason, "storyline": storyline_id, "message": ev.get("message_id") if ev else None},
        valid_from=now,
        provenance="inferred",
        source_ref=ev.get("message_id") if ev else None,
        quote=ev.get("quote") if ev else None,
        confidence=0.6,
    )


# ---- undo --------------------------------------------------------------------------------------------------


async def undo_event(
    conn: AsyncConnection, storyline_event_id: str, now: datetime, owner: OwnerContext, by: str = "owner"
) -> bool:
    """Take an event back out of its storyline: the replay runs without it, so the state, the actions and the
    tasks go back to what the rest of the evidence says. The event stays on record (`undone_at`, `undone_by`).
    False when there is no such event or it is already undone."""
    row = await (
        await conn.execute(
            "UPDATE storyline_event SET undone_at = %s, undone_by = %s WHERE id = %s AND undone_at IS NULL "
            "RETURNING storyline_id::text, mail_event_id",
            (now, by, uuid.UUID(storyline_event_id)),
        )
    ).fetchone()
    if row is None:
        return False
    sid, mail_event_id = row
    # the task changes this event caused are undone with it: the task reopens (a chat turn's event has no
    # mail event: its changes are recorded under its own reference)
    undone = await (
        await conn.execute(
            "UPDATE task_evidence SET undone_at = %s WHERE (mail_event_id = %s OR message_id = %s) "
            "AND undone_at IS NULL AND transition IN ('done', 'cancelled') RETURNING task_id",
            (now, mail_event_id, chat_ref(storyline_event_id)),
        )
    ).fetchall()
    for (task_id,) in undone:
        await conn.execute(
            "UPDATE tasks SET state = 'open', status = 'open', updated_at = %s WHERE id = %s "
            "AND status IN ('done', 'dismissed')",
            (now, task_id),
        )
    await recompute(conn, sid, now, owner)
    return True


async def undo_task_change(
    conn: AsyncConnection, evidence_id: str, now: datetime, owner: OwnerContext
) -> bool:
    """The owner takes back what the engine did to a task (closed it, cancelled it): the event that caused it
    is undone by him, so the engine does not close it again from the same evidence."""
    row = await (
        await conn.execute(
            "SELECT mail_event_id::text, task_id::text, message_id FROM task_evidence "
            "WHERE id = %s AND undone_at IS NULL",
            (uuid.UUID(evidence_id),),
        )
    ).fetchone()
    if row is None:
        return False
    mail_event_id, task_id, ref = row
    if mail_event_id is not None:
        se = await (
            await conn.execute(
                "SELECT id::text FROM storyline_event WHERE mail_event_id = %s AND undone_at IS NULL",
                (uuid.UUID(mail_event_id),),
            )
        ).fetchone()
    elif str(ref).startswith("chat:"):  # the owner's own chat turn caused it
        se = await (
            await conn.execute(
                "SELECT id::text FROM storyline_event WHERE id = %s AND undone_at IS NULL",
                (uuid.UUID(str(ref)[5:]),),
            )
        ).fetchone()
    else:
        return False
    await conn.execute("UPDATE task_evidence SET undone_at = %s WHERE id = %s", (now, uuid.UUID(evidence_id)))
    await conn.execute(
        "UPDATE tasks SET state = 'open', status = 'open', updated_at = %s WHERE id = %s",
        (now, uuid.UUID(task_id)),
    )
    if se is not None:
        await undo_event(conn, se[0], now, owner, "owner")
    return True


async def accept_proposal(
    conn: AsyncConnection, assertion_id: str, now: datetime, owner: OwnerContext
) -> bool:
    """The owner says yes to a proposed change of a task (the re-triage's, or a close the engine held back):
    only then does the task change. Returns whether something changed."""
    row = await (
        await conn.execute(
            "SELECT subject_id, value FROM assertion WHERE id = %s AND subject_kind = 'task' "
            "AND predicate = 'proposed_change' AND valid_to IS NULL",
            (uuid.UUID(assertion_id),),
        )
    ).fetchone()
    if row is None:
        return False
    task_id, value = row
    reason = value.get("reason")
    to = task_machine.next_state("open", reason, hard=False, by_owner=True) if reason else None
    if to is None:
        return False
    await _set_state(conn, task_id, to, now, None, None, to)
    await conn.execute(
        "UPDATE assertion SET valid_to = GREATEST(valid_from, %s), provenance = provenance WHERE id = %s",
        (now, uuid.UUID(assertion_id)),
    )
    await _assert(
        conn,
        ("task", task_id),
        "owner_decision",
        {"accepted": assertion_id, "reason": reason},
        valid_from=now,
        provenance="owner",
        source_ref=None,
        confidence=1.0,
    )
    return True


async def reject_proposal(conn: AsyncConnection, assertion_id: str, now: datetime) -> bool:
    """The owner says no: the proposal ends, and a record says it was his word."""
    row = await (
        await conn.execute(
            "UPDATE assertion SET valid_to = GREATEST(valid_from, %s) WHERE id = %s AND predicate = "
            "'proposed_change' AND valid_to IS NULL RETURNING subject_id",
            (now, uuid.UUID(assertion_id)),
        )
    ).fetchone()
    if row is None:
        return False
    await _assert(
        conn,
        ("task", row[0]),
        "owner_decision",
        {"rejected": assertion_id},
        valid_from=now,
        provenance="owner",
        source_ref=None,
        confidence=1.0,
    )
    return True


async def accept_transition(
    conn: AsyncConnection, assertion_id: str, now: datetime, owner: OwnerContext
) -> bool:
    """The owner says yes to a transition the rules held back (a closing with no exact key): his word makes
    the message's link a hard one, and the replay runs again. False when there is no such proposal."""
    row = await (
        await conn.execute(
            "SELECT subject_id, value FROM assertion WHERE id = %s AND subject_kind = 'storyline' "
            "AND predicate = 'proposed_transition' AND valid_to IS NULL",
            (uuid.UUID(assertion_id),),
        )
    ).fetchone()
    if row is None:
        return False
    storyline_id, value = row
    head = await (
        await conn.execute("SELECT entity_id::text FROM storyline WHERE id = %s", (uuid.UUID(storyline_id),))
    ).fetchone()
    if head is None or head[0] is None or not (value.get("message") or value.get("storyline_event")):
        return False
    if value.get("message"):
        await _edge(
            conn,
            "hard",
            ("message", value["message"]),
            ("entity", head[0]),
            "owner_confirmed",
            decided_by="owner",
            reason="the owner confirmed it",
            confidence=1.0,
        )
    else:  # the owner's own chat words: his yes names the storyline the words were about
        await conn.execute(
            "UPDATE assertion SET value = jsonb_set(value, '{hard}', 'true') WHERE subject_kind = "
            "'storyline_event' AND subject_id = %s AND predicate = 'chat_evidence' AND valid_to IS NULL",
            (value["storyline_event"],),
        )
    await _assert(
        conn,
        ("storyline", storyline_id),
        "owner_decision",
        {"accepted": assertion_id, "to": value.get("to")},
        valid_from=now,
        provenance="owner",
        source_ref=value.get("message"),
        confidence=1.0,
    )
    await recompute(conn, storyline_id, now, owner)
    return True


# ---- one message, end to end --------------------------------------------------------------------------


async def ingest(
    conn: AsyncConnection,
    msg: Msg,
    found: Understood,
    now: datetime,
    owner: OwnerContext,
    *,
    tasks: bool = True,
) -> Outcome:
    """Everything one message does: its events stored, linked, attached to storylines, the storylines
    replayed, their tasks projected (unless `tasks` is False: the re-triage only proposes). Run it inside the
    caller's transaction."""
    out = Outcome()
    stored = await store(conn, msg, found)
    out.events = len(stored)
    alike = await look_alike(conn, msg)
    entity_id = None if alike else await link(conn, msg, found, stored)
    touched = await attach(conn, msg, stored, entity_id, risky=bool(alike))
    if alike:
        await note_risk(conn, msg, "suspicious_sender", None, alike)
        out.risks += 1
    if found.injected:
        quote = next((e.evidence for e in found.effects if e.key == "injection_attempt"), None)
        await note_risk(conn, msg, "injection_attempt", quote, sorted(touched))
        out.risks += 1
    if touched:
        await note_effects(conn, msg, found, sorted(touched))
    elif stored and not alike:
        await propose_soft(conn, msg, found)
    if entity_id is not None and not alike:
        touched |= await adopt(conn, entity_id)
    for sid in sorted(touched):
        got = await recompute(conn, sid, now, owner, tasks=tasks)
        if got is not None:
            out.tasks_created += len(got[2].created)
            out.tasks_changed += got[2].changed
            out.proposals += got[2].proposals
            out.task_ids += got[2].created
    out.storylines = sorted(touched)
    await _assert(
        conn,
        ("message", msg.id),
        "read",
        {"by": "events", "events": len(stored)},
        valid_from=msg.sent_at,
        provenance="extracted",
        source_ref=msg.id,
        confidence=1.0,
    )
    return out
