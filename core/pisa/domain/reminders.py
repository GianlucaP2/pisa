"""Reminder wording (no model): fixed templates in Italian and English.

The minutes are counted from the moment the reminder goes out, so a reminder sent late (the catch-up after a
restart, or an event added a few minutes before its reminder time) still says the truth.
"""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain.lexicon.travel import MODE_WORDS

Kind = Literal["event", "task", "leave"]


def minutes_until(start: datetime, now: datetime) -> int:
    return max(0, round((start - now).total_seconds() / 60))


def reminder_text(kind: Kind, title: str, place: str | None, minutes: int, lang: str) -> str:
    """ "Tra 15 minuti: Dentista (Via Roma 1)." / "In 15 minutes: Dentist (Via Roma 1).\""""
    where = f" ({place})" if place else ""
    it = lang == "it-IT"
    if minutes == 0:
        if kind == "event":
            lead = "Ora" if it else "Now"
        else:
            lead = "Scade ora" if it else "Due now"
    elif it:
        unit = "minuto" if minutes == 1 else "minuti"
        lead = f"Tra {minutes} {unit}" if kind == "event" else f"Scade tra {minutes} {unit}"
    else:
        unit = "minute" if minutes == 1 else "minutes"
        lead = f"In {minutes} {unit}" if kind == "event" else f"Due in {minutes} {unit}"
    return f"{lead}: {title}{where}."


def reminder_summary(kind: Kind, title: str, start: datetime, lang: str, timezone: str) -> str:
    """The form a held reminder takes in the quiet-mode summary, read hours later: "Dentista alle 9:00",
    "Pagare la bolletta entro le 18:00" / "Dentist at 9:00", "Pay the bill by 18:00"."""
    local = start.astimezone(ZoneInfo(timezone))
    at = f"{local.hour}:{local.minute:02d}"
    if lang == "it-IT":
        return f"{title} alle {at}" if kind == "event" else f"{title} entro le {at}"
    return f"{title} at {at}" if kind == "event" else f"{title} by {at}"


def leave_text(title: str, minutes: int, mode: str, lang: str) -> str:
    """The leave-now reminder: "Esci ora: 18 minuti con i mezzi per Dentista." / "Leave now: 18 min by
    public transport to Dentist."""
    how = MODE_WORDS["it-IT" if lang == "it-IT" else "en-US"][mode]
    if lang == "it-IT":
        return f"Esci ora: {minutes} {'minuto' if minutes == 1 else 'minuti'} {how} per {title}."
    return f"Leave now: {minutes} min {how} to {title}."


def leave_summary(title: str, leave_at: datetime, lang: str, timezone: str) -> str:
    """The quiet-mode summary form: "Partenza per Dentista alle 8:42" / "Leave for Dentist at 8:42"."""
    local = leave_at.astimezone(ZoneInfo(timezone))
    at = f"{local.hour}:{local.minute:02d}"
    return f"Partenza per {title} alle {at}" if lang == "it-IT" else f"Leave for {title} at {at}"
