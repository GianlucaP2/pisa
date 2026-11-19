"""The public data Pisa reads, with each
source's licence, how long a copy stays fresh, and how often Pisa may ask again. Read-only, no key, no
personal data sent (a stop's or a point's coordinates at most, as the manifest says).

**Outside data**: what a source says is data for the model (the `public-data` block kind), never
instructions, and never a reason to act. A source that fails is skipped quietly: Pisa says it couldn't read
it, never guesses. No I/O."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

Licence = Literal["CC BY-SA 4.0", "CC BY 4.0", "IODL 2.0", "MTA terms (free, no key)", "US public domain",
                  "reviewed table"]  # fmt: skip
KIND = "public-data"  # the data block kind of everything read here (outside text)
USER_AGENT = "J-personal-assistant/1.0 (read-only public data; one person's assistant)"
STATIC_MAX = 80 * 1024 * 1024  # Rome's static GTFS is ~47 MB: a static dataset's cap
FEED_MAX = 8 * 1024 * 1024  # realtime feeds, alerts, bulletins, registries (their own cap)
FAIL_BACKOFF = timedelta(minutes=5)  # after a failure Pisa doesn't ask the same source again for this long


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    url: str
    licence: Licence
    attribution: str
    fresh: timedelta  # a copy younger than this is used as is (the rate limit: at most one call per window)
    stale: timedelta  # an older copy is still used when the source fails, up to this age (said as old)
    official: bool = True
    cap: int = FEED_MAX  # the most Pisa reads of one body
    # the country whose user Pisa reads it for (PISA_COUNTRY): an Italian source is off anywhere else
    country: str | None = None


_RSM = "https://romamobilita.it/wp-content/uploads/shared/"
_MTA = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"
_DPC = "https://raw.githubusercontent.com/pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica/master/"

SOURCES: dict[str, Source] = {
    s.key: s
    for s in (
        Source("rome_static", "RSM, GTFS", _RSM + "rome_static_gtfs.zip",
               "CC BY-SA 4.0", "Dati: Roma Servizi per la Mobilità (CC BY-SA)", timedelta(days=7),
               timedelta(days=30), cap=STATIC_MAX, country="IT"),
        Source("rome_trips", "RSM, GTFS-RT", _RSM + "rome_rtgtfs_trip_updates_feed.pb",
               "CC BY-SA 4.0", "Dati: Roma Servizi per la Mobilità (CC BY-SA)", timedelta(seconds=30),
               timedelta(minutes=10), country="IT"),
        Source("rome_alerts", "RSM, avvisi", _RSM + "rome_rtgtfs_service_alerts_feed.pb",
               "CC BY-SA 4.0", "Dati: Roma Servizi per la Mobilità (CC BY-SA)", timedelta(minutes=2),
               timedelta(hours=1), country="IT"),
        Source("nyc_static", "MTA subway GTFS", "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
               "MTA terms (free, no key)", "Data: MTA", timedelta(days=7), timedelta(days=30),
               cap=STATIC_MAX),
        Source("nyc_alerts", "MTA subway service alerts", _MTA + "camsys%2Fsubway-alerts.json",
               "MTA terms (free, no key)", "Data: MTA", timedelta(minutes=2), timedelta(hours=1)),
        *(
            Source(f"nyc_trips_{group}", f"MTA subway GTFS-RT ({group})", _MTA + f"nyct%2Fgtfs{suffix}",
                   "MTA terms (free, no key)", "Data: MTA", timedelta(seconds=30), timedelta(minutes=10))
            for group, suffix in (("1234567", ""), ("ace", "-ace"), ("bdfm", "-bdfm"), ("g", "-g"),
                                  ("jz", "-jz"), ("nqrw", "-nqrw"), ("l", "-l"), ("si", "-si"))
        ),
        Source("dpc_bulletin", "DPC, bollettino", _DPC + "files/all/latest_all.zip",
               "CC BY 4.0", "Dati: Dipartimento della Protezione Civile (CC BY 4.0)", timedelta(hours=1),
               timedelta(hours=36), country="IT"),
        Source("nws_alerts", "National Weather Service alerts", "https://api.weather.gov/alerts/active",
               "US public domain", "Data: NOAA/NWS", timedelta(minutes=5), timedelta(hours=2)),
        Source("lazio_pharmacies", "Regione Lazio, farmacie", "https://dati.lazio.it/dataset/"
               "551579e1-a65d-4e7e-80d7-9bfe07fb2bdc/resource/7658322d-b629-4d77-a9f1-e4aad7c8f83b/download/"
               "farmaciereglaziolatlon.csv", "IODL 2.0", "Dati: Regione Lazio (IODL 2.0)", timedelta(days=30),
               timedelta(days=365), country="IT"),
        Source("rome_ztl", "Roma Servizi per la Mobilità, ZTL in Centro (orari)",
               "https://romamobilita.it/muoversi-a-roma/ztl-in-centro/", "reviewed table",
               "Orari: Roma Servizi per la Mobilità, pagina «ZTL in Centro» (aggiornata il 28/07/2026)",
               timedelta(days=3650), timedelta(days=3650), country="IT"),
    )
}  # fmt: skip
NYC_GROUPS = ("1234567", "ace", "bdfm", "g", "jz", "nqrw", "l", "si")
# The MTA's feed of a line (route_id), to fetch only the ones a stop's lines need
NYC_GROUP_OF = {
    **{r: "1234567" for r in ("1", "2", "3", "4", "5", "6", "6X", "7", "7X", "GS")},
    **{r: "ace" for r in ("A", "C", "E", "H", "FS")},
    **{r: "bdfm" for r in ("B", "D", "F", "FX", "M")},
    "G": "g", "J": "jz", "Z": "jz", "L": "l", "SI": "si",
    **{r: "nqrw" for r in ("N", "Q", "R", "W")},
}  # fmt: skip


def on(key: str, country: str | None) -> bool:
    """A source is read only for a user in its country (PISA_COUNTRY); a source with none, anywhere."""
    source = SOURCES.get(key.split(":", 1)[0])
    return source is None or source.country is None or source.country.upper() == (country or "").upper()


def hosts() -> frozenset[str]:
    """Every host Pisa reads public data from (the network allowlist of these tools)."""
    from urllib.parse import urlsplit

    return frozenset(
        (urlsplit(s.url).hostname or "") for s in SOURCES.values() if s.licence != "reviewed table"
    )


def credit(keys: list[str]) -> str:
    """The attribution line for what a answer used (CC BY and BY-SA ask for it)."""
    seen: list[str] = []
    for key in keys:
        source = SOURCES.get(key)
        if source is not None and source.attribution not in seen:
            seen.append(source.attribution)
    return "; ".join(seen)


# A recipe without its own credit block: the credit of every source a public read may use
TOOL_CREDIT = "Dati: RSM, DPC, Regione Lazio; Data: MTA, NOAA/NWS"
