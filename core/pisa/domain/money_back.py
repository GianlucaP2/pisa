"""Refunds promised, subscriptions (duplicate, unused, price up), the Italian
bill watchdog, warranties, the promise keeper, things that run out. Pure rules over an email's words (data).


RAILS: an amount is copied as an email states it (`money.amount`), never computed, never invented; Pisa drafts
and the user sends; a promise is read only from the user's OWN sent words (never a received email, never the
quoted part of their reply). No I/O.
"""

import hashlib
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pisa.domain import money as money_rules
from pisa.domain import money_links as links
from pisa.domain.lexicon.dates import day_or_none, iso_day_or_none
from pisa.domain.lexicon.months import EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS, month_forms
from pisa.domain.lexicon.weekdays import EN_WEEKDAYS, IT_WEEKDAYS, IT_WEEKDAYS_PLAIN, weekday_numbers
from pisa.domain.text import ascii_fold

# --- 2. refunds promised, not received ---------------------------------------------------------------------

REFUND_DEFAULT_DAYS = 14  # the research: 14 days for an EU withdrawal when the email names no date
REFUND_GRACE_DAYS = 5  # "the deadline plus 5 days"
_REFUND_PROMISED = re.compile(
    r"\b(?:riceverai\s+(?:il\s+)?rimborso|rimborso\s+(?:emesso|effettuato|in\s+elaborazione|avviato|"
    r"disposto|è\s+stato\s+(?:emesso|avviato|disposto))|ti\s+rimborseremo|abbiamo\s+(?:avviato|emesso|"
    r"disposto)\s+(?:il\s+)?rimborso|refund(?:\s+of\s+\S+)?\s+(?:has\s+been\s+|was\s+)?(?:issued|processed|initiated|"
    r"approved)|(?:we've|we\s+have)\s+issued\s+(?:a|your)\s+refund|you(?:'ll|\s+will)\s+receive\s+(?:a|your)"
    r"\s+refund|your\s+refund\s+(?:is\s+on\s+its\s+way|will\s+be))",
    re.IGNORECASE,
)
_REFUND_CREDITED = re.compile(
    r"\b(?:rimborso\s+(?:accreditato|completato|ricevuto)|(?:ti\s+)?abbiamo\s+accreditato|accredito\s+"
    r"(?:del\s+)?rimborso|refund\s+(?:completed|credited|has\s+been\s+credited)|(?:we've|we\s+have)\s+"
    r"credited|has\s+been\s+credited\s+to)",
    re.IGNORECASE,
)
_WITHIN_DAYS = re.compile(
    r"(?:entro|within|in)\s+(?P<n>\d{1,2})\s*(?P<biz>giorni\s+lavorativi|business\s+days|working\s+days|"
    r"giorni|days)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RefundNotice:
    kind: Literal["promised", "credited"]
    amount: Decimal | None
    currency: str | None
    expected_by: date | None


def add_business_days(day: date, n: int) -> date:
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def read_refund(subject: str, body: str, sent: date) -> RefundNotice | None:
    """A refund an email promises (with the day it should arrive: its date, or "within N days", else none),
    or one it says was credited. The amount as the email states it."""
    text = f"{subject}\n{body}"
    found = money_rules.amount(text)
    value, currency = found if found is not None else (None, None)
    if _REFUND_CREDITED.search(text):
        return RefundNotice("credited", value, currency, None)
    if not _REFUND_PROMISED.search(text):
        return None
    by = money_rules.due_date(text, sent)
    if by is None and (m := _WITHIN_DAYS.search(text)) is not None:
        n = int(m.group("n"))
        biz = re.search(r"lavorativ|business|working", m.group("biz"), re.I) is not None
        by = add_business_days(sent, n) if biz else sent + timedelta(days=n)
    if by is not None and not sent <= by <= sent + timedelta(days=120):
        by = None
    return RefundNotice("promised", value, currency, by)


def refund_expected(sent: date, stated: date | None) -> date:
    return stated or sent + timedelta(days=REFUND_DEFAULT_DAYS)


def credit_matches(promised: Decimal | None, credited: Decimal | None) -> bool:
    """A credit closes a promise of the same sender: the same amount, or one of them states none."""
    return promised is None or credited is None or abs(promised - credited) <= Decimal("0.01")


def refund_task_title(merchant: str, money: str, lang: str) -> str:
    who = " ".join(merchant.split())[:60]
    if lang.startswith("it"):
        return f"Sollecitare il rimborso{' di ' + money if money else ''} da {who}"
    return f"Chase the refund{' of ' + money if money else ''} from {who}"


def refund_reminder(merchant: str, money: str, promised_on: date, expected_by: date, lang: str) -> str:
    """The reminder Pisa drafts for them to send (never sent by Pisa)."""
    if lang.startswith("it"):
        return (
            f"Oggetto: Rimborso non ricevuto\n\nBuongiorno,\nil {promised_on:%d/%m/%Y} mi avete comunicato "
            "un "
            f"rimborso{' di ' + money if money else ''}, previsto entro il {expected_by:%d/%m/%Y}. Ad oggi "
            "non "
            "l'ho ricevuto. Vi chiedo di verificare e di indicarmi la data dell'accredito.\n\nGrazie,\n"
            "[NOME] - [numero d'ordine]"
        )
    return (
        f"Subject: Refund not received\n\nHello,\non {promised_on:%m/%d/%Y} you told me a refund"
        f"{' of ' + money if money else ''} was on its way, expected by {expected_by:%m/%d/%Y}. I haven't "
        "received it yet. Please check and tell me when it will be credited.\n\nThank you,\n[NAME] - [order "
        "number]"
    )


# --- 3. subscriptions: duplicate, unused, price up --------------------------------------------------------

UNUSED_DAYS = 60
KEPT_DAYS = 90
_USE = re.compile(
    r"\b(?:nuovi\s+episodi|new\s+episodes?|nuovo\s+accesso|new\s+(?:sign-?in|login|device)|accesso\s+"
    r"(?:effettuato|da\s+un\s+nuovo)|sign-?in|log-?in|continua\s+a\s+(?:guardare|ascoltare)|continue\s+"
    r"(?:watching|listening)|il\s+tuo\s+riepilogo|your\s+(?:weekly|monthly|year\s+in)|hai\s+ascoltato|you\s+"
    r"listened|you\s+watched|hai\s+guardato|download)",
    re.IGNORECASE,
)


def sub_id(sender_key: str, currency: str) -> str:
    return hashlib.sha256(f"{sender_key}|{currency}".encode()).hexdigest()[:16]


def use_signal(subject: str, snippet: str) -> bool:
    """A service's email that says they used it (a login, a new device, "new episodes", a recap)."""
    return bool(_USE.search(f"{subject} {snippet}"))


def flags(
    *,
    first_on: date,
    last_use_on: date | None,
    kept_on: date | None,
    amounts: list[Decimal],
    duplicate: bool,
    today: date,
) -> list[str]:
    out: list[str] = []
    if duplicate:
        out.append("duplicate")
    kept = kept_on is not None and today - kept_on < timedelta(days=KEPT_DAYS)
    seen_long = today - first_on >= timedelta(days=UNUSED_DAYS)
    if not kept and seen_long and (last_use_on is None or today - last_use_on >= timedelta(days=UNUSED_DAYS)):
        out.append("unused")
    if len(amounts) >= 2 and amounts[-1] > amounts[-2]:
        out.append("price_up")
    return out


# --- 4. the Italian bill watchdog (prescrizione biennale) -------------------------------------------------

# the utilities' registrable domains (or a subdomain of one): "bollette.enel.it" yes,
# "enel.phish.ru" no
UTILITY_DOMAINS = frozenset({
    "enel.it", "enel.com", "enelenergia.it", "servizioelettriconazionale.it", "a2a.eu", "a2a.it",
    "a2aenergia.eu", "hera.it", "gruppohera.it", "acea.it", "iren.it", "gruppoiren.it", "edison.it",
    "edisonenergia.it", "eni.com", "eniplenitude.com", "plenitude.com", "sorgenia.it", "engie.it",
    "eon-energia.com", "illumia.it", "iberdrola.it", "abbanoa.it", "publiacqua.it", "aqp.it", "smatorino.it",
    "mmspa.eu", "gaia-spa.it", "gruppoveritas.it", "acquevenete.it", "abc.napoli.it",
})  # fmt: skip
_CONGUAGLIO = re.compile(
    r"\b(?:conguagli\w*|ricalcol\w*|import\w*\s+prescritt\w*|prescrizione|consumi\s+risalenti\s+a\s+più\s+di\s+"
    r"due\s+anni|maxi-?bollett\w*)\b",
    re.IGNORECASE,
)
_STATED_OLD = re.compile(
    r"\b(?:import\w*\s+prescritt\w*|(?:import\w*|consumi)\s+(?:relativi\s+a\s+)?consumi\s+risalenti\s+a\s+più"
    r"\s+di\s+due\s+anni|consumi\s+risalenti\s+a\s+più\s+di\s+due\s+anni)",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"(?:periodo|consumi|fatturazione|competenza)[^\n]{0,40}?dal\s+(?P<a>\d{1,2}[/.]\d{1,2}[/.]\d{4})\s+al\s+"
    r"(?P<b>\d{1,2}[/.]\d{1,2}[/.]\d{4})",
    re.IGNORECASE,
)
_OLD_AMOUNT = re.compile(
    r"(?:import\w*\s+prescritt\w*|consumi\s+risalenti\s+a\s+più\s+di\s+due\s+anni)[^\n€]{0,60}"
    r"(?P<amount>€\s?\d[\d.,]*|\d[\d.,]*\s?€)",
    re.IGNORECASE,
)
PRESCRIPTION = timedelta(days=730)


def _day(text: str) -> date | None:
    m = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text)
    if m is None:
        return None
    return day_or_none(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def utility_kind(text: str) -> Literal["luce", "gas", "acqua"] | None:
    low = text.casefold()
    if re.search(r"\b(?:servizio\s+idrico|acqua|idric\w*|acquedott\w*)\b", low):
        return "acqua"
    if re.search(r"\b(?:gas|pdr|smc|metano)\b", low):
        return "gas"
    if re.search(r"\b(?:luce|energia\s+elettrica|elettric\w*|pod|kwh)\b", low):
        return "luce"
    return None


@dataclass(frozen=True)
class BillFlag:
    utility: Literal["luce", "gas", "acqua"]
    period_from: date | None
    period_to: date | None
    amount: Decimal | None
    old_amount: Decimal | None
    reason: Literal["older_than_2_years", "stated_by_supplier"]


def read_conguaglio(from_address: str, subject: str, body: str, issued: date) -> BillFlag | None:
    """An Italian utility's bill (by the sender's address domain) that bills consumption more than 2 years
    before its date (a period ending over 24 months earlier), or says so itself; None otherwise. Amounts only
    as stated."""
    domain = links.domain_of(from_address)
    if not any(links.under(domain, d) for d in UTILITY_DOMAINS):
        return None
    text = f"{subject}\n{body}"
    if money_rules.NOT_MONEY.search(subject):  # an offer is never a bill
        return None
    if not _CONGUAGLIO.search(text):
        return None
    kind = utility_kind(text) or utility_kind(domain)
    if kind is None:
        return None
    periods = [(_day(m.group("a")), _day(m.group("b"))) for m in _PERIOD.finditer(text)]
    periods = [(a, b) for a, b in periods if a is not None and b is not None and a <= b <= issued]
    stated = bool(_STATED_OLD.search(text))
    old = [(a, b) for a, b in periods if issued - b > PRESCRIPTION]
    if not old and not stated:
        return None
    first = min(old or periods, key=lambda p: p[0]) if (old or periods) else (None, None)
    found = money_rules.amount(text)
    old_amount = None
    if (m := _OLD_AMOUNT.search(text)) is not None and (a := money_rules.amount(m.group("amount"))):
        old_amount = a[0]
    return BillFlag(
        kind,
        first[0],
        first[1],
        found[0] if found and found[1] == "EUR" else None,
        old_amount,
        "older_than_2_years" if old else "stated_by_supplier",
    )


def bill_task_title(supplier: str, lang: str) -> str:
    who = " ".join(supplier.split())[:60]
    if lang.startswith("it"):
        return f"Contestare il conguaglio {who} (prescrizione biennale)"
    return f"Contest the {who} catch-up bill (2-year limitation)"


def contest_letter(
    supplier: str,
    issued_on: date,
    period_from: date | None,
    period_to: date | None,
    amount: Decimal | None,
    old_amount: Decimal | None,
) -> tuple[str, str]:
    """The research's §7b letter (PEC or raccomandata A/R), amounts only as the bill states them."""

    def eur(v: Decimal | None, placeholder: str) -> str:
        return money_rules_text(v) if v is not None else placeholder

    period = f"{period_from:%d/%m/%Y} - {period_to:%d/%m/%Y}" if period_from and period_to else "[DAL - AL]"
    rest = (
        eur(amount - old_amount, "[IMPORTO NON CONTESTATO]")
        if amount and old_amount
        else "[IMPORTO NON CONTESTATO]"
    )
    subject = (
        "Reclamo scritto ed eccezione di prescrizione ex art. 1, comma 4, L. 205/2017 - fattura n. [N.] del "
        f"{issued_on:%d/%m/%Y} - POD/PDR [CODICE] - codice cliente [CODICE]"
    )
    text = (
        f"Spett.le {supplier},\n\ncon riferimento alla fattura in oggetto, di importo complessivo "
        f"{eur(amount, '€ [TOTALE]')}, contesto gli importi relativi ai consumi del periodo {period}, "
        "risalenti a "
        "più di due anni prima della data di emissione della fattura, pari a "
        f"{eur(old_amount, '€ [IMPORTO PRESCRITTO]')}, "
        "ed eccepisco la prescrizione biennale ai sensi dell'art. 1, commi 4 e seguenti, della Legge 27 "
        "dicembre "
        "2017 n. 205, come modificata dalla Legge 160/2019, e della disciplina ARERA di "
        "attuazione.\n\nDichiaro "
        "pertanto di non voler pagare tali importi e chiedo l'emissione di una fattura di storno/rettifica "
        "entro "
        "i termini previsti dalla regolazione ARERA, provvedendo nel frattempo al pagamento della sola parte "
        "non "
        f"contestata, pari a {rest}. Chiedo altresì la sospensione di ogni procedura di sollecito, messa in "
        "mora "
        "o distacco relativa agli importi contestati.\n\nIn assenza di risposta motivata entro 30 giorni, "
        "richiederò l'indennizzo automatico previsto e attiverò il Servizio Conciliazione ARERA.\n\nDistinti "
        "saluti,\n[NOME COGNOME] - [INDIRIZZO DI FORNITURA] - [Luogo, data]\nAllegati: copia fattura; "
        "documento "
        "d'identità."
    )  # fmt: skip
    return subject, text


def money_rules_text(value: Decimal) -> str:
    number = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {number}"


# --- 5. purchases and warranties -------------------------------------------------------------------------

PRESUMPTION_MONTHS, GUARANTEE_MONTHS, ACTION_MONTHS = 12, 24, 26
_ORDER = re.compile(
    r"\b(?:ordine\s+(?:confermato|spedito|consegnato|n\.?|numero|ricevuto)|il\s+tuo\s+ordine|ordinat[oa]:|"
    r"totale\s+(?:dell'\s*)?ordine|ordered:|conferma\s+"
    r"d'ordine|consegnat[oa]|order\s+(?:confirmation|confirmed|shipped|delivered|receipt)|your\s+order|"
    r"delivered|shipped|scontrino|receipt\s+for\s+your\s+(?:order|purchase))",
    re.IGNORECASE,
)
_DELIVERED = re.compile(
    r"\b(?:consegnat[oa]|è\s+stato\s+consegnato|delivered|your\s+package\s+was\s+delivered)\b", re.IGNORECASE
)
_QUOTED_ITEM = re.compile(
    r"[\"\u201c\u00ab'\u2018](?P<item>[^\"\u201d\u00bb\u2019]{3,120})[\"\u201d\u00bb\u2019]"
)
_LABELLED_ITEM = re.compile(
    r"^\s*(?:articolo|prodotto|item|product|descrizione|description)\s*[:\-]\s*(?P<item>[^\n]{3,120})$",
    re.IGNORECASE | re.MULTILINE,
)
_AND_MORE = re.compile(r"\s+(?:e\s+(?:altri|altro|\d+)\s+articol\w*|and\s+\d+\s+more\s+items?|\.\.\.|…)\s*$",
                       re.IGNORECASE)  # fmt: skip
_STOP_WORDS = frozenset({
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "da", "del", "della", "dei", "con", "per",
    "e",
    "the", "a", "an", "of", "for", "with", "and", "pack", "confezione", "x",
})  # fmt: skip


def item_key(item: str) -> str:
    """The item, comparable across orders: lower case, no accents, no numbers of size or count, its first
    four words."""
    folded = ascii_fold(item.casefold())
    words = [w for w in re.findall(r"[a-z]+", folded) if w not in _STOP_WORDS and len(w) > 1]
    return " ".join(words[:4])


@dataclass(frozen=True)
class Order:
    items: list[str]
    delivered: bool
    price: Decimal | None
    currency: str | None


def read_order(subject: str, body: str) -> Order | None:
    """An order, delivery or receipt email: its items (quoted in the subject, or labelled lines of the body),
    delivered or not, its amount as stated (one item only: never split a total)."""
    text = f"{subject}\n{body}"
    if not _ORDER.search(text) or money_rules.NOT_MONEY.search(subject):
        return None
    items: list[str] = []
    for m in _QUOTED_ITEM.finditer(subject):
        items.append(_AND_MORE.sub("", m.group("item")).strip())
    if not items:
        items = [m.group("item").strip() for m in _LABELLED_ITEM.finditer(body)][:10]
    items = [i for i in items if len(item_key(i)) >= 2]
    if not items:
        return None
    found = money_rules.amount(text)
    one = len(items) == 1
    return Order(
        items,
        bool(_DELIVERED.search(subject)),
        found[0] if found and one else None,
        found[1] if found and one else None,
    )


def months_after(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    return date(year, month, 28)


@dataclass(frozen=True)
class Warranty:
    presumption_until: date | None
    ends_on: date | None
    action_until: date | None
    in_warranty: bool


def warranty(purchased_on: date, delivered_on: date | None, region: str, today: date) -> Warranty:
    """The EU legal guarantee (Codice del Consumo art. 128-135): from the delivery (else the purchase), 12
    months of presumption, 24 of guarantee, 26 to act. The US has no legal term (None)."""
    if region != "EU":
        return Warranty(None, None, None, False)
    start = delivered_on or purchased_on
    ends = months_after(start, GUARANTEE_MONTHS)
    return Warranty(
        months_after(start, PRESUMPTION_MONTHS), ends, months_after(start, ACTION_MONTHS), today <= ends
    )


def region_of(currency: str | None, seller_address: str | None, home_tz: str) -> Literal["EU", "US"]:
    if currency == "USD":
        return "US"
    if currency in ("EUR", "GBP"):
        return "EU"
    domain = links.domain_of(seller_address)
    if domain.endswith((".it", ".eu", ".de", ".fr", ".es")):
        return "EU"
    return "US" if home_tz.startswith("America/") else "EU"


def matches(item: str, query: str) -> bool:
    """Their words name this item ("la lavatrice" → "Lavatrice Samsung WW90T")."""
    q = set(item_key(query).split())
    return bool(q) and q <= set(item_key(item).split()) | set(_stems(item))


_MONTH_NAMES = month_forms(IT_MONTHS, EN_MONTHS, EN_MONTHS_SHORT, extra={"sept": 9})


def said_date(words: str, iso: str) -> bool:
    """The purchase date the model gives is the one their words say: its month named (or in a
    day/month), its year when they say one, its day when the model gives one."""
    day = iso_day_or_none(iso if len(iso) >= 10 else iso[:7] + "-01")
    if day is None:
        return False
    low = ascii_fold(words.casefold())
    numeric = re.findall(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", low)
    month = any(re.search(rf"\b{n}\b", low) for n in _MONTH_NAMES[day.month]) or any(
        int(m) == day.month for _, m, _ in numeric
    )
    years = {int(y) for y in re.findall(r"\b(20\d{2})\b", low)} | {
        2000 + int(y) for _, _, y in numeric if len(y) == 2
    }
    year = not years or day.year in years
    days = {int(d) for d in re.findall(r"\b(\d{1,2})\b", low)}
    return month and year and (len(iso) < 10 or day.day in days)


def _stems(item: str) -> list[str]:
    return [w[:-1] for w in item_key(item).split() if len(w) > 4]


def warranty_line(item: str, w: Warranty, region: str, lang: str) -> str:
    it = lang.startswith("it")
    if region != "EU" or w.ends_on is None:
        return (
            f"{item}: negli USA non c'è una garanzia legale fissa: vale quella del produttore (e "
            "l'estensione "
            "della carta, se c'è)."
            if it
            else f"{item}: the US has no legal term: the maker's warranty applies (and the card's extension, "
            "if any)."
        )
    if it:
        if not w.in_warranty:
            return f"{item}: la garanzia legale è finita il {w.ends_on:%d/%m/%Y}."
        burden = (
            "fino al " + f"{w.presumption_until:%d/%m/%Y}" + " il difetto si presume già presente"
            if w.presumption_until
            else ""
        )
        return f"{item}: in garanzia legale fino al {w.ends_on:%d/%m/%Y} (risponde il venditore; {burden})."
    if not w.in_warranty:
        return f"{item}: the legal guarantee ended on {w.ends_on:%m/%d/%Y}."
    return f"{item}: under the legal guarantee until {w.ends_on:%m/%d/%Y} (the seller is liable)."


# --- 6. the promise keeper (their OWN sent words) ----------------------------------------------------------

_QUOTE_START = re.compile(
    # a quoted reply's header can be long ("Il giorno … alle ore … Nome <indirizzo>
    # ha scritto:"), and a client may wrap it once
    # a wrapped header's first line never ends a sentence ("Il preventivo: ti
    # mando il preventivo venerdì." right above it is their own line, not the header's start)
    r"^(?:>|-{2,}\s*(?:original|messaggio)|on\s[^\n]{2,240}?[^.!?\n](?:\n[^\n]{1,120}?)?\swrote:|"
    r"il\s[^\n]{2,240}?[^.!?\n](?:\n[^\n]{1,120}?)?\sha\s+scritto:|da:\s|from:\s|"
    r"inviato\s+da|sent\s+from)",
    re.IGNORECASE | re.MULTILINE,
)
_PROMISE = re.compile(
    r"\b(?:(?:ti|vi|le|gli)\s+(?:mando|invio|giro|inoltro|spedisco|passo|faccio\s+avere)|(?:te|ve|glie)"
    r"\s?l[oaie]\s+(?:mando|invio|giro)|i(?:'ll|\s+will)\s+(?:send|forward|email|get)(?:\s+(?:you|over))?)"
    r"\s+(?P<what>[^.!?\n;]{2,90})",
    re.IGNORECASE,
)
_WHEN = re.compile(
    r"\s*(?:,\s*)?\b(?:(?P<today>oggi|stasera|in\s+giornata|today|tonight|later\s+today|entro\s+sera)|"
    r"(?P<tomorrow>domani(?:\s+mattina|\s+sera)?|tomorrow(?:\s+morning)?)|(?:entro|by|on)?\s*(?P<wd>lunedì|"
    r"lunedi|martedì|martedi|mercoledì|mercoledi|giovedì|giovedi|venerdì|venerdi|sabato|domenica|monday|"
    r"tuesday|wednesday|thursday|friday|saturday|sunday)|(?P<week>la\s+(?:settimana\s+)?prossima\s+settimana|"
    r"la\s+settimana\s+prossima|next\s+week)|(?P<soon>appena\s+posso|al\s+più\s+presto|asap|as\s+soon\s+as\s+"
    r"(?:i\s+can|possible)|a\s+breve|shortly|later|più\s+tardi|dopo))\b.*$",
    re.IGNORECASE,
)
# accented or not
_WEEKDAYS = weekday_numbers(IT_WEEKDAYS, IT_WEEKDAYS_PLAIN, EN_WEEKDAYS)
_PRONOUN_ONLY = re.compile(r"^(?:lo|la|li|le|it|them|this|that|questo|quello|questa|quella|tutto|all)$",
                           re.IGNORECASE)  # fmt: skip
_TRAIL = re.compile(r"\s+(?:a\s+te|via\s+(?:email|mail|whatsapp)|per\s+email|by\s+email|in\s+allegato)\s*$",
                    re.IGNORECASE)  # fmt: skip


_AND = re.compile(r",\s*(?:e|and)\s+(?:anche\s+|also\s+)?|\s+e\s+anche\s+|\s+and\s+also\s+", re.IGNORECASE)


def own_words(body: str) -> str:
    """Their own part of a sent email: everything before the quoted reply (">", "On … wrote:", "Il … ha
    scritto:", a forwarded header)."""
    m = _QUOTE_START.search(body)
    return body[: m.start()] if m else body


@dataclass(frozen=True)
class Promise:
    what: str
    due_on: date | None


def promises(body: str, sent: date) -> list[Promise]:
    """The promises in their own words: "ti mando il contratto domani" → ("il contratto", tomorrow). An object
    that is only a pronoun ("te lo mando") is no promise Pisa can name."""
    out: list[Promise] = []
    for m in _PROMISE.finditer(own_words(body)):
        for part in _AND.split(m.group("what")):  # "il contratto domani, e anche le foto venerdì": two
            what, due = part.strip(), None
            if (w := _WHEN.search(what)) is not None:
                due = _due(w, sent)
                what = what[: w.start()].strip(" ,")
            # one thing, never a clause after it
            what = re.split(r"[,:;]", what, maxsplit=1)[0]
            what = _TRAIL.sub("", what).strip(" ,")
            what = re.sub(r"^(?:anche|pure|also|subito|poi)\s+", "", what, flags=re.IGNORECASE)
            if len(what) < 2 or _PRONOUN_ONLY.match(what) or len(what) > 80:
                continue
            if not any(p.what.casefold() == what.casefold() for p in out):
                out.append(Promise(what, due))
    return out[:3]


def _due(m: re.Match[str], sent: date) -> date | None:
    if m.group("today"):
        return sent
    if m.group("tomorrow"):
        return sent + timedelta(days=1)
    if m.group("wd"):
        target = _WEEKDAYS[m.group("wd").lower()]
        ahead = (target - sent.weekday()) % 7 or 7
        return sent + timedelta(days=ahead)
    if m.group("week"):
        return sent + timedelta(days=7 - sent.weekday())
    return None


def recipient_address(to: list[str]) -> str | None:
    """The first recipient's address (M-B: Pisa names them by one of their people, else
    by the address, never by a display name a reply copies from the other side)."""
    for header in to:
        m = re.match(r'^\s*"?[^"<]*"?\s*<([^>]+)>\s*$', header)
        if m:
            return m.group(1).strip().lower()
        if "@" in header:
            return header.strip().lower()
    return None


def recipient_name(to: list[str]) -> str | None:
    """The first recipient: their display name, else the address's local part."""
    for header in to:
        m = re.match(r'^\s*"?([^"<]+?)"?\s*<([^>]+)>\s*$', header)
        if m:
            return m.group(1).strip()[:120]
        if "@" in header:
            return header.split("@", 1)[0].strip()[:120]
    return None


# --- 7. things that run out ------------------------------------------------------------------------------

SUPPLY_MIN_BUYS = 3
SUPPLY_DAYS = (3, 180)


@dataclass(frozen=True)
class Supply:
    item_key: str
    item: str
    count: int
    every_days: int
    last_on: date
    next_on: date


def supply(key: str, item: str, days: list[date]) -> Supply | None:
    """Bought 3 times or more: its interval is the median gap (3-180 days); it runs out one gap after."""
    days = sorted(set(days))
    if len(days) < SUPPLY_MIN_BUYS:
        return None
    gaps = [(b - a).days for a, b in pairwise(days)]
    every = round(statistics.median(gaps))
    if not SUPPLY_DAYS[0] <= every <= SUPPLY_DAYS[1]:
        return None
    return Supply(key, item, len(days), every, days[-1], days[-1] + timedelta(days=every))


def supply_task_title(item: str, lang: str) -> str:
    what = " ".join(item.split())[:80]
    return f"Ricomprare {what}" if lang.startswith("it") else f"Buy {what} again"
