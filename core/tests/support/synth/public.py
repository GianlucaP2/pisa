# ruff: noqa: E501
"""The public-data fixtures (`tests/data/public`), synthetic and seeded: small files in each source's own format
(GTFS-Realtime protobuf, GTFS static zips, the MTA's alerts JSON, the NWS's GeoJSON, the Protezione Civile's
bulletin zip with its dBase tables, the Lazio pharmacy registry's CSV), with invented trips, alerts, texts,
pharmacies and people. Only public geography is real: the hub stations' names and rough coordinates, the alert
zones' codes and names, and the route ids Pisa's own tables know ("64", "360", "MEA", "A").

What the tests rely on, by construction: at Termini the next departures include several lines, two of a line and
direction at most, a trip that ends at Termini and ones outside the look-ahead; only the 360 reaches the stop
near `HOME`; one Metro A alert is active now; at Penn Station the A, C and E stop; two "Delays" and four planned
works in New York (the A's reroute active at 1795870000); a Moderate and a Minor NWS alert; a green bulletin
and a yellow one (Lazi-D, temporali, today); 4 of 11 pharmacies usable.

    uv run python -m tests.support.synth.public   # rewrites the files; the same seed gives the same bytes
"""

import csv
import io
import json
import math
import random
import zipfile
from pathlib import Path

from pisa.domain.gtfs_rt import put

SEED = 3
OUT = Path(__file__).resolve().parents[2] / "data" / "public"
ROME_NOW = 1793706988  # 3 Nov 2026 11:56:28 UTC (12:56 in Rome)
NYC_NOW = 1793707460
NYC_SENT = 1793705285  # the alerts feed's own time
TERMINI = (41.864, 12.527)
HOME = (41.855, 12.559)  # an invented home, about 3 km from Termini, on the 360's way
PENN = (40.826, -73.961)
ZIP_TIME = (2026, 11, 3, 12, 0, 0)  # every zip entry's date (the bytes stay the same run to run)

_STREETS = (
    "VIA FITTIZIA ALFA",
    "VIA FITTIZIA BETA",
    "PIAZZA FITTIZIA GAMMA",
    "LARGO FITTIZIO DELTA",
    "VIALE FITTIZIO EPSILON",
    "VIA FITTIZIA ZETA",
    "PIAZZALE FITTIZIO ETA",
    "VIA FITTIZIA THETA",
    "VIA FITTIZIA IOTA",
    "VIA FITTIZIA KAPPA",
    "VIA FITTIZIA LAMBDA",
    "LARGO FITTIZIO MI",
    "VIA FITTIZIA NI",
    "VIA FITTIZIA XI",
    "VIA FITTIZIA OMICRON",
    "VIA FITTIZIA PI",
    "VIA FITTIZIA RHO",
    "VIA FITTIZIA SIGMA",
    "VIA FITTIZIA TAU",
    "VIA FITTIZIA YPSILON",
    "VIA FITTIZIA PHI",
    "VIA FITTIZIA CHI",
    "VIA FITTIZIA PSI",
    "VIA FITTIZIA OMEGA",
    "VIA FITTIZIA ALFA SECONDA",
    "VIA FITTIZIA BETA SECONDA",
    "VIA FITTIZIA GAMMA SECONDA",
    "VIA FITTIZIA DELTA SECONDA",
    "VIA FITTIZIA EPSILON SECONDA",
    "VIA FITTIZIA ZETA SECONDA",
    "VIA FITTIZIA ETA SECONDA",
    "VIA FITTIZIA THETA SECONDA",
    "VIA FITTIZIA IOTA SECONDA",
    "VIA FITTIZIA KAPPA SECONDA",
    "VIA FITTIZIA LAMBDA SECONDA",
    "VIA FITTIZIA MI SECONDA",
    "VIA FITTIZIA NI SECONDA",
    "VIA FITTIZIA XI SECONDA",
    "VIA FITTIZIA OMICRON SECONDA",
    "VIA FITTIZIA PI SECONDA",
    "VIA FITTIZIA RHO SECONDA",
    "VIA FITTIZIA SIGMA SECONDA",
    "VIA FITTIZIA TAU SECONDA",
)


def _move(at: tuple[float, float], metres: float, bearing: float) -> tuple[float, float]:
    rad = math.radians(bearing)
    return (
        round(at[0] + metres * math.cos(rad) / 111_000, 3),
        round(at[1] + metres * math.sin(rad) / (111_000 * math.cos(math.radians(at[0]))), 3),
    )


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (a[0] - b[0]) * 111_000
    dx = (a[1] - b[1]) * 111_000 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(zipfile.ZipInfo(name, ZIP_TIME), data, zipfile.ZIP_DEFLATED)
    return out.getvalue()


# --- GTFS-Realtime ------------------------------------------------------------------------------------------


def _stop_time(sequence: int | None, stop_id: str, arrival: int | None, departure: int | None) -> bytes:
    body = put(1, sequence) if sequence is not None else b""
    if arrival is not None:
        body += put(2, put(2, arrival))
    if departure is not None:
        body += put(3, put(2, departure))
    return body + put(4, stop_id) + put(5, 0)


def _trip_entity(
    entity_id: str,
    trip_id: str,
    route: str,
    direction: int | None,
    stops: list[tuple[int | None, str, int | None, int | None]],
) -> bytes:
    descriptor = put(1, trip_id) + put(5, route) + (put(6, direction) if direction is not None else b"")
    update = put(1, descriptor + put(4, 0)) + b"".join(put(2, _stop_time(*s)) for s in stops)
    return put(2, put(1, entity_id) + put(3, update))


def _feed(timestamp: int, entities: list[bytes]) -> bytes:
    return put(1, put(1, "2.0") + put(2, 0) + put(3, timestamp)) + b"".join(entities)


def _translated(*texts: tuple[str, str]) -> bytes:
    return b"".join(put(1, put(1, text) + put(2, lang)) for text, lang in texts)


def _alert_entity(
    entity_id: str,
    routes: list[str],
    stops: list[str],
    period: tuple[int, int],
    cause: int | None,
    effect: int,
    header: list[tuple[str, str]],
    description: list[tuple[str, str]],
) -> bytes:
    body = put(1, put(1, period[0]) + put(2, period[1]))
    body += b"".join(put(5, put(1, "agenzia-esempio") + put(2, r)) for r in routes)
    body += b"".join(put(5, put(1, "agenzia-esempio") + put(5, s)) for s in stops)
    body += (put(6, cause) if cause is not None else b"") + put(7, effect)
    body += put(10, _translated(*header))
    if description:
        body += put(11, _translated(*description))
    return put(2, put(1, entity_id) + put(5, body))


# --- Rome ---------------------------------------------------------------------------------------------------

Stop = tuple[str, str, float, float, str]  # id, name, lat, lon, parent


def _rome(rng: random.Random) -> tuple[list[Stop], bytes, bytes]:
    stops: list[Stop] = []
    names = list(_STREETS)
    rng.shuffle(names)
    hub = ("90000", "TERMINI (MA-MB-FS)", 41.865, 12.528, "")
    platforms = [
        ("90001", "TERMINI (FERMATA A)", *_move(TERMINI, 90, 76), ""),
        ("90002", "TERMINI (FERMATA B)", *_move(TERMINI, 140, 256), ""),
        ("90003", "TERMINI (FERMATA C)", *_move(TERMINI, 120, 346), ""),
    ]
    stops += [hub, *platforms]
    serial = iter(range(91001, 99999))

    def street(at: tuple[float, float]) -> Stop:
        return (str(next(serial)), names.pop() if names else f"FERMATA {len(stops)}", at[0], at[1], "")

    def line(start: tuple[float, float], bearing: float, count: int, step: float = 420) -> list[Stop]:
        out: list[Stop] = []
        at = start
        for _ in range(count):
            at = _move(at, step, bearing + rng.uniform(-12, 12))
            stop = street(at)
            out.append(stop)
        stops.extend(out)
        return out

    lines: dict[str, list[Stop]] = {}
    # the 360: from the south-east, through Termini (A), then north-east past home
    before = line(_move(TERMINI, 2200, 191), 11, 3)
    after = line(_move(TERMINI, 150, 106), 106, 6, 400)
    home = ("91999", "VIA FITTIZIA OMEGA TERZA", HOME[0], HOME[1], "")
    after.insert(5, home)
    stops.append(home)
    terminus = ("91998", "CASALE FITTIZIO", *_move(HOME, 700, 116), "")
    stops.append(terminus)
    lines["360"] = [*before, platforms[0], *after, terminus]
    lines["64"] = [
        *line(_move(TERMINI, 1800, 136), 316, 2),
        platforms[1],
        *line(_move(TERMINI, 150, 311), 311, 7),
    ]
    lines["40"] = [platforms[2], *line(_move(TERMINI, 150, 331), 331, 6)]
    lines["H"] = [platforms[0], *line(_move(TERMINI, 150, 271), 271, 7)]
    lines["910"] = [
        *line(_move(TERMINI, 1500, 236), 56, 2),
        platforms[1],
        *line(_move(TERMINI, 150, 41), 41, 6),
    ]
    lines["170"] = [platforms[2], *line(_move(TERMINI, 150, 286), 286, 6)]
    lines["70"] = [*line(_move(TERMINI, 2500, 26), 206, 4), platforms[2]]  # ends at Termini
    lines["105"] = [*line(_move(TERMINI, 2600, 156), 336, 4), platforms[1]]  # ends at Termini
    for route, bearing in (("16", 176), ("71", 216), ("92", 76), ("492", 356), ("310", 126), ("75", 296)):
        lines[route] = line(_move(TERMINI, 1600, bearing), bearing + 90, 6)
    for stop in stops:
        if stop[0] not in {s[0] for s in platforms} | {hub[0]}:
            assert _distance((stop[2], stop[3]), TERMINI) > 470, stop
        if stop not in (home,) and stop[0] not in {s[0] for s in lines["360"]}:
            assert _distance((stop[2], stop[3]), HOME) > 650, stop

    # trips: (route, offsets in seconds of the Termini stop or of the first stop from now)
    plan: list[tuple[str, list[int]]] = [
        ("360", [-420, 300, 992, 2400, 6100]),
        ("64", [180, 840, 1500, 2700]),
        ("40", [60, 1260, 3300]),
        ("H", [420, 1800]),
        ("910", [600, 2100, 7000]),
        ("170", [-900, 960, 3000]),
        ("70", [240, 1320]),
        ("105", [500, 1900]),
        ("16", [100, 1300]),
        ("71", [700]),
        ("92", [200, 1600]),
        ("492", [900]),
        ("310", [350]),
        ("75", [1100]),
    ]
    entities: list[bytes] = []
    n = 0
    for route, offsets in plan:
        path = lines[route]
        anchor = next((i for i, s in enumerate(path) if s[0] in {p[0] for p in platforms}), 0)
        for offset in offsets:
            n += 1
            at_anchor = ROME_NOW + offset
            first = rng.randint(max(0, anchor - 2), anchor) if route not in ("70", "105") else 0
            rows: list[tuple[int | None, str, int | None, int | None]] = []
            for i in range(first, len(path)):
                when = at_anchor + (i - anchor) * rng.randint(80, 140)
                if i == first and rng.random() < 0.5:
                    rows.append((i + 1, path[i][0], None, when))
                else:
                    rows.append((i + 1, path[i][0], when, None))
            trip_id = f"{n}#{route}-{rng.randint(100, 999)}"
            entities.append(_trip_entity(f"e{n}", trip_id, route, 0 if offset % 2 == 0 else 1, rows))
    trips = _feed(ROME_NOW, entities)

    month = 30 * 86400
    alerts = _feed(
        ROME_NOW - 87,
        [
            _alert_entity(
                "40101",
                ["16"],
                [],
                (ROME_NOW - 3 * month, ROME_NOW + 6 * month),
                10,
                4,
                [("Deviazione temporanea della linea 16", "it")],
                [
                    (
                        "Per lavori sulla rete idrica la linea 16 non transita in via Fittizia Theta: fermate provvisorie in via Fittizia Zeta.",
                        "it",
                    )
                ],
            ),
            _alert_entity(
                "40102",
                ["71"],
                [],
                (ROME_NOW - 2 * month, ROME_NOW + 2 * month),
                10,
                4,
                [
                    ("Linea 71: percorso modificato per un cantiere", "it"),
                    ("Line 71: detour for road works", "en"),
                ],
                [("Fino a fine lavori la linea 71 è deviata in viale Fittizio Epsilon.", "it")],
            ),
            _alert_entity(
                "40103",
                ["92"],
                ["91003"],
                (ROME_NOW - month, ROME_NOW + 3 * month),
                10,
                6,
                [("Fermata spostata in via Fittizia Beta", "it")],
                [("La fermata è spostata di 50 metri per un transennamento.", "it")],
            ),
            _alert_entity(
                "40104",
                ["310"],
                [],
                (ROME_NOW - month, ROME_NOW + month),
                10,
                4,
                [("Chiusura temporanea di via Fittizia Lambda", "it")],
                [("La linea 310 è deviata nelle due direzioni.", "it")],
            ),
            _alert_entity(
                "40105",
                ["492"],
                [],
                (ROME_NOW - 4 * month, ROME_NOW + 5 * month),
                10,
                4,
                [("Lavori per la nuova tranvia: linea 492 deviata", "it")],
                [],
            ),
            _alert_entity(
                "40106",
                ["75"],
                [],
                (ROME_NOW + month, ROME_NOW + 2 * month),
                10,
                4,
                [("Dal prossimo mese la linea 75 cambia capolinea", "it")],
                [],
            ),
            _alert_entity(
                "synthetic-metro-a",
                ["MEA"],
                [],
                (1793700000, 1793800000),
                None,
                2,
                [("Metro A: rallentamenti tra Termini e Anagnina per un guasto tecnico", "it")],
                [],
            ),
        ],
    )
    return stops, trips, alerts


def _stops_txt(stops: list[Stop]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(
        [
            "stop_id",
            "stop_code",
            "stop_name",
            "stop_desc",
            "stop_lat",
            "stop_lon",
            "stop_url",
            "wheelchair_boarding",
            "stop_timezone",
            "location_type",
            "parent_station",
        ]
    )
    for sid, name, lat, lon, parent in sorted(stops):
        w.writerow([sid, sid, name, "", f"{lat:.3f}", f"{lon:.3f}", "", "", "", "", parent])
    return out.getvalue().encode()


# --- New York -----------------------------------------------------------------------------------------------

_NYC_STATIONS = (  # (id, name, lat, lon): the A/C/E along Eighth Avenue and its ends, and the 1/2/3 at Penn
    ("P02", "Inwood-207 St", 40.754, -73.825),
    ("P09", "168 St", 40.773, -73.857),
    ("P15", "125 St", 40.787, -73.894),
    ("P24", "59 St-Columbus Circle", 40.815, -73.944),
    ("P25", "50 St", 40.82, -73.951),
    ("P27", "42 St-Port Authority Bus Terminal", 40.823, -73.957),
    ("P28", "34 St-Penn Station", 40.826, -73.963),
    ("P30", "23 St", 40.831, -73.971),
    ("P31", "14 St", 40.834, -73.977),
    ("P32", "W 4 St-Wash Sq", 40.835, -73.988),
    ("P34", "Canal St", 40.84, -74.003),
    ("P36", "Chambers St", 40.843, -74.011),
    ("P55", "Euclid Av", 40.746, -74.081),
    ("P65", "Far Rockaway-Mott Av", 40.669, -74.191),
    ("Q01", "World Trade Center", 40.844, -74.012),
    ("Q05", "Jamaica Center-Parsons/Archer", 40.689, -74.057),
    ("127", "Times Sq-42 St", 40.821, -73.96),
    ("128", "34 St-Penn Station", 40.825, -73.966),
)
_NYC_ROUTES = {  # route → the stations it stops at, north to south
    "A": ["P02", "P15", "P24", "P27", "P28", "P31", "P32", "P34", "P36", "P65"],
    "C": ["P09", "P15", "P24", "P25", "P27", "P28", "P30", "P31", "P32", "P34", "P36", "P55"],
    "E": ["Q05", "P25", "P27", "P28", "P30", "P31", "P32", "P34", "Q01"],
}


def _nyc(rng: random.Random) -> tuple[bytes, bytes]:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"])
    for sid, name, lat, lon in _NYC_STATIONS:
        w.writerow([sid, name, f"{lat:.3f}", f"{lon:.3f}", "1", ""])
        for d in ("N", "S"):
            w.writerow([f"{sid}{d}", name, f"{lat:.3f}", f"{lon:.3f}", "", sid])
    static = _zip({"stops.txt": out.getvalue().encode()})

    entities: list[bytes] = []
    n = 0
    for route, starts in (
        ("A", [-200, 150, 700, 1500, 2600]),
        ("C", [90, 1100, 2400]),
        ("E", [-30, 600, 1800, 3900]),
    ):
        path = _NYC_ROUTES[route]
        penn = path.index("P28")
        for offset in starts:
            for direction in ("N", "S"):
                n += 1
                order = path if direction == "S" else list(reversed(path))
                at = order.index("P28")
                first = max(0, at - rng.randint(1, 3))
                rows: list[tuple[int | None, str, int | None, int | None]] = []
                for i in range(first, len(order)):
                    when = (
                        NYC_NOW + offset + (i - at) * rng.randint(90, 150) + (0 if direction == "N" else 75)
                    )
                    rows.append((None, f"{order[i]}{direction}", when, when))
                trip_id = f"{n:03d}{rng.randint(100, 999)}_{route}..{direction}{penn:02d}X"
                entities.append(_trip_entity(f"{n:06d}", trip_id, route, None, rows))
    trips = _feed(NYC_NOW, entities)
    return static, trips


def _mta_alert(
    alert_id: str, kind: str, routes: list[str], periods: list[tuple[int, int]], text: str
) -> dict[str, object]:
    return {
        "id": alert_id,
        "alert": {
            "active_period": [{"start": s, "end": e} for s, e in periods],
            "informed_entity": [
                {
                    "agency_id": "MTASBWY",
                    "route_id": r,
                    "transit_realtime.mercury_entity_selector": {"sort_order": f"MTASBWY:{r}:20"},
                }
                for r in routes
            ],
            "header_text": {
                "translation": [
                    {"text": text, "language": "en"},
                    {"text": f"<p>{text}</p>", "language": "en-html"},
                ]
            },
            "transit_realtime.mercury_alert": {
                "created_at": periods[0][0],
                "updated_at": periods[0][0] + 120,
                "alert_type": kind,
                "display_before_active": 0,
            },
        },
    }


def _nyc_alerts() -> bytes:
    body = {
        "header": {
            "gtfs_realtime_version": "2.0",
            "incrementality": "FULL_DATASET",
            "timestamp": NYC_SENT,
            "transit_realtime.mercury_feed_header": {"mercury_version": "1.0"},
        },
        "entity": [
            _mta_alert(
                "example:alert:1001",
                "Delays",
                ["7", "7X"],
                [(NYC_SENT - 860, NYC_SENT + 115)],
                "[7][7X] trains are running with delays while crews attend to a signal problem.",
            ),
            _mta_alert(
                "example:alert:1002",
                "Delays",
                ["J"],
                [(NYC_SENT - 1, NYC_SENT + 1015)],
                "[J] trains are running with delays after a train needed a door repair.",
            ),
            _mta_alert(
                "example:planned:2001",
                "Planned - Stops Skipped",
                ["7"] * 4,
                [(1795866300, 1795986000), (1796471100, 1796590800)],
                "Some [7] trains skip two stations for track work, nights.",
            ),
            _mta_alert(
                "example:planned:2002",
                "Planned - Reroute",
                ["C"] * 3,
                [(1795862700, 1795924800), (1795954500, 1796011200)],
                "[C] trains run via the [F] line between two stations, weekend.",
            ),
            _mta_alert(
                "example:planned:2003",
                "Planned - Reroute",
                ["A"] * 3,
                [(1795841100, 1796032800)],
                "[A] trains run local in both directions for signal work.",
            ),
            _mta_alert(
                "example:planned:2004",
                "Planned - Stops Skipped",
                ["4"] * 2,
                [(1794019500, 1794193200)],
                "Some [4] trains skip a station for platform work.",
            ),
        ],
    }
    return (json.dumps(body, indent=1, ensure_ascii=False) + "\n").encode()


def _nws() -> bytes:
    def feature(
        n: int, event: str, severity: str, area: str, sent: str, ends: str, headline: str
    ) -> dict[str, object]:
        oid = f"https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.example{n}.001.1"
        return {
            "id": oid,
            "type": "Feature",
            "geometry": None,
            "properties": {
                "@id": oid,
                "@type": "wx:Alert",
                "id": f"urn:oid:2.49.0.1.840.0.example{n}.001.1",
                "areaDesc": area,
                "geocode": {"SAME": ["099001"], "UGC": [f"EXZ00{n}"]},
                "affectedZones": [f"https://api.weather.gov/zones/forecast/EXZ00{n}"],
                "references": [],
                "sent": sent,
                "effective": sent,
                "onset": sent,
                "expires": ends,
                "ends": ends,
                "status": "Actual",
                "messageType": "Alert",
                "category": "Met",
                "severity": severity,
                "certainty": "Likely",
                "urgency": "Expected",
                "event": event,
                "sender": "alerts@weather.example.com",
                "senderName": "NWS Example Office",
                "headline": headline,
                "description": f"* WHAT...{event} for {area}.\n\n* WHEN...Until {ends}.",
                "instruction": "Take the usual care.",
                "response": "Execute",
                "note": None,
                "parameters": {"NWSheadline": [headline.upper()]},
                "scope": "Public",
                "code": "IPAWSv1.0",
                "language": "en-US",
                "web": "http://www.weather.gov/",
            },
        }

    body = {
        "type": "FeatureCollection",
        "features": [
            feature(
                2,
                "Special Weather Statement",
                "Minor",
                "Example Valley",
                "2026-11-03T05:10:00-05:00",
                "2026-11-03T09:00:00-05:00",
                "Special Weather Statement issued November 3 for Example Valley",
            ),
            feature(
                1,
                "Wind Advisory",
                "Moderate",
                "Example Coast",
                "2026-11-03T04:50:00-05:00",
                "2026-11-04T06:00:00-05:00",
                "Wind Advisory issued November 3 until November 4 for Example Coast",
            ),
        ],
        "title": "Current watches, warnings, and advisories",
    }
    return (json.dumps(body, indent=1) + "\n").encode()


# --- the Protezione Civile's bulletin -------------------------------------------------------------------------

_DBF_COLUMNS = (
    ("Zona_all", 80),
    ("Nome_zona", 146),
    ("Criticita", 80),
    ("Idrogeo", 80),
    ("Temporali", 80),
    ("Idraulico", 80),
)
_NONE = "Assenza di fenomeni significativi prevedibili / NESSUNA ALLERTA"
_ZONES = (
    ("Lazi-A", "Bacini Costieri Nord"),
    ("Lazi-B", "Bacino Medio Tevere"),
    ("Lazi-C", "Appennino di Rieti"),
    ("Lazi-D", "Bacini di Roma"),
    ("Lazi-E", "Aniene"),
    ("Lazi-F", "Bacini Costieri Sud"),
    ("Lazi-G", "Bacino del Liri"),
    ("Lomb-09", "Nodo Idraulico di Milano"),
    ("Pugl-D", "Salento"),
)


def _dbf(rows: list[tuple[str, ...]]) -> bytes:
    size = 1 + sum(w for _, w in _DBF_COLUMNS)
    header_len = 32 + 32 * len(_DBF_COLUMNS) + 1
    head = bytes([0x03, 126, 11, 2]) + len(rows).to_bytes(4, "little") + header_len.to_bytes(2, "little")
    head += size.to_bytes(2, "little") + bytes(20)
    for name, width in _DBF_COLUMNS:
        head += name.encode("ascii").ljust(11, b"\0") + b"C" + bytes(4) + bytes([width, 0]) + bytes(14)
    head += b"\r"
    body = b"".join(
        b" "
        + b"".join(
            value.encode("latin-1").ljust(width)[:width]
            for value, (_, width) in zip(row, _DBF_COLUMNS, strict=True)
        )
        for row in rows
    )
    return head + body + b"\x1a"


def _bulletin(yellow: bool) -> bytes:
    def table(day: str) -> bytes:
        rows: list[tuple[str, ...]] = []
        for zone, name in _ZONES:
            idro = temporali = idraulico = _NONE
            if zone == "Pugl-D" and day == "today":
                temporali = "Moderata per rischio temporali / ALLERTA ARANCIONE"
            if zone == "Pugl-D" and day == "tomorrow":
                temporali = "Ordinaria per rischio temporali / ALLERTA GIALLA"
            if yellow and zone == "Lazi-D" and day == "today":
                temporali = "Ordinaria per rischio temporali / ALLERTA GIALLA"
            worst = next((t for t in (temporali, idro, idraulico) if t != _NONE), _NONE)
            rows.append((zone, name, worst, idro, temporali, idraulico))
        return _dbf(rows)

    readme = (
        "Bollettino di esempio, sintetico: le tabelle delle zone di allerta di oggi e di domani, nel formato del "
        "bollettino nazionale (dBase III, una riga per zona, i livelli per rischio idrogeologico, temporali e "
        "idraulico). I livelli sono inventati per i test.\n"
    ).encode("latin-1")
    return _zip(
        {
            "20261102_1457_today.dbf": table("today"),
            "20261102_1457_tomorrow.dbf": table("tomorrow"),
            "README.txt": readme,
        }
    )


# --- the Lazio pharmacy registry ------------------------------------------------------------------------------

_PHARMACY_COLUMNS = (
    "CODICEIDENTIFICATIVOFARMACIA;CODFARMACIAASSEGNATODAASL;INDIRIZZO;DESCRIZIONEFARMACIA;PARTITAIVA;CAP;"
    "CODICECOMUNEISTAT;DESCRIZIONECOMUNE;FRAZIONE;CODICEPROVINCIAISTAT;SIGLAPROVINCIA;DESCRIZIONEPROVINCIA;"
    "CODICEREGIONE;DESCRIZIONEREGIONE;DATAINIZIOVALIDITA;DATAFINEVALIDITA;DESCRIZIONETIPOLOGIA;CODICETIPOLOGIA;"
    "LATITUDINE;LONGITUDINE;LOCALIZE"
)


def _pharmacies() -> bytes:
    def row(
        code: str,
        address: str,
        name: str,
        cap: str,
        lat: float,
        lon: float,
        *,
        ended: str = "-",
        istat: str = "58091",
        town: str = "ROMA",
    ) -> str:
        return ";".join(
            [
                code,
                "0",
                address,
                name,
                "-",
                cap,
                istat,
                town,
                "-",
                "58",
                "RM",
                "ROMA",
                "120",
                "LAZIO",
                "01/01/2005",
                ended,
                "Ordinaria",
                "1",
                f"{lat}",
                f"{lon}",
                "1",
            ]
        )

    near = [_move(TERMINI, m, b) for m, b in ((400, 306), (650, 76), (900, 196), (1300, 16))]
    lines = [
        _PHARMACY_COLUMNS,
        row("9081", "Via dell'Esempio, 11", "FARMACIA DELL'ESEMPIO SNC", "184", *near[0]),
        row("9082", "Via Fittizia Alfa, 40", "FARMACIA FITTIZIA ALFA", "185", *near[1]),
        row(
            "9083",
            "Piazza Fittizia Gamma, 2",
            "FARMACIA FITTIZIA GAMMA",
            "185",
            *_move(TERMINI, 300, 146),
            ended="31/12/2019",
        ),
        row("9084", "Via Fittizia Beta, 7", "FARMACIA FITTIZIA BETA SAS", "185", *near[2]),
        row("9085", "Viale Fittizio Epsilon, 120", "FARMACIA FITTIZIO EPSILON", "161", *near[3]),
        row(
            "9086",
            "Via Fittizia Zeta, 9",
            "FARMACIA FITTIZIA ZETA",
            "184",
            *_move(TERMINI, 500, 256),
            ended="28/02/2013",
        ),
        row(
            "9087",
            "Largo Fittizio Delta, 3",
            "FARMACIA FITTIZIO DELTA",
            "185",
            *_move(TERMINI, 700, 356),
            ended="30/09/2010",
        ),
        row(
            "9088",
            "Via Fittizia Theta, 51",
            "FARMACIA FITTIZIA THETA",
            "185",
            *_move(TERMINI, 800, 116),
            ended="31/01/2015",
        ),
        row(
            "9089",
            "Via Fittizia Kappa, 14",
            "FARMACIA FITTIZIA KAPPA",
            "187",
            *_move(TERMINI, 600, 226),
            ended="01/06/2021",
        ),
        row("9090", "Via Fittizia Iota, 30", "FARMACIA FITTIZIA IOTA", "185", 41.619, 12.019),  # 50 km off
        row(
            "9091",
            "Piazza Fittizia Copia, 1",
            "FARMACIA FITTIZIO MI",
            "00044",
            41.708,
            12.518,
            istat="58040",
            town="FRASCATI",
        ),
    ]
    return ("\r\n".join(lines) + "\r\n").encode("latin-1")


def build(out: Path = OUT, seed: int = SEED) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    rome_stops, rome_trips, rome_alerts = _rome(rng)
    agency = b"agency_id,agency_name,agency_url,agency_timezone\nagenzia-esempio,Agenzia Esempio,https://transit.example.com,Europe/Rome\n"
    nyc_static, nyc_trips = _nyc(rng)
    files = {
        "rome_trips.pb": rome_trips,
        "rome_alerts.pb": rome_alerts,
        "rome_gtfs.zip": _zip({"stops.txt": _stops_txt(rome_stops), "agency.txt": agency}),
        "nyc_gtfs.zip": nyc_static,
        "nyc_trips_ace.pb": nyc_trips,
        "nyc_alerts.json": _nyc_alerts(),
        "nws_alerts.json": _nws(),
        "dpc_latest_all.zip": _bulletin(yellow=False),
        "dpc_latest_all_yellow.zip": _bulletin(yellow=True),
        "lazio_pharmacies.csv": _pharmacies(),
    }
    for name, data in files.items():
        (out / name).write_bytes(data)


if __name__ == "__main__":
    build()
