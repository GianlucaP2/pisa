"""Numbers as Italian and English text write them: a decimal comma ("7,99") or point ("7.99"), a thousands
dot ("1.234,56") or comma ("1,234.56").

Each reader keeps its own rule, named here: `comma_float` reads one decimal separator, either one, and no
grouping; `grouped_amount` reads a price with its grouping; `last_separator_decimal` reads an amount whose
last separator before one or two digits is the decimal one.

No I/O here."""

import re
from decimal import Decimal, InvalidOperation


def decimal_point(raw: str) -> str:
    """The number with its decimal comma written as a point ("7,99" -> "7.99"); nothing else changes."""
    return raw.replace(",", ".")


def comma_float(raw: str) -> float:
    """ "7,99" or "7.99" -> 7.99 (no grouping); ValueError when it is no number."""
    return float(decimal_point(raw))


def comma_float_or_none(text: str | None) -> float | None:
    """`comma_float` of the stripped text, or None when it is no number (or missing)."""
    try:
        return comma_float((text or "").strip())
    except ValueError:
        return None


def grouped_amount(text: str) -> float | None:
    """'1.234,56' -> 1234.56, '199,00' -> 199.0, '189.85' -> 189.85, '1 299' -> 1299, '1.299' -> 1299; None
    when it is no number. A comma is always the decimal one; a point is when it is not followed by three
    digits."""
    raw = text.replace("\u00a0", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") == 1 and len(raw.split(".")[1]) == 3:
        raw = raw.replace(".", "")  # a thousands dot ("1.299")
    try:
        return float(raw)
    except ValueError:
        return None


def last_separator_decimal(raw: str) -> Decimal | None:
    """ "84,20", "1.234,50", "1,234.50", "61.07" -> the amount: the last separator before one or two digits is
    the decimal one, every other separator groups thousands; None when it is no number."""
    text = raw.replace(" ", "")
    if re.search(r"[.,]\d{2}$", text):
        whole, cents = text[:-3], text[-2:]
        text = re.sub(r"[.,]", "", whole) + "." + cents
    elif re.search(r"[.,]\d$", text):
        text = re.sub(r"[.,]", "", text[:-2]) + "." + text[-1]
    else:
        text = re.sub(r"[.,]", "", text)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
