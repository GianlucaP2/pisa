"""Weather alerts where the user is (the country of their latest location).

- **Italy**: the Protezione Civile's national bulletin of hydro-geological and hydraulic criticality
  (published daily around 16:00, today and tomorrow, 156+ alert zones; CC BY 4.0). Pisa reads its zone table
  (the bulletin's own DBF, in `latest_all.zip`) and the zones of the city they are in, from a reviewed table
  built from the bulletin's own lists of municipalities per zone (a city spans several: Rome five). The colour
  Pisa says is the worst of their city's zones.
- **The US**: the National Weather Service's active alerts for their point (`api.weather.gov`, public domain).

Outside data: the levels and the texts are the agencies' words, data for the model. No I/O."""

import io
import zipfile
from dataclasses import dataclass
from typing import Any, Literal, cast

from pisa.domain.gtfs_rt import clean

Level = Literal["verde", "gialla", "arancione", "rossa"]
LEVELS: tuple[Level, ...] = ("verde", "gialla", "arancione", "rossa")
RISKS = {"Idrogeo": "idrogeologico", "Temporali": "temporali", "Idraulico": "idraulico"}
DBF_MAX = 4 * 1024 * 1024
RECORDS_MAX = 1000

# REVIEWED TABLE: the alert zones whose municipality lists hold the city, from the bulletin of
# 2 Nov 2026 14:57 (`files/geojson/20261102_1457_today.json`'s "Comuni", matched to the DBF's `Zona_all` in
# the same order). Re-check when the DPC redraws its zones.
CITY_ZONES: dict[str, tuple[str, ...]] = {
    "roma": ("Lazi-A", "Lazi-B", "Lazi-D", "Lazi-E", "Lazi-F"),
    "fiumicino": ("Lazi-A", "Lazi-D"),
    "milano": ("Lomb-09",), "bergamo": ("Lomb-10",), "napoli": ("Camp-1",), "salerno": ("Camp-3",),
    "firenze": ("Tosc-A3",), "pisa": ("Tosc-A6",), "venezia": ("Vene-F2",), "verona": ("Vene-C2",),
    "torino": ("Piem-L",), "bologna": ("Emil-C2",), "bari": ("Pugl-C",), "genova": ("Ligu-B",),
    "palermo": ("Sici-C",), "catania": ("Sici-H", "Sici-I"), "cagliari": ("Sard-A", "Sard-B"),
}  # fmt: skip


@dataclass(frozen=True)
class ZoneLevel:
    zone: str
    name: str
    levels: dict[str, Level]  # risk → level

    @property
    def worst(self) -> Level:
        return cast(Level, max(self.levels.values(), key=LEVELS.index, default="verde"))


@dataclass(frozen=True)
class Bulletin:
    stamp: str  # "20261102_1457": the bulletin's date and time
    today: list[ZoneLevel]
    tomorrow: list[ZoneLevel]


def level_of(text: str) -> Level:
    """ "Ordinaria / ALLERTA GIALLA" → gialla; "Assenza di fenomeni significativi… / NESSUNA ALLERTA" →
    verde."""
    upper = text.upper()
    for level in ("ROSSA", "ARANCIONE", "GIALLA"):
        if f"ALLERTA {level}" in upper:
            return cast(Level, level.lower())
    return "verde"


def read_dbf(data: bytes) -> list[dict[str, str]]:
    """A dBase III table (the bulletin's zone list): its rows as text. Raises ValueError when malformed."""
    if len(data) < 33 or len(data) > DBF_MAX:
        raise ValueError("not a zone table")
    count = int.from_bytes(data[4:8], "little")
    header = int.from_bytes(data[8:10], "little")
    size = int.from_bytes(data[10:12], "little")
    if count > RECORDS_MAX or header > len(data) or size == 0:
        raise ValueError("not a zone table")
    columns: list[tuple[str, int]] = []
    i = 32
    while i < header - 1 and data[i] != 0x0D:
        name = data[i : i + 11].split(b"\0", 1)[0].decode("ascii", errors="replace")
        columns.append((name, data[i + 16]))
        i += 32
    rows: list[dict[str, str]] = []
    for r in range(count):
        record = data[header + r * size : header + (r + 1) * size]
        if len(record) < size or record[:1] == b"*":  # cut short, or deleted
            continue
        row: dict[str, str] = {}
        at = 1
        for name, width in columns:
            row[name] = record[at : at + width].decode("latin-1").strip()[:200]
            at += width
        rows.append(row)
    return rows


def zones_of(rows: list[dict[str, str]]) -> list[ZoneLevel]:
    out: list[ZoneLevel] = []
    for row in rows:
        zone = row.get("Zona_all", "")
        if not zone:
            continue
        levels: dict[str, Level] = {risk: level_of(row.get(column, "")) for column, risk in RISKS.items()}
        out.append(ZoneLevel(zone[:20], row.get("Nome_zona", "")[:80], levels))
    return out


def bulletin(data: bytes) -> Bulletin:
    """The latest bulletin's zip (`latest_all.zip`): today's and tomorrow's zone tables."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        found: dict[str, list[ZoneLevel]] = {}
        stamp = ""
        for which in ("today", "tomorrow"):
            name = next((n for n in names if n.endswith(f"_{which}.dbf")), None)
            if name is None:
                raise ValueError(f"no {which} table in the bulletin")
            info = archive.getinfo(name)
            if info.file_size > DBF_MAX:
                raise ValueError("zone table too large")
            found[which] = zones_of(read_dbf(archive.read(name)))
            stamp = name.rsplit("/", 1)[-1].removesuffix(f"_{which}.dbf")
    return Bulletin(stamp[:20], found["today"], found["tomorrow"])


def for_city(zones: list[ZoneLevel], city: str) -> list[ZoneLevel]:
    wanted = CITY_ZONES.get(city.strip().casefold(), ())
    return [z for z in zones if z.zone in wanted]


def worst(zones: list[ZoneLevel]) -> Level:
    return cast(Level, max((z.worst for z in zones), key=LEVELS.index, default="verde"))


def city_lines(b: Bulletin, city: str) -> list[str]:
    """One line per day for their city: the worst colour and which risks (data for the model, English)."""
    out: list[str] = []
    for label, zones in (("today", for_city(b.today, city)), ("tomorrow", for_city(b.tomorrow, city))):
        if not zones:
            continue
        level = worst(zones)
        if level == "verde":
            out.append(f"{label}: no alert (verde) in {city.title()}'s zones")
            continue
        risks = sorted({r for z in zones for r, lv in z.levels.items() if lv == level})
        names = ", ".join(sorted({z.name for z in zones if z.worst == level}))
        out.append(f"{label}: allerta {level} for {', '.join(risks)} ({names})")
    return out


# --- the US -------------------------------------------------------------------------------------------------

SEVERITY = ("Unknown", "Minor", "Moderate", "Severe", "Extreme")


def nws_alerts(body: Any) -> list[dict[str, str]]:
    """The NWS's active alerts (GeoJSON): event, severity, headline, until (its `ends` or `expires`)."""
    out: list[dict[str, str]] = []
    for feature in (body or {}).get("features") or []:
        p = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(p, dict) or p.get("status") not in (None, "Actual"):
            continue
        out.append({
            "event": clean(str(p.get("event") or ""), 80),
            "severity": clean(str(p.get("severity") or "Unknown"), 20),
            "headline": clean(str(p.get("headline") or ""), 300),
            "until": clean(str(p.get("ends") or p.get("expires") or ""), 40),
            "area": clean(str(p.get("areaDesc") or ""), 120),
        })  # fmt: skip
    out.sort(key=lambda a: -SEVERITY.index(a["severity"]) if a["severity"] in SEVERITY else 0)
    return out[:8]


def worth_saying(alert: dict[str, str]) -> bool:
    """The briefing says Moderate and worse; "Minor" stays in the answer when they ask."""
    return alert.get("severity") in ("Moderate", "Severe", "Extreme")
