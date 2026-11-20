"""Cost per **good** answer, the failures kept. Pure rules.

- **The replay**: the run's whole spend (every call: a failed scenario's, one a pass's cap stopped, all of it)
  over the scenarios that passed pass^k, and over the scenarios attempted. A failure never leaves the spend;
  it only stays out of the count it is divided by.
- **The Sunday card**: the user budget's week of AI spend (every call of the week, the failed and the refused
  proposals' too) over the week's accepted outcomes: their yes to a proposal Pisa made, a task Pisa made that
  got done, a draft of Pisa's they sent. Counts only, never a title or a word.
"""

from dataclasses import dataclass
from decimal import Decimal

PLACES = Decimal("0.0001")  # the euro figures to a hundredth of a cent (a replay scenario costs ~€0.001)


def per(spent: Decimal, n: int) -> Decimal | None:
    """`spent` over `n`, None when there is nothing to divide by (never a made-up zero)."""
    if n <= 0:
        return None
    return (spent / n).quantize(PLACES)


@dataclass(frozen=True)
class Accepted:
    """The week's accepted outcomes, counted from what Pisa records."""

    yes: int  # approvals they said yes to (tap, voice, words, Face ID), done and not undone; never a send
    tasks: int  # tasks Pisa made (from an email, their calendar, a bill, Pisa's own proposal), done this week
    drafts: int  # Pisa's email drafts they sent this week

    @property
    def total(self) -> int:
        return self.yes + self.tasks + self.drafts


def week(spent: Decimal, accepted: Accepted) -> dict[str, object]:
    """The card's figure as numbers (the wire's `cost_per_outcome`)."""
    each = per(spent, accepted.total)
    return {
        "spent_eur": str(spent.quantize(PLACES)),
        "yes": accepted.yes,
        "tasks_done": accepted.tasks,
        "drafts_sent": accepted.drafts,
        "accepted": accepted.total,
        "per_accepted_eur": str(each) if each is not None else None,
    }


def card_line(spent: Decimal, accepted: Accepted, lang: str) -> str:
    """One line of the Sunday card: the week's AI spend over what they accepted."""
    each = per(spent, accepted.total)
    total = spent.quantize(Decimal("0.01"))
    if lang == "it-IT":
        head = f"Costo per risultato: spesa AI €{total} su {accepted.total} accettati"
        parts = f"({accepted.yes} tuoi sì, {accepted.tasks} attività fatte, {accepted.drafts} bozze inviate)"
        tail = f"= €{each} l'uno." if each is not None else "= nessun risultato accettato questa settimana."
        return f"{head} {parts} {tail}"
    head = f"Cost per outcome: AI spend €{total} over {accepted.total} accepted"
    parts = f"({accepted.yes} yeses, {accepted.tasks} tasks done, {accepted.drafts} drafts sent)"
    tail = f"= €{each} each." if each is not None else "= nothing accepted this week."
    return f"{head} {parts} {tail}"
