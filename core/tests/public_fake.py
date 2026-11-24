"""The public sources, as synthetic files in their own formats (`tests/data/public`, its README):
an httpx handler that answers the public hosts from the fixtures, counts the calls per file, and can take a
host down."""

from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from pisa.domain import gtfs_rt
from pisa.domain.public_sources import hosts

DATA = Path(__file__).parent / "data" / "public"
ROME_NOW = 1793706988  # the Rome feeds' timestamp: 3 Nov 2026 11:56:28 UTC (12:56 in Rome)
NYC_NOW = 1793707460  # the MTA ace feed's: 11:04 in New York
TERMINI = (41.864, 12.527)
HOME = (41.855, 12.559)  # an invented home near a stop the 360 from Termini reaches (tests/support/synth)
PENN = (40.826, -73.961)
EMPTY_FEED = gtfs_rt.put(1, gtfs_rt.put(1, "2.0") + gtfs_rt.put(3, NYC_NOW))


class FakePublic:
    def __init__(self, dpc: str = "dpc_latest_all.zip") -> None:
        self.calls: list[str] = []
        self.requests: list[httpx.Request] = []
        self.down: set[str] = set()
        self.dpc = dpc

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in hosts()

    def file(self, request: httpx.Request) -> str | None:
        path = unquote(request.url.path)
        host = request.url.host
        if host == "romamobilita.it":
            files = {
                "rome_static_gtfs.zip": "rome_gtfs.zip",
                "rome_rtgtfs_trip_updates_feed.pb": "rome_trips.pb",
                "rome_rtgtfs_service_alerts_feed.pb": "rome_alerts.pb",
            }
            return files.get(path.rsplit("/", 1)[-1])
        if host == "rrgtfsfeeds.s3.amazonaws.com":
            return "nyc_gtfs.zip"
        if host == "api-endpoint.mta.info":
            if path.endswith("subway-alerts.json"):
                return "nyc_alerts.json"
            return "nyc_trips_ace.pb" if path.endswith("gtfs-ace") else "(empty feed)"
        if host == "raw.githubusercontent.com":
            return self.dpc
        if host == "api.weather.gov":
            return "nws_alerts.json"
        if host == "dati.lazio.it":
            return "lazio_pharmacies.csv"
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = self.file(request)
        if name is None:
            return httpx.Response(404, text="not here")
        self.calls.append(name)
        if request.url.host in self.down:
            return httpx.Response(503, text="down for maintenance")
        if name == "(empty feed)":
            return httpx.Response(200, content=EMPTY_FEED)
        return httpx.Response(200, content=(DATA / name).read_bytes())

    def count(self, name: str) -> int:
        return self.calls.count(name)


def handler(fake: FakePublic, other: Any = None) -> Any:
    """The tests' outbound HTTP: the public hosts to the fake, everything else to `other` (or refused)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if fake.owns(request):
            return fake(request)
        if other is not None:
            return other(request)
        raise httpx.ConnectError("network disabled in tests", request=request)

    return handle
