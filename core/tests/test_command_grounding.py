"""The reviewer's grounding probes (one command, several events), kept as tests."""

from datetime import date, datetime

from pisa.domain.actions import decide_mode

FRI = date(2026, 10, 30)


def d(h, m=0, day=30):
    return datetime(2026, 10, day, h, m)


def mode(words, title, start, end=None, small=False):
    return decide_mode(
        "calendar_create",
        words,
        read_outside=False,
        attendees=False,
        start=start,
        default_day=FRI,
        today=FRI,
        title=title,
        end=end,
        small_hours=small,
    )


def test_other_persons_plan_is_not_grounded():
    # the user mentions Marco's call; only the lunch is a command
    w = "Marco ha una call alle 4, metti il pranzo alle 13"
    assert mode(w, "Call di Marco", d(16)) != "words"


def test_other_persons_plan_variant():
    w = "Domani Nino ha il dentista alle 10, segna la palestra alle 18"
    assert mode(w, "Dentista Nino", d(10, day=31)) != "words"


def test_negation_by_no_is_not_grounded():
    w = "Il pranzo alle 13 sì, la call alle 4 no, mettili"
    assert mode(w, "Call", d(16)) != "words"


def test_owner_question_is_not_a_command():
    w = "Metto la call alle 4?"
    assert mode(w, "Call", d(16)) != "words"


def test_quattro_morning_is_proposal():
    assert mode("metti la call alle quattro", "Call", d(4)) != "words"  # EARLY_HOUR keeps it a proposal


def test_digit_10_silently_becomes_22():
    # the user said "alle 10": 22:00 is the model's guess, not the user's words
    assert mode("metti il dentista alle 10", "Dentista", d(22)) != "words"


def test_alle_8_becomes_20():
    assert mode("segna la colazione con Anna alle 8", "Colazione con Anna", d(20)) != "words"


def test_day_of_another_clause():
    # two days in one command: the call is tomorrow, model put it today -> must not act
    w = "Metti oggi alle 10 il dentista, domani alle 15 la call con Marco"
    assert mode(w, "Call con Marco", d(15)) != "words"
    assert mode(w, "Call con Marco", d(15, day=31)) == "words"  # its own day: tomorrow
    assert mode(w, "Dentista", d(10)) == "words"


def test_english_put_call():
    w = "put the call with Marco at 4 on the calendar"
    assert mode(w, "Call with Marco", d(16)) == "words"


def test_night_command_small_hours():
    w = (
        "Domani tra le 10 e le 14 mi arriva la spesa… alle 14 in ufficio, call con Marco fino alle quattro, "
        "dalle quattro call con Alessandro… metti tutto sul calendario e ricordamelo."
    )
    assert mode(w, "Arrivo spesa", d(10), d(14), small=True) == "words"
    assert mode(w, "Ufficio", d(14), d(16), small=True) == "words"
    assert mode(w, "Call con Marco", d(14), d(16), small=True) == "words"
    assert mode(w, "Call con Alessandro", d(16), d(17), small=True) == "words"
    # and on the 31st it must not act
    assert mode(w, "Arrivo spesa", d(10, day=31), d(14, day=31), small=True) != "words"
