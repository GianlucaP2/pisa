"""Contract 2.3's `pisa.maps` (the feature `maps.phone`): Pisa asks the phone's own maps (no server key)
for places (`pisa.maps.search`) or a travel time (`pisa.maps.eta`), and the phone answers `pisa.maps.result`
for the same `requestId`.

Here: the two requests as the schema allows them, and the phone's answer checked against `$defs/MapsPlace`,
`$defs/MapsEta` and `c_pisa_maps_result`. The answer is untrusted input: closed objects (no other property),
bounded text without control or bidirectional characters, numbers in range, at most `PLACES_MAX` places. An
answer outside those bounds is refused whole (`Refused`, the transport's `invalid_frame`); its messages never
quote a value (a name, an address or a coordinate). Pure: no I/O.
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

EXTENSION = "pisa.maps"
SEARCH = "pisa.maps.search"
ETA = "pisa.maps.eta"
RESULT = "pisa.maps.result"
WAIT_S = 4.0  # the phone answers at once: past this the request is over and the next provider is asked
PLACES_MAX = 10  # places in one answer (the schema's), and asked at most
ETA_MAX = (
    3  # destinations asked of the phone in one travel request (one `pisa.maps.eta` each); more: next one
)
QUERY_MAX = 200
KINDS = ("restaurant", "cafe", "pharmacy", "fuel", "supermarket", "atm", "parking", "other")
MODES = ("walk", "transit", "drive", "bike")
REASONS = ("denied", "unavailable", "no_results", "timeout", "unsupported")
SECONDS_MAX = 604_800
METRES_MAX = 40_000_000
COORD_DECIMALS = 5  # ~1 m: a point asked of the phone (it is the user's own phone)

_ID = re.compile(r"[A-Za-z0-9._:~-]{1,200}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_PHONE = re.compile(r"\+[1-9][0-9]{5,14}")
_URL = re.compile(r"https://\S+")
_RESULT_KEYS = frozenset({"requestId", "ok", "places", "eta", "reason"})
_PLACE_KEYS = frozenset(
    {"id", "name", "lat", "lon", "address", "kind", "distanceMeters", "openNow", "phone", "url"}
)
_ETA_KEYS = frozenset({"seconds", "distanceMeters", "mode", "departAt"})


class Refused(ValueError):
    """A `pisa.maps.result` outside contract 2.3's bounds: refused whole (`invalid_frame`). The message names
    the field, never its value."""


@dataclass(frozen=True)
class MapsPlace:
    id: str  # the phone's maps service's own id: opaque, only ever handed back to the same phone
    name: str
    lat: float
    lon: float
    address: str | None = None
    kind: str | None = None
    distance_m: int | None = None
    open_now: bool | None = None
    phone: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class MapsEta:
    seconds: int
    distance_m: int
    mode: str
    depart_at: str | None = None


@dataclass(frozen=True)
class MapsResult:
    request_id: str  # lower case
    ok: bool
    places: tuple[MapsPlace, ...] = ()
    eta: MapsEta | None = None
    reason: str | None = None


# --- what Pisa asks ---------------------------------------------------------------------------------------


def query_text(query: str) -> str | None:
    """The query as the phone may get it: no control or bidirectional character, spaces collapsed, at most
    QUERY_MAX characters; None when nothing is left."""
    text = " ".join(_BAD.sub(" ", query).split())[:QUERY_MAX].strip()
    return text or None


def point(coord: tuple[float, float]) -> dict[str, float]:
    return {"lat": round(coord[0], COORD_DECIMALS), "lon": round(coord[1], COORD_DECIMALS)}


def search_value(
    request_id: str, query: str, near: tuple[float, float] | None, limit: int
) -> dict[str, Any] | None:
    """`pisa.maps.search`'s value (`s_pisa_maps_search`); None for a query with nothing left to ask."""
    text = query_text(query)
    if text is None:
        return None
    value: dict[str, Any] = {"requestId": request_id, "query": text, "limit": max(1, min(limit, PLACES_MAX))}
    if near is not None:
        value["near"] = point(near)
    return value


def eta_value(
    request_id: str, origin: tuple[float, float], destination: tuple[float, float], mode: str
) -> dict[str, Any]:
    """`pisa.maps.eta`'s value (`s_pisa_maps_eta`), between two points."""
    return {"requestId": request_id, "from": point(origin), "to": point(destination), "mode": mode}


# --- what the phone answers -------------------------------------------------------------------------------


def _number(value: Any, low: float, high: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Refused(f"{what} is a number.")
    if not low <= value <= high:
        raise Refused(f"{what} is out of range.")
    return float(value)


def _integer(value: Any, high: int, what: str) -> int:
    number = _number(value, 0, high, what)
    if not number.is_integer():
        raise Refused(f"{what} is a whole number.")
    return int(number)


def _text(value: Any, high: int, what: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= high or _BAD.search(value):
        raise Refused(f"{what} is 1 to {high} characters, without control or bidirectional characters.")
    return value


def _matching(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):  # fullmatch: no trailing newline either
        raise Refused(f"{what} is not in its form.")
    return value


def _closed(value: Any, keys: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Refused(f"{what} is an object.")
    if set(value) - keys:
        raise Refused(f"{what} has a property the contract doesn't allow.")
    return value


def _place(value: Any) -> MapsPlace:
    body = _closed(value, _PLACE_KEYS, "A place")
    for key in ("id", "name", "lat", "lon"):
        if key not in body:
            raise Refused(f"A place needs its {key}.")
    kind = body.get("kind")
    if kind is not None and kind not in KINDS:
        raise Refused(f"A place's kind is one of {', '.join(KINDS)}.")
    open_now = body.get("openNow")
    if open_now is not None and not isinstance(open_now, bool):
        raise Refused("A place's openNow is true or false.")
    url = body.get("url")
    if url is not None and (not isinstance(url, str) or len(url) > 500):
        raise Refused("A place's url is https:// and at most 500 characters.")
    return MapsPlace(
        id=_matching(body["id"], _ID, "A place's id"),
        name=_text(body["name"], 120, "A place's name"),
        lat=_number(body["lat"], -90, 90, "A place's lat"),
        lon=_number(body["lon"], -180, 180, "A place's lon"),
        address=_text(body["address"], 200, "A place's address") if "address" in body else None,
        kind=kind,
        distance_m=_integer(body["distanceMeters"], METRES_MAX, "A place's distanceMeters")
        if "distanceMeters" in body
        else None,
        open_now=open_now,
        phone=_matching(body["phone"], _PHONE, "A place's phone") if "phone" in body else None,
        url=_matching(url, _URL, "A place's url") if url is not None else None,
    )


def _eta(value: Any) -> MapsEta:
    body = _closed(value, _ETA_KEYS, "The eta")
    for key in ("seconds", "distanceMeters", "mode"):
        if key not in body:
            raise Refused(f"The eta needs its {key}.")
    if body["mode"] not in MODES:
        raise Refused(f"The eta's mode is one of {', '.join(MODES)}.")
    depart = body.get("departAt")
    if depart is not None:
        try:
            if not isinstance(depart, str) or len(depart) > 40 or "T" not in depart.upper():
                raise ValueError
            parsed = datetime.fromisoformat(depart.replace("Z", "+00:00").replace("z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        except ValueError:
            raise Refused("The eta's departAt is an RFC 3339 date-time.") from None
    return MapsEta(
        seconds=_integer(body["seconds"], SECONDS_MAX, "The eta's seconds"),
        distance_m=_integer(body["distanceMeters"], METRES_MAX, "The eta's distanceMeters"),
        mode=body["mode"],
        depart_at=depart,
    )


def parse_result(value: Any) -> MapsResult:
    """`pisa.maps.result`'s value, checked whole (`c_pisa_maps_result`): ok with `places` (1 to 10) or `eta`,
    never a reason; or not ok with a reason, never places or an eta. `Refused` otherwise."""
    body = _closed(value, _RESULT_KEYS, "pisa.maps.result")
    request_id = body.get("requestId")
    if not isinstance(request_id, str) or not _UUID.fullmatch(request_id):
        raise Refused("pisa.maps.result carries the request's requestId.")
    ok = body.get("ok")
    if not isinstance(ok, bool):
        raise Refused("pisa.maps.result carries ok, true or false.")
    has_places, has_eta, reason = "places" in body, "eta" in body, body.get("reason")
    if ok:
        if has_places == has_eta or "reason" in body:
            raise Refused("An ok pisa.maps.result has places or an eta, and no reason.")
        if has_eta:
            return MapsResult(request_id.lower(), True, eta=_eta(body["eta"]))
        places = body["places"]
        if not isinstance(places, list) or not 1 <= len(places) <= PLACES_MAX:
            raise Refused(f"pisa.maps.result's places are 1 to {PLACES_MAX}.")
        return MapsResult(request_id.lower(), True, places=tuple(_place(p) for p in places))
    if has_places or has_eta or reason not in REASONS:
        raise Refused(
            f"A pisa.maps.result that isn't ok has a reason ({', '.join(REASONS)}) and nothing else."
        )
    return MapsResult(request_id.lower(), False, reason=reason)
