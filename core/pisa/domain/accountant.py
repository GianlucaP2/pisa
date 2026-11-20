"""The accountant package ("prepara i documenti per il commercialista"). Pure rules: their
words, the year, the email's text, the two summaries Pisa writes (the residency count, the vault), the caps.

RAIL: Pisa lists what it holds and drafts **one** email to the accountant they name, with the attachments; it
is sent only through the approval path (the card, with Face ID: documents leave with it). The draft stays a
draft. Pisa informs and never advises: the residency count says "è un conteggio, non una consulenza".
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pisa.domain import ledger as ledger_rules
from pisa.domain import vault as vault_rules
from pisa.domain.lexicon.advice import NOT_ADVICE

MAX_FILES = 20  # PDFs attached (the summaries are 2 more)
MAX_BYTES = 8 * 1024 * 1024  # all the PDFs together (a draft goes through Gmail's JSON API)
LISTED = 40  # bills and receipts named in the text, each kind

_ASKS = re.compile(
    r"\b(?:commercialist\w*|contabil\w*|accountant|cpa|tax\s+(?:advisor|preparer)|dichiarazione\s+dei\s+redditi|"
    r"730|modello\s+redditi)\b",
    re.IGNORECASE,
)
_PREPARE = re.compile(
    r"\b(?:prepar\w*|raccogl\w*|manda\w*|invia\w*|scriv\w*|mett\w*\s+insieme|document\w*|carte|"
    r"prepare|gather|collect|send|put\s+together|paperwork|documents?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Item:
    """A bill or a receipt Pisa holds: who, how much, when, and its PDF (when Gmail still has it)."""

    kind: str  # bill | receipt
    who: str
    amount: Decimal | None
    currency: str | None
    on: date
    due_on: date | None
    paid: bool
    email_id: str
    part_id: str | None


def asks(words: str) -> bool:
    """Their words ask for it: the accountant named, and a verb of preparing or the documents."""
    return bool(_ASKS.search(words or "")) and bool(_PREPARE.search(words or ""))


def tax_year(today: date, said: int | None, country: str | None) -> int:
    """The year they named; else, for a user in Italy (PISA_COUNTRY=IT), Italy's tax season: the year before
    through 31 October (the 730 by 30 September, Redditi by 31 October), this year from November; anywhere
    else the calendar year (no country's tax season is assumed)."""
    if said is not None:
        return said
    if (country or "").upper() != "IT":
        return today.year
    return today.year - 1 if today.month <= 10 else today.year


def _l(lang: str) -> str:
    return "it-IT" if lang == "it-IT" else "en-US"


def subject(year: int, lang: str) -> str:
    return f"Documenti {year}" if _l(lang) == "it-IT" else f"Documents for {year}"


def _line(item: Item, lang: str) -> str:
    money = ledger_rules.money_text(item.amount, item.currency, lang) or "?"
    when = item.on.strftime("%d/%m/%Y") if _l(lang) == "it-IT" else item.on.isoformat()
    paid = ""
    if item.kind == "bill":
        paid = (" (pagata)" if item.paid else "") if _l(lang) == "it-IT" else (" (paid)" if item.paid else "")
    return f"- {item.who[:60]}, {money}, {when}{paid}"


def body(
    year: int,
    bills: list[Item],
    receipts: list[Item],
    residency: str | None,
    documents: list[dict[str, object]],
    attached: int,
    left_out: list[str],
    lang: str,
) -> str:
    """The email, in the user's language: what Pisa holds for the year, listed; what is attached."""
    it = _l(lang) == "it-IT"
    parts = [
        "Buongiorno," if it else "Hello,",
        "",
        (f"le mando i documenti del {year} che ho raccolto." if it else f"here are my documents for {year}."),
    ]
    if bills:
        parts += ["", f"Bollette ({len(bills)}):" if it else f"Bills ({len(bills)}):"]
        parts += [_line(b, lang) for b in bills[:LISTED]]
    if receipts:
        parts += ["", f"Ricevute ({len(receipts)}):" if it else f"Receipts ({len(receipts)}):"]
        parts += [_line(r, lang) for r in receipts[:LISTED]]
    if residency:
        parts += ["", "Giorni di presenza:" if it else "Days of presence:", residency]
    if documents:
        parts += ["", "Documenti personali (scadenze):" if it else "Personal documents (expiry dates):"]
        parts += [vault_line(d, lang) for d in documents[:20]]
    parts += ["", (f"Allegati: {attached} file." if it else f"Attached: {attached} files.")]
    if left_out:
        parts.append(
            (
                "Non allegati (non più disponibili o troppo grandi): "
                if it
                else "Not attached (gone or too big): "
            )
            + ", ".join(left_out[:10])
        )
    parts += ["", "Grazie," if it else "Thank you,"]
    return "\n".join(parts)


def vault_line(document: dict[str, object], lang: str) -> str:
    """A vault document as the email lists it: never a number beyond its last 4."""
    kind = str(document.get("kind") or "other")
    last4 = document.get("number_last4")
    tail = f" (…{last4})" if last4 else ""
    expires = str(document.get("expires_on") or "?")
    word = "scade il" if _l(lang) == "it-IT" else "expires"
    return f"- {vault_rules.label(kind, _l(lang))}{tail}, {document.get('country') or ''}, {word} {expires}"


def residency_file(year: int, text: str, lang: str) -> tuple[str, str, bytes]:
    """The residency count as a small text attachment, with its "not advice" line."""
    note = NOT_ADVICE[_l(lang)]
    name = f"conteggio-presenza-{year}.txt" if _l(lang) == "it-IT" else f"days-of-presence-{year}.txt"
    content = text if note in text else f"{text}\n{note}"
    return name, "text/plain", content.encode()


def vault_file(year: int, documents: list[dict[str, object]], lang: str) -> tuple[str, str, bytes]:
    name = f"documenti-{year}.txt" if _l(lang) == "it-IT" else f"documents-{year}.txt"
    lines = [vault_line(d, lang) for d in documents[:50]]
    return name, "text/plain", ("\n".join(lines) + "\n").encode()


def pdf_name(item: Item, index: int) -> str:
    """A PDF's attachment name from Pisa's own record (never the PDF's: that is outside text)."""
    who = re.sub(r"[^A-Za-z0-9]+", "-", item.who).strip("-")[:30] or "documento"
    return f"{item.on.isoformat()}-{item.kind}-{who}-{index}.pdf"
