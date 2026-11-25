"""What a disrupted flight may be owed (EU261, US DOT), and the claim Pisa drafts.

The 2004 rules govern every claim today (Reg.
2026/2202 applies from late 2027 and keeps the 3 h threshold and the €250/400/600 sums). **Pisa drafts; the
user sends.** An email is data: a disruption is read from what an airline's email states (cancelled, a
delay in hours, a new arrival time), never acted on. **Amounts are the law's fixed sums** (art. 7), by the
great-circle distance of two airports Pisa knows; a US refund is the fare they paid, which Pisa never invents.
No I/O.
"""

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pisa.domain import countries
from pisa.domain import money_links as links
from pisa.domain.lexicon.advice import NOT_ADVICE_EN_HEAD, NOT_ADVICE_IT

Disruption = Literal["delay", "cancellation", "denied_boarding"]
Rule = Literal["eu261", "us_dot", "none"]
Status = Literal["possible", "eligible", "not_eligible", "sent", "paid", "dismissed"]
OWNER_STATUSES = frozenset({"sent", "paid", "dismissed"})  # their word: never changed by Pisa

DELAY_MIN = 180  # Sturgeon: 3 hours at the final destination
NOTICE_DAYS = 14  # art. 5(1)(c): told 14 days or more before: no compensation
SOFT_DEADLINE = timedelta(days=365)  # the research's "12 months from the flight"
CHECK_FIRST = timedelta(days=730)  # older than 2 years: "check first"

# EU261 applies to departures from these (EU27 + Iceland, Norway, Switzerland)
EU261_AREA = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
    "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "NO", "CH",
})  # fmt: skip
# EU (and EEA/CH) airlines, by IATA prefix: an arrival in the EU on one of these is covered too
EU_CARRIERS = frozenset({
    "AZ", "FR", "EC", "U2", "DS", "LH", "AF", "KL", "IB", "VY", "I2", "UX", "W6", "W4", "SK", "LX", "OS",
    "SN", "TP", "A3", "EI", "AY", "LO", "EW", "DE", "HV", "TO", "V7", "NO", "EN", "BT", "RO", "OU", "DY",
    "D8", "WK",
})  # fmt: skip

# FACT: the airports of countries.AIRPORTS, (lat, lon) in degrees
COORDS: dict[str, tuple[float, float]] = {
    "FCO": (41.8, 12.239), "CIA": (41.799, 12.595), "MXP": (45.631, 8.728), "LIN": (45.445, 9.277),
    "BGY": (45.674, 9.704), "NAP": (40.8860, 14.291), "VCE": (45.505, 12.352), "BLQ": (44.535, 11.289),
    "FLR": (43.8100, 11.205), "PSA": (43.684, 10.393), "TRN": (45.201, 7.65), "CTA": (37.467, 15.066),
    "PMO": (38.1760, 13.0910), "BRI": (41.139, 16.761), "CAG": (39.252, 9.054),
    "JFK": (40.641, -73.778), "EWR": (40.69, -74.174), "LGA": (40.777, -73.8740),
    "BOS": (42.366, -71.01), "IAD": (38.953, -77.457), "DCA": (38.851, -77.04),
    "MIA": (25.796, -80.2870), "ATL": (33.641, -84.428), "ORD": (41.974, -87.907),
    "DFW": (32.9, -97.04), "DEN": (39.856, -104.674), "LAX": (33.942, -118.409),
    "SFO": (37.621, -122.3790), "SEA": (47.45, -122.309), "LHR": (51.4700, -0.454),
    "CDG": (49.01, 2.548), "FRA": (50.038, 8.562), "MUC": (48.354, 11.7750), "AMS": (52.31, 4.768),
    "MAD": (40.498, -3.568), "ZRH": (47.458, 8.556), "IST": (41.275, 28.752),
}  # fmt: skip


def distance_km(a: str, b: str) -> int | None:
    """The great-circle distance of two airports Pisa knows (haversine, R = 6371 km), else None."""
    pa, pb = COORDS.get(a.upper()), COORDS.get(b.upper())
    if pa is None or pb is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (*pa, *pb))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)))


def carrier(flight: str) -> str:
    return flight[:2].upper()


def rule_for(origin: str | None, destination: str | None, flight: str) -> Rule:
    """EU261 when the flight leaves the EU area, or arrives there on an EU airline; else US DOT when it
    touches the US; else none. Unknown airports: none (never a guess)."""
    o = countries.airport(origin or "")
    d = countries.airport(destination or "")
    if o is None or d is None:
        return "none"
    if o.country in EU261_AREA or (d.country in EU261_AREA and carrier(flight) in EU_CARRIERS):
        return "eu261"
    if "US" in (o.country, d.country):
        return "us_dot"
    return "none"


def eu261_amount(km: int | None, intra_eu: bool) -> Decimal | None:
    """Art. 7: €250 up to 1,500 km; €400 within the EU above 1,500 km, or 1,500-3,500 km otherwise; €600."""
    if km is None:
        return None
    if km <= 1500:
        return Decimal(250)
    if intra_eu or km <= 3500:
        return Decimal(400)
    return Decimal(600)


@dataclass(frozen=True)
class Verdict:
    rule: Rule
    status: Status
    reason: str
    distance_km: int | None
    amount: Decimal | None


def assess(
    *,
    flight: str,
    origin: str | None,
    destination: str | None,
    disruption: Disruption,
    delay_min: int | None,
    notice_days: int | None,
    scheduled_arr: datetime | None,
    scheduled_at: datetime,
    now: datetime,
) -> Verdict:
    """What the law says of this disruption, today. RAIL: the amount only as art. 7's fixed sums."""
    rule = rule_for(origin, destination, flight)
    km = distance_km(origin, destination) if origin and destination else None
    if rule == "none":
        return Verdict(rule, "not_eligible", "not_covered", km, None)
    if rule == "us_dot":
        if disruption == "cancellation":
            return Verdict(rule, "eligible", "refund_due", km, None)
        return Verdict(rule, "not_eligible", "no_us_delay_compensation", km, None)
    o, d = countries.airport(origin or ""), countries.airport(destination or "")
    intra = bool(o and d and o.country in EU261_AREA and d.country in EU261_AREA)
    amount = eu261_amount(km, intra)
    if now - scheduled_at > CHECK_FIRST:
        return Verdict(rule, "possible", "older_than_2_years", km, amount)
    if disruption == "denied_boarding":
        return Verdict(rule, "eligible", "denied_boarding", km, amount)
    if disruption == "cancellation":
        if notice_days is None:  # when they were told is unknown: nothing is owed yet
            return Verdict(rule, "possible", "notice_unknown", km, amount)
        if notice_days >= NOTICE_DAYS:
            return Verdict(rule, "not_eligible", "notice_14_days", km, None)
        return Verdict(rule, "eligible", "cancelled_late", km, amount)
    if delay_min is None:
        return Verdict(rule, "possible", "delay_unknown", km, amount)
    if delay_min < DELAY_MIN:
        return Verdict(rule, "not_eligible", "under_3h", km, None)
    landed = (scheduled_arr or scheduled_at) + timedelta(minutes=delay_min)  # the real landing
    if now < landed:  # announced, or still in the air: the delay that counts is at the arrival
        return Verdict(rule, "possible", "delay_announced", km, amount)
    return Verdict(rule, "eligible", "delay_3h", km, amount)


WHY: dict[str, tuple[str, str]] = {
    "delay_3h": ("Arrivato con 3 ore o più di ritardo: compensazione EU261.",
                 "Arrived 3 hours or more late: EU261 compensation."),
    "cancelled_late": ("Cancellato con meno di 14 giorni di preavviso: compensazione EU261.",
                       "Cancelled with less than 14 days' notice: EU261 compensation."),
    "denied_boarding": ("Imbarco negato: compensazione EU261.", "Denied boarding: EU261 compensation."),
    "delay_unknown": ("Il ritardo all'arrivo non lo so: se è di 3 ore o più, spetta la compensazione.",
                      "The arrival delay isn't known: 3 hours or more means compensation."),
    "delay_announced": ("Ritardo annunciato: conta quello all'arrivo (3 ore o più).",
                        "A delay announced: what counts is the arrival (3 hours or more)."),
    "under_3h": ("Meno di 3 ore di ritardo: niente compensazione (pasti e assistenza sì).",
                 "Under 3 hours late: no compensation (meals and care, yes)."),
    "notice_unknown": ("Volo cancellato: se ti hanno avvisato meno di 14 giorni prima, spetta la "
                       "compensazione. Quando te l'hanno detto?",
                       "Flight cancelled: told less than 14 days before means compensation. "
                       "When were you told?"),
    "notice_14_days": ("Avvisato 14 giorni prima o più: niente compensazione, ma rimborso o un altro volo.",
                       "Told 14 days or more before: no compensation, but a refund or another flight."),
    "older_than_2_years": ("Volo di più di 2 anni fa: da verificare prima di chiedere.",
                           "A flight over 2 years ago: check before claiming."),
    "refund_due": ("Volo USA cancellato: il rimborso in denaro è automatico (7 giorni lavorativi con carta).",
                   "A US flight cancelled: the cash refund is automatic (7 business days by card)."),
    "no_us_delay_compensation": ("Negli USA il ritardo non dà compensazione in denaro.",
                                 "In the US a delay gives no cash compensation."),
    "not_covered": ("Volo non coperto da EU261 né dalle regole USA (o aeroporti che non conosco).",
                    "Not covered by EU261 or the US rules (or airports Pisa doesn't know)."),
}  # fmt: skip


def why(reason: str, lang: str, *, reducible: bool = False) -> str:
    pair = WHY.get(reason)
    text = "" if pair is None else pair[0] if lang.startswith("it") else pair[1]
    return f"{text} {reduced_note(lang)}" if reducible and text else text


REDUCIBLE_KM, REDUCIBLE_MIN = 3500, 240


def reducible(distance_km: int | None, delay_min: int | None) -> bool:
    """art. 7(2): over 3,500 km and under 4 hours late, the airline may halve the sum."""
    return bool(
        distance_km and distance_km > REDUCIBLE_KM and delay_min is not None and delay_min < REDUCIBLE_MIN
    )


def reduced_note(lang: str) -> str:
    if lang.startswith("it"):
        return "La compagnia può ridurla del 50% (art. 7.2)."
    return "The airline may reduce it by 50% (art. 7(2))."


# every claim and bill Pisa shows closes with it
def not_advice(lang: str, kind: str = "claim") -> str:
    if lang.startswith("it"):
        if kind == "bill":
            return f"{NOT_ADVICE_IT}: il fornitore può contestare l'eccezione."
        return f"{NOT_ADVICE_IT}: la compagnia può opporre circostanze eccezionali."
    if kind == "bill":
        return f"{NOT_ADVICE_EN_HEAD}: the supplier may dispute it."
    return f"{NOT_ADVICE_EN_HEAD}: the airline may cite extraordinary circumstances."


# --- reading an airline's email (data) ----------------------------------------------------------

_FLIGHT = re.compile(r"\b(?P<al>[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(?P<n>\d{1,4})\b")
# A cancellation stated as a fact (never an offer, a button or a "you can cancel")
_CANCELLED = re.compile(
    r"\b(?:(?:è|e')\s+stat[oa]\s+(?:cancellat|annullat|soppress)\w*|(?:volo|flight)\s+(?:\w+\s+){0,3}"
    r"(?:cancellat|annullat|soppress)[oa]\b|(?:has\s+been|was|is)\s+cancel+ed|cancel+ed\s+flight|"
    r"cancellazione\s+del\s+(?:tuo\s+)?volo|flight\s+cancel+ation)",
    re.IGNORECASE,
)
_NOT_STATED = re.compile(
    r"\b(?:cancellazione\s+gratuita|free\s+cancel+ation|you\s+can\s+cancel|puoi\s+(?:cancellare|annullare)|"
    r"annulla\s+(?:la\s+)?prenotazione|cancel\s+(?:your|the)\s+booking|if\s+your\s+flight\s+is\s+cancel+ed|"
    r"se\s+il\s+(?:tuo\s+)?volo\s+(?:è|viene)\s+cancellat)",
    re.IGNORECASE,
)
_DELAYED = re.compile(
    r"\b(?:in\s+ritardo|ritardat[oa]|ritardo\s+(?:di|previsto|stimato)|delayed|delay\s+of|nuovo\s+orario|"
    r"new\s+(?:departure|arrival)\s+time|schedule\s+change|rescheduled|riprotett\w*|rebooked)\b",
    re.IGNORECASE,
)
_DENIED = re.compile(
    r"\b(?:imbarco\s+negato|negato\s+l'imbarco|denied\s+boarding|overbook\w*)\b", re.IGNORECASE
)
_HOURS = re.compile(
    r"(?:ritardo|delay(?:ed)?)\D{0,30}?(?P<h>\d{1,2})\s*(?:or[ae]|hours?|h)\b"
    r"(?:\s*(?:e|and)\s*(?P<m>\d{1,2})\s*(?:minut[oi]|minutes?|min))?"
    r"|(?:ritardo|delay(?:ed)?)\D{0,30}?(?P<mm>\d{2,3})\s*(?:minut[oi]|minutes?|min)\b"
    r"|(?P<h2>\d{1,2})\s*(?:or[ae]|hours?)\s*(?:di\s+ritardo|late|delay)",
    re.IGNORECASE,
)
_NEW_ARRIVAL = re.compile(
    r"(?:nuovo\s+orario\s+di\s+arrivo|arrivo\s+previsto|new\s+arrival\s+time|now\s+arrives?|arriverà\s+alle|"
    r"estimated\s+arrival)\D{0,12}(?P<h>[01]?\d|2[0-3])[:.](?P<m>[0-5]\d)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Notice:
    disruption: Disruption
    flights: list[str]
    delay_min: int | None = None
    new_arrival: tuple[int, int] | None = None  # (hour, minute), local at the destination


def read_notice(subject: str, body: str) -> Notice | None:
    """What an airline's email STATES about a flight: cancelled, delayed (by how much, or the new arrival
    time), boarding denied; None when it states none of them (an offer, "you can cancel" is nothing)."""
    text = f"{subject}\n{body}"
    flights = sorted({f"{m.group('al')}{m.group('n')}" for m in _FLIGHT.finditer(text)})
    if not flights:
        return None
    stated = _NOT_STATED.sub(" ", text)
    if _DENIED.search(stated):
        return Notice("denied_boarding", flights)
    if _CANCELLED.search(stated):
        return Notice("cancellation", flights)
    if not _DELAYED.search(stated):
        return None
    delay: int | None = None
    if (m := _HOURS.search(stated)) is not None:
        if m.group("h"):
            delay = int(m.group("h")) * 60 + int(m.group("m") or 0)
        elif m.group("mm"):
            delay = int(m.group("mm"))
        elif m.group("h2"):
            delay = int(m.group("h2")) * 60
    arrival = None
    if (a := _NEW_ARRIVAL.search(stated)) is not None:
        arrival = (int(a.group("h")), int(a.group("m")))
    return Notice("delay", flights, delay if delay is None or delay <= 4320 else None, arrival)


def delay_from_arrival(scheduled_arr: datetime, new: tuple[int, int]) -> int | None:
    """The delay a stated new arrival time (local at the destination) makes: the next such clock time
    after the scheduled one, within 24 hours; an earlier time is no delay."""
    moved = scheduled_arr.replace(hour=new[0], minute=new[1], second=0, microsecond=0)
    if moved < scheduled_arr - timedelta(hours=2):
        moved += timedelta(days=1)
    minutes = int((moved - scheduled_arr).total_seconds() // 60)
    return minutes if 0 <= minutes <= 1440 else None


# --- the user's words -----------------------------------------------------------------------------------

_SAID_HOURS = re.compile(
    r"(?P<h>\d{1,2}|un'|una|due|tre|quattro|cinque|sei|sette|otto|one|two|three|four|five|six|seven|eight)"
    r"\s*(?:or[ae]|hours?|h)\b(?:\s*(?:e|and)\s*(?:mezz[oa]|(?P<m>\d{1,2})\s*(?:minut[oi]|minutes?)))?",
    re.IGNORECASE,
)
_NUMBERS = {"un'": 1, "una": 1, "one": 1, "due": 2, "two": 2, "tre": 3, "three": 3, "quattro": 4, "four": 4,
            "cinque": 5, "five": 5, "sei": 6, "six": 6, "sette": 7, "seven": 7, "otto": 8,
            "eight": 8}  # fmt: skip
_SAID_DELAY = re.compile(r"\b(?:ritard\w*|late|delay\w*)\b", re.IGNORECASE)
_NEAR_DELAY = re.compile(r"\s*(?:di\s+)?(?:ritard\w*|late|delay\w*)\b", re.IGNORECASE)
_BEFORE_DELAY = re.compile(r"\b(?:ritardo|delay(?:ed)?|late)(?:\s+(?:di|of|by|previsto))?\s*$", re.IGNORECASE)
_SAID_CANCEL = re.compile(r"\b(?:cancellat\w*|annullat\w*|soppress\w*|cancel+ed)\b", re.IGNORECASE)
_SAID_DENIED = re.compile(r"\b(?:imbarco|boarding|overbook\w*)\b", re.IGNORECASE)


def said_disruption(words: str) -> tuple[Disruption | None, int | None]:
    """What the user's own words say happened to a flight, and the delay in minutes when they give it."""
    if _SAID_DENIED.search(words) and re.search(r"negat|denied|lasciat|non\s+mi\s+hanno", words, re.I):
        return "denied_boarding", None
    if _SAID_CANCEL.search(words):
        return "cancellation", None
    if not _SAID_DELAY.search(words):
        return None, None
    # the number their delay names ("4 ore di ritardo", "ritardo di 4 ore"), else the first
    m = next(
        (h for h in _SAID_HOURS.finditer(words)
         if _NEAR_DELAY.match(words, h.end()) or _BEFORE_DELAY.search(words, 0, h.start())),
        None,
    ) or _SAID_HOURS.search(words)  # fmt: skip
    if m is None:
        return "delay", None
    raw = m.group("h").lower()
    hours = int(raw) if raw.isdigit() else _NUMBERS.get(raw, 0)
    minutes = 30 if re.search(r"mezz[oa]", m.group(0), re.I) else int(m.group("m") or 0)
    return "delay", hours * 60 + minutes


def flights_said(words: str) -> list[str]:
    return [f"{m.group('al')}{m.group('n')}" for m in _FLIGHT.finditer(words.upper())]


# --- the claim Pisa drafts; the user sends it -----------------------------------------


def _hm(minutes: int) -> tuple[int, int]:
    return minutes // 60, minutes % 60


def draft(
    *,
    lang: str,
    flight: str,
    origin: str | None,
    destination: str | None,
    scheduled_at: datetime,
    scheduled_arr: datetime | None,
    disruption: Disruption,
    delay_min: int | None,
    rule: Rule,
    amount: Decimal | None,
    distance_km: int | None,
    booking_last4: str | None,
    notice_days: int | None = None,
) -> tuple[str, str]:
    """(subject, text): the claim letter with what Pisa knows; the rest in [brackets] for the user. Only a
    booking code's last 4 characters: "[codice …AB12]"."""
    airline = links.AIRLINES.get(carrier(flight))
    it = lang.startswith("it")
    day = scheduled_at.strftime("%d/%m/%Y")
    dep = scheduled_at.strftime("%H:%M")
    arr = scheduled_arr.strftime("%H:%M") if scheduled_arr else "[ORA]"
    o, d = origin or "[ORIGINE]", destination or "[DESTINAZIONE]"
    pnr = (
        f"[codice …{booking_last4}]"
        if booking_last4 and it
        else f"[code …{booking_last4}]"
        if booking_last4
        else "[PNR]"
    )
    money = f"{amount:.0f}" if amount is not None else "[250/400/600]"
    km = str(distance_km) if distance_km else "[KM]"
    name = airline.name if airline else ("[Compagnia]" if it else "[Airline]")
    # art. 7(2), a note for them in brackets (they decide what to send)
    half = reducible(distance_km, delay_min) and disruption == "delay"
    note_it = f"\n\n[Nota per te, da togliere: {reduced_note('it-IT')}]" if half else ""
    note_en = f"\n\n[A note for you, to delete: {reduced_note('en-US')}]" if half else ""
    if rule == "us_dot":
        subject = f"Refund request - flight {flight} of {scheduled_at:%m/%d/%Y}, booking {pnr}"
        text = (
            f"Dear {name} Customer Relations,\n\nmy flight {flight} from {o} to {d}, scheduled on "
            f"{scheduled_at:%m/%d/%Y} at {dep}, booking {pnr}, was cancelled. Under the DOT rule on "
            "automatic "
            "refunds (14 CFR Part 260) I am owed a cash refund of the fare and of any paid extras not "
            "provided, within 7 business days for a card purchase. I have not received it. Please refund "
            "[AMOUNT PAID] to the original form of payment.\n\nIf it isn't refunded, I will file a complaint "
            f"with the U.S. Department of Transportation.\n\nSincerely,\n[NAME] - [EMAIL] - [PHONE]\n[Date]"
        )
        return subject, text
    if it:
        if disruption == "cancellation":
            # only what is known is stated; the rest is theirs to confirm, in brackets
            if notice_days is not None:
                what = (
                    f"Il volo è stato cancellato con un preavviso di {notice_days} giorni, meno dei 14 "
                    "previsti dall'art. 5, par. 1, lett. c). [Se vero: non mi è stato offerto un volo "
                    "alternativo nei termini dello stesso articolo.]"
                )
            else:
                what = (
                    "Il volo è stato cancellato con comunicazione del [DATA DELL'AVVISO]. [Se vero: meno di "
                    "14 "
                    "giorni prima della partenza, senza offerta di un volo alternativo nei termini dell'art. "
                    "5, "
                    "par. 1, lett. c).]"
                )
        elif disruption == "denied_boarding":
            what = "Mi è stato negato l'imbarco contro la mia volontà."
        elif delay_min is not None:
            h, m = _hm(delay_min)
            what = f"Il volo è arrivato a destinazione con un ritardo di {h} ore e {m} minuti."
        else:
            what = (
                "Il volo è arrivato a destinazione alle [ORA EFFETTIVA], con un ritardo di [X] ore e [Y] "
                "minuti."
            )
        subject = (
            f"Richiesta di compensazione pecuniaria ai sensi del Regolamento (CE) n. 261/2004 - "
            f"prenotazione {pnr}, volo {flight} del {day}"
        )
        text = (
            f"Spett.le {name},\n\nil sottoscritto [NOME COGNOME] era passeggero del volo {flight} da {o} a "
            f"{d}, "
            f"con partenza prevista il {day} alle {dep} e arrivo previsto alle {arr}, codice di prenotazione "
            f"{pnr}, biglietto/i n. [NUMERI].\n\n{what}\n\nAi sensi degli artt. 5, 6 e 7 del Reg. (CE) "
            "261/2004, "
            "come interpretati dalla Corte di giustizia UE (cause riunite C-402/07 e C-432/07 Sturgeon; "
            f"C-581/10 Nelson), chiedo la compensazione pecuniaria di € {money} per passeggero (distanza "
            f"{km} "
            f"km).{note_it}\n\nQualora riteniate sussistere circostanze eccezionali (art. 5, par. 3), vi "
            "chiedo di "
            "indicarle specificamente e documentarle, precisando le misure ragionevoli adottate per evitarle "
            "(CGUE C-549/07 Wallentin-Hermann).\n\nChiedo il pagamento entro 14 giorni mediante bonifico: "
            "intestatario [NOME] - IBAN [IBAN]. Non accetto voucher in sostituzione del pagamento in denaro "
            "(art. 7, par. 3).\n\nIn mancanza di riscontro entro 6 settimane presenterò reclamo all'ENAC e "
            "mi "
            "riservo di agire nelle sedi competenti, anche tramite il procedimento europeo per le "
            "controversie "
            "di modesta entità.\n\nAllego: conferma di prenotazione, carta/e d'imbarco, comunicazione di "
            "cancellazione/ritardo, ricevute.\n\nDistinti saluti,\n[NOME COGNOME] - [INDIRIZZO] - [EMAIL] - "
            "[TELEFONO] - [Luogo, data]"
        )  # fmt: skip
        return subject, text
    if disruption == "cancellation":
        if notice_days is not None:
            what = (
                f"The flight was cancelled with {notice_days} days' notice, less than the 14 days of Article "
                "5(1)(c). [If true: I was not offered rerouting within the time limits of that Article.]"
            )
        else:
            what = (
                "The flight was cancelled on [DATE OF NOTICE]. [If true: less than 14 days before departure, "
                "and I was not offered rerouting within the time limits of Article 5(1)(c).]"
            )
    elif disruption == "denied_boarding":
        what = "I was denied boarding against my will."
    elif delay_min is not None:
        h, m = _hm(delay_min)
        what = f"The flight arrived at {d} {h} hours and {m} minutes after the scheduled arrival time."
    else:
        what = (
            f"The flight arrived at {d} at [ACTUAL_ARR], [X] hours and [Y] minutes after the scheduled "
            "arrival."
        )
    subject = f"EU Regulation 261/2004 compensation claim - booking {pnr}, flight {flight} of {day}"
    text = (
        f"Dear {name} Customer Relations,\n\nI was a passenger on flight {flight} from {o} to {d}, scheduled "
        "to "
        f"depart on {day} at {dep} and to arrive at {arr}, booking reference {pnr}, ticket number(s) "
        f"[TICKET_NO(S)].\n\n{what}\n\nUnder Articles 5, 6 and 7 of Regulation (EC) No 261/2004, as "
        "interpreted "
        "by the Court of Justice of the EU (Joined Cases C-402/07 and C-432/07 Sturgeon, and C-581/10 "
        "Nelson), "
        f"I am entitled to compensation of €{money} per passenger for a flight distance of {km} km.{note_en}"
        "\n\nIf "
        "you "
        "consider that extraordinary circumstances apply, please state them specifically and provide "
        "supporting evidence, and explain which reasonable measures were taken to avoid the delay or "
        "cancellation (Article 5(3); CJEU C-549/07 Wallentin-Hermann). A technical problem with the aircraft "
        "does not normally qualify.\n\nPlease pay the amount within 14 days by bank transfer to: Account "
        "holder: [NAME] - IBAN: [IBAN] - BIC: [BIC]. I do not accept vouchers or travel credit in place of "
        "cash compensation (Article 7(3)).\n\nAttached: booking confirmation, boarding pass(es), [disruption "
        "notification], [receipts].\n\nIf I do not receive a satisfactory reply within 6 weeks, I will refer "
        "the matter to the competent National Enforcement Body and may begin proceedings, including the "
        "European Small Claims Procedure, without further notice.\n\nYours faithfully,\n[OWNER NAME] - "
        "[ADDRESS] - [EMAIL] - [PHONE]\n[Date]"
    )  # fmt: skip
    return subject, text


def buttons(flight: str, rule: Rule, lang: str) -> list[tuple[str, str, str | None]]:
    """(title, url, note) for the claim's buttons: the airline's own site with the path to its form
    (when Pisa knows the airline), then the public form (the EU's for EU261, DOT's for the US)."""
    it = lang.startswith("it")
    out: list[tuple[str, str, str | None]] = []
    airline = links.AIRLINES.get(carrier(flight))
    if airline is not None:
        out.append((
            f"Apri {airline.name}" if it else f"Open {airline.name}", airline.url,
            airline.steps_it if it else airline.steps_en,
        ))  # fmt: skip
    if rule == "eu261":
        out.append(("Modulo UE reclami" if it else "EU complaint form", links.EU_FORM, None))
    elif rule == "us_dot":
        out.append(("Reclamo al DOT" if it else "DOT complaint", links.DOT_FORM, None))
    return out


def task_title(flight: str, rule: Rule, amount: Decimal | None, lang: str) -> str:
    airline = links.AIRLINES.get(carrier(flight))
    who = airline.name if airline else None
    if lang.startswith("it"):
        if rule == "us_dot":
            return f"Controllare il rimborso del volo {flight}" + (f" ({who})" if who else "")
        sums = f" ({amount:.0f} €)" if amount is not None else ""
        return f"Chiedere {'a ' + who + ' ' if who else ''}la compensazione del volo {flight}{sums}"
    if rule == "us_dot":
        return f"Check the refund for flight {flight}" + (f" ({who})" if who else "")
    sums = f" (€{amount:.0f})" if amount is not None else ""
    return f"Claim {'from ' + who + ' ' if who else ''}the compensation for flight {flight}{sums}"


def deadline(scheduled_at: datetime) -> date:
    return (scheduled_at + SOFT_DEADLINE).date()
