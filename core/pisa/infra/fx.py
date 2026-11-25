"""USD→EUR from the ECB daily reference rates, stored in app_settings['usd_to_eur'].

If the ECB can't be reached, the last stored rate is used, or 0.86 when none was ever stored.
"""

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.db.settings import get_setting, set_setting
from pisa.logging import get_logger

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
DEFAULT_USD_TO_EUR = Decimal("0.86")
_log = get_logger("pisa.models.fx")


def parse_ecb_daily(xml_text: str) -> tuple[date, Decimal]:
    """(rate date, USD→EUR). The ECB quotes 1 EUR = <rate> USD, so USD→EUR = 1 / rate."""
    root = ET.fromstring(xml_text)  # noqa: S314
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time"):
            for child in cube:
                if child.get("currency") == "USD":
                    usd_per_eur = Decimal(child.get("rate", ""))
                    if usd_per_eur <= 0:
                        break
                    return date.fromisoformat(cube.get("time", "")), (Decimal(1) / usd_per_eur).quantize(
                        Decimal("0.000001")
                    )
    raise ValueError("no USD rate in the ECB feed")


async def refresh_usd_to_eur(conn: AsyncConnection, http: httpx.AsyncClient) -> Decimal:
    """Fetch and store today's rate. On failure keep the stored rate and log the real cause."""
    try:
        response = await http.get(ECB_DAILY_URL, timeout=20)
        response.raise_for_status()
        rate_date, rate = parse_ecb_daily(response.text)
    except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
        current = await usd_to_eur(conn)
        _log.warning("fx_refresh_failed", cause=f"{exc.__class__.__name__}: {exc}", using=str(current))
        return current
    await set_setting(
        conn, "usd_to_eur", Jsonb({"rate": str(rate), "date": rate_date.isoformat(), "source": "ecb"})
    )
    _log.info("fx_refreshed", usd_to_eur=str(rate), date=rate_date.isoformat())
    return rate


async def usd_to_eur(conn: AsyncConnection) -> Decimal:
    stored = await get_setting(conn, "usd_to_eur")
    if isinstance(stored, dict) and stored.get("rate"):
        return Decimal(str(stored["rate"]))
    return DEFAULT_USD_TO_EUR
