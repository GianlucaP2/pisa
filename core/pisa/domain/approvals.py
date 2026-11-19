"""Approvals: an action outside Pisa waits for the user's yes. Pure rules, no I/O.

- **States**: `pending` → `approved` (executing) → `done` | `failed`; `pending` → `rejected` | `expired`. A
  decision on anything but `pending` is refused (`approval_decided`, or `approval_expired`).
- **Limits**: expires 10 minutes after creation; at most 5 pending at once.
- **Undo**: calendar kinds, for 24 hours after they ran (Pisa acts and offers Undo rather than asking);
  a sent email can't be undone (the approval says so).
- **Origin**: `words` (the user's own words: done at once, no question), `proposal` (Pisa proposed it:
  one yes), `held` (from outside text, or not in the user's words: one yes). `superseded`: an email's
  approval replaced by the read-back of a newer version.
- **Biometric**: an approval that `requires_biometric` (an email to someone outside the thread) is approved
  only with `auth` = biometric (Face ID).
"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo

from pisa.domain.tools import TOOLS, ApprovalKind

Kind = ApprovalKind
Status = Literal["pending", "approved", "rejected", "expired", "done", "failed", "superseded"]
Auth = Literal["tap", "voice", "biometric", "words", "autonomy:tell", "autonomy:silent"]
Origin = Literal["words", "proposal", "held"]

KINDS: tuple[Kind, ...] = get_args(Kind)
EXPIRES_AFTER = timedelta(minutes=10)
# Pisa keeps listing what it asked and nobody answered, this long (an overnight correction finds it).
UNANSWERED_KEPT = timedelta(hours=6)
MAX_PENDING = 5
LIST_ALL_DAYS = 30  # GET /v1/approvals?status=all
# 24 h for what Pisa changed in the calendar, never for a sent email: each tool's undo window, in the registry
UNDO_MINUTES: dict[str, int | None] = {t.approval: t.undo_minutes for t in TOOLS if t.approval is not None}
# the tool that asks with each approval kind
TOOL_OF: dict[str, str] = {t.approval: t.name for t in TOOLS if t.approval is not None}
DETAILS_MAX_LINES = 8
DETAIL_MAX = 200
SUMMARY_MAX = 500


def fit(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0] or text[: limit - 1]
    return cut + "…"


def fit_details(lines: list[str]) -> list[str]:
    """The protocol's limits: at most 8 lines of at most 200 characters."""
    return [fit(line, DETAIL_MAX) for line in lines if line.strip()][:DETAILS_MAX_LINES]


TEXT_LINE_PREFIXES = ("Testo: ", "Text: ")  # an email's text (pisa.domain.actions.text_line)


def push_body(summary: str, details: list[str]) -> str:
    """An approval's APNs body: the summary and its details, without the email's text (a lock screen shows
    who and what, never the message itself)."""
    return "\n".join([summary, *(d for d in details if not d.startswith(TEXT_LINE_PREFIXES))])


def undo_until(kind: str, executed_at: datetime) -> datetime | None:
    minutes = UNDO_MINUTES.get(kind)
    return executed_at + timedelta(minutes=minutes) if minutes else None


# The app reads a replaced email question as expired (an unknown status broke the whole list).
SUPERSEDED_RESULT = "Replaced by a newer version of the email."
# On the wire, `undo_minutes` stays ≤ 60 until the app accepts 1440 (IOS 17; builds ≤ 21 drop the frame): the
# server's own undo window (`undo_until`, 24 h) is unchanged and Changes shows it.
WIRE_UNDO_MINUTES_MAX = 60


def view(row: dict[str, Any], now: datetime, timezone: str) -> dict[str, Any]:
    """The approval as the API and the app see it, times in the user's zone. A pending one past
    its time reads `expired` even before the minute's sweep marks it."""
    tz = ZoneInfo(timezone)
    status = row["status"]
    if status == "pending" and row["expires_at"] <= now:
        status = "expired"
    result = row["result"]
    if status == "superseded":  # an email's question replaced by a newer read-back: over, like expired
        status, result = "expired", result or SUPERSEDED_RESULT
    until = row["undo_until"]
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "summary": row["summary"],
        "details": list(row["details"] or []),
        "created_at": row["created_at"].astimezone(tz).isoformat(),
        "expires_at": row["expires_at"].astimezone(tz).isoformat(),
        "status": status,
        "result": result,
        "undo_until": until.astimezone(tz).isoformat()
        if until and not row["undone_at"] and until > now
        else None,
        "requires_biometric": row["requires_biometric"],
        "origin": row.get("origin", "proposal"),
        "tier": row.get("tier", 1),
        "verified": row.get("verified"),
    }


def action_done(row: dict[str, Any], now: datetime, timezone: str) -> dict[str, Any]:
    """The `action_done` message's fields: what Pisa changed, its result, and until when it can be
    undone (absent when it can't)."""
    seen = view(row, now, timezone)
    return {"approval_id": seen["id"], "kind": seen["kind"], "summary": seen["summary"],
            "result": seen["result"] or "", "undo_until": seen["undo_until"]}  # fmt: skip


_NOT_DONE = {"it-IT": "Ok, non lo faccio.", "en-US": "OK, I won't do it."}
_FAILED = {"it-IT": "Non è stato fatto: {cause}", "en-US": "It wasn't done: {cause}"}
MAYBE = ("Potrebbe ", "It may have ")  # a failure whose outcome Pisa can't know


def outcome_sentence(status: str, result: str | None, lang: str) -> str | None:
    """What Pisa says after the user answered on the phone: the result, the failure's cause, or "OK, I
    won't"."""
    if status == "done" and result:
        return result
    if status == "failed" and result and result.startswith(MAYBE):
        return result  # never "it wasn't done: it may have been done"
    if status == "failed":
        return _FAILED[lang].format(cause=result or "")
    if status == "rejected":
        return _NOT_DONE[lang]
    return None


# a proposal still waiting this close to its end is said once more, by Pisa's voice when it can
LAPSE_NOTICE_BEFORE_S = 120
_LAPSING = {
    "it-IT": "«{summary}» aspetta ancora il tuo sì: scade tra due minuti.",
    "en-US": "«{summary}» is still waiting for your yes: it lapses in two minutes.",
}


def lapsing(summary: str, lang: str) -> str:
    return _LAPSING.get(lang, _LAPSING["en-US"]).format(summary=summary.strip().rstrip("?.!")[:200])


# --- a Face ID approval, signed by the phone's key (protocol/README.md) ------------------------------------

DIGEST_HEADER = "pisa-approval-digest/1"
# Server bookkeeping a payload may gain after the challenge, never part of what is approved: `heard`
# (marks an email read back in full). No other path writes an approval's payload before its decision.
DIGEST_EXCLUDED = frozenset({"heard"})


def payload_sha256(payload: dict[str, Any]) -> str:
    kept = {k: v for k, v in payload.items() if k not in DIGEST_EXCLUDED}
    text = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def recipients_line(addresses: list[str]) -> str:
    return ",".join(sorted({a.strip().lower() for a in addresses if a.strip()}))


def approval_digest(kind: str, payload: dict[str, Any], recipients: list[str]) -> str:
    """sha256 hex of `pisa-approval-digest/1\\n<kind>\\n<payload_sha256>\\n<recipients>`, no trailing
    newline."""
    preimage = "\n".join((DIGEST_HEADER, kind, payload_sha256(payload), recipients_line(recipients)))
    return hashlib.sha256(preimage.encode()).hexdigest()
