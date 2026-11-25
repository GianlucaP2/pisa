"""Pisa's skills, "how Pisa does this", retrieved per turn and placed after the cached
prefix.

An entry is a Markdown file in `pisa/brain/skills/`: a front matter (name, title, `when`, `triggers`, `tools`,
`status`, `source`, optionally `pin`) and a body (what matters, how Pisa does it, pitfalls, examples). Only
`status: reviewed` entries are used: a learned draft waits for review.

Retrieval: the user's words, against each entry's title + `when` + triggers.
- With an embedding model (`memory.embed`), the entries' vectors live in pgvector (`pisa.services.skills`):
  cosine.
- Without one, here: character-trigram TF-IDF (Italian and English morphology, typos: "calendaro" still finds
  "calendario"); free, no call.
- The top 3 above a floor, plus the pinned ones (`night` between 00:00 and 05:00; `research` once a research
  tool was called), plus, for a short follow-up ("quella di stasera"), the previous turn's skills.

The bodies are guidance, not a pipeline: "your own notes, follow them when they fit".
"""

import math
import re
from collections import Counter
from collections.abc import Container, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pisa.domain import locale
from pisa.domain.profile import plain

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
TOP_K = 3
CARRY_WORDS = 4  # a follow-up this short continues the previous turn's skills…
CARRY_BELOW = 0.3  # … unless its own words clearly match another one ("anzi no, annulla")
FLOOR = 0.08  # below this nothing is injected: Pisa improvises from the base prompt
NIGHT = range(0, 5)  # the `night` pin: 00:00-05:00


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    when: str
    triggers: tuple[str, ...]
    status: str
    body: str
    pin: str = ""  # a context that always brings it in ("night", "research"), whatever the words
    tools: tuple[str, ...] = ()  # the tools its body tells the model to use (none: it needs none)

    @property
    def key_text(self) -> str:
        return " | ".join([self.title, self.when, *self.triggers])


def parse(text: str, name: str = "?") -> Skill:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{name}: no front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.split("  #")[0].strip()
    return Skill(
        name=meta["name"],
        title=meta["title"].strip('"'),
        when=meta["when"],
        triggers=tuple(t.strip() for t in meta.get("triggers", "").split("|") if t.strip()),
        status=meta.get("status", "draft"),
        body=match.group(2).strip(),
        pin=meta.get("pin", ""),
        tools=tuple(t.strip() for t in meta.get("tools", "").split(",") if t.strip()),
    )


@cache
def load(directory: Path = SKILLS_DIR) -> tuple[Skill, ...]:
    """The reviewed skills (a learned draft is used only once it is reviewed)."""
    skills = [parse(p.read_text(encoding="utf-8"), p.name) for p in sorted(directory.glob("*.md"))]
    return tuple(s for s in skills if s.status == "reviewed")


def named(name: str) -> Skill | None:
    """A reviewed entry by name; None when it is missing or set back to draft (the brain still loads)."""
    return next((s for s in load() if s.name == name), None)


def _grams(text: str) -> Counter[str]:
    grams: Counter[str] = Counter()
    for word in re.findall(r"[a-z0-9]+", plain(text)):
        padded = f" {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
        grams[f"w:{word}"] += 2  # whole words weigh more than their pieces
    return grams


class Library:
    """The skills' trigram index (free, no call); `pisa.services.skills` puts pgvector in front of it."""

    def __init__(self, skills: Sequence[Skill]) -> None:
        self.skills = list(skills)
        docs = [_grams(s.key_text) for s in self.skills]
        df: Counter[str] = Counter()
        for d in docs:
            df.update(d.keys())
        n = len(docs)
        self._idf = {g: math.log((n + 1) / (c + 0.5)) for g, c in df.items()}
        self._docs = [self._weigh(d) for d in docs]

    def _weigh(self, grams: Counter[str]) -> dict[str, float]:
        vec = {g: (1 + math.log(c)) * self._idf.get(g, 0.0) for g, c in grams.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {g: v / norm for g, v in vec.items()}

    def rank(self, words: str) -> list[tuple[float, Skill]]:
        """Every unpinned skill with its score, best first (cosine of the trigram vectors)."""
        q = self._weigh(_grams(words))
        scores = [sum(w * d.get(g, 0.0) for g, w in q.items()) for d in self._docs]
        ranked = sorted(zip(scores, self.skills, strict=True), key=lambda p: -p[0])
        return [(s, sk) for s, sk in ranked if not sk.pin]

    def search(
        self,
        words: str,
        *,
        context: frozenset[str] = frozenset(),
        previous: Sequence[str] = (),
        ranked: list[tuple[float, Skill]] | None = None,
        k: int = TOP_K,
    ) -> list[tuple[float, Skill]]:
        """Top-k by the words (or `ranked`, the embeddings' order), plus the pinned skills of the context,
        plus the previous turn's skills for a short follow-up."""
        pinned = [(1.0, s) for s in self.skills if s.pin and s.pin in context]
        found = [(s, sk) for s, sk in (ranked if ranked is not None else self.rank(words))[:k] if s >= FLOOR]
        carried: list[tuple[float, Skill]] = []
        if len(re.findall(r"\w+", words)) <= CARRY_WORDS and (not found or found[0][0] < CARRY_BELOW):
            carried = [(0.5, s) for s in self.skills if s.name in previous]
        out: list[tuple[float, Skill]] = []
        for score, skill in [*pinned, *carried, *found]:
            if all(skill.name != o.name for _, o in out):
                out.append((score, skill))
        return out[: k + len(pinned)]


def contexts(hour: int, researched: bool, drawing: bool = False, blocks: bool = False) -> frozenset[str]:
    """The pins this turn brings in: the night (00:00-05:00), research once a research tool was called, the
    drawing skill on a turn that draws (their words ask for a picture, another view of a kept
    drawing, or a tap in one; the box on), the drawing blocks' skill (the same, box on or
    off)."""
    return (
        frozenset({"night"} if hour in NIGHT else set())
        | (frozenset({"research"}) if researched else frozenset())
        | (frozenset({"drawing"}) if drawing else frozenset())
        | (frozenset({"drawing-blocks"}) if blocks else frozenset())
    )


# an example written once per language: "**Example (it)**" / "**Example (en)**", up to the next bold heading
_EXAMPLE = re.compile(r"^\*\*Example \((en|it)\)\*\*\n.*?(?=\n\n\*\*|\Z)", re.DOTALL | re.MULTILINE)


def in_language(body: str, lang: str) -> str:
    """The body with only the example in `lang`, headed
    "**Example**"; a body without per-language examples as it is."""
    want = locale.short(lang)

    def keep(m: re.Match[str]) -> str:
        return m.group(0).replace(f"**Example ({want})**", "**Example**", 1) if m.group(1) == want else ""

    return re.sub(r"\n{3,}", "\n\n", _EXAMPLE.sub(keep, body)).strip()


def offered(found: Sequence[tuple[float, Skill]], tools: Container[str]) -> list[tuple[float, Skill]]:
    """The skills whose guidance this turn can follow: one that names tools none of which the turn has
    (their feature off: "Te lo ricordo alle 6" with `remember_to` never offered) goes; one with no tools or
    at least one of them stays."""
    return [(score, s) for score, s in found if not s.tools or any(t in tools for t in s.tools)]


def block(found: Sequence[tuple[float, Skill]], lang: str) -> str:
    """The turn's skills, for the dynamic block after the cached prefix: guidance, not a pipeline."""
    if not found:
        return ""
    parts = [
        "\nHow Pisa usually does this (your own notes; follow them when they fit, think for yourself when "
        "the situation is new or they don't fit):"
    ]
    parts += [f"### {s.title}\n{in_language(s.body, lang)}" for _, s in found]
    return "\n\n".join(parts)
