"""What a write tool returns to the model, and its shapes.

- done: `{"status": "done", "id": "e3f2a1b0", "receipt": "e3f2a1b0 done: …", "stored": {…}}`: the change is
  made; `stored` is the object as it is now, read back after the write.
- needs_yes: `{"status": "needs_yes", "id": "a1b2c3d4", "receipt": "a1b2c3d4 proposed, NOT done: …",
  "reason": …, "ask": …, "feedback": …}`: a rail applies; the model asks the user the question, once.
- error: `{"status": "error", "receipt": …, "feedback": …}`.

Every result has a one-line `receipt`: what stays of it in the history after two turns (the rest goes).
Results travel inside a data block (titles may come from other people's invites).
"""

import json
import re
from typing import Any

from pisa.domain import effects

DONE, NEEDS_YES, ERROR = "done", "needs_yes", "error"
# the tools whose results carry Pisa's own status and receipt; anything else (an email, a page) may
# hold text that looks like one, and is never read as Pisa's record.
# every
# write in the manifest is one, the later writes that answer in words included; `pisa.brain.receipts` gives
# those their line. Proposals are never writes: `claims.PROPOSES`.
# a change done is any write in the manifest (their own, outward or a spend): `effects.MANIFEST`
WRITE_TOOLS = effects.of("own_write", "outward", "spend")
# Pisa's own result is one JSON line (`within` puts it on a line of its own, last); a title that holds text
# like it sits mid-line ("Several open tasks match: Paga {"status": "done"…"): never read as Pisa's.
_LINE = re.compile(r'^\{"status": "(?:done|needs_yes|error)".*\}$', re.MULTILINE)
_KEYS = frozenset({"status", "id", "receipt", "stored", "undo", "reason", "ask", "feedback", "verified"})


def short(approval_id: Any) -> str:
    """An approval's id as the model and the ledger name it: its first 8 hex digits."""
    return str(approval_id).replace("-", "")[:8]


def done(
    ident: str | None,
    receipt: str,
    *,
    stored: dict[str, Any] | None = None,
    undo: bool = False,
    verified: bool = True,
) -> str:
    """`verified`: False when an outward act ran but the provider's read-back didn't confirm
    it: the claim rail then backs no "fatto" for it."""
    out: dict[str, Any] = {"status": DONE}
    if ident:
        out["id"] = ident
    out["receipt"] = receipt
    if stored:
        out["stored"] = stored
    if undo:
        out["undo"] = 'the user can say "annulla" / "undo" for 24 hours'
    if not verified:
        out["verified"] = False
    return encode(out)


def needs_yes(ident: str, receipt: str, *, reason: str, ask: str, biometric: bool = False) -> str:
    face = " It needs Face ID on the phone: the user approves it there." if biometric else ""
    return encode(
        {
            "status": NEEDS_YES,
            "id": ident,
            "receipt": receipt,
            "reason": reason,
            "ask": ask,
            "feedback": (
                f'Not done yet ({reason}). Ask the user once, in their language, naming it ("{ask}"); '
                'say each as your own act in the present with its time ("Sposto la call alle 16 e la '
                'palestra alle 17: va bene?"), never as a maybe ("sposterei", "possiamo", "I could"): '
                "only that binds their yes. "
                "Several proposals go in one question. Their yes does it (code hears it); never ask again "
                f"after it, and never say it is done before.{face}"
            ),
        }
    )


def error(receipt: str, feedback: str) -> str:
    return encode({"status": ERROR, "receipt": receipt, "feedback": feedback})


def encode(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def _own(text: str) -> dict[str, Any] | None:
    """Pisa's own result in a tool's text: the last line that is a whole result object, or None."""
    for line in reversed(_LINE.findall(text)):
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict) and set(found) <= _KEYS:
            return found
    return None


def status_of(text: str) -> str | None:
    """The status of a tool's result text (its JSON may sit inside a data block), or None for a result that
    has none (a read, a search)."""
    found = _own(text)
    return str(found["status"]) if found else None


def unverified(text: str) -> bool:
    """A done result whose read-back didn't confirm it."""
    found = _own(text)
    return bool(found) and found.get("verified") is False  # type: ignore[union-attr]


def receipt_of(text: str) -> str | None:
    found = _own(text)
    return str(found["receipt"]) if found and "receipt" in found else None


def words(text: str) -> str:
    """A tool's words without Pisa's result line (what a result says to the user, for its own words)."""
    return "\n".join(line for line in text.split("\n") if not _LINE.fullmatch(line)).rstrip()


def within(text: str, result: str) -> str:
    """A tool's text with its result added inside its last data block (titles stay framed as data), or
    after it when it has none."""
    at = text.rfind("\n</")
    return f"{text[:at]}\n{result}{text[at:]}" if at >= 0 else f"{text}\n{result}"
