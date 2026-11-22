"""One consent card for every outward act. Pure rules, no I/O.

- **The record**: every act Pisa would do outside itself is one `Action{kind, payload, cost, reversible,
  data_out, tier}`, made from an approval's kind, its stored payload and who it writes to or notifies. Every
  approval kind maps onto it (`KINDS`): there is no second system. Its `hash` is the digest (protocol
  §): kind, payload (without the server's bookkeeping) and recipients; cost, reversibility and tier are
  functions of the kind and the payload, so the hash binds them too.
- **The yes is bound to the hash**: stored when the card is made, recomputed from the row when the yes comes.
  A changed recipient, amount, time or text is a different hash: the yes is void and nothing runs. One yes
  runs it once (the row's status, under its lock).
- **How long a yes holds**: 10 minutes on the phone (a tap, Face ID); by voice, 120 s from the first time Pisa
  read the card out, and a spoken "sì" counts only within 10 s of Pisa's latest read-out (later, Pisa reads it
  again: `voice_yes`).
- **Tiers**: 0 information (nothing leaves; never a card); 1 send, book, contact (one yes); 2 money or Pisa's
  own code (Face ID, signed by the phone's Secure Enclave key, as the signed actions).
- **One renderer per surface**: `spoken` (TTS: who, what, when, how much, what can't be undone) and
  `phone_card` (the app's card).
- **Read-back**: after acting, the provider's own state is read back; Pisa says it is done only when it
  confirms (`verified`); otherwise `unverified` is what Pisa says, never "fatto".
- **An ambiguous target** asks which (`which`), never guesses.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pisa.domain import approvals, quarantine

Tier = Literal[0, 1, 2]
Verified = Literal["verified", "not_verified"]
VoiceYes = Literal["ok", "late", "over"]

PHONE_TTL = approvals.EXPIRES_AFTER  # 10 minutes: a tap or Face ID on the phone
VOICE_TTL = timedelta(seconds=120)  # by voice: from the first time Pisa read the card out
READ_OUT_GRACE = timedelta(seconds=10)  # a "sì" later than this after Pisa's read-out: Pisa reads it again
# until the phone says when it finished playing a card (`approval_heard`),
# Pisa's read-out ends this long after the text left: 14 characters a second
SPOKEN_CHARS_PER_S = 14
TIERS: tuple[Tier, ...] = (0, 1, 2)


@dataclass(frozen=True)
class Spec:
    """What an approval kind is as an act: the brain tool that proposes it, its tier, whether Pisa undoes
    it."""

    tool: str
    tier: Tier
    reversible: bool


# Every approval kind (pisa.domain.approvals.KINDS) is here: a test fails when one is missing.
KINDS: dict[str, Spec] = {
    "calendar_create": Spec("create_event", 1, True),
    "calendar_move": Spec("move_event", 1, True),
    "calendar_cancel": Spec("cancel_event", 1, True),
    "email_send": Spec("send_draft", 1, False),
}
# Tier 2 by its payload, whatever the kind: money that leaves them, or Pisa's own code changed.
MONEY_KEYS = ("amount", "price", "cost")
CODE_KEYS = ("merge", "code_change")


@dataclass(frozen=True)
class Action:
    kind: str
    payload: Mapping[str, Any]
    cost: str | None  # the amount as stated ("12.50 EUR"), None when nothing is paid
    reversible: bool
    data_out: tuple[str, ...]  # every address it writes to or notifies: lower case, sorted, once
    tier: Tier

    @property
    def hash(self) -> str:
        """The yes is bound to this (the digest: one binding for the voice, the tap and Face ID)."""
        return approvals.approval_digest(self.kind, dict(self.payload), list(self.data_out))

    @property
    def face_id(self) -> bool:
        return self.tier == 2


def cost_of(payload: Mapping[str, Any]) -> str | None:
    for key in MONEY_KEYS:
        value = payload.get(key)
        if value not in (None, "", 0, "0"):
            return str(value)
    return None


def tier_of(kind: str, payload: Mapping[str, Any]) -> Tier:
    """2 for money or Pisa's own code; else the kind's (1 for every approval kind today); 0 for none."""
    if cost_of(payload) is not None or any(payload.get(k) for k in CODE_KEYS):
        return 2
    spec = KINDS.get(kind)
    return spec.tier if spec is not None else 0


def of(kind: str, payload: Mapping[str, Any], recipients: list[str]) -> Action:
    """The act an approval stands for (its row's kind and payload; `recipients`: whom it writes to or
    notifies, as the digest reads them)."""
    spec = KINDS.get(kind)
    data_out = tuple(a for a in approvals.recipients_line(recipients).split(",") if a)
    return Action(
        kind=kind,
        payload=dict(payload),
        cost=cost_of(payload),
        reversible=spec.reversible if spec is not None else False,
        data_out=data_out,
        tier=tier_of(kind, payload),
    )


def needs_face_id(action: Action, requires_biometric: bool) -> bool:
    """Tier 2 always; tier 1 when the kind's own rule asks it (an email to someone outside the thread)."""
    return action.face_id or requires_biometric


SPOKEN_MAX = timedelta(seconds=60)  # never a longer window


def spoken_for(text: str) -> timedelta:
    """How long Pisa takes to say `text` aloud up to its question (14 characters a second): the read-out ends
    that much later, at most `SPOKEN_MAX` (a long reply never stretches the 10 s or the 120 s: a long email
    read back that ends "late" just has its short card read again; `approval_heard` is the real fix)."""
    asked = text[: text.rfind("?") + 1] or text
    return min(timedelta(seconds=len(" ".join(asked.split())) / SPOKEN_CHARS_PER_S), SPOKEN_MAX)


# --- how long a yes holds -----------------------------------------------------------------------------------


def voice_until(read_out_at: datetime | None, created_at: datetime) -> datetime:
    """A spoken yes holds 120 s from the first read-out (the card's creation when none was stamped)."""
    return (read_out_at or created_at) + VOICE_TTL


def voice_yes(
    said_at: datetime, *, read_out_at: datetime | None, until: datetime | None, created_at: datetime
) -> VoiceYes:
    """A spoken "sì" said at `said_at`: `ok`; `late`, more than 10 s after Pisa's latest read-out (Pisa reads
    it again); `over`, past the 120 s a spoken yes holds (the phone's card holds 10 minutes)."""
    if said_at > (until or voice_until(None, created_at)):
        return "over"
    if said_at - (read_out_at or created_at) > READ_OUT_GRACE:
        return "late"
    return "ok"


# --- the renderers ------------------------------------------------------------------------------------------

_SPOKEN = {
    "it-IT": {
        "who": "a {who}",
        "notified": "avviso {who}",
        "cost": "costa {cost}",
        "final": "non si può annullare",
        "undo": "si può annullare per 24 ore",
        "ask": "Confermi?",
        "again": "Te lo rileggo:",
        "phone": "Il sì a voce vale due minuti dopo che te l'ho letto: confermalo sul telefono.",
    },
    "en-US": {
        "who": "to {who}",
        "notified": "I'll tell {who}",
        "cost": "it costs {cost}",
        "final": "it can't be undone",
        "undo": "it can be undone for 24 hours",
        "ask": "Shall I?",
        "again": "Here it is again:",
        "phone": "A spoken yes holds two minutes after I read it: confirm it on the phone.",
    },
}


def _words(lang: str) -> dict[str, str]:
    return _SPOKEN.get(lang, _SPOKEN["en-US"])


def on_the_phone(lang: str) -> str:
    """Past the 120 s a spoken yes holds: the phone's card (10 minutes) decides it."""
    return _words(lang)["phone"]


def _who(action: Action, payload: Mapping[str, Any]) -> str:
    """The people it reaches, as the card names them (an email's names; a calendar change's notified)."""
    if action.kind == "email_send":
        return str(payload.get("names") or ", ".join(action.data_out))
    if payload.get("send_updates") == "all":
        return ", ".join(str(n) for n in payload.get("notified") or [])
    return ""


def spoken(action: Action, summary: str, lang: str, *, again: bool = False) -> str:
    """The card Pisa reads out (TTS): what, to whom, when, how much, and what can't be undone, then the one
    question. `again`: Pisa reads it again (a yes came too late after the read-out)."""
    w = _words(lang)
    what = approvals.fit(summary, approvals.SUMMARY_MAX).rstrip("?.! ")
    parts = [what]
    who = _who(action, action.payload)
    if who and who not in what:
        parts.append(w["notified" if action.kind != "email_send" else "who"].format(who=who))
    when = str(action.payload.get("when") or "")
    if when and when not in what:
        parts.append(when)
    if action.cost is not None:
        parts.append(w["cost"].format(cost=action.cost))
    parts.append(w["undo"] if action.reversible else w["final"])
    # a value only outside text gave is named before the yes, as on the card
    parts += [
        quarantine.source_line(what, source, lang)
        for what, source in action.payload.get("outside") or ()
        if what in quarantine.WHATS
    ]
    line = ", ".join(parts) + ". " + w["ask"]
    return f"{w['again']} {line}" if again else line


def phone_card(
    action: Action, summary: str, details: list[str], *, verified: str | None, lang: str
) -> dict[str, Any]:
    """The app's card (one renderer for the phone): the same facts as the spoken one, and the hash the yes is
    bound to, the tier (2: Face ID) and, after acting, the read-back."""
    return {
        "what": summary,
        "who": _who(action, action.payload) or None,
        "when": str(action.payload.get("when") or "") or None,
        "cost": action.cost,
        "reversible": action.reversible,
        "irreversible_line": None if action.reversible else _words(lang)["final"],
        "data_out": list(action.data_out),
        "details": list(details),
        "tier": action.tier,
        "face_id": action.face_id,
        "action_hash": action.hash,
        "verified": verified,
    }


# --- read-back ----------------------------------------------------------------------------------------------

_UNVERIFIED = {
    ("it-IT", "email"): "Ho passato l'email a Gmail, ma non riesco a verificare che sia partita: "
    "controlla la posta inviata.",
    ("it-IT", "calendar"): "Ho chiesto la modifica di «{title}» a Google, ma non riesco a verificarla: "
    "controlla il calendario.",
    ("en-US", "email"): "I handed the email to Gmail, but I can't verify it left: check your sent mail.",
    ("en-US", "calendar"): "I asked Google to change «{title}», but I can't verify it: check the calendar.",
}


def unverified(kind: str, payload: Mapping[str, Any], lang: str) -> str:
    """What Pisa says when the provider's read-back doesn't confirm the act: never "fatto"."""
    family = "email" if kind == "email_send" else "calendar"
    lang = lang if lang in ("it-IT", "en-US") else "en-US"
    return _UNVERIFIED[lang, family].format(title=str(payload.get("title") or "?"))


def instant(value: Any) -> datetime | None:
    """A Google `{dateTime}` (or an ISO text) as an aware datetime, for comparing the same moment."""
    text = value.get("dateTime") if isinstance(value, Mapping) else value
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def calendar_confirms(kind: str, payload: Mapping[str, Any], live: Mapping[str, Any] | None) -> bool:
    """The calendar's read-back: the event as Google holds it now is what the card said."""
    if kind == "calendar_cancel":
        return live is None or live.get("status") == "cancelled"
    if live is None or live.get("status") == "cancelled":
        return False
    wanted = payload["body"]["start"] if kind == "calendar_create" else payload["start"]
    want, got = instant(wanted), instant(live.get("start"))
    return want is not None and got is not None and want == got


# --- an ambiguous target ------------------------------------------------------------------------------------

_WHICH = {
    "it-IT": ("Quale {target}: {listed}?", " o "),
    "en-US": ("Which {target}: {listed}?", " or "),
}


def which(target: str, candidates: list[str], lang: str) -> str:
    """One question naming every candidate ("Quale Marco: Marco Rossi o Marco Bianchi?"): Pisa never picks."""
    form, last = _WHICH.get(lang, _WHICH["en-US"])
    shown = [c for c in dict.fromkeys(candidates) if c][:5]
    listed = ", ".join(shown[:-1]) + last + shown[-1] if len(shown) > 1 else "".join(shown)
    return form.format(target=" ".join(target.split())[:60], listed=listed)
