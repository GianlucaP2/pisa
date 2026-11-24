"""The lexicon's tables and parsers (domain/lexicon): what each named set accepts."""

from datetime import date, datetime
from decimal import Decimal

from pisa.domain.lexicon.dates import day_or_28, day_or_none, full_year, iso_day_or_none, iso_moment_or_none
from pisa.domain.lexicon.months import (
    EN_MONTHS,
    EN_MONTHS_SHORT,
    IT_MONTHS,
    IT_MONTHS_SHORT,
    MONTH_NAMES,
    month_forms,
    month_numbers,
)
from pisa.domain.lexicon.numbers import (
    comma_float,
    comma_float_or_none,
    decimal_point,
    grouped_amount,
    last_separator_decimal,
)
from pisa.domain.lexicon.weekdays import DAY_NAMES, WEEKDAY_NUMBERS, WEEKDAYS_SHORT, weekday_numbers


def test_month_tables_have_twelve_names_january_first() -> None:
    for table in (IT_MONTHS, EN_MONTHS, IT_MONTHS_SHORT, EN_MONTHS_SHORT):
        assert len(table) == 12
    assert MONTH_NAMES["it-IT"][9] == "ottobre" and MONTH_NAMES["en-US"][9] == "October"


def test_month_numbers_keeps_each_callers_set() -> None:
    money = month_numbers(IT_MONTHS, EN_MONTHS, EN_MONTHS_SHORT, extra={"sept": 9})
    assert money["sept"] == 9 and money["may"] == 5 and "gen" not in money
    trips = month_numbers(IT_MONTHS_SHORT, EN_MONTHS_SHORT)
    assert trips["dic"] == 12 and trips["dec"] == 12 and len(trips) == 20


def test_month_forms_lists_each_word_once_in_table_order() -> None:
    forms = month_forms(IT_MONTHS, EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS_SHORT)
    assert forms[2] == ("febbraio", "february", "feb")
    assert forms[5] == ("maggio", "may", "mag")


def test_weekdays_are_monday_first() -> None:
    assert WEEKDAY_NUMBERS["lunedi"] == 0 and WEEKDAY_NUMBERS["sunday"] == 6
    assert weekday_numbers(("monday",) * 1, start=1) == {"monday": 1}
    assert DAY_NAMES["it-IT"][1] == "martedì" and WEEKDAYS_SHORT["en-US"][1] == "Tue"


def test_numbers_each_rule() -> None:
    assert decimal_point("7,99") == "7.99"
    assert comma_float("1,5") == 1.5
    assert comma_float_or_none(" 45,1 ") == 45.1 and comma_float_or_none(None) is None
    assert comma_float_or_none("n/a") is None
    assert [grouped_amount(x) for x in ("1.234,56", "199,00", "189.85", "1.299", "x")] == [
        1234.56,
        199.0,
        189.85,
        1299.0,
        None,
    ]
    assert last_separator_decimal("1.234,50") == Decimal("1234.50")
    assert last_separator_decimal("1,234.5") == Decimal("1234.5")
    assert last_separator_decimal("abc") is None


def test_dates_none_or_28_for_a_day_that_does_not_exist() -> None:
    assert day_or_none(2026, 4, 31) is None and day_or_none(2026, 4, 30) == date(2026, 4, 30)
    assert day_or_28(2027, 2, 29) == date(2027, 2, 28) and day_or_28(2028, 2, 29) == date(2028, 2, 29)
    assert full_year(26) == 2026 and full_year(1999) == 1999
    assert iso_day_or_none("2026-11-04") == date(2026, 11, 4) and iso_day_or_none("4/11") is None
    assert iso_moment_or_none("2026-11-04T09:30") == datetime(2026, 11, 4, 9, 30)
    assert iso_moment_or_none("domani") is None
