"""The AdE's cassetto fiscale API (Provv. n. 200918 of 6 Jul 2026, art. 23 D.Lgs. 1/2024):
interoperability APIs for taxpayers registered with Fisconline; the first
service is the CU download.
**Built and off**: their Fisconline credentials are sealed like every key and used only for a call.

The API's own security terms (Allegato 1) and its endpoints are published to registered taxpayers only; they
were not available when this was written, so the client refuses with the real cause instead of guessing a call
to a tax office. The next step is a provider that implements `documents` against Allegato 1.
"""

from typing import Any

import httpx

from pisa.connectors.sources.errors import SourceError


class AdeCassetto:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def documents(self, secret: dict[str, str], years: int) -> list[dict[str, Any]]:
        raise SourceError(
            "The AdE's cassetto fiscale API isn't wired yet: its terms (Allegato 1 of Provv. 200918) must be "
            "read first. Pisa keeps your credentials sealed and calls nothing until then."
        )
