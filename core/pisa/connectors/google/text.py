"""HTML email → plain text with the standard library (no rendering, no remote content).

What a person doesn't see is left out and counted (`pisa.domain.visible`).
A schema.org JSON-LD block (booking and order emails carry one) is kept after the text, one line each, for the
exact keys.
"""

import json

from pisa.domain.visible import html, normalize
from pisa.logging import get_logger

_log = get_logger("pisa.gmail.text")

LD_MAX = 4_000  # characters of JSON-LD kept per email


def _ld_line(raw: str) -> str | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    line = json.dumps(data, ensure_ascii=False, separators=(", ", ": "))
    return line if "schema.org" in line and len(line) <= LD_MAX else None


def html_to_text(markup: str) -> tuple[str, int, tuple[str, ...]]:
    """The visible text (JSON-LD after it), the count of hidden runs left out, and what each hidden element
    held (in memory only: compares it with the plain part)."""
    seen = html(markup)
    if seen.unparsed is not None:
        _log.warning("mail_html_unparsed", cause=seen.unparsed)
    text = normalize(seen.text)
    blocks = [line for raw in seen.ld if (line := _ld_line(raw))]
    return ("\n\n".join([text, *blocks]) if blocks else text), seen.hidden, seen.hidden_texts
