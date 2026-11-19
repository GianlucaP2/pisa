"""A bill's PDF, read locally (`pisa/connectors/pdf_text.py`, pypdf; never a third-party
service) for the payee, the amount, the due date and the IBAN's last 4. Pure rules over the PDF's text.

RAIL: the PDF is **outside text**. Its words never reach the model as instructions; code copies the
four facts as the PDF states them (an amount only after a "total to pay" cue, never the first number on the
page; a date only after a due cue), never computes them, and keeps only the IBAN's last 4 characters.

Caps (`MAX_BYTES`, `MAX_PAGES`, `MAX_CHARS`, `MAX_PDFS`): a bigger or longer PDF is not read at all.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pisa.domain import money as money_rules

MAX_BYTES = 3 * 1024 * 1024  # a utility's bill is ~100-500 KB
MAX_PAGES = 8  # a longer PDF is a contract or a statement, never one bill
MAX_CHARS = 40_000  # of text taken from it
MAX_PDFS = 2  # read per email (the bill and, at most, its payment slip)
READ_SECONDS = 10.0  # the local reader's budget for one PDF

_PDF_NAME = re.compile(r"\.pdf$", re.IGNORECASE)
# The total to pay (never "consumi", "IVA", "quota fissa": those are lines of it).
_TOTAL_CUE = re.compile(
    r"\b(?:totale\s+(?:da\s+pagare|bolletta|fattura|documento|dovuto|a\s+pagare)|importo\s+(?:da\s+pagare|"
    r"totale|dovuto|a\s+pagare)|da\s+pagare|totale\s+complessivo|amount\s+due|total\s+(?:amount\s+)?due|"
    r"balance\s+due|total\s+to\s+pay|please\s+pay|new\s+charges\s+total|total\s+amount)\b[^\d€$]{0,40}",
    re.IGNORECASE,
)
# An IBAN as bills print it (spaces every 4): two letters, two digits, 11 to 30 letters and digits.
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30})\b")
# The payee: a company's name as the bill prints it ("Enel Energia S.p.A.", "Consolidated Edison Inc.").
_COMPANY = re.compile(
    r"\b((?:[A-Z][A-Za-z0-9&'.\-]*\s){0,4}[A-Z][A-Za-z0-9&'.\-]*)\s+(S\.?\s?p\.?\s?A\.?|S\.?\s?r\.?\s?l\.?|"
    r"S\.?\s?n\.?\s?c\.?|Inc\.?|LLC|L\.L\.C\.|Corp\.?|Corporation|Company|Co\.)(?![A-Za-z])"
)
_NOT_PAYEE = re.compile(
    r"\b(?:banca|bank|poste|paypal|intesa|unicredit|bnl|chase|citi|wells)\b", re.IGNORECASE
)
_SPACES = re.compile(r"\s+")
_BARE = re.compile(r"\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)")


@dataclass(frozen=True)
class PdfBill:
    payee: str | None
    amount: Decimal | None
    currency: str | None
    due_on: date | None
    iban_last4: str | None
    paid: bool

    @property
    def any(self) -> bool:
        return self.amount is not None or self.due_on is not None


def is_pdf(filename: str, mime: str) -> bool:
    return mime.lower() == "application/pdf" or bool(_PDF_NAME.search(filename or ""))


def candidate(from_address: str, subject: str, snippet: str) -> bool:
    """An email worth opening the PDF of: a biller's address domain, or a bill's words in its subject or
    snippet; marketing never (an offer's PDF is a brochure)."""
    text = f"{subject} {snippet}"
    if money_rules.NOT_MONEY.search(text):
        return False
    domain = (from_address or "").rsplit("@", 1)[-1].lower() + "."
    return bool(
        money_rules.BILLER_DOMAINS.search(domain)
        or money_rules.BILL_WORDS.search(text)
        or money_rules.NOTICE.search(text)
    )


def _flat(text: str) -> str:
    return _SPACES.sub(" ", text or "")[:MAX_CHARS]


def total(text: str) -> tuple[Decimal, str] | None:
    """The amount after a "total to pay" cue ("Totale da pagare € 84,20", "Amount due $61.07")."""
    flat = _flat(text)
    for cue in _TOTAL_CUE.finditer(flat):
        found = money_rules.amount(flat[max(0, cue.end() - 4) : cue.end() + 30])
        if found is not None:
            return found
        bare = _BARE.match(
            flat, cue.end()
        )  # "Totale da pagare 84,20": the PDF's one currency, when it has one
        if bare is not None and (currency := _only_currency(flat)) is not None:
            value = money_rules.number(bare.group(1))
            if value is not None:
                return value, currency
    return None


def _only_currency(text: str) -> str | None:
    euro = "€" in text or bool(re.search(r"\bEUR\b|\beuro\b", text, re.IGNORECASE))
    dollar = "$" in text or bool(re.search(r"\bUSD\b", text))
    return "EUR" if euro and not dollar else "USD" if dollar and not euro else None


def iban_last4(text: str) -> str | None:
    """The last 4 characters of the first IBAN printed (the bill's own or their account's for a direct debit).
    RAIL: the rest is never kept."""
    m = _IBAN.search(_flat(text).upper())
    if m is None:
        return None
    iban = m.group(1).replace(" ", "")
    if not iban[2:4].isdigit() or len(iban) < 15:
        return None
    return iban[-4:]


def payee(text: str) -> str | None:
    """The company the bill is from, as printed; never a bank's (that is where they pay from)."""
    for m in _COMPANY.finditer(_flat(text)[:6000]):
        name = f"{m.group(1)} {m.group(2)}".strip()
        if _NOT_PAYEE.search(name) or len(name) < 4:
            continue
        return name[:120]
    return None


def read(text: str, sent: date) -> PdfBill | None:
    """The bill's four facts in the PDF's text, or None when it holds none of them (a brochure, a leaflet)."""
    flat = _flat(text)
    if not flat.strip() or money_rules.NOT_MONEY.search(flat[:400]):
        return None
    found = total(flat)
    bill = PdfBill(
        payee=payee(flat),
        amount=found[0] if found else None,
        currency=found[1] if found else None,
        due_on=money_rules.due_date(flat, sent),
        iban_last4=iban_last4(flat),
        paid=bool(money_rules.PAID.search(flat)),
    )
    return bill if bill.any else None


def merge(seen: money_rules.Seen | None, pdf: PdfBill | None, *, biller: bool) -> money_rules.Seen | None:
    """What the email said, completed by its PDF: the email's own amount and date stand; the
    PDF fills what the email left out. A PDF alone makes a bill only from a biller's address with an amount
    (a stranger's PDF never becomes a task)."""
    if pdf is None:
        return seen
    if seen is not None and seen.kind == "bill":
        return money_rules.Seen(
            "bill",
            seen.amount if seen.amount is not None else pdf.amount,
            seen.currency if seen.amount is not None else pdf.currency,
            seen.due_on or pdf.due_on,
            seen.paid or pdf.paid,
        )
    if seen is None and biller and pdf.amount is not None:
        return money_rules.Seen("bill", pdf.amount, pdf.currency, pdf.due_on, pdf.paid)
    return seen


def clean_name(name: str | None) -> str | None:
    """A payee for a title: letters, digits and a few marks, at most 60 characters."""
    if not name:
        return None
    kept = re.sub(r"[^\w&'.\- ]", "", name, flags=re.UNICODE)
    kept = _SPACES.sub(" ", kept).strip()
    return kept[:60] or None


def short_payee(name: str) -> str:
    """ "Enel Energia S.p.A." → "Enel Energia" (the company form goes in a title)."""
    cut = _COMPANY.sub(lambda m: m.group(1), name)
    return cut.strip(" .,") or name
