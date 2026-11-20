"""The user's trips, built from their confirmation emails (flights, trains, hotels, car rentals).

**An email is data.** This module reads an email's labelled fields into legs; nothing an email asks
for is ever done. An email that says a flight is cancelled (or tells them to cancel it) builds nothing and
changes nothing: it is only noted, and the user decides. Times stay in each leg's own zone (a 10:35 departure
from Rome is 10:35 Rome time; its 14:20 arrival is New York's). Only the last 4 characters of a booking code
leave this module. No I/O."""

import re
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pisa.domain import countries
from pisa.domain.countries import Where
from pisa.domain.lexicon.dates import day_or_none
from pisa.domain.lexicon.months import EN_MONTHS_SHORT, IT_MONTHS_SHORT, month_numbers

LegKind = Literal["flight", "train", "car", "hotel", "other"]
TRANSPORT: frozenset[str] = frozenset({"flight", "train"})

# --- recognising a confirmation -----------------------------------------------------------------------------

CONFIRMS = re.compile(
    r"conferm|confirm|prenotazion|booking|itinerar|e-?ticket|biglietto|ricevuta|reservation|your trip|"
    r"il tuo viaggio|carta d.?imbarco|boarding pass",
    re.IGNORECASE,
)
# An email that STATES a cancellation: a sentence of fact, or a subject announcing it. Never an
# offer or a button ("cancellazione gratuita", "You can cancel your booking online", "Annulla la
# prenotazione").
# It only adds a flag on the trip it names: its legs are read as any other's, nothing is changed.
CANCELS_SUBJECT = re.compile(
    r"\b(?:cancellat[oaie]|annullat[oaie]|cancel+ed|cancellation of your|cancellazione del (?:volo|treno))\b",
    re.IGNORECASE,
)
CANCELS = re.compile(
    r"(?:è stato|è stata|e' stato|sono stati|was|were|has been|have been)\s+(?:cancellat|annullat|cancel+ed)",
    re.IGNORECASE,
)

_MONTHS = month_numbers(IT_MONTHS_SHORT, EN_MONTHS_SHORT)
_DATE = re.compile(
    r"(?P<iso>(?P<y1>\d{4})-(?P<m1>\d{2})-(?P<d1>\d{2}))"
    r"|(?P<dmy>(?P<d2>\d{1,2})[/.](?P<m2>\d{1,2})[/.](?P<y2>\d{4}))"  # D/M/Y or M/D/Y: by the line's hints
    r"|(?P<words>(?P<d3>\d{1,2})\s+(?P<mon3>[A-Za-z]{3})[a-z]*\.?,?\s+(?P<y3>\d{4}))"
    r"|(?P<us>(?P<mon4>[A-Za-z]{3})[a-z]*\.?\s+(?P<d4>\d{1,2}),?\s+(?P<y4>\d{4}))"
)
_TIME = re.compile(r"\b(?P<h>[01]?\d|2[0-3])[:.](?P<m>[0-5]\d)\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?(?!\d)")
_CODE = re.compile(
    r"(?:codice (?:di )?prenotazione|codice biglietto|numero (?:di )?prenotazione|PNR|booking (?:reference|"
    r"code|number|ref)|confirmation (?:number|code|no\.?)|reservation (?:number|code)|conferma n\.?)"
    r"\s*[:#]?\s*"
    r"(?P<code>[A-Z0-9][A-Z0-9-]{3,19})\b",
    re.IGNORECASE,
)
# An airline's code has a letter ("Flight 1234" is no airline "12")
_FLIGHT = re.compile(
    r"\b(?i:volo|flight)\s*(?:n\.?|no\.?|number|numero)?\s*:?\s*(?P<al>[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(?P<n>\d{1,4})\b"
)
_PLUS_DAYS = re.compile(r"\(\s*\+\s*(?P<n>[1-3])\s*\)|\+(?P<m>[1-3])\s*(?:day|giorn)", re.IGNORECASE)
_AMPM = re.compile(r"\d\s*[AaPp]\.?[Mm]\b")
_ITALIAN = re.compile(r"partenza|arrivo|ritiro|riconsegna|soggiorno|struttura|giorno", re.IGNORECASE)
# Who the booking is for: a passenger's or a guest's name, labelled
_PASSENGER = re.compile(
    r"(?:passegger[oi]|passengers?|ospite|guest(?: name)?|intestatario|traveller|traveler|nome)\s*[:\-]\s*"
    r"(?P<name>[A-Za-zÀ-ú' .-]{2,60})",
    re.IGNORECASE,
)
_ROUTE = re.compile(r"\b(?P<a>[A-Z]{3})\b\s*(?:→|->|\u2013|\u2014|-|>|to|a)\s*\b(?P<b>[A-Z]{3})\b")
_TRAIN = re.compile(
    r"\b(?P<name>frecciarossa|frecciargento|frecciabianca|italo|intercity|regionale|treno|train|amtrak|acela)"
    r"\s*(?:n\.?|no\.?)?\s*(?P<n>\d{2,5})\b",
    re.IGNORECASE,
)
_STATIONS = re.compile(r"(?P<a>[A-Za-zÀ-ú.' ]{3,40}?)\s*(?:→|->|>)\s*(?P<b>[A-Za-zÀ-ú.' ]{3,40})")
_RENTAL = re.compile(r"noleggio|car rental|rent a car|car hire|hertz|avis|europcar|sixt|enterprise|budget",
                     re.IGNORECASE)  # fmt: skip
_STAY = re.compile(
    r"hotel|booking\.com|airbnb|soggiorno|alloggio|struttura|your stay|check-in", re.IGNORECASE
)

DEP_LABELS = r"partenza|departure|departs|depart|parte alle|ritiro|pick-?up|check-?in"
ARR_LABELS = r"arrivo|arrival|arrives|arrive|riconsegna|drop-?off|return|check-?out"

# Online check-in opens this many hours before departure, by airline (IATA prefix); 24 for most
CHECK_IN_HOURS: dict[str, int] = {"LH": 23, "LX": 23, "OS": 23, "AF": 30, "KL": 30, "EK": 48, "TK": 24}
CHECK_IN_DEFAULT_H = 24
HOTEL_IN, HOTEL_OUT = time(15, 0), time(11, 0)

# The defaults `app_settings.trips` changes (minutes)
BUFFERS: dict[str, int] = {
    "international_min": 180, "domestic_min": 120, "train_min": 20, "transfer_default_min": 45,
}  # fmt: skip


@dataclass(frozen=True)
class Leg:
    kind: LegKind
    at: datetime  # aware, in the leg's own zone
    until: datetime | None = None
    ref: str | None = None
    place: str | None = None
    tz: str | None = None
    confirmation_last4: str | None = None
    origin: Where | None = None
    destination: Where | None = None  # where a transport leg arrives; a stay's city
    email_id: str | None = None

    @property
    def key(self) -> str:
        """A leg is its kind, its ref, its local departure day and its booking's last 4: a resent
        confirmation or an itinerary change is the same leg, never a second one."""
        ref = re.sub(r"\s+", "", (self.ref or "").upper())
        return f"{self.kind}|{ref}|{self.at.date().isoformat()}|{self.confirmation_last4 or ''}"

    def same(self, other: "Leg") -> bool:
        """The same leg: the same key; or, with the same booking (last 4), the same kind and ref
        moved by at most 3 days (an itinerary change)."""
        if self.key == other.key:
            return True
        if not (self.confirmation_last4 and self.confirmation_last4 == other.confirmation_last4):
            return False
        if self.kind != other.kind or self.key.split("|")[1] != other.key.split("|")[1] or not self.ref:
            return False
        return abs((self.at.date() - other.at.date()).days) <= 3

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "at": self.at.isoformat()}
        for name in ("until", "ref", "place", "tz", "confirmation_last4"):
            if (value := getattr(self, name)) is not None:
                out[name] = value.isoformat() if isinstance(value, datetime) else value
        return out

    def stored(self) -> dict[str, Any]:
        """The leg as Pisa keeps it (with where its ends are, for grouping and the residency counter)."""
        out = self.json()
        out["key"] = self.key
        if self.origin is not None:
            out["origin"] = [self.origin.city, self.origin.country, self.origin.tz]
        if self.destination is not None:
            out["destination"] = [self.destination.city, self.destination.country, self.destination.tz]
        if self.email_id:
            out["email_id"] = self.email_id
        return out


def from_stored(data: dict[str, Any]) -> Leg:
    def where(value: Any) -> Where | None:
        return Where(*value) if value else None

    return Leg(
        kind=data["kind"],
        at=datetime.fromisoformat(data["at"]),
        until=datetime.fromisoformat(data["until"]) if data.get("until") else None,
        ref=data.get("ref"),
        place=data.get("place"),
        tz=data.get("tz"),
        confirmation_last4=data.get("confirmation_last4"),
        origin=where(data.get("origin")),
        destination=where(data.get("destination")),
        email_id=data.get("email_id"),
    )


@dataclass(frozen=True)
class Read:
    """What one email gave: its legs, or that it speaks of a cancellation (noted, never acted on)."""

    legs: list[Leg] = field(default_factory=list)
    cancel_refs: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # the passengers' or guests' names it gives


def last4(code: str | None) -> str | None:
    if not code:
        return None
    clean = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    return clean[-4:] if len(clean) >= 4 else None


# The email's own hint for a line that has none (its other lines' words, its 12-hour times)
_HINT: ContextVar[str | None] = ContextVar("trips_date_hint", default=None)


def order_of(line: str) -> str | None:
    """How a numeric date reads on this line: M/D/Y with a 12-hour time ("6:10 PM"), D/M/Y with
    Italian words; else unknown (an ambiguous date is then refused, never guessed)."""
    if _AMPM.search(line):
        return "mdy"
    if _ITALIAN.search(line):
        return "dmy"
    return None


def _date(m: re.Match[str], order: str | None = "dmy") -> date | None:
    if m.group("iso"):
        return day_or_none(int(m.group("y1")), int(m.group("m1")), int(m.group("d1")))
    if m.group("dmy"):
        a, b, year = int(m.group("d2")), int(m.group("m2")), int(m.group("y2"))
        if order is None:
            if a > 12:
                order = "dmy"
            elif b > 12:
                order = "mdy"
            elif a == b:
                order = "dmy"
            else:
                return None  # 05/11: 5 November or May 11? Never a guess
        return day_or_none(year, b, a) if order == "dmy" else day_or_none(year, a, b)
    if m.group("words"):
        month = _MONTHS.get(m.group("mon3").lower()[:3])
        return day_or_none(int(m.group("y3")), month, int(m.group("d3"))) if month else None
    if m.group("us"):
        month = _MONTHS.get(m.group("mon4").lower()[:3])
        return day_or_none(int(m.group("y4")), month, int(m.group("d4"))) if month else None
    return None


def _time_of(text: str) -> time | None:
    m = _TIME.search(text)
    if m is None:
        return None
    hour, minute = int(m.group("h")), int(m.group("m"))
    if (ampm := m.group("ampm")) is not None:
        pm = ampm.lower().startswith("p")
        hour = (hour % 12) + (12 if pm else 0)
    return time(hour, minute)


def when(
    text: str, tz: str | None, default_time: time | None = None, base: date | None = None
) -> datetime | None:
    """The first date (and time) in `text`, in zone `tz`. With no date, a time is on `base`'s day, moved on by
    "(+1)" ("Arrival: 12:15 (+1)"). A numeric date reads by the line's own hints (`order_of`)."""
    if tz is None:
        return None
    order = order_of(text) or _HINT.get()
    dates = list(_DATE.finditer(text))
    found = next((d for m in dates if (d := _date(m, order)) is not None), None)
    if found is None and dates:
        return None  # a date that can't be read for sure: no time on a guessed day
    if found is None:
        if base is None:
            return None
        plus = _PLUS_DAYS.search(text)
        found = base + timedelta(days=int((plus.group("n") or plus.group("m")) if plus else 0))
    at = _time_of(_DATE.sub(" ", _PLUS_DAYS.sub(" ", text))) or default_time
    if at is None:
        return None
    return datetime.combine(found, at, tzinfo=ZoneInfo(tz))


def _labelled(text: str, labels: str) -> str | None:
    """The rest of the first line that starts with (or holds, after a separator) one of `labels`."""
    for line in text.splitlines():
        if re.search(rf"(?:^|[\s|•·,;(])(?:{labels})\b\s*[:\-]?", line, re.IGNORECASE):
            m = re.search(rf"(?:{labels})\b\s*[:\-]?\s*(.*)$", line, re.IGNORECASE)
            if m is not None:
                return m.group(1)
    return None


def read_email(email_id: str, subject: str, body: str, fallback_tz: str) -> Read:
    """An email's legs, when it is a confirmation of a flight, a train, a stay or a rental, and the refs of a
    cancellation it states (a flag on the trip, never a change: Pisa never changes a trip because an email
    says so)."""
    text = f"{subject}\n{body or ''}"
    cancel_refs: list[str] = []
    if CANCELS_SUBJECT.search(subject) or CANCELS.search(body or ""):
        cancel_refs = [f"{m.group('al')}{m.group('n')}" for m in _FLIGHT.finditer(text)]
        cancel_refs = list(dict.fromkeys(cancel_refs + [m.group("n") for m in _TRAIN.finditer(text)]))
    if not CONFIRMS.search(text):
        return Read(cancel_refs=cancel_refs)
    code = _CODE.search(text)
    four = last4(code.group("code")) if code else None
    hint = _HINT.set(order_of(text))  # the email's own way of writing a date
    try:
        legs = (
            _flights(text, four)
            or _trains(text, four)
            or _rental(text, four)
            or _stay(text, four, fallback_tz)
        )
    finally:
        _HINT.reset(hint)
    names = [m.group("name").strip(" .-") for m in _PASSENGER.finditer(body or "")]
    return Read(legs=[replace(leg, email_id=email_id) for leg in legs], cancel_refs=cancel_refs, names=names)


def _flights(text: str, four: str | None) -> list[Leg]:
    """One leg per flight number, its route and its times from its own block (from the number to the next
    one: a header "Your trip: FCO → JFK" never pairs with the way back). A flight needs a departure
    and an arrival after it, or it is no leg."""
    found = list(_FLIGHT.finditer(text))
    legs: list[Leg] = []
    seen: set[str] = set()
    for n, m in enumerate(found):
        number = f"{m.group('al')}{m.group('n')}"
        if number in seen:
            continue
        block = text[m.start() : found[n + 1].start() if n + 1 < len(found) else len(text)]
        route = next(
            ((r.group("a"), r.group("b"), a, b) for r in _ROUTE.finditer(block)
             if (a := countries.airport(r.group("a"))) and (b := countries.airport(r.group("b")))),
            None,
        )  # fmt: skip
        if route is None:
            continue
        code_a, code_b, origin, destination = route
        dep_line = next(
            (x for x in block.splitlines() if re.search(r"partenza|departure|departs|depart", x, re.I)), None
        )
        arr_line = next(
            (x for x in block.splitlines() if re.search(r"arrivo|arrival|arrives|arrive", x, re.I)), None
        )
        dep = when(dep_line, origin.tz) if dep_line else None
        if dep is None or arr_line is None:
            continue
        arr = when(arr_line, destination.tz, base=dep.date())
        if arr is None or arr <= dep:
            continue  # an arrival that can't be read, or before the departure: no leg (never a wrong one)
        seen.add(number)
        legs.append(Leg(
            kind="flight", at=dep, until=arr, ref=number, place=f"{code_a} → {code_b}", tz=origin.tz,
            confirmation_last4=four, origin=origin, destination=destination,
        ))  # fmt: skip
    return legs


def _trains(text: str, four: str | None) -> list[Leg]:
    m = _TRAIN.search(text)
    if m is None:
        return []
    route = next(
        ((a, b, s) for s in _STATIONS.finditer(text)
         if (a := countries.city(s.group("a"))) and (b := countries.city(s.group("b"))) and a != b),
        None,
    )  # fmt: skip
    if route is None:
        return []
    origin, destination, stations = route
    dep_line = _labelled(text, DEP_LABELS)
    arr_line = _labelled(text, ARR_LABELS)
    dep = when(dep_line, origin.tz) if dep_line else None
    arr = when(arr_line, destination.tz, base=dep.date()) if arr_line and dep else None
    if dep is None and (only := when(text, origin.tz)) is not None:
        dep = only
    if dep is None or (arr_line is not None and (arr is None or arr <= dep)):
        return []  # an arrival that can't be read, or before the departure: no leg
    name = m.group("name").capitalize()
    place = f"{stations.group('a').strip()} → {stations.group('b').strip()}"
    return [Leg(kind="train", at=dep, until=arr,
                ref=f"{name} {m.group('n')}", place=place, tz=origin.tz, confirmation_last4=four,
                origin=origin, destination=destination)]  # fmt: skip


def _rental(text: str, four: str | None) -> list[Leg]:
    if not _RENTAL.search(text):
        return []
    pick = _labelled(text, r"ritiro|pick-?up")
    drop = _labelled(text, r"riconsegna|drop-?off|return")
    where = countries.city(pick) or countries.city(text)
    if pick is None or where is None:
        return []
    at = when(pick, where.tz, time(10, 0))
    if at is None:
        return []
    until = when(drop, where.tz, time(10, 0)) if drop else None
    brand = _RENTAL.search(text)
    return [Leg(kind="car", at=at, until=until if until and until > at else None,
                ref=brand.group(0).title() if brand else None, place=where.city, tz=where.tz,
                confirmation_last4=four, destination=where)]  # fmt: skip


def _stay(text: str, four: str | None, fallback_tz: str) -> list[Leg]:
    if not _STAY.search(text):
        return []
    check_in = _labelled(text, r"check-?in|arrivo")
    check_out = _labelled(text, r"check-?out|partenza")
    if check_in is None:
        return []
    named = _labelled(text, r"hotel|struttura|property|alloggio")
    address = _labelled(text, r"indirizzo|address|citt[aà]|city")
    where = countries.city(address) or countries.city(named) or countries.city(text)
    tz = where.tz if where else fallback_tz
    at = when(check_in, tz, HOTEL_IN)
    if at is None:
        return []
    until = when(check_out, tz, HOTEL_OUT) if check_out else None
    # the property's name only (never the rest of the line: a guest's name, a document number)
    name = re.split(r"[,(|;]| - |\d", named or "")[0].strip(" .:-")[:60] or None
    return [Leg(kind="hotel", at=at, until=until if until and until > at else None, ref=name,
                place=where.city if where else None, tz=tz, confirmation_last4=four,
                destination=where)]  # fmt: skip


# --- legs into trips ----------------------------------------------------------------------------------------

CONNECTION_MAX = timedelta(hours=24)  # an onward flight within this of the arrival continues the journey
STAY_MAX = timedelta(days=90)  # a trip with no way back yet holds legs this long after its arrival


@dataclass
class Shape:
    """A trip as grouping sees it."""

    id: str
    origin: Where | None
    destination: Where | None
    depart_at: datetime
    arrive_at: datetime
    back_at: datetime | None = None  # the arrival home, once a leg brings them back
    legs: list[Leg] = field(default_factory=list)
    doubt: str | None = None  # unverified_sender | someone_else (no tasks, no evidence)
    cancelled: bool = False
    by_owner: bool = False  # they changed it (a PATCH): a merge never drops it, only adds legs to it

    def end(self) -> datetime:
        return self.back_at or self.arrive_at + STAY_MAX


def known(trips: list[Shape], leg: Leg) -> tuple[Shape, Leg] | None:
    """The trip and its leg this one is the same as, if any."""
    for trip in trips:
        for other in trip.legs:
            if other.same(leg):
                return trip, other
    return None


def changed(old: Leg, new: Leg) -> bool:
    """A later email gives the same leg other times: a proposed change, never applied by itself."""
    return old.at != new.at or (new.until is not None and old.until != new.until)


def place_leg(trips: list[Shape], leg: Leg) -> Shape | None:
    """The trip this leg belongs to, or None for a new one. Already there (the same leg): that trip."""
    if (found := known(trips, leg)) is not None:
        return found[0]
    if leg.kind in TRANSPORT and leg.origin is not None:
        for trip in sorted(trips, key=lambda t: t.depart_at, reverse=True):
            if trip.back_at is not None or trip.destination is None:
                continue
            if trip.destination.city == leg.origin.city and trip.arrive_at <= leg.at <= trip.end():
                return trip
        return None
    for trip in sorted(trips, key=lambda t: t.depart_at, reverse=True):
        if trip.depart_at - timedelta(days=1) <= leg.at <= trip.end():
            if (
                leg.destination is None
                or trip.destination is None
                or leg.destination.city
                in (trip.destination.city, *(x.destination.city for x in trip.legs if x.destination))
            ):
                return trip
    return None


def attach(trip: Shape, leg: Leg) -> None:
    """The leg joins the trip: a connection moves its arrival on; a leg that ends where it began brings it
    home."""
    if any(other.same(leg) for other in trip.legs):
        return
    trip.legs.append(leg)
    trip.legs.sort(key=lambda x: x.at)
    if leg.kind not in TRANSPORT or leg.destination is None:
        return
    if trip.origin is not None and leg.destination.city == trip.origin.city:
        trip.back_at = leg.until or leg.at
    elif leg.at - trip.arrive_at <= CONNECTION_MAX:
        trip.destination, trip.arrive_at = leg.destination, leg.until or leg.at


def new_shape(trip_id: str, leg: Leg) -> Shape:
    return Shape(trip_id, leg.origin, leg.destination, leg.at, leg.until or leg.at, legs=[leg])


def _in_order(legs: list[Leg]) -> list[Leg]:
    """Legs in the journey's own order (time, then key): never the order their emails were read in."""
    return sorted(legs, key=lambda x: (x.at, x.key, x.email_id or ""))


def rebuild(trip_id: str, legs: list[Leg]) -> Shape:
    """A trip made again from its legs (after the user applied a proposed change, or a merge)."""
    ordered = _in_order(legs)
    shape = new_shape(trip_id, ordered[0])
    for leg in ordered[1:]:
        attach(shape, leg)
    return shape


def journey(trip_id: str, legs: list[Leg]) -> Shape | None:
    """These legs as one trip, read in the journey's order (the outbound first): None when one of them would
    start a trip of its own. Order-independent by construction: the legs are sorted before they are placed."""
    ordered = _in_order(legs)
    # the outbound opens the journey, as when it is read first: a check-in a few hours before it is its stay
    first = next((x for x in ordered if x.kind in TRANSPORT), ordered[0])
    if first.at - ordered[0].at > timedelta(days=1):
        first = ordered[0]
    ordered.remove(first)
    shape = new_shape(trip_id, first)
    for leg in ordered:
        if place_leg([shape], leg) is None:
            return None
        attach(shape, leg)
    return shape


def _same_booking(a: Shape, b: Shape) -> bool:
    """One booking's confirmations (the same code's last 4 on a transport leg of each), close in time."""
    fours = {x.confirmation_last4 for x in a.legs if x.confirmation_last4 and x.kind in TRANSPORT}
    if not fours.isdisjoint(x.confirmation_last4 for x in b.legs if x.kind in TRANSPORT):
        legs = a.legs + b.legs
        return max(x.at for x in legs) - min(x.at for x in legs) <= STAY_MAX
    return False


def joined(survivor: Shape, other: Shape) -> Shape | None:
    """Two trips that are one journey, as one (`survivor`'s id); None when they are two. A hotel or a return
    read before its outbound started a trip of its own: when the outbound arrives the two are one. Matched on
    the booking (the same code on a flight or train of each) or, else, dates and places: every leg of both
    fits one trip read in the journey's order."""
    if survivor.doubt != other.doubt or survivor.cancelled or other.cancelled or other.by_owner:
        return None
    legs = survivor.legs + [x for x in other.legs if not any(x.same(y) for y in survivor.legs)]
    if (shape := journey(survivor.id, legs)) is None and _same_booking(survivor, other):
        shape = rebuild(survivor.id, legs)
    if shape is not None:
        shape.doubt, shape.by_owner = survivor.doubt, survivor.by_owner
    return shape


def merge(trips: list[Shape], trip: Shape) -> tuple[Shape, Shape] | None:
    """Another trip that is one journey with `trip`: (the two as one, the trip that goes into it). The one
    they changed stays (a trip they changed never goes); else the one that starts first."""
    for other in sorted(trips, key=lambda t: (t.depart_at, t.id)):
        if other.id == trip.id:
            continue
        keep, gone = sorted((trip, other), key=lambda t: (not t.by_owner, t.depart_at, t.id))
        if (shape := joined(keep, gone)) is not None:
            return shape, gone
    return None


def regroup(trips: list[Shape]) -> list[Shape]:
    """Pisa's own trips (none they changed, one `doubt`) made again from the SET of their legs, as
    reading every leg once in the journey's order (`_in_order`) would make them: a function of the legs, never
    of the order their emails were read in. So an open one-way trip that took in a stay weeks later gives it
    back once the return closes the trip. Each new trip keeps the id of the trip that held its first leg (else
    of another trip that held one of its legs); '' when none is left (a new trip)."""
    if not trips:
        return []
    legs: list[Leg] = []
    for leg in _in_order([x for t in trips for x in t.legs]):
        if not any(leg.same(y) for y in legs):
            legs.append(leg)
    groups: list[Shape] = []
    for n, leg in enumerate(legs):
        if (trip := place_leg(groups, leg)) is None:
            groups.append(new_shape(f"#{n:06d}", leg))
        else:
            attach(trip, leg)
    for group in groups:
        group.doubt = trips[0].doubt
    changed = True
    while changed:
        changed = False
        for group in sorted(groups, key=lambda t: (t.depart_at, t.id)):
            if (found := merge(groups, group)) is not None:
                shape, gone = found
                groups = [shape if t.id == shape.id else t for t in groups if t.id != gone.id]
                changed = True
                break
    held: list[tuple[Leg, str]] = [(x, t.id) for t in trips for x in _in_order(t.legs)]
    used: set[str] = set()
    for group in sorted(groups, key=lambda t: (t.depart_at, _in_order(t.legs)[0].key)):
        ids = [trip_id for leg in _in_order(group.legs) for x, trip_id in held if x.same(leg)]
        group.id = next((i for i in ids if i not in used), "")
        used.add(group.id)
    return sorted(groups, key=lambda t: (t.depart_at, _in_order(t.legs)[0].key))


def same_legs(a: Shape, b: Shape) -> bool:
    return sorted(x.key for x in a.legs) == sorted(x.key for x in b.legs) and a.back_at == b.back_at


def combine_checklists(keep: list[dict[str, Any]], gone: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A merge never drops their ticks. The two lists as one (each text once, the survivor's
    order first); where both hold an item, their wins (their own item, then a ticked one)."""
    out: dict[str, dict[str, Any]] = {}
    for item in [*keep, *gone]:
        text = str(item.get("text") or "").strip().casefold()
        if not text:
            continue
        old = out.get(text)
        if old is None or (item.get("by") == "owner", bool(item.get("done"))) > (
            old.get("by") == "owner",
            bool(old.get("done")),
        ):
            out[text] = item
    order = list(dict.fromkeys(str(i.get("text") or "").strip().casefold() for i in [*keep, *gone]))
    # no cap: a merge never drops an item of theirs
    return [out[t] for t in order if t in out]


def connection(trip: Shape, leg: Leg) -> bool:
    """A transport leg within 24 h of the trip's previous arrival continues the journey (no
    leave-at:
    they are already at the airport)."""
    before = [x.until or x.at for x in trip.legs if x.kind in TRANSPORT and x.at < leg.at and x is not leg]
    return bool(before) and leg.at - max(before) <= CONNECTION_MAX


def title(shape: Shape) -> str:
    if shape.origin is not None and shape.destination is not None:
        return f"{shape.origin.city} → {shape.destination.city}"
    if shape.destination is not None:
        return f"Soggiorno a {shape.destination.city}"
    first = shape.legs[0]
    return first.ref or first.place or "Viaggio"


# --- what Pisa makes of a trip
# ---------------------------------------------------------------------------------


def check_in_opens(leg: Leg) -> datetime | None:
    if leg.kind != "flight" or not leg.ref:
        return None
    return leg.at - timedelta(hours=CHECK_IN_HOURS.get(leg.ref[:2].upper(), CHECK_IN_DEFAULT_H))


def buffer_min(leg: Leg, buffers: dict[str, int]) -> int:
    if leg.kind == "train":
        return buffers["train_min"]
    abroad = (
        leg.origin is not None
        and leg.destination is not None
        and leg.origin.country != leg.destination.country
    )
    return buffers["international_min" if abroad else "domestic_min"]


def leave_at(leg: Leg, travel_min: int | None, buffers: dict[str, int]) -> datetime:
    """When to leave for the airport or the station: the departure, minus the buffer, minus the way there
    (the routes tools'; else the default transfer)."""
    way = travel_min if travel_min is not None else buffers["transfer_default_min"]
    return leg.at - timedelta(minutes=buffer_min(leg, buffers) + way)


def buffers_of(setting: dict[str, Any] | None) -> dict[str, int]:
    out = dict(BUFFERS)
    for name, value in (setting or {}).items():
        if name in out and isinstance(value, int) and 0 <= value <= 600:
            out[name] = value
    return out


def local_text(at: datetime, lang: str) -> str:
    return f"{at:%d/%m} alle {at:%H:%M}" if lang == "it-IT" else f"{at:%b %d} at {at:%H:%M}"


def _clock_of(leg: Leg, lang: str) -> str:
    """Whose clock a time is on (the leg's own city's)."""
    city = leg.origin.city if leg.origin is not None else None
    if not city:
        return ""
    return f" (ora di {city})" if lang == "it-IT" else f" ({city} time)"


def check_in_title(leg: Leg, opens: datetime, lang: str) -> str:
    if lang == "it-IT":
        return f"Check-in online {leg.ref}: apre il {local_text(opens, lang)}{_clock_of(leg, lang)}"
    return f"Online check-in {leg.ref}: opens {local_text(opens, lang)}{_clock_of(leg, lang)}"


def leave_title(leg: Leg, at: datetime, lang: str) -> str:
    to = "l'aeroporto" if leg.kind == "flight" else "la stazione"
    where = (leg.place or "").split("→")[0].strip()
    if lang == "it-IT":
        return f"Partire per {to} ({where}) per {leg.ref}: alle {at:%H:%M}{_clock_of(leg, lang)}"
    what = "airport" if leg.kind == "flight" else "station"
    return f"Leave for the {what} ({where}) for {leg.ref}: at {at:%H:%M}{_clock_of(leg, lang)}"


def abroad(origin: str | None, destination: str | None) -> bool:
    return bool(origin and destination and origin != destination)


# --- landing and leaving ------------------------------------------------------------------------------------

LANDING_WINDOW = timedelta(hours=12)  # a zone change this long after a leg's arrival is that landing
LANDING_EARLY = timedelta(minutes=30)  # and from 30 minutes before it (never hours early)
LANDING_HOLD = timedelta(minutes=10)  # the new zone must still hold on a report this much later
CHECKLIST_DAYS = 3


def landed_leg(legs: list[Leg], now: datetime, tz: str | None, region: str | None) -> Leg | None:
    """The transport leg the phone's new zone or region says they just got off: arrived in the last 12 h (or
    due within 30 min: an early landing), where the phone now is."""
    best: Leg | None = None
    for leg in legs:
        if leg.kind not in TRANSPORT or leg.destination is None:
            continue
        arrived = leg.until or leg.at
        if not (now - LANDING_WINDOW <= arrived <= now + LANDING_EARLY):
            continue
        there = leg.destination.tz == tz or (region is not None and leg.destination.country == region)
        if there and (best is None or arrived > (best.until or best.at)):
            best = leg
    return best


def welcome(city: str, now_local: str, tomorrow: list[str], bound: list[str], lang: str) -> str:
    """The landing's one re-brief ("Benvenuto a New York: sono le 18:40, domani hai…")."""
    if lang == "it-IT":
        line = f"Benvenuto a {city}: sono le {now_local}"
        line += (
            f", domani hai {'; '.join(tomorrow[:3])}." if tomorrow else ", domani non hai niente in agenda."
        )
    else:
        line = f"Welcome to {city}: it's {now_local}"
        line += f", tomorrow you have {'; '.join(tomorrow[:3])}." if tomorrow else ", nothing on tomorrow."
    # what only this country allows, said once on landing ("questi 3 li puoi fare solo qui")
    only = countries.only_here_line(bound[:4], lang)
    return f"{line} {only}" if only else line


def welcome_back_line(city: str, bound: list[str], lang: str) -> str:
    """A landing in their quiet hours, said in their next briefing (no clock: the briefing has
    one)."""
    line = f"Benvenuto a {city}." if lang == "it-IT" else f"Welcome to {city}."
    only = countries.only_here_line(bound[:4], lang)
    return f"{line} {only}" if only else line


def leaving_line(city: str, days: int, bound_here: list[str], lang: str) -> str:
    """3 days before a trip abroad: the list is ready, and what only this country allows."""
    from pisa.domain.countries import only_here_line

    only = only_here_line(bound_here, lang)
    if lang == "it-IT":
        head = f"Tra {days} giorni parti per {city}: la lista per partire è pronta, chiedimela."
    else:
        head = f"In {days} days you leave for {city}: your packing list is ready, just ask."
    return f"{head} {only}" if only else head


@dataclass(frozen=True)
class Document:
    """A vault document as the checklist needs it (when the vault is there)."""

    label: str
    country: str
    expires_on: date


def checklist(
    *,
    to_country: str | None,
    until: date,
    documents: list[Document],
    bound_here: list[str],
    check_ins: list[str],
    mine: list[str],
    lang: str,
) -> list[dict[str, Any]]:
    """Pisa's items for a trip abroad, then their own from earlier trips."""
    it = lang == "it-IT"
    items: list[str] = []
    for doc in documents:
        ok = doc.expires_on >= until
        soon = ok and doc.expires_on < until + timedelta(days=180)
        state = (
            ("valido" if it else "valid")
            if ok
            else ("SCADUTO per il viaggio" if it else "EXPIRED for the trip")
        )
        extra = (" (scade entro 6 mesi)" if it else " (expires within 6 months)") if soon else ""
        items.append(f"{doc.label}: {state}, scade il {doc.expires_on:%Y-%m-%d}{extra}" if it
                     else f"{doc.label}: {state}, expires {doc.expires_on:%Y-%m-%d}{extra}")  # fmt: skip
    if not documents:
        items.append(
            "Documenti: passaporto valido per tutto il viaggio"
            if it
            else "Documents: a passport valid for the trip"
        )
    if (adapter := countries.plug(to_country, lang)) is not None:
        items.append(adapter)
    items += [bound_item(t, lang) for t in bound_here[:6]]
    items += check_ins
    items.append("Assicurazione di viaggio" if it else "Travel insurance")
    out = [{"text": text[:200], "done": False, "by": "pisa"} for text in items]
    seen = {item["text"].casefold() for item in out}
    for text in mine:
        if text.casefold() not in seen:
            out.append({"text": text[:200], "done": False, "by": "owner"})
            seen.add(text.casefold())
    return out[:30]


_BOUND_PREFIXES = ("Da finire qui: ", "To finish here: ")


def bound_item(title: str, lang: str) -> str:
    return f"{_BOUND_PREFIXES[0] if lang == 'it-IT' else _BOUND_PREFIXES[1]}{title}"


def sync_bound(items: list[dict[str, Any]], bound_here: list[str], lang: str) -> list[dict[str, Any]]:
    """A checklist made days ago, brought up to the open country-bound tasks now: Pisa's line for a
    task closed since leaves (unless they ticked it), a task bound since joins, their own items stay."""
    wanted = [bound_item(t, lang)[:200] for t in bound_here[:6]]
    out: list[dict[str, Any]] = []
    at: int | None = None
    for item in items:
        mine = item.get("by") == "pisa" and str(item.get("text", "")).startswith(_BOUND_PREFIXES)
        if mine:
            if item["text"] not in wanted and not item.get("done"):
                at = len(out)
                continue
            at = len(out) + 1  # new ones join after Pisa's bound lines
        out.append(item)
    have = {str(i.get("text")) for i in out}
    new = [{"text": t, "done": False, "by": "pisa"} for t in wanted if t not in have]
    if at is None:  # after Pisa's own lines, before theirs
        at = next((n for n, i in enumerate(out) if i.get("by") != "pisa"), len(out))
    return (out[:at] + new + out[at:])[:30]


def checklist_sheet(trip_title: str, items: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    """The sheet of a departure checklist: what's left, then what's done."""
    left = [i["text"] for i in items if not i.get("done")]
    done = [i["text"] for i in items if i.get("done")]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "title": "Da fare prima di partire" if lang == "it-IT" else "Before you leave"}
    ]
    blocks.append({"type": "list", "items": left[:8]} if left else
                  {"type": "text", "text": "Tutto fatto." if lang == "it-IT" else "All done."})  # fmt: skip
    if len(left) > 8:
        blocks.append({"type": "list", "items": left[8:16]})
    if done:
        blocks += [{"type": "divider"}, {"type": "chips", "items": [d[:40] for d in done[:12]]}]
    title_text = f"Partenza: {trip_title}" if lang == "it-IT" else f"Leaving: {trip_title}"
    return {"title": title_text[:80], "blocks": blocks}


# --- whose booking, from whom
# ----------------------------------------------------------------

# Who sells travel: airlines, railways, travel agencies, hotels, rentals. Only a confirmation from one of
# these
# (or a domain the user added in `app_settings.trips.senders`) makes tasks and residency evidence; any
# other is a
# trip "da confermare" until they confirm it (a phishing "booking" never acts).
TRAVEL_DOMAINS: tuple[str, ...] = (
    "ita-airways.com", "alitalia.com", "delta.com", "united.com", "aa.com", "jetblue.com", "lufthansa.com",
    "swiss.com", "airfrance.com", "klm.com", "britishairways.com", "iberia.com", "ryanair.com", "easyjet.com",
    "vueling.com", "emirates.com", "turkishairlines.com", "aerlingus.com", "virginatlantic.com",
    "trenitalia.it", "trenitalia.com", "italotreno.it", "italotreno.com", "amtrak.com", "trainline.com",
    "booking.com", "airbnb.com", "expedia.com", "hotels.com", "edreams.com", "opodo.com", "lastminute.com",
    "trip.com", "volagratis.com", "marriott.com", "hilton.com", "hyatt.com", "ihg.com", "accor.com",
    "hertz.com", "avis.com", "europcar.com", "sixt.com", "enterprise.com", "budget.com",
)  # fmt: skip


def travel_sender(address: str | None, extra: tuple[str, ...] = ()) -> bool:
    """The sender's domain is one that sells travel (or a subdomain of one)."""
    if not address or "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1].strip().lower().rstrip(">")
    return any(domain == d or domain.endswith("." + d) for d in (*TRAVEL_DOMAINS, *extra))


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", countries.plain(text))}


def for_someone_else(names: list[str], owner_names: list[str]) -> bool:
    """The booking names people and none of them is the user (by a name of theirs, 3+ letters). With no name
    in the email, or none known for them, it is theirs."""
    if not names or not owner_names:
        return False
    his = set().union(*(_tokens(n) for n in owner_names))
    return bool(his) and not any(_tokens(n) & his for n in names)
