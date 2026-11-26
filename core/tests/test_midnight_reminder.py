"""§36 c2-en: a reminder set for 00:29 was told as "12:29". The result names the time unambiguously for a
12-hour reader, "12:29 AM" reads as midnight, and a "set for" sentence naming a time no reminder was set
for is held (code then says the time it was set for). Synthetic times only."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pisa.brain.follow_tools import _fire_words  # pyright: ignore[reportPrivateUsage]
from pisa.domain import abilities, reminders

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 11, 25, 23, 57, tzinfo=TZ)
AT = datetime(2026, 11, 26, 0, 29, tzinfo=TZ)


def test_the_result_says_am_or_pm_to_a_twelve_hour_reader() -> None:
    assert _fire_words(AT, NOW, TZ, twelve=True) == "12:29 AM (00:29) on Thu 26 Nov"
    assert _fire_words(AT, NOW, TZ) == "00:29 on Thu 26 Nov"
    noon = datetime(2026, 11, 26, 12, 5, tzinfo=TZ)
    assert _fire_words(noon, NOW, TZ, twelve=True) == "12:05 PM (12:05) on Thu 26 Nov"


def test_twelve_am_is_midnight_and_a_bare_twelve_is_noon() -> None:
    hours = [sorted(m.hours) for m in reminders.said_moments("I've set it for 12:29 AM.")]
    assert hours == [[0]]
    assert [sorted(m.hours) for m in reminders.said_moments("I've set it for 12:29.")] == [[12]]


def test_a_set_sentence_with_the_wrong_time_is_held() -> None:
    result = [f"Reminder set: «call Anna» at {_fire_words(AT, NOW, TZ, twelve=True)} (id x)."]
    assert abilities._set_for(result, date(2026, 11, 25)) == [(0, 29, date(2026, 11, 26))]  # pyright: ignore[reportPrivateUsage]
    assert abilities.set_time_wrong("I've set it for 12:29 on Thursday, to call Anna.", result, NOW, TZ)
    assert not abilities.set_time_wrong("I've set it for 12:29 AM on Thursday.", result, NOW, TZ)
    assert not abilities.set_time_wrong("Your dentist is Friday at 10.", result, NOW, TZ)  # no "set"
    nine = ["Reminder set: «dentist» at 09:00 on Thu 26 Nov (id y)."]
    both = (
        "Il promemoria è impostato per giovedì 26 alle 9, il giorno prima del dentista di venerdì 27 alle 10."
    )
    assert not abilities.set_time_wrong(both, nine, NOW, TZ)  # the right time beside another moment stands
