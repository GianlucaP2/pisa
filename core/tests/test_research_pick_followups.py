# ruff: noqa: E501
"""One row builder with core-sources-1
(in `test_research_pick.py`), depth run 3's misses and the comparison's round 2, and d08's quick scored by the
offline replay itself. The places are synthetic (Borgo Lieto, `tests/replay/depth_world.py`)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pisa.brain import findings
from pisa.brain.research_tools import WIDEN_FIRST, ShownPlace, _findings, _show, too_few  # pyright: ignore[reportPrivateUsage]
from pisa.brain.research_turn import WORKING, ResearchTurn
from pisa.domain import research as research_rules
from pisa.services.conversation import NO_ANSWER
from tests.replay import depth
from tests.replay.depth_compare import REWRITE
from tests.test_depth_eval import _play_depth  # pyright: ignore[reportPrivateUsage]
from tests.test_research_depth import Step, talk

ROME = ZoneInfo("Europe/Rome")
_MAP: list[list[Step]] = [
    [("map_search", {"query": "bar caffè", "near": "here", "provider": "google", "open_now": True})]
]
RUN3 = "run3-arm-a.txt"


def _scenario(sid: str) -> dict[str, Any]:
    return next(s for s in depth.SCENARIOS if s["id"] == sid)


# --- (d08) a quick one stays quick: one card; the replay judges it from what the code shows -----------------


def test_a_d08_e2e_a_quick_coffee_shows_one_card_never_a_table(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Depth run 3, d08 (2 of 3 runs): "Un caffè al volo qui" shown as a table of 3. The model's three
    places become the first one's card."""
    base = _scenario("d08-a-quick-coffee-stays-quick")
    picks = [(k, "aperto, qui accanto", []) for k in ("caffe-piazza", "bar-rondini", "bar-fulmine")]
    s = {**base, "id": "d08-three", "script": [[
        base["script"][0][0], [depth.card("Un caffè", "Un caffè al volo qui", picks)],
        "Il più vicino è Caffè della Piazza, aperto.",
    ]]}  # fmt: skip
    result = _play_depth(client, db, fake_models, secrets_key, clock, http_handler, s)
    assert [ok for _, c, ok, _ in result.checks if c == "quick"] == [True], depth.details(result)
    assert not result.hard, depth.details(result)
    assert "Caffè della Piazza" in result.shown[0] and "Bar delle Rondini" not in result.shown[0], (
        result.shown
    )


def test_a_quick_is_his_words_only() -> None:
    assert research_rules.quick("Un caffè al volo qui") and research_rules.quick("a quick coffee near here")
    assert not research_rules.quick("Dove faccio una buona colazione qui vicino?")


def _replay_run3(
    client: Any, db: Any, fake_models: Any, secrets_key: Any, clock: Any, http_handler: Any
) -> Any:
    from tests.replay import depth_real

    return depth_real, [
        depth_real.Replayed(rec, _play_depth(client, db, fake_models, secrets_key, clock, http_handler,
                                             depth_real.played(rec)))
        for rec in depth_real.parse(RUN3)
    ]  # fmt: skip


# --- (a) why for them now on the card, from their own data ------------------------------------------------------


def test_b_the_card_s_why_starts_with_his_situation() -> None:
    """Depth run 3, d14/d15: the card's why was a walk at most. Now code's reasons from their data come first:
    the minutes their next event leaves them, who is with them, the walk, what they like that the place has."""
    turn = ResearchTurn(lang="it-IT", prompt_chars=0)
    turn.places = {
        "g": {"name": "Gelateria Nuvola", "travel_min": 4, "open_now": True, "category": "gelateria"}
    }
    turn.now = datetime(2026, 11, 11, 16, 0, tzinfo=ROME)
    turn.event = ("Cinema", datetime(2026, 11, 11, 16, 40, tzinfo=ROME))
    turn.company = ["Giulia"]
    shown: list[dict[str, Any]] = []

    async def show(card: dict[str, Any], focus: str, purpose: str) -> str:
        shown.append(card)
        return "shown"

    turn.show = show
    import asyncio

    asyncio.run(
        _show(turn, "Gelato", "Il gelato più premiato", [ShownPlace(place_id="g", why="Cono d'oro 2026")])
    )
    why = shown[0]["places"][0]["why"]
    assert why.startswith("Ti lascia solo 36 minuti prima di Cinema alle 16:40"), why
    assert "Giulia" in why and "4 minuti" in why and why.endswith("ono d'oro 2026"), why
    assert findings.for_him(turn, "g", turn.places["g"], "", "it-IT", None)[0]  # the sheet's row: the same


# --- (a) d18: the general tool before "no"; (b) never an estimate by eye before a search ----------------------


def test_c_a_no_or_an_estimate_before_any_search_waits_for_one() -> None:
    w = "When's the next train to Selvamora?"
    for said in ("I don't have a train timetable for Selvamora.", "I can't check train times to Selvamora.",
                 "I couldn't get a train time for Selvamora.", "Check the operator's app for the time."):  # fmt: skip
        assert research_rules.unsearched(said, w, searched=False) is not None, said
        assert research_rules.unsearched(said, w, searched=True) is None, said
    by_eye = "A occhio ci vogliono 10-15 minuti."
    assert research_rules.unsearched(by_eye, "Quanto ci mette il caffè a scendere a 60 gradi?", False)
    assert research_rules.unsearched("At 10:12 from Castelmira Centrale.", w, False) is None
    # a "no" to a change, or on words that look nothing up, is no refusal for lack of a tool
    assert (
        research_rules.unsearched("Non posso spostarla senza il tuo sì.", "sposta la palestra", False) is None
    )


def test_c_d18_e2e_a_no_goes_back_for_a_web_search_and_the_answer_is_said(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Depth run 3, d18 (3 of 3 runs: "I don't have a train timetable", no search): the "no" waits; the model
    is sent back once to web_search; it finds the train and says it."""
    base = _scenario("d18-the-next-train")
    [search, read, answer] = base["script"][0]
    no = "I don't have a train timetable for Selvamora. My live feed only covers city buses and metro."
    s = {**base, "id": "d18-no", "script": [[
        {"hint": "you didn't look it up", "then": search, "else": no}, read, answer,
    ]]}  # fmt: skip
    result = _play_depth(client, db, fake_models, secrets_key, clock, http_handler, s)
    said = result.turns[0].reply
    assert "10:12" in said and "don't have" not in said and "feed" not in said, said
    assert all(ok for _, c, ok, _ in result.checks if c in ("sources", "short")), depth.details(result)


def test_c_e2e_a_no_the_search_round_left_unanswered_is_said_as_it_was(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The search round says nothing: his answer is the model's own honest "no", never silence."""
    no = "Non ho gli orari dei treni per Selvamora."
    t = talk(db, secrets_key, [("Prossimo treno per Selvamora?", [], no)], lang="it-IT")
    assert t.turns[0].reply.strip() == no, t.turns[0].reply
    assert len(t.turns[0].requests) == 2, t.turns[0].requests  # the answer, then the one search round


# --- (b) the text's first line is the pick, never the voice's filler -----------------------------------------


def test_d_the_filler_is_for_the_ear_never_in_the_text(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    final = "Ti consiglio il Bar Centrale, in piazza: aperto ora."
    t = talk(db, secrets_key, [("Un caffè al volo qui", _MAP, final)])
    turn = t.turns[0]
    assert turn.said[0] == WORKING["it-IT"], turn.said  # heard first
    assert not turn.reply.startswith(WORKING["it-IT"]) and WORKING["it-IT"] not in turn.reply, turn.reply
    assert turn.reply.startswith("Ti consiglio il Bar Centrale"), turn.reply


# --- (b) the claim rail's truth line goes back to the model on every turn that asked and made no change ------


def test_e_e2e_a_change_claimed_on_a_plain_turn_is_rewritten_never_the_rail_s_words(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Not a research turn ("cosa dice la mail di Marco?"): the model claims a move it never made. On main Pisa
    said the rail's "Non l'ho spostato: …"; now the claim goes back to the model, once, and its rewrite is
    said."""
    from tests.replay.scenarios import SCENARIOS, c
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    base = next(s for s in SCENARIOS if s["id"] == "b6-f-a-haiku-reads-the-email-and-does-not-move")
    then = "Marco ti chiede di spostare la palestra di domani alle 20: vuoi che lo faccia?"
    claimed = {"hint": REWRITE, "then": then, "else": "Ho spostato la palestra di domani alle 20."}
    s = {**base, "id": "plain-claimed", "script": [[[c("search_my_email", query="Marco")], claimed]]}
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, s)
    said = result.turns[-1].reply
    assert said.strip() == then, said
    assert "Non l'ho" not in said and "Ho spostato" not in said, said


# --- (a) d12, d20: enough candidates before the pick ---------------------------------------------------------


def test_f_a_pick_among_too_few_asks_once_for_more() -> None:
    import asyncio

    turn = ResearchTurn(lang="en-US", prompt_chars=0)
    turn.pick, turn.guides_asked = True, True
    turn.places = {"a": {"name": "Forno Rondine"}, "b": {"name": "Caffè della Piazza"}}
    assert too_few(turn) == 2
    said = asyncio.run(_findings(turn, "Breakfast", "best breakfast", [], "a", [], "take"))
    assert said == WIDEN_FIRST.format(n=2) and turn.pending is not None
    assert too_few(turn) is None  # once
    turn.widen_asked = False
    turn.places["c"] = {"name": "Latteria del Sole"}
    assert too_few(turn) is None  # three seen: enough


# --- (a) d15: a name said only as a result wrote it ---------------------------------------------------------


def test_g_a_name_no_result_wrote_is_found() -> None:
    known = research_rules.plain(
        "Gelateria Nuvola, Via Nuvola 29, Lucimbra. Cono d'oro 2026. Tavola Chiara Tre Lune"
    )
    assert research_rules.misnamed(
        "Gelateria Nuvola, in via Nuvola a Lucimbra: ha il Cono d'oro.", known
    ) == ["Nuvola a Lucimbra"]
    for ok in ("La più premiata è Gelateria Nuvola, in via Nuvola 29.", "Ha le Tre Lune di Tavola Chiara.",
               "The Gelateria Nuvola is open.", "Vai da Gelateria Nuvola a Lucimbra.",
               # a sentence's first word joined to one name is no name (the rail replay's b7-k22, voice6-01e)
               "Domani a Roma la massima è 16 °C.", "Controlla su Trenitalia o Italo quale arriva prima."):  # fmt: skip
        assert research_rules.misnamed(ok, known) == [], ok
    assert research_rules.misnamed("Forno Antico è qui dietro.", known) == ["Forno Antico"]
    assert research_rules.misnamed("Vai al Forno Antico.", known) == ["Forno Antico"]


def test_g_e2e_a_place_no_result_named_is_never_said(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    final = "Il più vicino è il Bar Centrale, in piazza. Poi c'è il Forno Antico, poco più in là."
    t = talk(db, secrets_key, [("Un caffè al volo qui", _MAP, final)])
    said = t.turns[0].reply
    assert "Bar Centrale" in said and "Forno Antico" not in said, said
    rewrites = [r for r in t.turns[0].requests if "each name as a result wrote it" in r["instructions"]]
    assert len(rewrites) == 1, t.turns[0].requests  # the one rewrite round


def test_g_e2e_only_names_no_result_wrote_is_an_honest_no_never_silence(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = talk(db, secrets_key, [("Un caffè al volo qui", _MAP, "Vai al Forno Antico, qui dietro.")])
    said = t.turns[0].reply
    assert "Forno Antico" not in said and said.strip(), said
    assert said.strip() == NO_ANSWER["it-IT"], said


def test_g_a_clause_with_an_unwritten_name_is_cut_when_the_rest_stands() -> None:
    """Depth run 3, d15 run 1: the address "in via Nuvola a Lucimbra" (no result wrote it) goes, the answer
    stays; a sentence left naming nothing a result wrote is "" (it goes back to the model)."""
    known = research_rules.plain("Gelateria Nuvola, Via Nuvola 29, Lucimbra. Cono d'oro 2026.")
    said = "Gelateria Nuvola, in via Nuvola a Lucimbra: ha il Cono d'oro 2026, e a 305 metri è aperta fino alle 23. "
    assert research_rules.without_misnamed(said, known) == (
        "Gelateria Nuvola: ha il Cono d'oro 2026, e a 305 metri è aperta fino alle 23. "
    )
    assert research_rules.without_misnamed("Poi c'è il Forno Antico, poco più in là.", known) == ""
    assert (
        research_rules.without_misnamed("Gelateria Nuvola è aperta.", known) == "Gelateria Nuvola è aperta."
    )


def test_g_e2e_a_clause_with_an_unwritten_name_goes_the_answer_stays(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = talk(
        db,
        secrets_key,
        [("Un caffè al volo qui", _MAP, "Il Bar Centrale, in via del Centro a Milano: aperto ora.")],
    )
    said = t.turns[0].reply
    assert said.strip() == "Il Bar Centrale: aperto ora.", said
    assert not [r for r in t.turns[0].requests if "each name as a result wrote it" in r["instructions"]]
