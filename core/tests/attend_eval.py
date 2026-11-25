"""The attend eval with fake models (no call, no cost):
`PYDANTIC_AI_NO_BANNER=1 uv run python -m tests.attend_eval`.

- `labelled` answers each candidate with its label (the eval's ceiling): agreement 1.000.
- `chatty` says everything now, by voice: the rails keep it to the breakpoints and to the voice rate.
- `careless` drops everything, floors included, and changes a floor's time: no floor is missed after the
  rails.
- `rules` gives no answer: today's rules decide every case (the baseline the model has to beat).
- `slow` answers after the 3 s budget: today's rules, counted as timeouts.
"""

import asyncio
import json
from typing import Any

from pisa.domain import attend as A
from pisa_tools import attend_eval as E

_CHOICE: dict[str, A.Choice] = {
    "now": "now",
    "breakpoint": "breakpoint",
    "later": "fold_next",
    "drop": "drop",
}


async def labelled(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
    out = []
    for c in members:  # the members of one call (floors go by the rules, emails in a call of their own)
        n = int(c.id.rsplit(".", 1)[1])
        s = case.signals[n]
        choice = _CHOICE[s.label]
        if s.label == "later" and s.deadline_min:
            choice = "at"
        at = case.at.replace(second=0, microsecond=0) + E.timedelta(minutes=(s.deadline_min or 60) // 2)
        channel: A.Channel = "phone_banner" if s.label == "drop" else "voice_and_phone"
        if s.label == "drop" and s.must_show:
            choice = "show_silently"
        out.append(A.Decision((c.id,), choice, channel, c.text, at if choice == "at" else None, f"case {n}"))
    # What arrives together at the same pause is said in one sentence ("Due cose: …").
    together = [d for d, c in zip(out, members, strict=True) if d.choice == "breakpoint" and not c.alone]
    if len(together) > 1:
        group = [c for c in members if c.id in {i for d in together for i in d.ids}]
        merged = A.Decision(tuple(c.id for c in group), "breakpoint", "voice_and_phone",
                            A.merged_text(group, "it-IT"), purpose="insieme")  # fmt: skip
        out = [d for d in out if d not in together] + [merged]
    return out


async def chatty(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
    return [A.Decision((c.id,), "now", "voice", c.text, purpose="subito") for c in members]


async def downgrader(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
    """Puts every floor off to a pause on a silent banner: the rails restore them."""
    return [A.Decision((c.id,), "breakpoint", "phone_banner", c.text) for c in members]


async def careless(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
    return [A.Decision((c.id,), "drop", "voice", "Esci quando vuoi, alle 23:59.") for c in members]


async def rules(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
    return []


async def slow(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
    await asyncio.sleep(A.DECIDE_TIMEOUT_NO_FLOOR_S + 0.5)
    return await labelled(case, members, moment)


FAKES = {
    "labelled": labelled,
    "chatty": chatty,
    "careless": careless,
    "downgrader": downgrader,
    "rules": rules,
}


def numbers(fake: Any, part: str | None = None) -> dict[str, Any]:
    out = asyncio.run(E.evaluate(fake, E.select(part)))
    out.pop("results")
    return out


if __name__ == "__main__":
    for name, fake in FAKES.items():
        print(name, json.dumps(numbers(fake)))
