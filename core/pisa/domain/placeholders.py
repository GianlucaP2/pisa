"""A write whose words are a placeholder is never a change the user
asked.

A model padding a parallel batch of calls can fill the ones it doesn't mean with `{}`,
"PLACEHOLDER_NOT_USED" or "dummy". RAIL: whatever the cause, a write tool whose title, words or purpose is
empty or a placeholder is refused before it runs, and the model is told why.
"""

import re
from typing import Any

# The arguments that carry what the user asked: empty is never meant.
CONTENT = frozenset(
    {"title", "what", "text", "event", "task_ref", "email", "to", "purpose", "owner_said", "statement",
     "quote", "notes", "topic"}
)  # fmt: skip
# a tool's optional content argument: empty or "none" is the model omitting it (absent).
OPTIONAL = frozenset(
    {("create_event", "owner_said"), ("move_event", "owner_said"), ("cancel_event", "owner_said"),
     ("plan_stop", "what"), ("edit_draft", "text"),
     ("add_document", "notes")}  # a write now, its note is optional
)  # fmt: skip
# A whole value that names no thing: the words models put in a call they don't mean. Whole values only:
# "Test medico" or "Todo il giorno" are real titles. A bracketed one only as a short whole token ("<title>",
# "{}"): "[Lavoro] Riunione [urgente]" or an HTML body are real.
_PLACEHOLDER = re.compile(
    r"^\W*(?:placeholder\w*|\w*_not_used|not[ _]used|unused|dummy\w*|n/?a|null|none|nil|undefined|tbd|"
    r"lorem ipsum.*|string|xxx+|\.\.\.|…)\W*$|^[<{\[]\w{0,20}[>}\]]$",
    re.IGNORECASE,
)


def placeholder(value: str) -> bool:
    """A value that names nothing: blank, punctuation only, or a placeholder word ("dummy", "<title>")."""
    text = value.strip()
    return not any(c.isalnum() for c in text) or bool(_PLACEHOLDER.match(text))


def junk(args: dict[str, Any], tool: str = "") -> str | None:
    """The first content argument of a write (its title, words, purpose) that is empty or a placeholder, or
    None. An optional detail ("place": "none", create_event's `owner_said`) is the model's way of omitting it,
    never a reason to refuse."""
    for name, value in args.items():
        if name in CONTENT and (tool, name) not in OPTIONAL and isinstance(value, str) and placeholder(value):
            return name
    return None


def absent(args: dict[str, Any], tool: str) -> dict[str, Any]:
    """The arguments, a tool's optional content argument that is empty or a placeholder as absent (None)."""
    return {
        name: None if (tool, name) in OPTIONAL and isinstance(value, str) and placeholder(value) else value
        for name, value in args.items()
    }
