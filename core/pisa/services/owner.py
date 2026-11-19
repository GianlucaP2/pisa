"""The provider of the user's context: one `Owner` per runtime, built from Pisa's settings (language, country,
home zone) and the settings table, moved by `services/zone.py` when the primary phone reports a zone."""

from collections.abc import Callable
from datetime import datetime

from pisa.domain.owner import Owner
from pisa.services.settings import read
from pisa.settings import Settings


def provide(settings: Settings, clock: Callable[[], datetime]) -> Owner:
    """The runtime's Owner: PISA_LANGUAGE, PISA_COUNTRY and PISA_TIMEZONE until the phone and Settings say
    otherwise."""
    return Owner(
        settings.timezone, language=settings.language, country=settings.country, read=read, clock=clock
    )
