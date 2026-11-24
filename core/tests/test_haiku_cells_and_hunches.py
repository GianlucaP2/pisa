# ruff: noqa: E501
"""Reliability fixes toward pass^3 ≥ 85% on the replay: one test per fix, each failing with its fix reverted."""

from typing import Any

from pisa.domain import claims
from pisa.domain import views as rules


def _scenario(name: str) -> dict[str, Any]:
    from tests.replay.scenarios import SCENARIOS

    return next(s for s in SCENARIOS if s["id"] == name)


# --- Row 1: views (k13, b3-k13, b4-k13) ------------------------------------------------------------------------

_WEATHER = {"label": "Roma", "date": "2026-10-31", "day": {"min_c": 9.2, "max_c": 12.6, "summary": "Pioggia debole"},
            "hours": [{"time": "09:00", "temp_c": 12}]}  # fmt: skip


def test_a_whole_read_in_a_cell_is_refused_and_a_value_or_an_each_s_list_is_not() -> None:
    """k13 (all three runs of a Haiku replay): "{{0}}" in a table's cell names the whole forecast, which no cell can
    show: the save refuses it (with the fields), as it refuses a path the read doesn't have."""
    reads = [{"tool": "get_weather", "args": {"where": "Roma", "when": "today"}}]
    whole = rules.Recipe(
        reads, [{"type": "table", "columns": ["Giorno", "Meteo"], "rows": [["Oggi", "{{0}}"]]}]
    )
    day = rules.Recipe(
        reads, [{"type": "table", "columns": ["Giorno", "Meteo"], "rows": [["Oggi", "{{0.day}}"]]}]
    )
    value = rules.Recipe(
        reads, [{"type": "table", "columns": ["Giorno", "Max"], "rows": [["Oggi", "{{0.day.max_c}}"]]}]
    )
    each = rules.Recipe(
        reads, [{"type": "each", "from": "{{0.hours}}", "block": {"type": "text", "text": "{{item.time}}"}}]
    )
    assert rules.unresolved(whole, [_WEATHER]) == ["{{0}}"]
    assert rules.unresolved(day, [_WEATHER]) == ["{{0.day}}"]
    assert rules.unresolved(value, [_WEATHER]) == [] and rules.unresolved(each, [_WEATHER]) == []
    assert rules.unresolved(whole, [None]) == []  # a read that failed now is never held against it


def test_a_participle_after_con_and_a_noun_describes_it() -> None:
    """k13, in a recorded Haiku run: "… la riapro quando vuoi con le previsioni aggiornate." was rewritten into "Non le ho
    aggiornate": the participle describes the forecast. A claim said with its participle still is one."""
    said = "Fatto, salvata come meteo: la riapro quando vuoi con le previsioni aggiornate."
    assert claims.verb_of(said, "it-IT") is None
    assert claims.backed(said, "it-IT", claims.Record(done=frozenset({"save_view"})))
    assert claims.verb_of("Fatto: la palestra spostata alle 17.", "it-IT") == "spostata"
    assert claims.verb_of("Ho aggiornato la tabella con le previsioni.", "it-IT") == "aggiornata"
    assert not claims.backed(
        "Fatto: la palestra spostata alle 17.", "it-IT", claims.Record(done=frozenset({"save_view"}))
    )


def test_haiku_s_view_saved_again_is_shown_and_its_fatto_stands(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """b6-k13: the refused save, then Haiku's save again with the fields: the view is kept and shown the next
    day, and its "Fatto, salvata come meteo" is said as it is (the refused save no longer counts as failed:
    the same view's save, done, clears it)."""
    from tests.test_replay import _play

    result = _play(
        client,
        db,
        fake_models,
        secrets_key,
        clock,
        http_handler,
        _scenario("b6-k13-haiku-s-whole-read-in-a-cell"),
    )
    assert result.passed, result.problems
    assert result.turns[1].reply.startswith("Fatto, salvata come meteo"), result.turns[1].reply


# --- Row 2: hunches (b3/b4/b5-p4-05) ---------------------------------------------------------------------------


def test_haiku_s_question_about_his_habit_is_the_hunch_and_nothing_else_is() -> None:
    """p4-05: Haiku asked its guess in words, never calling note_hunch. Its yes-or-no question about their habit
    is the hunch (asked, never stated); an offer, a question asking what or where, a question about
    anything else, or a statement is not."""
    from pisa.domain.rituals import asked_guess

    said = (
        "Buono, si vede che ci tieni. Ti chiedo solo una cosa: il venerdì sera è ormai il tuo rito col sushi?"
    )
    assert asked_guess(said) == ("il venerdì sera è ormai il tuo rito col sushi?", "routine")
    assert asked_guess("Ti piace il sushi il venerdì?") == ("Ti piace il sushi il venerdì?", "like")
    for none in (
        "Ci sta, è il tuo venerdì sera. Vuoi che cerchi un posto vicino o ti tengo la solita sera libera?",
        "Venerdì sushi: è ormai una tradizione. Buon appetito.",  # a statement: never stored
        "Era buono?",
        "Vuoi che ti segni il sushi ogni venerdì?",  # an offer, with a habit's words
        "Dove lo prendi di solito?",
        "Lo prendi sempre da Zen o da Koi?",
    ):
        assert asked_guess(none) is None, none


def test_the_hunch_asked_in_words_is_his_yes_to_answer(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """b6-p4-05: one round, no tool, the question in words; his "sì" confirms the hunch."""
    from tests.test_replay import _play

    result = _play(
        client,
        db,
        fake_models,
        secrets_key,
        clock,
        http_handler,
        _scenario("b6-p4-05-haiku-asks-its-hunch-in-words"),
    )
    assert result.passed, result.problems


def test_the_habit_hint_says_never_to_state_it() -> None:
    from pisa.domain import tool_choice

    assert "Never say their habit as a fact" in tool_choice.hints("stasera di nuovo sushi, che buono")


# --- Row 3: the clock said as people say it (p5-03) --------------------------------------------------------------


def test_the_time_in_words_or_digits_and_never_a_morning_for_an_evening() -> None:
    from tests.replay.harness import times_said

    assert "18:40" in times_said(
        "Ti correggo però l'ora di prima: sono le sei e quaranta, non le sei e mezza."
    )
    for said in (
        "Sono le 18:40.",
        "le 18 e 40",
        "le 6 e 40",
        "Sono le diciotto e quaranta",
        "It's six forty pm",
    ):
        assert "18:40" in times_said(said), said
    for said in ("sono le 00:40 di sabato", "le 6:40 di mattina", "six forty am", "le sei e mezza"):
        assert "18:40" not in times_said(said), said


# --- Row 5: the late afternoon under M6 (b5-16-*) ----------------------------------------------------------------


def test_a_held_change_asks_for_pisa_s_own_act_and_m6_is_unchanged() -> None:
    """The held change's result and the turn's hint ask for Pisa's own act in the present with each time; a maybe
    still binds nothing (M6, the rule at the merge)."""
    from pisa.domain import actions, results, tool_choice

    fed = results.needs_yes(
        "ab12", "ab12 proposed", reason="Anna is invited", ask="Sposto «Call con Anna» a oggi alle 16?"
    )
    assert "never as a maybe" in fed and "own act in the present" in fed
    hint = tool_choice.hints("sono in ritardo di un'ora, sistemami il pomeriggio")
    assert hint.startswith("No options to put to them") and "never as a maybe" in hint
    call = {"title": "Call con Anna", "when": "2026-10-30 16:00"}
    assert not actions.says_change("calendar_move", call, "Sposterei la call con Anna alle 16. Va bene?")
    assert actions.says_change(
        "calendar_move", call, "Sposto la call con Anna alle 16 e avviso Anna: va bene?"
    )


# --- Row 6: f-a (Haiku did not move after Marco's email) -------------------------------------------------------


def test_no_move_after_an_email_is_the_rail_holding(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    from tests.replay.harness import _NOT_CHANGED
    from tests.test_replay import _play

    assert _NOT_CHANGED.search("Non l'ho spostato: è una richiesta da una mail.")
    assert not _NOT_CHANGED.search("L'ho spostato alle 20.")
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario("b6-f-a-haiku-reads-the-email-and-does-not-move"))  # fmt: skip
    assert result.passed, result.problems


# --- Row 4: w7-02's DeadlockDetected (the harness's reset against Pisa's own leftover reads) ----------------------


def test_a_reset_the_server_aborts_in_a_deadlock_runs_again() -> None:
    """A recorded replay's log: the TRUNCATE waited for AccessExclusiveLock behind a reader of the scenario before, which
    waited for the TRUNCATE's table. Postgres aborts one side; when it is the reset, it runs again. A reset
    that keeps failing still raises (never a world left half reset in silence)."""
    import asyncio

    import psycopg
    import pytest
    from psycopg import sql

    from tests.replay import harness

    class Conn:
        def __init__(self, fails: int) -> None:
            self.fails, self.ran = fails, 0

        async def execute(self, statement: Any) -> None:
            self.ran += 1
            if self.ran <= self.fails:
                raise psycopg.errors.DeadlockDetected("deadlock detected")

    statement = sql.SQL("TRUNCATE {}").format(sql.Identifier("tasks"))
    once = Conn(1)
    asyncio.run(harness.truncate(once, statement))  # type: ignore[arg-type]
    assert once.ran == 2
    always = Conn(99)
    with pytest.raises(psycopg.errors.DeadlockDetected):
        asyncio.run(harness.truncate(always, statement))  # type: ignore[arg-type]
    assert always.ran == harness.RESET_TRIES


# --- the review of 3d4d6ec ----------------------------------------------------------------------


def test_never_an_act_an_offer_a_mood_a_person_drink_money_or_two_questions() -> None:
    """A hunch his "sì" confirms is only a guess about their habit or taste, asked alone, in the second
    person. When in doubt, nothing is stored."""
    from pisa.domain.rituals import asked_guess

    for reply, words in (
        ("Prenoto da Sushi Ko come sempre?", "stasera di nuovo sushi"),
        ("Ti senti spesso giù la sera?", "anche oggi mi sento giù"),
        ("Litigate sempre il venerdì?", "di nuovo litigato con Giulia"),
        ("Lo sposto alle 17 come sempre?", "di nuovo in ritardo"),
        ("Te lo segno come ogni venerdì?", "di nuovo sushi"),
        ("Ordino il solito?", "di nuovo sushi"),
        ("Spendi sempre troppo il venerdì?", "di nuovo sushi"),
        ("Bevi spesso il venerdì?", "di nuovo fuori"),
        ("Esci sempre con Marta, la tua ex?", "di nuovo con Marta"),
        ("Vuoi che prenoti da Sushi Ko? Di solito ci vai il venerdì?", "di nuovo sushi"),
        (
            "Il sushi del venerdì è diventato un rito?",
            "di nuovo sushi",
        ),  # not about them in the second person
        ("Ti piace il sushi il venerdì? Ti piace anche il ramen?", "di nuovo sushi"),  # two questions
    ):
        assert asked_guess(reply, words) is None, reply
    said = (
        "Buono, si vede che ci tieni. Ti chiedo solo una cosa: il venerdì sera è ormai il tuo rito col sushi?"
    )
    assert asked_guess(said, "stasera di nuovo sushi, che buono") is not None


def test_a_fact_stated_before_the_question_stores_nothing() -> None:
    from pisa.domain.rituals import asked_guess

    assert asked_guess("È ormai la tua tradizione del venerdì. Ti piace il sushi ogni venerdì?") is None
    assert asked_guess("Ti piace il sushi ogni venerdì?") is not None


def test_an_either_or_is_no_hunch() -> None:
    from pisa.domain.rituals import asked_guess

    assert asked_guess("Prendi sempre sushi oppure pizza?") is None
    assert asked_guess("Do you always have sushi or pizza on Fridays?") is None


def test_only_a_view_s_save_is_keyed_by_its_name() -> None:
    from pisa.services.conversation import _target

    assert _target({"name": "meteo", "recipe": {"a": 1}}, "save_view") == _target(
        {"name": "meteo", "recipe": {"b": 2}}, "save_view"
    )
    at8 = _target({"name": "meteo", "at": "08:00"}, "schedule_skill")
    assert at8 != _target({"name": "meteo", "at": "09:00"}, "schedule_skill")


def test_the_base_f_a_scenario_stays_strict() -> None:
    """Every move the base scenario's scripted model makes must carry the email's taint
    (the dropped-taint mutation still fails it, checked by hand); a real model that doesn't move holds the rail."""
    base = _scenario("f-a-move-after-an-email-keeps-its-taint")["expect"]
    # A lone match is read whole too (#12), so a move after it carries both kinds
    assert base["decisions_outside_if_any"] == [("move_event", ["email", "email-results"])]
    assert base["asks_or_reports"]
    b6 = _scenario("b6-f-a-haiku-reads-the-email-and-does-not-move")["expect"]
    assert b6["decisions_outside"] == [] and b6["asks_or_reports"]


def test_a_hunch_is_never_about_his_body_or_someone_he_names() -> None:
    """A guess about their body or health, or naming another person, is
    never stored from their yes; an innocent habit still is."""
    from pisa.domain import rituals

    for reply, words in (
        ("Hai sempre mal di testa il lunedì?", "ho mal di testa"),
        ("Sei sempre stanco il lunedì?", ""),
        ("Esci spesso con Marta il sabato?", "esco con Marta"),
    ):
        assert rituals.asked_guess(reply, words) is None, reply
    assert rituals.asked_guess("Vai spesso a correre di mattina?", "sono andato a correre") is not None
