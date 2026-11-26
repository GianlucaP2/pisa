"""GTFS-Realtime, read. A minimal protobuf wire-format reader for the fields Pisa uses of a GTFS-RT
`FeedMessage` (trip updates and service alerts; https://gtfs.org/realtime/reference/), so Pisa needs no
protobuf library. Extensions (the MTA's NYCT fields) and the fields Pisa doesn't use are skipped by their wire
type.

A feed is outside data: ids and texts are kept as text, cut short, never read as instructions. A
malformed feed raises `FeedError`, never anything else. No I/O."""

import struct
from dataclasses import dataclass, field

MAX_ENTITIES = 20_000  # a whole city's feed is ~1,300 trips (Rome) or ~3,000 (NYC's eight)
MAX_FIELDS = 200_000  # the fields of one message (Rome's whole feed's top level is ~1,300)
TEXT_MAX = 600


class FeedError(ValueError):
    """The bytes aren't a GTFS-RT feed Pisa can read."""


@dataclass(frozen=True)
class StopTime:
    stop_id: str
    sequence: int | None
    arrival: int | None  # POSIX seconds
    departure: int | None
    skipped: bool = False


@dataclass(frozen=True)
class TripUpdate:
    trip_id: str
    route_id: str
    direction: int | None
    stops: tuple[StopTime, ...]
    cancelled: bool = False


@dataclass(frozen=True)
class Alert:
    id: str
    routes: tuple[str, ...]
    stops: tuple[str, ...]
    periods: tuple[tuple[int | None, int | None], ...]
    header: str
    description: str
    effect: int | None = None
    cause: int | None = None


@dataclass
class Feed:
    timestamp: int | None = None
    trips: list[TripUpdate] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


# --- the wire format ---------------------------------------------------------------------------------------


def _varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(data) or shift > 63:
            raise FeedError("truncated varint")
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def fields(data: bytes) -> list[tuple[int, int | bytes]]:
    """A message's fields in order: (number, value), a varint as an int, a length-delimited field as bytes,
    a fixed 64/32-bit field as its bytes. Raises FeedError on anything malformed."""
    out: list[tuple[int, int | bytes]] = []
    i = 0
    while i < len(data):
        if len(out) >= MAX_FIELDS:
            raise FeedError("too many fields")
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise FeedError("field number 0")
        if wire == 0:
            value, i = _varint(data, i)
            out.append((number, value))
        elif wire == 2:
            size, i = _varint(data, i)
            if i + size > len(data):
                raise FeedError("truncated field")
            out.append((number, data[i : i + size]))
            i += size
        elif wire == 1:
            if i + 8 > len(data):
                raise FeedError("truncated fixed64")
            out.append((number, data[i : i + 8]))
            i += 8
        elif wire == 5:
            if i + 4 > len(data):
                raise FeedError("truncated fixed32")
            out.append((number, data[i : i + 4]))
            i += 4
        else:
            raise FeedError(f"wire type {wire}")
    return out


def clean(text: str, limit: int = 120) -> str:
    """Outside text as Pisa keeps it: printable, one line, cut (a feed's, an alert's, the NWS's)."""
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:limit]


def _text(value: int | bytes, limit: int = 120) -> str:
    if not isinstance(value, bytes):
        return str(value)
    return clean(value.decode("utf-8", errors="replace"), limit)


def _int(value: int | bytes) -> int | None:
    return value if isinstance(value, int) else None


def _signed(value: int | bytes) -> int | None:
    """An int64 or int32 varint (two's complement over 64 bits)."""
    if not isinstance(value, int):
        return None
    return value - (1 << 64) if value >= 1 << 63 else value


def _sub(value: int | bytes) -> list[tuple[int, int | bytes]]:
    return fields(value) if isinstance(value, bytes) else []


# --- the messages ------------------------------------------------------------------------------------------


def _event_time(value: int | bytes) -> int | None:
    """StopTimeEvent: 1 delay, 2 time (int64), 3 uncertainty."""
    for number, inner in _sub(value):
        if number == 2:
            return _signed(inner)
    return None


def _stop_time(value: int | bytes) -> StopTime:
    """StopTimeUpdate: 1 stop_sequence, 4 stop_id, 2 arrival, 3 departure, 5 schedule_relationship."""
    stop, sequence, arrival, departure, skipped = "", None, None, None, False
    for number, inner in _sub(value):
        if number == 1:
            sequence = _int(inner)
        elif number == 4:
            stop = _text(inner, 40)
        elif number == 2:
            arrival = _event_time(inner)
        elif number == 3:
            departure = _event_time(inner)
        elif number == 5:
            skipped = inner == 1  # SKIPPED
    return StopTime(stop, sequence, arrival, departure, skipped)


def _trip_update(value: int | bytes) -> TripUpdate:
    """TripUpdate: 1 trip (TripDescriptor: 1 trip_id, 5 route_id, 6 direction_id, 4 schedule_relationship),
    2 stop_time_update."""
    trip_id, route_id, direction, cancelled = "", "", None, False
    stops: list[StopTime] = []
    for number, inner in _sub(value):
        if number == 1:
            for n, v in _sub(inner):
                if n == 1:
                    trip_id = _text(v, 80)
                elif n == 5:
                    route_id = _text(v, 40)
                elif n == 6:
                    direction = _int(v)
                elif n == 4:
                    cancelled = v == 3  # CANCELED
        elif number == 2:
            stops.append(_stop_time(inner))
    return TripUpdate(trip_id, route_id, direction, tuple(stops), cancelled)


def _translated(value: int | bytes, prefer: str = "") -> str:
    """TranslatedString: 1 translation (1 text, 2 language): the preferred language's plain text, else the
    first that isn't HTML."""
    found: list[tuple[str, str]] = []
    for number, inner in _sub(value):
        if number == 1:
            text, lang = "", ""
            for n, v in _sub(inner):
                if n == 1:
                    text = _text(v, TEXT_MAX)
                elif n == 2:
                    lang = _text(v, 20).casefold()
            found.append((lang, text))
    plain = [(lang, t) for lang, t in found if "html" not in lang and t]
    for lang, text in plain:
        if prefer and lang.startswith(prefer):
            return text
    return plain[0][1] if plain else ""


def _alert(entity_id: str, value: int | bytes, prefer: str) -> Alert:
    """Alert: 1 active_period (1 start, 2 end), 5 informed_entity (2 route_id, 5 stop_id), 6 cause,
    7 effect, 10 header_text, 11 description_text."""
    routes: list[str] = []
    stops: list[str] = []
    periods: list[tuple[int | None, int | None]] = []
    header = description = ""
    effect = cause = None
    for number, inner in _sub(value):
        if number == 1:
            start = end = None
            for n, v in _sub(inner):
                if n == 1:
                    start = _int(v)
                elif n == 2:
                    end = _int(v)
            periods.append((start, end))
        elif number == 5:
            for n, v in _sub(inner):
                if n == 2:
                    routes.append(_text(v, 40))
                elif n == 5:
                    stops.append(_text(v, 40))
        elif number == 6:
            cause = _int(inner)
        elif number == 7:
            effect = _int(inner)
        elif number == 10:
            header = _translated(inner, prefer)
        elif number == 11:
            description = _translated(inner, prefer)
    return Alert(entity_id, tuple(routes), tuple(stops), tuple(periods), header, description, effect, cause)


def parse(data: bytes, *, prefer: str = "") -> Feed:
    """A FeedMessage: 1 header (3 timestamp), 2 entity (1 id, 2 is_deleted, 3 trip_update, 5 alert)."""
    try:
        top = fields(data)
        feed = Feed()
        entities = 0
        for number, value in top:
            if number == 1:
                for n, v in _sub(value):
                    if n == 3:
                        feed.timestamp = _int(v)
            elif number == 2:
                entities += 1
                if entities > MAX_ENTITIES:
                    break
                entity_id, deleted = "", False
                parts = _sub(value)
                for n, v in parts:
                    if n == 1:
                        entity_id = _text(v, 80)
                    elif n == 2:
                        deleted = v == 1
                if deleted:
                    continue
                for n, v in parts:
                    if n == 3:
                        feed.trips.append(_trip_update(v))
                    elif n == 5:
                        feed.alerts.append(_alert(entity_id, v, prefer))
        return feed
    except (FeedError, struct.error, IndexError) as err:
        raise FeedError(str(err) or "malformed feed") from None


# --- writing (the tests' recorded fixtures and fakes) -------------------------------------------------------


def _put_varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def put(number: int, value: int | str | bytes) -> bytes:
    """One field in the wire format: an int as a varint, text or bytes length-delimited."""
    if isinstance(value, int):
        return _put_varint(number << 3) + _put_varint(value)
    raw = value.encode() if isinstance(value, str) else value
    return _put_varint(number << 3 | 2) + _put_varint(len(raw)) + raw
