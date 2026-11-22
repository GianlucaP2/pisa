"""No coordinate in the exported tree has more than 3 decimal places (about 100 m).

A precise coordinate in a fixture can be a real person's home copied from real life, so every exported text
file is scanned: the paths the public export copies, minus the frozen `protocol/v2/` (only a new contract
version changes it) and binary files (a zip's text members are scanned too: the GTFS fixtures). A
value is coordinate-looking when it is

- after a latitude/longitude key (`lat`, `lng`, `lon`, `long`, `latitude`, `longitude`, the map regions'
  `southLatitude` …, `x`, `y`) in JSON, Python, keyword arguments, query strings (`latitude%5D=`, `lat=`);
- one of a pair (a tuple, a CSV row, a `lat,lon` string, `%2C`, WKT's space) within the ranges of a
  latitude and a longitude, when one of the two is 1 or more in size (two tiny ones are money, not a place);
- any other number from 1 to 180 in size (a place far from 0,0 is caught alone too, on separate lines).

Decimals are counted without trailing zeros: 45.480000 (a formatted 45.48) has 2. A small offset added to a
coordinate in code (`shop[0] + 0.00027`) is not a place. The allowlist below names the few numbers that look
like coordinates and are not, each with its reason. This file itself is not scanned (its examples).
"""

import io
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

# the export's allow-list (these and the root's files), minus the frozen protocol/v2/
EXPORTED = (
    "core", "protocol", "relay", "scripts/ci", "scripts/smoke", "deploy", "docs-site",
    ".github/workflows/pisa-ci.yml",
)  # fmt: skip
FROZEN = ("protocol/v2/",)
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}

# (file, number): numbers that are not coordinates
ALLOWED = {
    ("core/pisa/api/schemas.py", "1.1000000000000001"): "a JSON double's rounding error, in a comment",
    ("core/tests/test_ai_api.py", "1.1000000000000001"): "the same double, a euro cap the API must round",
    ("core/tests/test_fx_catalog.py", "1.1625"): "the ECB's EUR/USD rate in a fake feed",
    ("core/tests/test_ledger.py", "19.5001"): "a monthly euro cap",
    ("protocol/README.md", "10.48550"): "arXiv's DOI prefix",
}

NUMBER = r"-?\d{1,3}\.\d+"
KEY = re.compile(
    r"(?:(?<=%5B)|(?<![A-Za-z]))(?i:lat|lng|lon|long|latitude|longitude|southLatitude|northLatitude"
    rf"|westLongitude|eastLongitude|x|y)(?:%5D)?[\"']?\s*[:=]\s*[\"']?({NUMBER})(?![\d.])"
)
PAIR = re.compile(rf"(?<![\w.:/-])({NUMBER})(?:\s*(?:,|;|%2C)\s*|\s+)({NUMBER})(?![\w./])")
BARE = re.compile(rf"(?<![\w.:/-])({NUMBER})(?![\w./])")


def _precise(token: str) -> bool:
    return len(token.split(".")[1].rstrip("0")) > 3


def precise_coordinates(text: str) -> list[tuple[int, str]]:
    """(line, value) of each coordinate-looking value with more than 3 decimals."""
    spans: dict[int, str] = {}
    for m in KEY.finditer(text):
        if _precise(m.group(1)):
            spans[m.start(1)] = m.group(1)
    for m in PAIR.finditer(text):
        a, b = float(m.group(1)), float(m.group(2))
        if abs(a) <= 90 and abs(b) <= 180 and max(abs(a), abs(b)) >= 1:
            for i in (1, 2):
                if _precise(m.group(i)):
                    spans[m.start(i)] = m.group(i)
    for m in BARE.finditer(text):
        if 1 <= abs(float(m.group(1))) <= 180 and _precise(m.group(1)):
            spans[m.start(1)] = m.group(1)
    return [(text.count("\n", 0, at) + 1, value) for at, value in sorted(spans.items())]


def _text(raw: bytes) -> str | None:
    """The UTF-8 text, or None for a binary file."""
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def exported_texts() -> list[tuple[str, str]]:
    """(path, text) of every exported text file and of each text member of a zip (`a.zip!b.txt`)."""
    paths = [p for p in ROOT.iterdir() if p.is_file()]
    for name in EXPORTED:
        top = ROOT / name
        if top.is_file():
            paths.append(top)
        elif top.is_dir():
            paths += [p for p in top.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.parts)]
    out: list[tuple[str, str]] = []
    for path in sorted(set(paths)):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(FROZEN):
            continue
        raw = path.read_bytes()
        if path.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for member in archive.namelist():
                    if (text := _text(archive.read(member))) is not None:
                        out.append((f"{rel}!{member}", text))
        elif (text := _text(raw)) is not None:
            out.append((rel, text))
    return out


def test_no_exported_file_has_a_coordinate_with_more_than_3_decimals() -> None:
    found = [
        f"{rel}:{line}"
        for rel, text in exported_texts()
        if rel != SELF
        for line, value in precise_coordinates(text)
        if (rel, value) not in ALLOWED
    ]
    assert not found, f"coordinates with more than 3 decimals (invent one with at most 3): {found}"


def test_the_scan_sees_the_exported_tree() -> None:
    paths = {rel for rel, _ in exported_texts()}
    assert {"README.md", "core/pyproject.toml", "protocol/openapi.json", "relay/pyproject.toml"} <= paths
    assert {"core/tests/conftest.py", "core/tests/data/public/nyc_gtfs.zip!stops.txt"} <= paths
    assert not any(p.startswith(("protocol/v2/", "apps/", "docs/")) for p in paths)


def test_the_scan_catches_every_shape() -> None:
    caught = [  # made-up numbers, no place in particular
        '{"lat": 10.12345, "lon": 20.54321}',  # JSON keys
        '"lat": 0.12345,\n  "lon": 0.4',  # a key with a value near 0
        "HOME = (10.12345, 20.54321)",  # a Python tuple
        "place(db, lat=10.12345, lon=20.5)",  # keyword arguments
        '"coordinate": {"latitude": 10.1234, "longitude": 20.5}',  # a provider's response
        "9081;ROMA;10.123456;20.54321;1",  # a CSV row
        '{"near": "10.1234,20.5432"}',  # a string
        "searchLocation=10.123400%2C20.500000",  # an encoded pair in a URL
        "pickup%5Blatitude%5D=10.123400",  # a ride link's parameter
        "https://maps.example/@10.1234,20.5432,17z",  # a map URL
        "POINT(20.5432 10.1234)",  # WKT
        "AIRPORT = (50.12, -0.1234)",  # a longitude near 0, paired
        "[\n    10.1234,\n    20.5,\n]",  # separate lines
        "lat_min = 10.1234",  # a lone value far from 0
    ]
    for text in caught:
        assert precise_coordinates(text), text
    passed = [
        '{"lat": 10.123, "lon": 20.5}',
        "searchLocation=10.120000%2C20.500000",  # a 3-decimal value, formatted
        "(TRATTORIA[0] + 0.0009, TRATTORIA[1])",  # an offset, not a place
        "VALUES (%s, 0.0195, 0.0195, true)",  # money
        'Decimal("0.000001")',
        "2026-10-21T09:12:34.567891+02:00",  # a timestamp's microseconds
        "doi:10.1145/3442188.3445922",
    ]
    for text in passed:
        assert not precise_coordinates(text), text
