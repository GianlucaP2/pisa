"""skill 7: the nearest pharmacy in Rome, from the Regione Lazio's open registry (IODL 2.0). It has
names, addresses and coordinates, **no hours and no on-duty rota** (none is open data; the private rota sites
are never scraped): Pisa names the closest ones and never says one is open.

The registry is old and its coordinates are uneven (a Rome pharmacy placed 50 km south; "41..74"): a row is
kept only when it is active (no end date), in Rome (ISTAT 058091), and its point falls inside the city's box.
No I/O."""

import csv
import io
from dataclasses import dataclass

from pisa.domain.lexicon.numbers import comma_float_or_none
from pisa.domain.places import distance_m, map_links

ROME_ISTAT = {"58091", "058091"}
_ROME_BOX = (41.65, 12.23, 42.10, 12.86)
NEAR_MAX_M = 3000
ROWS_MAX = 20_000


@dataclass(frozen=True)
class Pharmacy:
    code: str
    name: str
    address: str
    cap: str
    lat: float
    lon: float


def parse(text: str) -> list[Pharmacy]:
    """The registry's CSV (`;`-separated, Latin-1): Rome's active pharmacies with a believable point."""
    out: list[Pharmacy] = []
    s, w, n, e = _ROME_BOX
    for i, row in enumerate(csv.DictReader(io.StringIO(text), delimiter=";")):
        if i >= ROWS_MAX:
            break
        if (row.get("CODICECOMUNEISTAT") or "").strip() not in ROME_ISTAT:
            continue
        if (row.get("DATAFINEVALIDITA") or "-").strip() not in ("", "-"):
            continue  # closed
        lat, lon = comma_float_or_none(row.get("LATITUDINE")), comma_float_or_none(row.get("LONGITUDINE"))
        if lat is None or lon is None or not (s <= lat <= n and w <= lon <= e):
            continue
        cap = (row.get("CAP") or "").strip()
        out.append(Pharmacy(
            code=(row.get("CODICEIDENTIFICATIVOFARMACIA") or "").strip()[:20],
            name=" ".join((row.get("DESCRIZIONEFARMACIA") or "Farmacia").split()).title()[:80],
            address=" ".join((row.get("INDIRIZZO") or "").split())[:120],
            cap=cap.zfill(5) if cap.isdigit() else cap[:5],
            lat=lat, lon=lon,
        ))  # fmt: skip
    return out


def nearest(pharmacies: list[Pharmacy], at: tuple[float, float], k: int = 3) -> list[tuple[int, Pharmacy]]:
    found = sorted(((distance_m(at, (p.lat, p.lon)), p) for p in pharmacies), key=lambda pair: pair[0])
    return [(d, p) for d, p in found if d <= NEAR_MAX_M][:k]


def line(distance: int, p: Pharmacy) -> str:
    return f"{p.name}, {p.address}, {p.cap} Roma: about {distance} m away (hours unknown)"


def link(p: Pharmacy) -> str:
    """Apple Maps for the address (the registry's point is a hint, the address is what they walk to)."""
    return map_links(f"Farmacia {p.address} {p.cap} Roma", p.lat, p.lon, None)["apple_maps"]
