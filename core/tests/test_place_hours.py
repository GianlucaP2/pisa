"""Opening hours: open now and the next change in Europe/Rome, across midnight and DST, closed days
and special days, around the clock; plus distances, text keys and links."""

from datetime import UTC, date, datetime

import pytest

from pisa.domain.places import (
    Period,
    category_key,
    closes_between,
    distance_m,
    map_links,
    open_state,
    text_key,
)
from tests.assistant_env import rome

TZ = "Europe/Rome"
# Monday to Friday 12:00-15:00 and 19:00-23:30; Friday and Saturday evenings until 01:00; closed on Sunday.
WEEK = [
    *(Period(d, "12:00", "15:00") for d in range(1, 6)),
    *(Period(d, "19:00", "23:30") for d in range(1, 5)),
    Period(5, "19:00", "01:00"),
    Period(6, "19:00", "01:00"),
]


def hours(start: datetime, end: datetime) -> float:
    """Real elapsed hours (in UTC: a same-zone subtraction counts wall-clock time)."""
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600


@pytest.mark.parametrize(
    ("now", "is_open", "change"),
    [
        (rome(2026, 10, 21, 13, 0), True, rome(2026, 10, 21, 15, 0)),  # Wednesday lunch
        (rome(2026, 10, 21, 15, 0), False, rome(2026, 10, 21, 19, 0)),  # closes at 15:00 sharp
        (rome(2026, 10, 21, 11, 59), False, rome(2026, 10, 21, 12, 0)),
        (rome(2026, 10, 23, 23, 45), True, rome(2026, 10, 24, 1, 0)),  # Friday night, across midnight
        (rome(2026, 10, 24, 0, 30), True, rome(2026, 10, 24, 1, 0)),  # Saturday 00:30: still Friday's evening
        (rome(2026, 10, 24, 12, 30), False, rome(2026, 10, 24, 19, 0)),  # no lunch on Saturday
        (rome(2026, 10, 26, 2, 0), False, rome(2026, 10, 26, 12, 0)),  # Sunday closed → Monday lunch
    ],
)
def test_open_now_and_next_change(now: datetime, is_open: bool, change: datetime) -> None:
    assert open_state(WEEK, now, TZ) == (is_open, change)


def test_dst_night_in_autumn() -> None:
    # 25 Oct 2026: clocks go back at 03:00. Saturday 19:00 → Sunday 01:00 is open; Sunday is closed.
    assert open_state(WEEK, rome(2026, 10, 25, 0, 30), TZ) == (True, rome(2026, 10, 25, 1, 0))
    is_open, change = open_state(WEEK, rome(2026, 10, 25, 1, 30), TZ)
    assert (is_open, change) == (False, rome(2026, 10, 26, 12, 0))
    # A bar open 22:00-04:00 is open 7 hours on that night (25 h day), not 6.
    bar = [Period(6, "22:00", "04:00")]
    _, closes = open_state(bar, rome(2026, 10, 24, 22, 30), TZ)
    assert closes is not None and closes == rome(2026, 10, 25, 4, 0)
    assert hours(rome(2026, 10, 24, 22, 0), closes) == 7


def test_dst_night_in_spring() -> None:
    # 29 Mar 2026: clocks go forward at 02:00. Open 22:00-04:00 is 5 real hours that night.
    bar = [Period(6, "22:00", "04:00")]
    is_open, closes = open_state(bar, rome(2026, 3, 29, 3, 30), TZ)
    assert is_open and closes is not None and closes == rome(2026, 3, 29, 4, 0)
    assert hours(rome(2026, 3, 28, 22, 0), closes) == 5


def test_dated_periods_and_closed_special_days() -> None:
    current = [Period(1, "09:00", "13:00", date(2026, 10, 26))]  # special hours on Monday the 26th
    assert open_state(current + WEEK, rome(2026, 10, 26, 10, 0), TZ) == (True, rome(2026, 10, 26, 13, 0))
    holiday = frozenset({date(2026, 10, 21)})  # closed on Wednesday the 21st
    assert open_state(WEEK, rome(2026, 10, 21, 13, 0), TZ, holiday) == (False, rome(2026, 10, 22, 12, 0))


def test_around_the_clock_and_unknown() -> None:
    always = [Period(7, "00:00", None)]
    assert open_state(always, rome(2026, 10, 21, 3, 0), TZ) == (True, None)
    assert open_state([], rome(2026, 10, 21, 3, 0), TZ) == (None, None)
    midnight = [Period(d, "00:00", "24:00") for d in range(1, 8)]
    assert open_state(midnight, rome(2026, 10, 21, 23, 59), TZ)[0] is True


def test_closes_between() -> None:
    post = [Period(d, "08:20", "13:35") for d in range(1, 6)]
    wed = rome(2026, 10, 21, 23, 59)
    assert closes_between(post, rome(2026, 10, 21, 8, 30), wed, TZ) == rome(2026, 10, 21, 13, 35)
    # Not open yet at 08:00, but it closes at 13:35, before the task's day is over: that is the warning.
    assert closes_between(post, rome(2026, 10, 21, 8, 0), wed, TZ) == rome(2026, 10, 21, 13, 35)
    assert closes_between(post, rome(2026, 10, 21, 8, 30), rome(2026, 10, 21, 12, 0), TZ) is None  # due first
    assert closes_between(post, rome(2026, 10, 21, 14, 0), wed, TZ) is None  # no opening left in between


def test_helpers() -> None:
    assert 6_000 < distance_m((45.48, 9.154), (45.443, 9.097)) < 6_100
    assert text_key("Via Immaginaria, 1 - Milàno ") == "via immaginaria 1 milano"
    assert (
        category_key("PublicTransport") == "public_transport" and category_key("post_office") == "post_office"
    )
    links = map_links("Trattoria da Mario", 45.46, 9.19, "ChIJabc")
    assert links == {
        "apple_maps": "https://maps.apple.com/?q=Trattoria%20da%20Mario&ll=45.46,9.19",
        "google_maps": "https://www.google.com/maps/search/?api=1&query=Trattoria%20da%20Mario&query_place_id=ChIJabc",
    }


def test_open_around_the_clock_is_every_day() -> None:
    # Google's single period with no close means 24/7, not "Sunday only".
    from pisa.ports.places import Period
    from pisa.services.places import weekly_hours

    always = weekly_hours([Period(day=7, open="00:00", close=None, date=None)])
    assert always == [{"day": d, "open": "00:00", "close": "24:00"} for d in range(1, 8)]
    lunch = weekly_hours([Period(day=1, open="12:00", close="15:00", date=None)])
    assert lunch == [{"day": 1, "open": "12:00", "close": "15:00"}]
