""" "Pisa cost you / found you", one line of the Sunday card, from code. Pure rules.

- **Cost**: the month's AI spend as the AI ledger recorded it (`llm_calls.cost_eur`, settled calls only: a
  reservation is not a cost) plus the paid services' (the maps and places calls `place_calls` recorded, at the
  price table, after the month's free allowance), in EUR.
- **Found**: flight compensations they marked paid, refunds received, subscriptions they cancelled themselves
  (their last charge as Pisa saw it, once, in the month they said it): each as recorded, by currency.
- **Never estimated**: an item with no recorded amount is counted, never valued; no "you would have…".
"""

from dataclasses import dataclass, field
from decimal import Decimal

from pisa.domain import ledger as ledger_rules


@dataclass(frozen=True)
class Found:
    kind: str  # claim | refund | subscription
    amount: Decimal | None
    currency: str | None


@dataclass(frozen=True)
class CostFound:
    month: str  # YYYY-MM
    ai_eur: Decimal
    services_eur: Decimal
    found: list[Found] = field(default_factory=list)

    @property
    def cost_eur(self) -> Decimal:
        return (self.ai_eur + self.services_eur).quantize(Decimal("0.01"))

    def found_by_currency(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for f in self.found:
            if f.amount is not None and f.currency:
                out[f.currency] = out.get(f.currency, Decimal(0)) + f.amount
        return out


_KIND_WORDS = {
    "it-IT": {
        "claim": ("compensazione", "compensazioni"),
        "refund": ("rimborso", "rimborsi"),
        "subscription": ("abbonamento disdetto", "abbonamenti disdetti"),
    },
    "en-US": {
        "claim": ("compensation", "compensations"),
        "refund": ("refund", "refunds"),
        "subscription": ("subscription cancelled", "subscriptions cancelled"),
    },
}


def _l(lang: str) -> str:
    return "it-IT" if lang == "it-IT" else "en-US"


def _sum(amounts: dict[str, Decimal], lang: str) -> str:
    order = sorted(amounts, key=lambda c: (c != "EUR", c))
    joiner = " e " if _l(lang) == "it-IT" else " and "
    return joiner.join(ledger_rules.money_text(amounts[c], c, lang) for c in order)


def line(data: CostFound, lang: str) -> str:
    """ "Questo mese Pisa ti è costato 4,12 € (AI 3,80 €, servizi 0,32 €) e ti ha fatto ritrovare 312,00 €
    (2 rimborsi, 1 compensazione)." Only what is recorded."""
    key = _l(lang)
    cost = ledger_rules.money_text(data.cost_eur, "EUR", lang)
    ai = ledger_rules.money_text(data.ai_eur.quantize(Decimal("0.01")), "EUR", lang)
    services = ledger_rules.money_text(data.services_eur.quantize(Decimal("0.01")), "EUR", lang)
    counts: dict[str, int] = {}
    for f in data.found:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    what = ", ".join(
        f"{n} {_KIND_WORDS[key][k][0 if n == 1 else 1]}"
        for k in ("claim", "refund", "subscription")
        if (n := counts.get(k, 0))
    )
    amounts = data.found_by_currency()
    # a sum beside the full count only when every item has its amount; else say how many don't
    missing = sum(1 for f in data.found if f.amount is None or not f.currency)
    if key == "it-IT":
        parts = f"AI {ai}, servizi {services}"
        head = f"Questo mese Pisa ti è costato {cost} ({parts})"
        if not data.found:
            return f"{head}; soldi ritrovati registrati: ancora nessuno."
        valued = _sum(amounts, lang) if amounts else "importi non registrati"
        if amounts and missing:
            what += f"; {missing} senza importo registrato"
        return f"{head} e ti ha fatto ritrovare {valued} ({what})."
    parts = f"AI {ai}, services {services}"
    head = f"This month Pisa cost you {cost} ({parts})"
    if not data.found:
        return f"{head}; money found on record: none yet."
    valued = _sum(amounts, lang) if amounts else "amounts not recorded"
    if amounts and missing:
        what += f"; {missing} with no amount on record"
    return f"{head} and found you {valued} ({what})."


def wire(data: CostFound, lang: str) -> dict[str, object]:
    """The summary's `cost_found`: money as decimal strings with their currency."""
    return {
        "month": data.month,
        "cost": {
            "amount": f"{data.cost_eur:.2f}",
            "currency": "EUR",
            "ai": f"{data.ai_eur.quantize(Decimal('0.01')):.2f}",
            "services": f"{data.services_eur.quantize(Decimal('0.01')):.2f}",
        },
        "found": [{"amount": f"{v:.2f}", "currency": c} for c, v in sorted(data.found_by_currency().items())],
        "found_items": {
            k: sum(1 for f in data.found if f.kind == k) for k in ("claim", "refund", "subscription")
        },
        # the flag: the items found with no recorded amount, as a field (the app shouldn't parse `text`)
        "unvalued": sum(1 for f in data.found if f.amount is None or not f.currency),
        "text": line(data, lang),
    }
