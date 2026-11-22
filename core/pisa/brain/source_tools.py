"""The brain's three source tools, each found through `search_tools` (never offered at once unless
their words point to them, `domain/tool_choice`), each offered only while a connector that feeds it is on (off
means off: `build_agent`'s `withheld`). Nothing here is a line of any turn's prompt.

- `health_summary(days)`: their health and sport days (Apple Health's summaries, Garmin's pull), derived
  numbers compared with their own earlier days. Describe and compare, never interpret: no condition named, no
  verdict, no advice on medicines (`domain/health_days.FORBIDDEN`).
- `money_recent(days)`: their bank's movements, read-only. The counterparties are the bank's words: outside
  text, inside a data block.
- `obligations(window_days)`: the admin radar's dates, code's own words; an amount as printed, never computed;
  no tax advice; a phishing-like message counted, never tracked.
"""

from collections.abc import Callable

from pydantic_ai import Agent

from pisa.brain.registry import offer
from pisa.brain.untrusted import block
from pisa.ports.connectors import SourcesData

HEALTH_RULE = (
    "Say their numbers and how they compare with their own days, plainly. Never name a condition, never give "
    "a "
    "verdict, never advise on medicines; if they ask what a number means for their body, suggest their "
    "doctor."
)
MONEY_RULE = "Read-only: Pisa can't pay or move money. Say the sums as the bank's figures, never as advice."
RADAR_RULE = (
    "Dates and amounts as printed in the notice, never computed; Pisa gives no tax advice. Pisa pays, files "
    "and books nothing: they do it on the channel named."
)


def add_source_tools(agent: Agent[None, str], sources: SourcesData, lang: Callable[[], str]) -> None:
    @offer(agent)
    async def health_summary(days: int = 7) -> str:
        """Their health and sport days from Apple Health or Garmin: sleep, steps, exercise, workouts, resting
        heart rate, HRV, VO2 max, medication reminders taken or not logged; each compared with their own
        earlier days ("come ho dormito?", "quanti passi ho fatto questa settimana?", "i miei allenamenti").

        Args:
            days: how many days back, today included (1 to 30).
        """
        got = await sources.health_summary(days, lang())
        if not got["lines"]:
            return "Nothing yet: no health or sport day has arrived for those days."
        found = block("health", "\n".join(got["lines"]), "their health and sport days, as Pisa recorded them")
        return f"{found}\n{HEALTH_RULE}"

    @offer(agent)
    async def money_recent(days: int = 30) -> str:
        """Their bank account's recent movements, read-only (money in and out, the latest payments, who they
        were to or from): "quanto ho speso questo mese?", "movimenti del conto", "mi è arrivato il bonifico?".

        Args:
            days: how many days back, today included (1 to 90).
        """
        got = await sources.money_recent(days, lang())
        if not got["totals"]:
            return "Nothing in those days: no movement from their bank."
        totals = "; ".join(
            f"{'in' if t['direction'] == 'in' else 'out'} {t['amount']} {t['currency']} ({t['count']} moves)"
            for t in got["totals"]
        )
        moves = "\n".join(
            f"{m['day']} {'+' if m['direction'] == 'in' else '-'}{m['amount']} {m['currency']} {m['party']}"
            + (" (pending)" if m["pending"] else "")
            for m in got["moves"]
        )
        found = block("bank", moves, "their bank's movements; the names are the bank's text")
        return f"Totals over {days} days: {totals}\n{found}\n{MONEY_RULE}"

    @offer(agent)
    async def obligations(window_days: int = 60) -> str:
        """Their admin dates in Italy (the radar): pagoPA notices, SEND notices, insurance and car (bollo,
        revisione, RC), documents expiring, IMU, income tax (730, Redditi, acconti), ISEE, the condominium,
        the bank's consent to renew ("cosa scade?", "ho scadenze questo mese?", "quando scade il bollo?").

        Args:
            window_days: due from today to this many days ahead (1 to 400).
        """
        got = await sources.obligations(window_days, lang())
        lines = [
            f"{i['due_on'] or 'no date'} [{i['state']}] {i['what']}"
            + (f"; amount as printed {i['amount_as_printed']}" if i["amount_as_printed"] else "")
            + (f"; if missed: {i['penalty']}" if i["penalty"] else "")
            + (f"; where: {i['channel']}" if i["channel"] else "")
            for i in got["items"][:25]
        ]
        if got["flagged"]:
            lines.append(f"{got['flagged']} message(s) looked like phishing (a fake renewal or office): not "
                         "tracked; never open their links")  # fmt: skip
        if not lines:
            return f"Nothing due in the next {got['window_days']} days."
        found = block("obligations", "\n".join(lines), "Pisa's admin radar, in Pisa's own words")
        return f"{found}\n{RADAR_RULE}"
