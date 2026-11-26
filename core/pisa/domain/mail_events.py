"""What the events call says about one message, checked in code. Pure rules, no I/O.

The model reads the message's new text (never its quoted history) and answers with events and effects. Code
keeps only what the text itself backs up:
- a **key** (an order number, a tracking code) is kept only when it stands in the new text, or the subject,
  word for word (a regex check on token boundaries, so "ORD-123" never matches inside "ORD-1234");
- an **event** or an **effect** is kept only when its quote is verbatim in the new text, its kind is
  on the fixed list, and (for an event) it is not the owner's own sentence coming back;
- who wrote the message is the owner model's word, never the model's: an owner-authored message has the owner
  as every actor and can't ask the owner anything;
- text that reads as an instruction to an assistant is a risk effect, and the sentences it sits in back no
  event.
Nothing the model writes becomes a task here: the effects' reactions are proposals only.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pisa.domain import email_keys
from pisa.domain.playbooks import ACTION_KEYS, EFFECT_KEYS, EVENT_KINDS, PLAYBOOKS
from pisa.domain.text import fold

KEY_KINDS = ("order_id", "tracking", "booking_ref", "invoice_no", "other")
QUOTE_MAX = 200
EVENTS_MAX = 6
EFFECTS_MAX = 8
KEYS_PER_EVENT_MAX = 5
# the labelled numbers the rules find on their own (`email_keys`) and the kind they are kept as
_RULE_KIND = {
    "order": "order_id",
    "booking": "booking_ref",
    "ticket": "booking_ref",
    "tracking": "tracking",
    "reference": "other",
}
_EDGE = r"(?<![A-Za-z0-9-])"
_EDGE_END = r"(?![A-Za-z0-9-])"

# Text that talks to an assistant, not to the owner. A hit is a risk effect; it never moves a storyline.
_INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|above|prior)\b"
    r"|\bignor\w*\s+(le\s+|tutte\s+le\s+)?(istruzioni|regole)\b"
    r"|\b(dimentica|ignora)\s+(quanto|tutto|le)\b"
    r"|\bsystem\s*:|\bsistema\s*:"
    r"|\b(assistant|assistente|ai)\s*,"
    r"|\bmark\s+(all|every)\b[^.]{0,30}\b(done|complete)"
    r"|\b(segna|chiudi)\s+(tutt\w+|ogni)\b"
    r"|\b(close|delete)\s+(every|all)\b|\b(elimina|cancella)\s+tutt\w+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Key:
    kind: str
    value: str  # the normal form: trimmed, upper case, no inner spaces
    verbatim: str  # the characters as the message wrote them (never more)


@dataclass(frozen=True)
class Event:
    kind: str
    playbook: str | None
    actor: str
    debtor: str
    keys: tuple[Key, ...]
    occurred_at: datetime | None
    quote: str


@dataclass(frozen=True)
class Effect:
    kind: str
    key: str
    evidence: str
    reaction: tuple[str, str] | None = None  # (action kind, action key): a proposal, never a task


@dataclass
class Understood:
    events: list[Event] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    keys: list[Key] = field(default_factory=list)  # every key of the message (events' and the rules')
    injected: bool = False
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (what, why): for the logs and the tests


def normal(value: str) -> str:
    """A key's normal form: trimmed, upper case, no inner spaces."""
    return "".join(email_keys.plain(value).split()).upper()


def appears(value: str, text: str) -> str | None:
    """The characters of `value` as they stand in `text` (word for word, case aside, whole token), or None."""
    v = " ".join(email_keys.plain(value).split())
    if len(v) < 3:
        return None
    found = re.search(_EDGE + re.escape(v) + _EDGE_END, email_keys.plain(text), re.IGNORECASE)
    return found.group(0) if found else None


_APOSTROPHES = str.maketrans(
    {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "`": "'"}
    | dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2212", "-")  # a model's typographic dashes (§39)
)
_REPLY_OR_FORWARD = re.compile(r"^\s*(?:re|r|fw|fwd|i|inoltro|aw|wg|tr|rv|sv)\s*:", re.IGNORECASE)


def _flat(text: str) -> str:
    """Spaces, case, accents and curly quotes aside: a model copying "e pronta" writes "è pronta" (§36 B)."""
    return " ".join(fold(email_keys.plain(text).translate(_APOSTROPHES)).casefold().split())


def quoted_in(quote: str, text: str) -> str | None:
    """The quote as a verbatim stretch of `text` (spaces and case aside), else None. It comes back as the
    model wrote it, spaces tidied."""
    shown = " ".join(email_keys.plain(quote).split()).strip(" \"'«»")
    q = _flat(shown).strip(" .,;:!?")
    if len(q) < 4:
        return None
    return shown if q in _flat(text) else None


def _norm_overlap(s: str) -> str:
    return re.sub(r"[>\s]+", " ", (s or "").lower()).strip(" .,;:!?")


def overlaps(text: str, span: str) -> bool:
    """One text holds the other (the shorter at least 12 characters), or one holds at least 80 percent of the
    other's words (at least 4 words): the eval's own rule for "this is that sentence again"."""
    nt, ns = _norm_overlap(text), _norm_overlap(span)
    if not nt or not ns:
        return False
    if (ns in nt and len(ns) >= 12) or (nt in ns and len(nt) >= 12):
        return True
    a, b = set(nt.split()), set(ns.split())
    inter = len(a & b)
    return (len(a) >= 4 and inter / len(a) >= 0.8) or (len(b) >= 4 and inter / len(b) >= 0.8)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) >= 8]


def injected_sentences(text: str) -> list[str]:
    """The sentences of the text that talk to an assistant."""
    return [s for s in sentences(text) if _INJECTION.search(s)]


def _when(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


# The kinds that are the other side's move: an owner's own "received, thanks" or "Friday is fine" in a
# reply is his answer, never their acknowledgement or a booking (release audit §37: an owner's reply read as
# "acknowledged" closed a request he had only answered; read as "appointment" it opened a booking).
_OTHERS_MOVES = frozenset({"acknowledged", "appointment"})
_OWNER_ANSWERS = frozenset({"owner_replied", "owner_promise"})
# the owner saying what he will do: a first-person future, or a time ahead
_PROMISE = re.compile(
    r"\b(?:i'll|i will|i shall|i'm going to|i am going to|let me get back)\b|\b\w+[aei]rò\b"
    r"|\b(?:entro|domani|stasera|più tardi|in giornata|by tomorrow|tomorrow|later today|tonight)\b",
    re.IGNORECASE,
)


def _owner_answer(events: list[Event], text: str, *, replying: bool) -> list[Event]:
    """The owner's events in a message he wrote. Acknowledged is never his: it is his reply. In a reply,
    an appointment he names is his answer too (the other side books it), and a reply with no answer kept is
    still his answer: code knows he wrote back, whatever the model read in it."""
    out: list[Event] = []
    for ev in events:
        if ev.kind == "acknowledged" or (replying and ev.kind == "appointment"):
            ev = replace(ev, kind="owner_replied", playbook=None, debtor="other", occurred_at=None)
        if ev.kind == "owner_replied" and any(e.kind == "owner_replied" for e in out):
            continue
        out.append(ev)
    if replying and not any(e.kind in _OWNER_ANSWERS for e in out) and (said := sentences(text)):
        # a promise keeps what he owes (his reply task) where an answer would let it go
        promise = next((x for x in said if _PROMISE.search(x)), None)
        kind = "owner_promise" if promise else "owner_replied"
        out.append(Event(kind, None, "owner", "other", (), None, (promise or said[0])[:QUOTE_MAX]))
    return out


# a refund the shop says it will process is its promise, not the money (§38: "will be processed within 7
# days" read as granted made a check for money nobody had sent)
_LATER = re.compile(r"\b(?:will|shall|going to|sara|verra|saranno|verranno)\b|\b\w+[aei]ra\b")
_GRANTED = re.compile(
    r"\b(?:approved|accepted|issued|granted|refunded|credited|refund (?:you|the|your)|credit (?:you|your)|"
    r"approvat\w*|accettat\w*|emess\w*|accreditat\w*|concess\w*|rimborsat\w*|rimborser\w*)\b"
)


def _said_later(ev: Event) -> Event:
    """A refund_granted whose quote only says what will be done (no word that it was approved or made) is the
    shop acknowledging the request."""
    flat = _flat(ev.quote)
    if ev.kind == "refund_granted" and _LATER.search(flat) and not _GRANTED.search(flat):
        return replace(ev, kind="acknowledged")
    return ev


# a counterpart's thanks or "I'll wait for it" after the owner answered or promised asks nothing (§40 S131:
# "Great, I will wait for it." reopened a promise as a new request)
_WAITS = re.compile(
    r"\b(?:i'll wait|i will wait|i am waiting|i'm waiting|waiting for|look forward|looking forward|no rush|"
    r"thanks|thank you|great|perfect|ok|okay|got it|aspetto|attendo|resto in attesa|in attesa|grazie|"
    r"perfetto|ottimo|va bene|nessuna fretta)\b"
)
# anything that asks, pays, sends, attaches or sets a day keeps the request (when in doubt, it asks)
_ASKS = re.compile(
    r"\?|\b(?:please|could you|can you|would you|will you|send\w*|need\w*|let me know|confirm\w*|kindly|"
    r"pay\w*|reply|answer|attach\w*|due|deadline|by (?:today|tomorrow|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|per favore|potrebbe|potresti|puoi|puo|preg\w*|mand\w*|invi\w*|mi serve|"
    r"serve|servono|fammi sapere|conferm\w*|pag\w*|rispond\w*|allega\w*|entro|scad\w*|ricordati)\b"
)


def only_waits(quote: str, text: str) -> bool:
    """The sentence thanks or says its writer will wait, and nothing in the message asks: no request to the
    owner. The whole message decides ("Thanks for writing. We need a photo." asks, whichever sentence the
    model quoted)."""
    return bool(_WAITS.search(_flat(quote))) and not _ASKS.search(_flat(text))


def rule_keys(text: str) -> list[Key]:
    """The labelled numbers the rules find in the text on their own ("Order n. ORD-1", "Tracking: TRK1"):
    verbatim by construction."""
    out: list[Key] = []
    for k in email_keys.found_keys(text):
        kind = _RULE_KIND.get(k.kind)
        if kind and (shown := appears(k.value, text)):
            out.append(Key(kind, normal(k.value), shown))
    return out


def _key(raw: Mapping[str, Any], text: str, subject: str) -> Key | None:
    kind, value = str(raw.get("kind") or "other"), str(raw.get("value") or "")
    shown = appears(value, text) or appears(value, subject)
    if shown is None:
        return None
    return Key(kind if kind in KEY_KINDS else "other", normal(value), shown)


def validate(
    raw: Mapping[str, Any],
    *,
    text: str,
    subject: str,
    owner_authored: bool,
    owner_said: Sequence[str] = (),
    replying: bool = False,
) -> Understood:
    """The model's answer, kept only where the text backs it. `owner_said`: sentences the owner wrote earlier
    in this thread (his own words must never come back as a request). `replying`: someone else wrote earlier
    in this thread, so an owner's message there is his answer (`_owner_answer`)."""
    out = Understood()
    # the sender's own subject is theirs too ("Order X cancelled"); a reply's or forward's names an older one
    own_subject = "" if _REPLY_OR_FORWARD.match(subject or "") else (subject or "")
    hostile = injected_sentences(text) + injected_sentences(own_subject)
    out.injected = bool(hostile)
    seen_keys: dict[str, Key] = {}

    def keep(key: Key) -> None:
        seen_keys.setdefault(f"{key.kind}|{key.value}", key)

    for item in list(raw.get("events") or ())[:EVENTS_MAX]:
        kind = str(item.get("kind") or "")
        if kind not in EVENT_KINDS:
            out.dropped.append((kind or "?", "unknown_kind"))
            continue
        said = str(item.get("quote") or "")
        quote = quoted_in(said, text) or (quoted_in(said, own_subject) if own_subject else None)
        if quote is None:
            out.dropped.append((kind, "quote_not_verbatim"))
            continue
        if any(overlaps(quote, h) for h in hostile):
            out.dropped.append((kind, "instruction_in_text"))
            continue
        if kind == "request_to_owner" and (owner_authored or any(overlaps(quote, s) for s in owner_said)):
            out.dropped.append((kind, "owner_own_words"))
            continue
        if kind == "request_to_owner" and only_waits(quote, text):
            out.dropped.append((kind, "a_wait_not_a_request"))
            continue
        if kind in ("owner_replied", "owner_promise") and not owner_authored:
            out.dropped.append((kind, "not_by_owner"))
            continue
        actor = "owner" if owner_authored else "other"
        debtor = str(item.get("debtor") or "none")
        debtor = debtor if debtor in ("owner", "other", "none") else "none"
        if kind == "request_to_owner":
            debtor = "owner"  # a request to the owner is his to answer, whatever the model says
        keys: list[Key] = []
        for rk in list(item.get("keys") or ())[:KEYS_PER_EVENT_MAX]:
            if (key := _key(rk, text, subject)) is not None:
                keys.append(key)
                keep(key)
            else:
                out.dropped.append(("key", "not_verbatim"))
        playbook = item.get("playbook")
        out.events.append(
            Event(
                kind=kind,
                playbook=playbook if playbook in PLAYBOOKS else None,
                actor=actor,
                debtor=debtor,
                keys=tuple(keys),
                occurred_at=_when(item.get("occurred_at")),
                quote=quote[:QUOTE_MAX],
            )
        )
    later = [_said_later(e) for e in out.events]
    out.events = [  # a demoted grant beside the shop's own acknowledgement is that one
        e for e, was in zip(later, out.events, strict=True)
        if not (e.kind != was.kind and any(o.kind == e.kind for o in later if o is not e))
    ]  # fmt: skip
    if owner_authored:
        out.events = _owner_answer(out.events, text, replying=replying)
    elif any(e.kind == "request_to_owner" for e in out.events):
        # a message that asks the owner for something has settled nothing: its "thanks for writing" is
        # courtesy, never the other side's acknowledgement (§37 S053)
        out.events = [e for e in out.events if e.kind != "acknowledged"]
    for key in rule_keys(f"{subject}\n{text}"):
        keep(key)
    out.keys = list(seen_keys.values())

    for item in list(raw.get("effects") or ())[:EFFECTS_MAX]:
        kind, key = str(item.get("kind") or ""), str(item.get("key") or "")
        if key not in EFFECT_KEYS.get(kind, ()):
            out.dropped.append((f"{kind}/{key}", "not_in_fixed_list"))
            continue
        evidence = quoted_in(str(item.get("evidence") or ""), text)
        if evidence is None:
            out.dropped.append((f"{kind}/{key}", "evidence_not_verbatim"))
            continue
        react = item.get("reaction")
        reaction: tuple[str, str] | None = None
        if isinstance(react, Mapping):
            rk, rkey = str(react.get("kind") or ""), str(react.get("key") or "")
            if rkey in ACTION_KEYS.get(rk, ()):
                reaction = (rk, rkey)
        if (kind, key) == ("risk", "injection_attempt"):
            out.injected = True
        out.effects.append(Effect(kind, key, evidence[:QUOTE_MAX], reaction))
    if hostile and not any(e.key == "injection_attempt" for e in out.effects):
        out.effects.append(Effect("risk", "injection_attempt", _flat(hostile[0])[:QUOTE_MAX]))
    return out
