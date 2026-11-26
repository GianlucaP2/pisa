"""The portrait at the top of "Su di te". Pure rules,
no I/O.

- **From what**: their active claims only, never a candidate, never a sensitive one, never an
  email rule (a setting, not them). Fewer than `MIN_CLAIMS`: Pisa knows too little, the portrait is empty (no
  model call).
- **Rebuilt** only when what it is written from changed (`claims_hash`: the claims' ids and sentences, their
  language).
- **What the model may write** (`checked`): themes from a fixed list, lines that each name the claims
  behind them (by their number in the list Pisa gave), a line's words sharing content with its claims (no
  fact of its own); the rest is dropped by code. Lengths are capped.
- **Their corrections** (`corrected`): "Non è vero" removes the line; "Correggi" puts their words in its
  place.
- **"Pisa didn't know what they like"** (`said_unknown`): Pisa's own reply said so (code's phrases, Pisa's
  words).
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pisa.domain.profile import is_sensitive, stems

MIN_CLAIMS = 3
TEXT_MAX = 400
LINE_MAX = 160
THEMES_MAX = 8
LINES_MAX = 6
CLAIM_IDS_MAX = 10
WORDS_MAX = 200  # their correction's words
CLAIMS_READ = 60  # claims a portrait is written from, at most (the most confident first)
MAX_TOKENS = 900
EMAIL_RULES = ("email_sender:", "email_kind:")
THEMES = ("city", "food", "music", "evenings", "travel", "moving", "work", "style", "other")
TITLES = {
    "it-IT": {"city": "Dove vivi", "food": "Cibo", "music": "Musica", "evenings": "Serate",
              "travel": "Viaggi", "moving": "Come ti muovi", "work": "Lavoro",
              "style": "Come ti piace che ti parli", "other": "Altro"},
    "en-US": {"city": "Where you live", "food": "Food", "music": "Music", "evenings": "Evenings",
              "travel": "Travel", "moving": "How you get around", "work": "Work",
              "style": "How you like me to talk", "other": "Other"},
}  # fmt: skip


@dataclass(frozen=True)
class Source:
    """A claim the portrait may be written from."""

    id: str
    kind: str
    statement: str


def usable(row: dict[str, Any]) -> bool:
    return (
        row["state"] == "active"
        and not row["sensitive"]
        and row["kind"] != "sensitivity"
        and not str(row.get("subject_key") or "").startswith(EMAIL_RULES)
    )


def claims_hash(sources: Sequence[Source], lang: str) -> str:
    text = "\n".join(sorted(f"{s.id}|{s.kind}|{s.statement}" for s in sources)) + f"\n{lang}"
    return hashlib.sha256(text.encode()).hexdigest()


def empty() -> dict[str, Any]:
    return {"text": "", "themes": []}


def _clip(text: Any, cap: int) -> str:
    one = " ".join("".join(c if c.isprintable() else " " for c in str(text or "")).split())
    if len(one) <= cap:
        return one
    cut = one[: cap - 1]
    return (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;:") + "…"


def checked(raw: dict[str, Any], sources: Sequence[Source], lang: str) -> dict[str, Any]:
    """The model's portrait as code keeps it: known themes (an unknown one is `other`), each line naming 1 to
    10 of the given claims by their number (1-based) and sharing content with them, capped lengths; a theme
    with no line left goes. No line left: the empty portrait."""
    titles = TITLES["it-IT" if lang.startswith("it") else "en-US"]
    themes: dict[str, dict[str, Any]] = {}
    for theme in raw.get("themes") or []:
        tid = str(theme.get("id") or "other").strip().lower()
        tid = tid if tid in THEMES else "other"
        # the title is code's (the model's own words would be a line no claim stands behind)
        bucket = themes.setdefault(tid, {"id": tid, "title": titles[tid], "lines": []})
        for line in theme.get("lines") or []:
            numbers = [n for n in line.get("claims") or [] if isinstance(n, int) and 1 <= n <= len(sources)]
            behind = list(dict.fromkeys(sources[n - 1] for n in numbers))[:CLAIM_IDS_MAX]
            text = _clip(line.get("text"), LINE_MAX)
            if not behind or not text:
                continue
            if not stems(text) & stems(" ".join(s.statement for s in behind)):
                continue  # a line that says nothing its claims say
            if is_sensitive("like", text):
                continue  # a sensitive word the claims didn't have never reaches the page
            if len(bucket["lines"]) < LINES_MAX:
                bucket["lines"].append({"text": text, "claim_ids": [s.id for s in behind]})
    kept = [t for t in themes.values() if t["lines"]][:THEMES_MAX]
    if not kept:
        return empty()
    text = _clip(raw.get("text"), TEXT_MAX)
    if is_sensitive("like", text) or not stems(text) & stems(" ".join(s.statement for s in sources)):
        text = ""  # held to the lines' rules (no sensitive word, their claims' content)
    return {"text": text, "themes": kept}


def find_line(themes: Iterable[dict[str, Any]], claim_ids: Sequence[str]) -> tuple[int, int] | None:
    """(theme, line) whose claim_ids are exactly these, or None."""
    want = sorted(claim_ids)
    for t, theme in enumerate(themes):
        for n, line in enumerate(theme["lines"]):
            if sorted(line["claim_ids"]) == want:
                return t, n
    return None


def corrected(
    themes: list[dict[str, Any]], at: tuple[int, int], words: str | None, kept_id: str | None
) -> list[dict[str, Any]]:
    """The themes after their answer on one line: removed ("Non è vero", or their words now a sensitive
    claim), or their words in its place, behind the one claim that keeps them."""
    out = [dict(t, lines=list(t["lines"])) for t in themes]
    t, n = at
    if words is None or kept_id is None:
        del out[t]["lines"][n]
    else:
        out[t]["lines"][n] = {"text": _clip(words, LINE_MAX), "claim_ids": [kept_id]}
    return [t for t in out if t["lines"]]


# Pisa's own words saying it doesn't know what they like (the frontier comparison: "Non so ancora cosa ti
# piace davvero", "Dei tuoi gusti musicali non so ancora nulla", "Sui gusti non ho altro").
_UNKNOWN = re.compile(
    r"\b(?:non so (?:ancora )?(?:cosa|che cosa|quali?|che) ti piac|"
    r"(?:dei|sui) tuoi gusti[^.?!]{0,40}(?:non so|non ho|niente|nulla)|"
    r"non conosco (?:ancora )?i tuoi gusti|"
    r"i don'?t (?:yet )?know what you like|i don'?t know your tastes)",
    re.IGNORECASE,
)


def said_unknown(reply: str) -> bool:
    return _UNKNOWN.search(" ".join(reply.replace("\u2019", "'").split())) is not None
