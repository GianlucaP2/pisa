"""Bills and receipts Pisa recognises in the user's email. Pure rules over an email's
sender, subject and snippet only: no new connector scope, no PDF (no pure-Python PDF reader is in the lock).

- **A bill** is a real request to pay: a utility or a tax notice (Italian and US utilities, F24,
  pagoPA, TARI, "your bill is ready", "amount due") with an amount. A direct debit or a "payment received" is
  listed as paid and never becomes a task; offers and newsletters are never bills.
- **A receipt** is a charge already made (a subscription's renewal, an order's receipt): the material of
  "subscriptions seen": the same sender, a similar amount, monthly or yearly. Read-only: Pisa asks
  "vuoi che ti prepari la disdetta?" and never contacts a company.
The email's words are data: an amount or a date is copied as found, never computed from instructions.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pisa.domain.lexicon.dates import day_or_none, full_year, iso_day_or_none
from pisa.domain.lexicon.months import EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS, month_numbers
from pisa.domain.lexicon.numbers import last_separator_decimal

SeenKind = Literal["bill", "receipt"]
# how far back the money scans read (bills here, refunds and claims in money_back): the emails 45 days back
# (a bill sent before Pisa was connected is still due), the receipts 400 (a yearly subscription needs two
# charges a year apart)
SCAN_DAYS = 45
RECEIPTS_DAYS = 400
BILL_KIND = "bill_or_deadline"  # the triage's kind for bills: their rule "never this kind" names it

# Who sends bills, by the sender's address domain (never a display name, "Tim Rossi"): Italian
# and US utilities, telecoms and tax notices; a label of the domain ("bollette.enel.it", "coned.com").
BILLER_DOMAINS = re.compile(
    r"(?:^|\.)(?:enel|enelenergia|servizioelettriconazionale|a2a|a2aenergia|hera|gruppohera|acea|iren|edison|"
    r"edisonenergia|eniplenitude|plenitude|sorgenia|engie|eon-energia|illumia|iberdrola|tim|telecomitalia|"
    r"vodafone|fastweb|windtre|iliad|sky|abbanoa|publiacqua|pagopa|agenziaentrate|agenziaentrateriscossione|"
    r"coned|conedison|nationalgrid|nationalgridus|pseg|verizon|att|comcast|xfinity|spectrum|t-mobile|optimum|"
    r"nyc)\.",
    re.IGNORECASE,
)
# A real notice to pay names itself: an invoice or notice marker, beside its due date.
NOTICE = re.compile(
    r"\b(?:fattura\s+(?:n\.?|numero|del)|avviso\s+di\s+pagamento|pago\s*pa|pagopa|f24|codice\s+avviso|"
    r"your\s+(?:\w+\s+)?bill\s+is\s+(?:ready|available)|statement\s+is\s+(?:ready|available)|"
    r"amount\s+due|balance\s+due)\b",
    re.IGNORECASE,
)
BILL_WORDS = re.compile(
    r"\b(?:bollett\w*|fattura\s+(?:n\.?|numero|di|del|emessa)|avviso\s+di\s+pagamento|pago\s*pa|pagopa|f24|"
    r"tari|imu|canone|da\s+pagare|importo\s+(?:da\s+pagare|dovuto)|scadenza\s+(?:pagamento|bolletta)|"
    r"your\s+(?:\w+\s+)?bill\s+is\s+(?:ready|available)|new\s+bill|amount\s+due|payment\s+due|balance\s+due|"
    r"statement\s+is\s+(?:ready|available)|due\s+date)\b",
    re.IGNORECASE,
)
PAID = re.compile(
    r"\b(?:pagamento\s+(?:ricevuto|effettuato|completato)|grazie\s+per\s+(?:il|aver)\s+pagat\w*"
    r"|gi[aà]\s+pagat\w*|"
    r"addebit\w*\s+(?:diretto|su\s+conto|in\s+conto|sepa)|domiciliat\w*|domiciliazione|rid\b|sdd\b|"
    r"payment\s+(?:received|confirmed|successful)|thank\s+you\s+for\s+your\s+payment|autopay|auto-?pay|"
    r"will\s+be\s+(?:automatically\s+)?(?:debited|charged))",
    re.IGNORECASE,
)
_RECEIPT_WORDS = re.compile(
    r"\b(?:ricevuta|receipt|rinnovo\s+(?:del(?:l')?\s*)?(?:abbonamento|piano)|abbonamento\s+rinnovato|"
    r"your\s+subscription|subscription\s+(?:renewed|receipt|confirmation)|membership|il\s+tuo\s+abbonamento|"
    r"your\s+(?:monthly|annual|yearly)\s+(?:plan|charge)|payment\s+receipt|order\s+receipt|invoice\s+for)\b",
    re.IGNORECASE,
)
# Marketing, in the subject or the snippet: never a bill, even a utility's.
NOT_MONEY = re.compile(
    r"\b(?:offert\w*|scont[oi]|promo\w*|newsletter|news\b|novit[aà]|risparmi\w*|coupon|saldi|sale\b|"
    r"deals?\b|black\s+friday|cyber\s+monday|attiva\s+(?:subito|ora|l'offerta)|scopri|per\s+\d+\s+mesi|"
    r"% off|webinar|upgrade|passa\s+a)",
    re.IGNORECASE,
)
_AMOUNT = re.compile(
    r"(?P<pre>€|EUR|USD|US\$|\$|£|GBP)\s?(?P<a>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)(?!\d)"
    r"|(?P<b>\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})\s?(?P<post>€|EUR\b|euro\b|USD\b|dollari\b)",
    re.IGNORECASE,
)
_CURRENCY = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "dollari": "USD",
    "£": "GBP",
    "gbp": "GBP",
}
_MONTHS = month_numbers(IT_MONTHS, EN_MONTHS, EN_MONTHS_SHORT, extra={"sept": 9})
_DUE_CUE = re.compile(
    r"\b(?:scadenza|scade|entro\s+il|entro|da\s+pagare\s+entro|pagare\s+entro|due(?:\s+date)?|pay\s+by|"
    r"payable\s+by|by)\b[:\s]*",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2})|(?P<d>\d{1,2})[/.](?P<m>\d{1,2})(?:[/.](?P<y>\d{2,4}))?"
    r"|(?P<d2>\d{1,2})\s+(?P<mon>[a-z]+)\.?(?:\s+(?P<y2>\d{4}))?"
    r"|(?P<mon3>[a-z]+)\.?\s+(?P<d3>\d{1,2}),?(?:\s+(?P<y3>\d{4}))?",
    re.IGNORECASE,
)
SIMILAR = Decimal("0.15")  # a subscription's charges differ by at most 15% (a price change, a tax)
MONTHLY = (timedelta(days=25), timedelta(days=36))
YEARLY = (timedelta(days=350), timedelta(days=380))


@dataclass(frozen=True)
class Seen:
    kind: SeenKind
    amount: Decimal | None
    currency: str | None
    due_on: date | None
    paid: bool


@dataclass(frozen=True)
class Subscription:
    sender_key: str
    name: str
    amount: Decimal
    currency: str
    period: Literal["monthly", "yearly"]
    last_on: date
    count: int


def amount(text: str) -> tuple[Decimal, str] | None:
    """The first amount in the text with its currency: "€ 84,20", "84,20 €", "$61.07", "1.234,50 EUR"."""
    m = _AMOUNT.search(text)
    if m is None:
        return None
    raw = m.group("a") or m.group("b") or ""
    unit = (m.group("pre") or m.group("post") or "").lower()
    return (value, _CURRENCY.get(unit, "EUR")) if (value := number(raw)) is not None else None


def number(raw: str) -> Decimal | None:
    """An amount written on a bill ("84,20", "1.234,50"), or None when it is none or out of range."""
    value = last_separator_decimal(raw)
    return value if value is not None and Decimal("0") < value < Decimal("100000") else None


def due_date(text: str, sent: date) -> date | None:
    """The due date after a cue ("scadenza 20/11/2026", "entro il 5 dicembre", "due Nov 20"), in the year it
    was sent (or the next, when that day already passed by more than a month)."""
    for cue in _DUE_CUE.finditer(text):
        m = _DATE.match(text, cue.end())
        if m is None:
            continue
        found = _date(m, sent)
        if found is not None:
            return found
    return None


def _date(m: re.Match[str], sent: date) -> date | None:
    if m.group("iso"):
        return iso_day_or_none(m.group("iso"))
    if m.group("d"):
        day, month, year = int(m.group("d")), int(m.group("m")), m.group("y")
    elif m.group("d2"):
        day, month, year = int(m.group("d2")), _MONTHS.get(m.group("mon").lower(), 0), m.group("y2")
    else:
        day, month, year = int(m.group("d3")), _MONTHS.get(m.group("mon3").lower(), 0), m.group("y3")
    if not month:
        return None
    if year is not None:
        return day_or_none(full_year(int(year)), month, day)
    found = day_or_none(sent.year, month, day)
    if found is None:
        return None
    return found if found >= sent - timedelta(days=31) else day_or_none(sent.year + 1, month, day)


def read(from_name: str, from_address: str, subject: str, snippet: str, sent: date) -> Seen | None:
    """What this email is about money, or None: a bill (to pay, or paid by direct debit), a receipt.
    marketing anywhere is nothing; a bill comes from a biller's address domain, or names itself
    as a notice (an invoice or pagoPA marker) with a due date: never a "Bolletta" and an amount alone."""
    text = f"{subject} {snippet}"
    if NOT_MONEY.search(text):  # an offer, even a utility's ("la bolletta scende a 39,90 €")
        return None
    found = amount(text)
    domain = (from_address or "").rsplit("@", 1)[-1].lower() + "."
    due = due_date(text, sent)
    paid = bool(PAID.search(text))
    biller = bool(BILLER_DOMAINS.search(domain))
    notice = bool(NOTICE.search(text))
    billish = bool(BILL_WORDS.search(text)) and (due is not None or notice or paid) and (biller or notice)
    if billish and found is not None:
        value, currency = found
        return Seen("bill", value, currency, due, paid)
    if _RECEIPT_WORDS.search(text) and found is not None:
        value, currency = found
        return Seen("receipt", value, currency, None, True)
    return None


def sender_key(from_address: str, from_name: str) -> str:
    """The recurrence key: the sender's address lowercased (its name when there is none)."""
    return (from_address or from_name).strip().lower()[:200]


def recurring(charges: list[tuple[str, str, Decimal, str, date]]) -> list[Subscription]:
    """Subscriptions seen in the receipts (sender_key, name, amount, currency, day): the same sender charging
    a similar amount every month (3 charges or more) or every year (2 or more), newest first."""
    by_sender: dict[tuple[str, str], list[tuple[date, Decimal, str]]] = defaultdict(list)
    for key, name, value, currency, day in charges:
        by_sender[(key, currency)].append((day, value, name))
    found: list[Subscription] = []
    for (key, currency), rows in by_sender.items():
        rows.sort()
        latest = rows[-1]
        alike = [r for r in rows if abs(r[1] - latest[1]) <= latest[1] * SIMILAR]
        gaps = [b[0] - a[0] for a, b in pairwise(alike)]
        if len(alike) >= 3 and gaps and all(MONTHLY[0] <= g <= MONTHLY[1] for g in gaps[-2:]):
            period: Literal["monthly", "yearly"] = "monthly"
        elif len(alike) >= 2 and gaps and YEARLY[0] <= gaps[-1] <= YEARLY[1]:
            period = "yearly"
        else:
            continue
        found.append(Subscription(key, latest[2], latest[1], currency, period, latest[0], len(alike)))
    return sorted(found, key=lambda s: s.last_on, reverse=True)


def biller(from_address: str) -> bool:
    """A biller's address domain (Italian and US utilities, telecoms, tax notices)."""
    return bool(BILLER_DOMAINS.search((from_address or "").rsplit("@", 1)[-1].lower() + "."))


def bill_task_title(
    name: str, due_on: date | None, lang: str, *, money: str = "", today: date | None = None
) -> str:
    """ "Pagare la bolletta Enel, 84,20 €, entro il 12" (the month named when it is another
    month's; the deadline is the task's own `due_at`). Pisa never pays it."""
    who = " ".join(name.split())[:60]
    cost = f", {money}," if money else ""
    if lang == "it-IT":
        if due_on is None:
            return f"Pagare la bolletta {who}" + (f", {money}" if money else "")
        same = today is not None and (due_on.year, due_on.month) == (today.year, today.month)
        day = f"{due_on.day}" if same else f"{due_on.day} {IT_MONTHS[due_on.month - 1]}"
        return f"Pagare la bolletta {who}{cost} entro il {day}"
    if due_on is None:
        return f"Pay the {who} bill" + (f", {money}" if money else "")
    return f"Pay the {who} bill{cost} by {due_on:%b} {due_on.day}"


def ask_cancel(name: str, money: str, period: str, lang: str) -> str:
    """ "Netflix, 12,99 € al mese: vuoi che ti prepari la disdetta?" """
    if lang == "it-IT":
        every = "al mese" if period == "monthly" else "all'anno"
        return f"{name}, {money} {every}: vuoi che ti prepari la disdetta?"
    every = "a month" if period == "monthly" else "a year"
    return f"{name}, {money} {every}: shall I prepare the cancellation?"
