"""Tasks that move on: a task overdue for a while leaves the plan and the
briefing with one question; unanswered, it goes. Pure rules, no I/O.

- **Aged**: open, overdue by `AGED_AFTER` (3 days), and not kept by the user in the last 3 days (their "non
  ancora" to Pisa's question keeps it, then it may be asked again).
- **Asked once**: "«X» è scaduto da 4 giorni: è ancora da fare?" (a check-in: done / not yet, by voice or a
  tap).
- **Gone**: asked `DISMISS_AFTER` (7 days) ago, unanswered, still open: dismissed with Undo (restore), its
  reason in Pisa's decisions.
- **The plan's undated tasks**: at most `UNDATED_MAX` (5) a day; what was left out yesterday comes first.
"""

from datetime import datetime, timedelta

AGED_AFTER = timedelta(days=3)
DISMISS_AFTER = timedelta(days=7)
UNDATED_MAX = 5


def aged(due_at: datetime | None, kept_at: datetime | None, now: datetime) -> bool:
    if due_at is None or due_at > now - AGED_AFTER:
        return False
    return kept_at is None or kept_at < now - AGED_AFTER


def question(title: str, days: int, lang: str) -> str:
    if lang == "it-IT":
        return f"«{title}» è scaduto da {days} giorni: è ancora da fare?"
    return f"“{title}” has been overdue for {days} days: still to do?"


def gone_reason(lang: str) -> str:
    if lang == "it-IT":
        return "Scaduto e senza risposta da 7 giorni: tolto (puoi rimetterlo)"
    return "Overdue and unanswered for 7 days: dropped (you can restore it)"
