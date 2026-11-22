# ruff: noqa: E501
"""Reliability fixes toward pass^3 ≥ 85% on the replay: one test per fix, each failing with its fix reverted."""

from typing import Any

from pisa.domain import zone
from pisa.domain.owner import Owner
from tests.owner import owner_at

NEW_YORK = zone.PhoneContext("America/New_York", "US", "h12")


# --- Row 2: the zone (p1-r1, p1-14, p5-03, p2-laura: 5 h off) ------------------------------------------------


def _play_with_another_pass(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any,
                            http_handler: Any, scenario: dict[str, Any]) -> Any:  # fmt: skip
    """The scenario, while another pass of the same process (the server's `brain-replay` plays three at once,
    each with its own runtime and so its own Owner) moves its own phone to New York in the middle of this
    turn: at every model call."""
    from tests.replay import harness
    from tests.test_replay import _play

    other = owner_at()  # the other pass's runtime's Owner
    real = harness.script

    def leaky(scen: dict[str, Any]) -> Any:
        model = real(scen)

        async def each(messages: Any, info: Any) -> Any:
            other.place = NEW_YORK
            async for part in model(messages, info):
                yield part

        return each

    harness.script = leaky  # type: ignore[assignment]
    try:
        return _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    finally:
        harness.script = real  # type: ignore[assignment]


def test_another_pass_s_phone_never_moves_this_scenario(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """p1-r1 ("found ['Palestra 2026-11-01T02:00']"): the gym lands at 21:00 Rome, never 02:00."""
    from tests.replay.scenarios import SCENARIOS

    base = next(s for s in SCENARIOS if s["id"] == "b4-p1-r1-haiku-s-own-move")
    result = _play_with_another_pass(client, db, fake_models, secrets_key, clock, http_handler, base)
    assert result.passed, result.problems


def test_the_clock_tool_reads_this_scenario_s_zone(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """p5-03 (Rome's 18:40 after landing): the clock tool in this scenario's own zone while another pass is in
    New York (p2-laura's their_time reads the same zone: "Laura: same time as the user" under the leak)."""
    from tests.replay.scenarios import SCENARIOS

    for sid in ("b4-p5-03-the-clock-tool-in-his-zone",):
        base = next(s for s in SCENARIOS if s["id"] == sid)
        result = _play_with_another_pass(client, db, fake_models, secrets_key, clock, http_handler, base)
        assert result.passed, (sid, result.problems)


def test_each_runtime_keeps_its_own_zone_and_one_runtime_keeps_one() -> None:
    import asyncio

    async def main() -> None:
        seen: list[str] = []

        async def scenario(owner: Owner, mine: zone.PhoneContext | None, other_first: asyncio.Event) -> None:
            owner.place = mine
            await other_first.wait()
            seen.append(owner.tz)

        go = asyncio.Event()
        pisa = owner_at()  # Pisa's own runtime (the api's phone report moves it)
        task = asyncio.create_task(scenario(owner_at(), None, go))
        london = asyncio.create_task(
            scenario(owner_at(), zone.PhoneContext("Europe/London"), go)
        )  # its own phone's report
        await asyncio.sleep(0)
        pisa.place = NEW_YORK
        go.set()
        await task
        await london
        assert seen == ["Europe/Rome", "Europe/London"]
        assert pisa.tz == "America/New_York"  # one runtime: one zone for every task

    asyncio.run(main())


# --- Row 3: the memory checker's abstention (m-01, m-03, m-04, m-06) ----------------------------------------


def test_haiku_s_own_abstentions_count_by_their_meaning() -> None:
    """A recorded Haiku run's replies: each says Pisa has no record. `_ABSTAINS` (a list of phrases) missed them."""
    from tests.replay import harness

    for reply in (
        "Non ho niente di segnato su martedì scorso: cosa hai mangiato?",
        "I couldn't find anything about what Tomasz said about Kraków.",
        "Non ho un registro di quando sei arrivato in ufficio ieri.",
        "Non vedo nulla su Ornella nelle mie note.",
        "Nessuna cena registrata per martedì.",
        "There's nothing in my records about that trip.",
        "Non ne ho traccia: me la racconti?",
        "No, non me l\u2019hai detto.",
        "I don\u2019t know.",  # typed on a phone
    ):
        assert harness.abstains(reply), reply


def test_an_answer_or_a_question_is_no_abstention() -> None:
    from tests.replay import harness

    for reply in (
        "Martedì hai mangiato la pizza, non ho dubbi.",
        "Sei arrivato alle 9.",
        "Tomasz said the trip was great.",
        "Non ne hai traccia?",  # a question asks, it doesn't abstain
        "È andata bene, mi pare.",
        "Ornella ti ha detto che cambia lavoro.",
    ):
        assert not harness.abstains(reply), reply


# --- Row 1: the yes chain for held guest-event moves (one-question-one-yes, four scenarios) -------------------------------------------


def test_a_bare_question_to_go_on_asks_his_yes_for_the_proposal_before_it() -> None:
    from pisa.domain import actions, claims

    for ask in ("Va bene?", "Ti va bene?", "Procedo?", "Mi confermi?", "Ok?", "Lo faccio?", "Shall I?",
                "Sound good?", "Does that work for you?"):  # fmt: skip
        assert claims.confirms(ask), ask
    for other in ("Ti serve altro?", "A che ora?", "Va bene la palestra alle 17?", "Vuoi che avvisi Anna?",
                  "Anything else?"):  # fmt: skip
        assert not claims.confirms(other), other
    empty = claims.Record(frozenset(), frozenset(), frozenset())
    said = "Sposto la call con Anna alle 16 e la palestra alle 17. Avviso Anna. Va bene?"
    assert claims.unbacked(said, "it-IT", empty) == []  # a proposal, never a claim
    assert claims.claim(said) != "doing"
    asked = actions.asked_sentence(said)
    assert "alle 16" in asked and "alle 17" in asked
    move = {"when": "2026-10-30 16:00"}
    assert actions.says_change("calendar_move", move, said)


def test_no_rail_is_loosened() -> None:
    from pisa.domain import actions, claims

    empty = claims.Record(frozenset(), frozenset(), frozenset())
    # a change claimed as made is still a claim, whatever follows
    assert claims.unbacked("Ho spostato la palestra alle 17. Va bene?", "it-IT", empty)
    # a question that asks something else never makes the act before it a proposal
    assert claims.unbacked("Sposto la palestra alle 17. Ti serve altro?", "it-IT", empty)
    # a question between the act and the yes asks its own
    assert claims.unbacked("Sposto la palestra alle 17. Avviso Anna? Va bene?", "it-IT", empty)
    # M6: "Va bene?" binds a move only when Pisa said its time before it; a cancel only with a cancel verb
    move = {"when": "2026-10-30 17:00"}
    assert not actions.says_change("calendar_move", move, "Sposto la palestra. Va bene?")
    assert not actions.says_change(
        "calendar_move", move, "Vuoi la palestra alle 17? Sposto la call. Va bene?"
    )
    # Read clause by clause: a cancel verb's clause, and a noun phrase after it, never bind a move
    # ("Cancello la call e sposto la palestra alle 17" binds the move by its own clause: their "sì" applies it)
    assert not actions.says_change("calendar_move", move, "Cancello la palestra alle 17. Ok?")
    assert not actions.says_change("calendar_move", move, "Cancello la call e la palestra alle 17. Ok?")
    assert not actions.says_change("calendar_cancel", {}, "Sposto la palestra alle 17. Va bene?")
    # the rail's own words never ask: "Non l'ho spostato: aspetta il tuo sì" is negated
    assert not claims.asks_yes("Non l'ho spostato: aspetta il tuo sì.")
    assert claims.asks_yes("Aspettano il tuo sì.")


# --- Row 4: the output limit on six-change turns (i2-afternoon-six-changes, b3-i2) ---------------------------

THINKING = 1024  # a medium-effort think before six writes (Haiku's mean turn wrote 555 tokens on top of it)


def test_a_six_change_round_fits_the_conversation_s_output_cap(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """Haiku's own six-change round (b3-i2: six writes with their words and purposes, and finish), at about 3.5
    characters a token, plus a medium-effort think: under the cap the conversation agent asks for. At 1024 it
    crashed ("it used its whole output limit before answering")."""
    import json

    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from tests.replay import harness
    from tests.replay.scenarios import SCENARIOS
    from tests.test_replay import _play

    base = next(s for s in SCENARIOS if s["id"] == "b3-i2-haiku-leaves-three-timed-parts-out")
    real = harness.script

    def capped(scen: dict[str, Any]) -> Any:
        model = real(scen)

        async def each(messages: Any, info: Any) -> Any:
            parts = [part async for part in model(messages, info)]
            written = sum(
                len(json.dumps(p)) if not isinstance(p, str) else len(p)
                for part in parts
                for p in ([part] if isinstance(part, str) else [(c.name, c.json_args) for c in part.values()])
            )
            cap = int((info.model_settings or {}).get("max_tokens") or 0)
            if written / 3.5 + THINKING > cap:
                raise UnexpectedModelBehavior(f"Model token limit ({cap}) exceeded")
            for part in parts:
                yield part

        return each

    harness.script = capped  # type: ignore[assignment]
    try:
        result = _play(client, db, fake_models, secrets_key, clock, http_handler, base)
    finally:
        harness.script = real  # type: ignore[assignment]
    assert result.passed, result.problems


def test_laura_s_hour_on_a_saturday_is_10_there_15_here() -> None:
    """p2-laura (3/3 "lacks '14'"): Haiku said "dalle 15 di qui (le 10 da lei)", what their_time says on a
    Saturday; the checker wanted a weekday's 14. The expectation follows Pisa's rule now."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from pisa.domain import people_offers
    from tests.replay.scenarios import SCENARIOS

    now = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)  # 10:00 in Rome, 05:00 in New York, a Saturday
    found = people_offers.clock("America/New_York", "Europe/Rome", now)
    assert found is not None and found.verdict == "early" and found.next_good is not None
    assert found.next_good.astimezone(ZoneInfo("Europe/Rome")).hour == 15
    for sid in ("p2-che-ore-sono-da-laura-a-new-york", "b4-p2-laura-s-hour-hinted"):
        assert next(s for s in SCENARIOS if s["id"] == sid)["expect"]["reply_has"] == ["15"]


def test_haiku_s_own_questions_point_back_at_the_times() -> None:
    from pisa.domain import actions, claims

    move = {"when": "2026-10-30 16:00"}
    for said in (
        "Ti propongo di spostare la call con Anna alle 16, avvisando Anna, e la palestra alle 17. Va bene, sì o no?",
        "Ho preparato lo spostamento: la Call con Anna alle 16 e la Palestra alle 17, entrambe oggi venerdì 30. "
        "Ti sposto così e avviso Anna? Dimmi sì o no.",
        "Propongo call alle 16 e palestra alle 17, con Anna avvisata. Confermi?",
    ):
        assert actions.says_change("calendar_move", move, said), said
    assert claims.points_back("Lo aggiungo così?") and not claims.points_back("Lo aggiungo alle 10?")
    # i1's own rule: a question that says its own time is its own sentence
    i1 = "Palestra è fatta, annullabile per 24 ore. Aggiungo il Macro a mezzanotte?"
    assert actions.asked_sentence(i1).strip() == "Aggiungo il Macro a mezzanotte?"


def test_his_words_decide_comes_first_in_the_turn() -> None:
    """The one-question-one-yes scenario (and q2-16, b3-16, b4-q2-16): ask_choice's refusal came after plan_options and show_plan had
    put three options in front of Haiku; the line is in the turn's instructions now, before any tool."""
    from pisa.domain import tool_choice

    line = tool_choice.hints("sono in ritardo di un'ora, sistemami il pomeriggio")
    assert line.startswith("No options to put to them") and "60 minutes later" in line
    assert tool_choice.hints("sono in ritardo di un'ora: salto la palestra o la call?") == ""
    assert tool_choice.hints("cosa ho oggi?") == ""


# --- Row 5: views and the hunch -----------------------------------------------------------------------------


def test_the_views_under_another_pass_s_new_york(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """k13, b3-k13, b4-k13 ("non so dove sei", "il servizio meteo non mi dà le previsioni per Roma", "il
    telefono non è collegato"): the weather and the screen read the zone another pass had moved to New York.
    With the scope (row 2) each plays in its own."""
    from tests.replay.scenarios import SCENARIOS

    for sid in ("k13-save-the-view-then-show-it-again", "b3-k13-haiku-saves-then-says-meteo",
                "b4-k13-haiku-s-own-recipe"):  # fmt: skip
        base = next(s for s in SCENARIOS if s["id"] == sid)
        result = _play_with_another_pass(client, db, fake_models, secrets_key, clock, http_handler, base)
        assert result.passed, (sid, result.problems)


def test_a_hunch_is_one_question_and_the_reply_says_it() -> None:
    from pisa.domain import rituals
    from pisa.services.rituals import RitualsService

    two = "Il venerdì sera il sushi è diventato la tua abitudine? Lo prendi quasi sempre così?"
    assert rituals.hunch_question(two) == "Il venerdì sera il sushi è diventato la tua abitudine?"
    assert rituals.hunch_question("ti piace il sushi il venerdì") == "ti piace il sushi il venerdì?"
    question = rituals.hunch_question(two)
    assert not RitualsService.said(question, "Il venerdì sera è ormai il tuo rito col sushi?")
    assert RitualsService.said(question, "Buono. Il venerdì sera il sushi è diventato la tua abitudine?")


def test_a_promise_for_when_he_is_back_is_no_claim_and_a_past_act_still_is() -> None:
    from pisa.domain import claims

    empty = claims.Record(frozenset(), frozenset(), frozenset())
    assert claims.unbacked("Quando torni te la metto in lista.", "it-IT", empty) == []
    assert claims.unbacked("When you land I move it to 9.", "en-US", empty) == []
    assert claims.unbacked("Quando sei uscito ho spostato la cena.", "it-IT", empty)
    assert claims.unbacked("Te la metto in lista.", "it-IT", empty)


def test_a_wish_s_placeholder_that_finds_nothing_is_refused_with_the_fields() -> None:
    """k22 (a recorded Haiku run): {{get_weather.max}} filled nothing and the trial went on with the placeholder on the
    sheet ("il numero non si è ancora riempito"); the wish is refused with the read's fields, as save_view."""
    import asyncio
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from pisa.services.wishes import WishService

    asked: list[Any] = []

    class Sheets:
        async def trial(self, title: str, stored: dict[str, Any], again: str) -> Any:
            asked.append(again)
            return (
                None,
                [],
                "Not saved: {{get_weather.max}} name no single value in what the reads answer now. Their …",
            )

    service = WishService(MagicMock(), Sheets(), MagicMock(), owner=owner_at(),  # type: ignore[arg-type]
                          clock=lambda: datetime(2026, 11, 3, 20, 0, tzinfo=UTC))  # fmt: skip
    recipe = {"reads": [{"tool": "get_weather", "args": {"when": "tomorrow", "where": "Roma"}}],
              "blocks": [{"type": "stat", "label": "Massima", "value": "{{get_weather.max}}", "unit": "°C"}]}  # fmt: skip
    said, sheet = asyncio.run(service.wish(
        "massima di domani a Roma", "vorrei che tu potessi dirmi la massima di domani a Roma",
        [{"input": "la massima di domani a Roma", "has": "°"}, {"input": "previsione di domani", "has": "Roma"}],
        recipe, hosts=set(), outside=False,
    ))  # fmt: skip
    assert (
        sheet is None
        and said.startswith("Not made: {{get_weather.max}}")
        and asked == ["Call wish_skill again"]
    )


# --- the review of 0d7bf26 ----------------------------------------------------------------------


def test_a_widened_question_never_binds_what_pisa_says_it_won_t_do() -> None:
    from pisa.domain import actions

    def move(title: str, at: str) -> dict[str, Any]:
        return {"title": title, "when": f"2026-10-30 {at}"}

    paola = "La cena con Paola non la cancello: è importante. Sposto solo la palestra alle 17. Va bene?"
    assert not actions.says_change("calendar_cancel", {"title": "Cena con Paola"}, paola)
    assert actions.says_change("calendar_move", move("Palestra", "17:00"), paola)
    call = move("Call con Anna", "16:00")
    for said in (
        "Non sposto la call con Anna alle 16: lei è occupata. Sposto solo la palestra alle 17. Va bene?",
        "La call con Anna alle 16 la lascio com'è. Sposto la palestra alle 17. Confermi?",
        "Ieri alle 16 avevi la call con Anna. Sposto la palestra alle 17. Ti va bene così?",
        # the verb said, but of the past or of what stays
        "Ieri avevi spostato la call con Anna alle 16. Sposto la palestra alle 17. Va bene?",
        "Sposto la palestra alle 17 e la call con Anna alle 16 la lascio com'è. Confermi?",
    ):
        assert not actions.says_change("calendar_move", call, said), said
    # Haiku's own still bind; i1's "non l'ho ancora messo" is pending, not refused
    assert actions.says_change("calendar_move", call, "Propongo call alle 16 e palestra alle 17, con Anna "
                                                      "avvisata. Confermi?")  # fmt: skip
    macro = {"title": "Macro", "when": "2026-10-31 00:00"}
    assert actions.says_change("calendar_create", macro, "Il Macro a mezzanotte non l'ho ancora messo: è "
                                                         "mezzanotte di stanotte. Lo aggiungo così?")  # fmt: skip


def test_his_words_decide_only_his_own_delay_and_his_whole_day() -> None:
    from pisa.domain import coverage

    assert coverage.no_choice("sono in ritardo di un'ora, sistemami il pomeriggio") is not None
    assert coverage.no_choice("I'm running an hour late, fix my afternoon") is not None
    for words in ("Sono in ritardo di mezz'ora, sposta la call con Anna",
                  "Anna è in ritardo di 20 minuti, sposta la call",
                  "il volo è in ritardo di due ore, sistemami la cena"):  # fmt: skip
        assert coverage.no_choice(words) is None, words


def test_a_hunch_never_states_a_fact_about_him() -> None:
    from pisa.domain import rituals

    assert rituals.hunch_question("Ti sei lasciato con Giulia. Il venerdì esci da solo?") == (
        "Il venerdì esci da solo?"
    )
    assert rituals.hunch_states("Ti sei lasciato con Giulia. Il venerdì esci da solo?")
    assert not rituals.hunch_states("Ti piace il sushi il venerdì?")
    assert not rituals.hunch_states("Il venerdì sera il sushi è la tua abitudine? Lo prendi sempre così?")


def test_a_widened_question_binds_only_pisa_s_own_act_never_an_infinitive() -> None:
    """A sentence before "Va bene?" binds a change only when it says Pisa's own
    act (first person) or Pisa's proposal; a modal, a regret, their request quoted back, or a change set aside never
    does."""
    from pisa.domain import actions

    paola = {"title": "Cena con Paola"}
    call = {"title": "Call con Anna", "when": "2026-10-30 16:00"}
    for kind, payload, said in (
        ("calendar_cancel", paola, "Potrei cancellare la cena con Paola, ma ti consiglio di no. Va bene?"),
        ("calendar_cancel", paola, "Non è il caso di cancellare la cena con Paola. Tengo tutto. Va bene?"),
        (
            "calendar_cancel",
            paola,
            "Senza cancellare la cena con Paola, sposto la palestra alle 17. Va bene?",
        ),
        ("calendar_move", call, "Avrei voluto spostare la call con Anna alle 16 ma non si può. Va bene?"),
        (
            "calendar_move",
            call,
            "Invece di spostare la call con Anna alle 16, sposto la palestra. Ti va bene così?",
        ),
        ("calendar_move", call, "Mi hai chiesto di spostare la call con Anna alle 16. Non posso. Va bene?"),
    ):
        assert not actions.says_change(kind, payload, said), said
    assert actions.says_change("calendar_cancel", paola, "Cancello la cena con Paola. Va bene?")
    assert actions.says_change("calendar_move", call, "Sposto la call con Anna alle 16. Va bene?")
