"""Every spoken answer of a research
conversation short, and the reason truly theirs. Target: depth run 2
(`depth-2026-11-10-agent-brain-2-arm-a.md`): 7 of its 8 hard fails a spoken answer over 60 words, mostly on a
follow-up. Every word here is synthetic."""

from datetime import UTC, datetime
from typing import Any

import pytest

import tests.research_world as research_world
from pisa.brain import findings
from pisa.brain.research_tools import guides_missing
from pisa.brain.research_turn import WORKING, ResearchTurn
from pisa.domain import research as research_rules
from pisa.domain import talk as form
from tests.replay import depth
from tests.replay.depth_observed import list_with_the_price_last as observed_list
from tests.replay.depth_observed import observed, observed_2
from tests.replay.depth_observed import on_the_phone_and_an_offer as observed_offer
from tests.replay.depth_observed import quick_at_length as observed_quick
from tests.replay.depth_world import PLACES, WEB
from tests.test_depth_eval import _play_depth  # pyright: ignore[reportPrivateUsage]
from tests.test_research_depth import BREAKFAST, BREAKFAST_ROUNDS, LONG_REPLY, Step, talk

NOW = datetime(2026, 11, 12, 19, 0, tzinfo=UTC)
COUNTED = frozenset(WORKING.values())


@pytest.fixture(autouse=True)
def _breakfast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research_world, "PLACES", BREAKFAST + research_world.PLACES)


def _turn(**more: Any) -> ResearchTurn:
    turn = ResearchTurn(lang=more.pop("lang", "it-IT"), prompt_chars=0)
    for k, v in more.items():
        setattr(turn, k, v)
    return turn


def _scenario(sid: str) -> dict[str, Any]:
    [s] = [x for x in depth.SCENARIOS if x["id"] == sid]
    return s


# --- 1. every spoken answer of a research conversation: two sentences, ≤ 35 words ---------------------------


def test_1_a_third_sentence_goes_on_the_screen_never_aloud() -> None:
    said = ["Tavola Chiara le dà le Tre Lune. ", "Il Faro la mette prima. "]
    assert research_rules.brief("E poi c'è anche il blog.", said) == ("", "E poi c'è anche il blog.")
    assert research_rules.brief("Il Faro la mette prima.", said[:1]) == ("Il Faro la mette prima.", "")


def test_1_the_interim_line_counts_in_words_never_as_a_sentence() -> None:
    said = ["Cerco, un attimo. ", "Costa 55 euro. "]
    assert research_rules.brief("Cinque portate.", said, COUNTED) == ("Cinque portate.", "")
    long = "Ha " + "un piatto buono e " * 7 + "un dolce."  # 31 words: past what is left of the 35
    assert research_rules.brief(long, said, COUNTED) == ("", " ".join(long.split()))


def test_1_a_first_sentence_too_long_is_cut_at_a_clause_the_rest_shown() -> None:
    one = (
        "Sì, il menu vegetariano costa 55 euro per cinque portate, con il risotto alle erbe di riva, lo "
        "sformato di zucca, i tortelli di ricotta, le verdure dell'orto alla brace e la crostata di more, e "
        "alla carta "
        "c'è anche la trota affumicata."
    )
    said, rest = research_rules.brief(one, [])
    assert said.startswith("Sì, il menu vegetariano costa 55 euro") and said.endswith(".")
    assert research_rules.spoken_words(said) <= research_rules.ANSWER_WORDS
    assert rest.endswith("trota affumicata.")
    for piece in (said[:-1], rest):  # cut between two clauses: each piece is the sentence's own words
        assert piece.strip() and piece.strip() in one


def test_1_a_list_or_an_address_is_never_read_aloud() -> None:
    assert research_rules.brief("- Brace Viva", []) == ("", "- Brace Viva")
    assert research_rules.brief("Il sito è https://braceviva.example/menu.", [])[0] == ""


def test_1_e2e_run_2_s_long_follow_ups_are_short_and_their_rest_is_on_the_phone(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """d04 as depth run 2's model played it: the follow-up ("Quali guide la citano?") answered, then ~50
    more words aloud. Pisa says two sentences within 35 words; the rest is a note on the phone."""
    s = _scenario("d04-pizza-which-guides-cite-it")
    result = _play_depth(client, db, fake_models, secrets_key, clock, http_handler, observed_2(s))
    said = result.turns[1].reply
    assert said.startswith("Tavola Chiara le dà le Tre Lune"), said
    assert 0 < research_rules.spoken_words(said) <= depth.SHORT_WORDS, said
    assert "convinto" not in said and "convinto" in result.shown[1], result.shown[1]
    assert not result.hard, result.hard
    assert ("follow-up", True) in [(c, ok) for _, c, ok, _ in result.checks], depth.details(result)


def test_1_e2e_the_observed_run_2_path_has_no_long_answer(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Every depth scenario with a follow-up, as run 2's model played it: on main (before this lane) each
    follow-up is a long text read aloud (a hard fail); here none is. The figures go into the hand-off."""
    played = [s for s in depth.SCENARIOS if len(s["script"]) > 1]
    results = [[_play_depth(client, db, fake_models, secrets_key, clock, http_handler, observed_2(s))]
               for s in played]  # fmt: skip
    text = depth.line(results)
    print("\n" + text)
    assert not [h for rs in results for r in rs for h in r.hard], text
    short = [ok for rs in results for r in rs for _, c, ok, _ in r.checks if c == "short"]
    assert all(short), text


WHY = (
    "Perché la guida lo premia per i cornetti. È a due passi da te. E poi il caffè lo tostano loro, ogni "
    "settimana"
)


def test_1_e2e_a_follow_up_s_last_words_are_cut_too(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The follow-up's last words (no full stop: the turn's end says them, not the stream) keep the rule."""
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("Perché proprio quello?", [], WHY),
    ])  # fmt: skip
    said = t.turns[1].reply.strip()
    assert said == "Perché la guida lo premia per i cornetti. È a due passi da te.", said
    assert any("tostano" in line for note in t.phone.of("note") for line in note["lines"]), t.phone.of("note")


def test_1_e2e_a_follow_up_from_pisa_s_findings_keeps_the_conversation_a_research_one(
    db, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    """Depth run 2 (d16, "I like the second better" after "why not the other?"): a follow-up answered from
    `my_findings` keeps the next one short too."""
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("E il secondo?", [[("my_findings", {})]], "Il Bar Centro Express è in piazza."),
        ("Perché proprio quello?", [], WHY + "."),
    ])  # fmt: skip
    said = t.turns[2].reply.strip()
    assert said == "Perché la guida lo premia per i cornetti. È a due passi da te.", said


def test_1_a_findings_answer_is_a_research_turn() -> None:
    from types import SimpleNamespace

    from pisa.services.conversation import _reply_kind  # pyright: ignore[reportPrivateUsage]

    assert _reply_kind(SimpleNamespace(tools=["my_findings"]), None) == "research"  # type: ignore[arg-type]
    assert _reply_kind(SimpleNamespace(tools=["agenda"]), None) == "tool"  # type: ignore[arg-type]


# --- 2. Pisa's own lead in English: two sentences
# --------------------------------------------------------------


def test_2_pisa_s_own_english_lead_is_two_sentences_the_phone_at_the_end() -> None:
    own = research_rules.own_lead(5, "Trattoria del Remo", ["6 minutes on foot", "open tonight"], "en-US")
    # The pick and their reasons, never a count; the others on the phone
    assert (
        own
        == "For you now, Trattoria del Remo: 6 minutes on foot, open tonight. The others are on your phone."
    )
    assert len(form.split_sentences(own)) == 2
    one = research_rules.own_lead(1, "Trattoria del Remo", [], "en-US")
    assert one == "For you now, Trattoria del Remo. The rest is on your phone."
    assert len(form.split_sentences(research_rules.own_lead(5, "Forno Antico", [], "it-IT"))) == 2


# --- 3. "why for you now" with the figures: the minutes left, who is with them, their tastes
# -------------------


def test_3_the_minutes_his_next_event_leaves_him() -> None:
    theatre = ("Teatro Vela", datetime(2026, 11, 12, 20, 0, tzinfo=UTC))
    assert research_rules.why_now(6, True, NOW, theatre, "it-IT")[0] == (
        "ti lascia 54 minuti prima di Teatro Vela alle 20:00"
    )
    later = ("Teatro Vela", datetime(2026, 11, 12, 21, 0, tzinfo=UTC))  # 114 left: no figure past 90
    assert research_rules.why_now(6, True, NOW, later, "en-US")[0] == "in time for Teatro Vela at 21:00"
    # an invite's or a sensitive title: Pisa's words
    secret = ("", datetime(2026, 11, 12, 19, 40, tzinfo=UTC))
    assert research_rules.why_now(10, None, NOW, secret, "it-IT")[0] == (
        "ti lascia solo 30 minuti prima del tuo prossimo impegno alle 19:40"
    )
    assert research_rules.why_now(10, None, NOW, secret, "en-US")[0] == (
        "it leaves you only 30 minutes before your next event at 19:40"
    )


def test_3_who_is_with_him_only_from_his_own_words() -> None:
    assert research_rules.company("Una cena con Giulia e Marco stasera") == ["Giulia", "Marco"]
    assert research_rules.company("Dinner with Anna before the theatre") == ["Anna"]
    assert research_rules.company("Un posto con vista, con calma") == []
    assert (
        research_rules.company("Un tavolo con Wifi e prese, dinner with Friends") == []
    )  # dictation's capitals
    assert research_rules.company("Somewhere to eat before the theatre") == []
    why = research_rules.why_now(6, None, NOW, None, "it-IT", company=["Giulia"])
    assert why == ["anche per Giulia", "a 6 minuti a piedi"]


def test_3_his_tastes_only_when_the_place_has_them() -> None:
    turn = _turn(
        taste=["cornetti integrali", "sushi"],
        places={"p1": {"name": "Forno Antico", "category": "bakery"}},
        reviews={"p1": "Cornetti integrali buonissimi, sfogliati a mano."},
    )
    assert turn.likes("p1") == ["cornetti integrali"]
    assert turn.likes("nope") == []
    why = research_rules.why_now(4, None, NOW, None, "it-IT", likes=turn.likes("p1"))
    assert why == ["a 4 minuti a piedi", "cornetti integrali, come piace a te"]


def test_3_the_row_s_why_carries_them() -> None:
    turn = _turn(
        lang="it-IT", now=NOW, event=("Teatro Vela", datetime(2026, 11, 12, 20, 0, tzinfo=UTC)),
        company=["Giulia"], taste=["cornetti"],
        places={"p1": {"name": "Forno Antico", "travel_min": 6, "open_now": True}},
        reviews={"p1": "I cornetti migliori della zona."},
    )  # fmt: skip
    row = findings._row(turn, "p1", turn.places["p1"], "", {"pros": [], "cons": []}, "it-IT")  # pyright: ignore[reportPrivateUsage]
    assert row["now"][:3] == [
        "ti lascia 54 minuti prima di Teatro Vela alle 20:00", "anche per Giulia", "a 6 minuti a piedi",
    ]  # fmt: skip
    assert "cornetti, come piace a te" in row["now"]


def test_3_e2e_who_is_with_him_is_on_the_sheet(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = talk(
        db, secrets_key, [("Dove posso fare colazione qui vicino con Giulia?", BREAKFAST_ROUNDS, LONG_REPLY)]
    )
    [sheet] = t.phone.of("sheet")
    assert "anche per giulia" in str(sheet).casefold(), sheet


def test_3_the_depth_tracer_knows_pisa_s_sums_on_his_own_data() -> None:
    """The figure is his calendar's less the walk a tool returned: traced, never invented."""
    from tests.replay.depth_world import Answer, DepthWorld

    s = _scenario("d19-dinner-before-the-theatre") | {"now": depth.THU + "20:00"}
    world = DepthWorld(s["area"])
    world.answered.append(Answer(0, "travel_time", {}, values=["Trattoria del Remo 6 min 0.4 km 6"]))
    sums = depth.own_arithmetic(s, world, 0).split()
    assert "60" in sums and "54" in sums
    assert depth.own_arithmetic(_scenario("d01-breakfast-the-guides-pick"), world, 0) == ""


def test_3_the_walk_s_metres_are_evidence_like_its_minutes() -> None:
    """The map's walk answer carries the metres too: saying them as given is traced, never invented."""
    import asyncio

    from tests.replay.depth_world import DepthWorld

    s = _scenario("d01-breakfast-the-guides-pick")
    world = DepthWorld(s["area"])
    out = asyncio.run(world.travel(PLACES[0].id, None, None, None))
    ev = depth.evidence(world, 0)
    metres, km = str(out["distance_m"]), f"{out['distance_m'] / 1000:.1f}"
    assert depth.numbers(f"{metres} m") <= ev.numbers
    assert depth.numbers(f"{km} km") <= ev.numbers


def test_3_e2e_the_theatre_s_minutes_on_the_sheet_traced(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """d19 an hour later (20:00, the theatre at 21:00): the why says the minutes it leaves them, and the
    depth's tracer finds no untraced figure."""
    s = _scenario("d19-dinner-before-the-theatre")
    later = observed(s | {"now": depth.THU + "20:00"})
    result = _play_depth(client, db, fake_models, secrets_key, clock, http_handler, later)
    assert "minutes before Teatro Vela at 21:00" in result.shown[0], result.shown[0]
    assert not [h for h in result.hard if "untraced" in h], result.hard


# --- 4. a guide is a guide ----------------------------------------------------------------------------------


def test_4_a_place_s_own_menu_page_is_no_guide() -> None:
    names = [p.name for p in PLACES]
    sites = [p.website for p in PLACES if p.website]
    for page in WEB:
        if page.kind in ("guide", "press", "award"):
            assert not research_rules.own_page(page.url, sites, names), page.url
        if page.kind == "menu":
            assert research_rules.own_page(page.url, sites, names), page.url
    assert research_rules.own_page("https://www.example.org/carta/", [], [])  # a menu path
    assert research_rules.own_page("https://site.example/x", ["https://site.example"], [])  # its website


def test_4_the_guides_check_counts_a_guide_never_a_menu() -> None:
    menu = "https://braceviva.example/menu"
    guide = "https://tavolachiara.example/castelmira"
    places = {"p1": {"name": "Pizzeria Brace Viva", "website": "https://braceviva.example"}}
    only_menu = _turn(pick=True, web_tried=True, places=places, read={menu}, urls={menu: "Menu", guide: "G"})
    assert "read_page" in str(guides_missing(only_menu))  # the menu read: the guide is still asked for
    no_guide = _turn(pick=True, web_tried=True, places=places, read={menu}, urls={menu: "Menu"})
    assert guides_missing(no_guide) is None  # searched: only the place's own page: nothing to ask
    other = "https://osterialanterna.example/menu"  # another place's menu among the results: no guide either
    places2 = places | {"p2": {"name": "Osteria Lanterna"}}
    menus = _turn(pick=True, web_tried=True, places=places2, read={menu}, urls={menu: "M", other: "M"})
    assert guides_missing(menus) is None
    read = _turn(pick=True, web_tried=True, places=places, read={menu, guide}, urls={menu: "M", guide: "G"})
    assert guides_missing(read) is None


def test_4_a_place_s_own_page_never_cites_it() -> None:
    menu = "https://braceviva.example/menu"
    guide = "https://tavolachiara.example/castelmira"
    turn = _turn(
        places={"p1": {"name": "Pizzeria Brace Viva"}},
        urls={menu: "Brace Viva: menu", guide: "Tavola Chiara"}, read={menu, guide},
        pages={menu: "Pizzeria Brace Viva: Margherita 8 euro.", guide: "Tre Lune: Pizzeria Brace Viva."},
    )  # fmt: skip
    assert [c["host"] for c in findings.cited_by("Pizzeria Brace Viva", turn)] == ["tavolachiara.example"]


# --- 5. the lead's sentence splitter -----------------------------------------------------------------------


def test_5_split_sentences_keeps_decimals_times_and_initials() -> None:
    text = "Il Caffè S. Eustachio ha 4.5 stelle. Apre alle 7.30 domani. Poi il resto."
    assert [s.strip() for s in form.split_sentences(text)] == [
        "Il Caffè S. Eustachio ha 4.5 stelle.", "Apre alle 7.30 domani.", "Poi il resto.",
    ]  # fmt: skip


# --- review: the cut never inside a figure, the stream's initials, their likes' words ---------


def test_r_a_clause_cut_never_splits_a_time_a_price_or_a_decimal() -> None:
    one = (
        "Ti consiglio Brace Viva, che apre alle 19:30 e chiude alle 23:00, perché la pizza è premiata "
        "dal Gambero Rosso con tre spicchi e il forno a legna è ottimo e il servizio è veloce e simpatico "
        "davvero."
    )
    said, rest = research_rules.brief(one, [])
    assert said == "Ti consiglio Brace Viva, che apre alle 19:30 e chiude alle 23:00.", said
    assert rest.startswith("perché la pizza") and rest in one, rest  # the screen's words exactly theirs
    long = (
        "Ha 4,5 stelle su 1200 recensioni e la guida la cita tra le migliori trattorie della città per la "
        "cucina di stagione e per i prezzi onesti che trovi raramente in centro storico qui."
    )
    said, _ = research_rules.brief(long, [])
    assert said.startswith("Ha 4,5 stelle"), said
    near = (  # the words run out right after "alle 19": the cut is at the comma before, never "alle 19."
        "Ti consiglio Brace Viva perché la pizza è premiata dal Gambero Rosso con tre spicchi e il forno a "
        "legna è ottimo e il servizio è veloce e simpatico davvero sempre, che apre alle 19:30 e chiude "
        "tardi, e costa poco."
    )
    said, rest = research_rules.brief(near, [])
    assert said.endswith("davvero sempre.") and rest.startswith("che apre alle 19:30"), (said, rest)
    for words in (said, research_rules.brief("Costa 8,50 euro, " + "e poi " * 20 + "basta.", [])[0]):
        assert not words.rstrip(".…").endswith(("4", "8", ",")), words


def test_r_an_initial_cut_by_the_stream_never_ends_the_answer() -> None:
    """The stream cuts after "Caffè S. " (a full stop and a space): the piece after it finishes that sentence,
    never a new one; Pisa never stops at "Ti direi Caffè S."."""
    said = ["Cerco, un attimo. ", "Ti direi Caffè S. "]
    piece = (
        "Eustachio, perché i cornetti sono i migliori della zona, il caffè è tostato da loro ogni "
        "settimana e il bancone è sempre pieno."
    )
    kept, rest = research_rules.brief(piece, said, COUNTED)
    assert kept.startswith("Eustachio, perché i cornetti"), (kept, rest)
    begun = ["Il Forno è buono. ", "Ma ti direi Caffè S. "]  # the second sentence, cut by the stream
    assert research_rules.brief("Eustachio, per i cornetti.", begun) == ("Eustachio, per i cornetti.", "")
    short = ["Cerco, un attimo. ", "Ti direi Caffè S. ", "Eustachio, per i cornetti. "]
    assert (
        research_rules.brief("Il Bar Centro è più vicino.", short, COUNTED)[0]
        == "Il Bar Centro è più vicino."
    )


def test_r_his_likes_are_profile_sentences_said_by_their_object() -> None:
    assert research_rules.liked("Ama i cornetti integrali") == "cornetti integrali"
    assert research_rules.liked("Gli piacciono i posti tranquilli") == "posti tranquilli"
    turn = _turn(
        taste=["Gli piacciono i cornetti integrali", "Ama il sushi"],
        places={"p1": {"name": "Forno Antico", "category": "bakery"}},
        reviews={"p1": "Cornetti integrali buonissimi."},
    )
    assert turn.likes("p1") == ["cornetti integrali"]


CLAIMED = (
    "Perché la guida lo premia per i cornetti integrali sfogliati a mano ogni mattina dal fornaio. È a due "
    "passi da te e apre presto anche la domenica mattina alle sette. Ti ho messo i dettagli sul telefono. E "
    "poi il caffè lo tostano loro."
)


def test_r_e2e_it_s_on_your_phone_stays_within_the_words(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("Perché proprio quello?", [], CLAIMED),
    ])  # fmt: skip
    said = t.turns[1].reply
    assert 0 < research_rules.spoken_words(said) <= research_rules.ANSWER_WORDS, said


HELD = (
    "Perché la guida lo premia per i cornetti. Te l'ho segnato. È a due passi da te. E il caffè lo tostano "
    "loro."
)


def test_r_e2e_a_held_sentence_counts_as_said(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("Perché proprio quello?", [], HELD),
    ])  # fmt: skip
    said = t.turns[1].reply
    assert "tostano" not in said and "due passi" not in said, said


def test_r_e2e_a_profile_question_never_lifts_the_rule(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Depth d16's shape: a follow-up that notes a taste Pisa must ask about (read after the kept findings:
    their yes first); the answer stays short and code's question is said whole after it."""
    note = {
        "kind": "like",
        "statement": "Ama i cornetti sfogliati",
        "quote": "mi piacciono i cornetti sfogliati",
    }
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("Perché il Forno? Comunque mi piacciono i cornetti sfogliati",
         [[("my_findings", {})], [("remember_about_me", note)]], WHY + "."),
    ])  # fmt: skip
    said = t.turns[1].reply
    assert "Vuoi che mi ricordi" in said, said  # code's question, whole
    assert "tostano" not in said, said


MENU = (
    "Il menu ha cinque portate. Te le dico in breve.\n- risotto alle erbe\n- sformato di zucca\n- tortelli\n"
    "- verdure alla brace\n- crostata di more\n- trota affumicata\n- caffè\nIn tutto costa 55 euro."
)


def test_r_e2e_a_streamed_list_never_pushes_the_price_off_the_note(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("E il menu?", [], MENU),
    ])  # fmt: skip
    [*_, note] = t.phone.of("note")
    assert any("55 euro" in line for line in note["lines"]), note


OUT_OF_ORDER = (
    "Perché la guida lo premia per i cornetti. Te lo spiego meglio: è un posto che ha convinto quasi tutti "
    "quelli che l'hanno provato, il servizio è attento, l'ambiente è accogliente e quello che spendi vale "
    "quello che ricevi. È a due passi da te. Apre presto."
)


def test_r_e2e_once_a_sentence_is_on_the_screen_what_follows_goes_there_too(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Depth run 2's d20 shape on the stub: a long second sentence went to the phone and a shorter third one
    was said after it, out of its order. The spoken answer is the answer's beginning, the rest follows it."""
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("Perché proprio quello?", [], OUT_OF_ORDER),
    ])  # fmt: skip
    said = t.turns[1].reply
    assert said.strip() == "Perché la guida lo premia per i cornetti.", said
    [note] = t.phone.of("note")
    assert " ".join(note["lines"]).endswith("È a due passi da te. Apre presto."), note


# --- The real model's own answers, replayed offline ----------------------


def test_a_web_address_in_brackets_is_left_for_the_screen_never_read_or_silencing() -> None:
    """Review round 2: the real answers wrote "(ilfaro.example/castelmira/pizzerie-2026)" (read aloud)
    and "(…, https://ilfaro.example/…)" (the whole answer silenced). An address in brackets is an aside the
    sources show: said without it. One outside brackets, a bare "host/path" too, leaves the sentence to the
    screen."""
    said, rest = research_rules.brief(
        "Il Faro la mette prima in classifica (ilfaro.example/castelmira/pizzerie-2026), e Tavola Chiara "
        "le dà le Tre Lune (https://tavolachiara.example/castelmira).",
        [],
    )
    assert said == "Il Faro la mette prima in classifica, e Tavola Chiara le dà le Tre Lune." and not rest
    assert research_rules.brief("Costa 12 euro (6 ridotto, alle 19:30).", [])[0] == (
        "Costa 12 euro (6 ridotto, alle 19:30)."
    )
    assert research_rules.brief("Vedi ilfaro.example/castelmira per i dettagli.", [])[0] == ""
    assert research_rules.brief("Il sito è https://braceviva.example/menu.", [])[0] == ""


SHAPES = {
    "list_with_the_price_last": observed_list,
    "on_the_phone_and_an_offer": observed_offer,
    "quick_at_length": observed_quick,
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_e2e_the_review_s_three_model_shapes_stay_short(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, shape
) -> None:
    """The review's "before paying" 3: a list with the price last, "it's on your phone" with an offer
    mid-answer, a quick fact in ~50 words. No hard fail, every turn short, the follow-up's cue said or
    shown."""
    played = [x for s in depth.SCENARIOS if (x := SHAPES[shape](s)) is not s]
    assert played, shape
    results = [[_play_depth(client, db, fake_models, secrets_key, clock, http_handler, s)] for s in played]
    text = depth.line(results) + "\n" + "\n".join(depth.details(r) for rs in results for r in rs)
    print("\n" + shape + ": " + text)
    assert not [h for rs in results for r in rs for h in r.hard], text
    for rs in results:
        for r in rs:
            assert all(ok for _, c, ok, _ in r.checks if c in ("short", "follow-up")), depth.details(r)
            for t, said in enumerate(r.turns, 1):
                if t > 1 or shape == "quick_at_length":
                    # two (the interim is the ear's only, never in the text)
                    assert len(form.split_sentences(said.reply)) <= 2, said.reply
    if shape == "list_with_the_price_last":
        assert all("55" in r.shown[1] for rs in results for r in rs), [r.shown for rs in results for r in rs]


# --- The ruled items and what the real answers showed ----------------------------


AGENDA_REPLY = (
    "Domani alle 10 hai il dentista. Alle 13 pranzi con Paolo. Alle 16 c'è la riunione. La sera sei libero."
)


def test_e2e_a_follow_up_that_reads_his_agenda_is_not_cut(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Review M1: right after a pick, a question about their day (their agenda, their tasks, their mail) is no
    research conversation's: said whole, nothing put on a note."""
    s = _scenario("d04-pizza-which-guides-cite-it")
    asked = "E domani che ho in agenda?"
    s = s | {
        "turns": [s["turns"][0], s["turns"][1] | {"owner": asked}],
        "script": [s["script"][0], [[("get_agenda", {"day": "tomorrow"})], AGENDA_REPLY]],
        "depth": [s["depth"][0], {}],
    }
    result = _play_depth(client, db, fake_models, secrets_key, clock, http_handler, s)
    assert "get_agenda" in [n for n, _ in result.turns[1].calls], result.turns[1].calls
    assert "La sera sei libero" in result.turns[1].reply, result.turns[1].reply
    assert "Dettagli" not in result.shown[1], result.shown[1]


def test_the_kept_request_is_his_own_words() -> None:
    """Review M2: the findings kept for the next turn hold their words as `request`, never the model's (nor a
    label: the service passes `research.said`)."""
    turn = _turn(findings={"request": "Cena con Marco (dalla pagina)", "shown": [], "seen": []})
    assert findings.snapshot(turn, "Dove ceniamo?")["request"] == "Dove ceniamo?"  # type: ignore[index]


def test_e2e_the_findings_kept_hold_his_words(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The model's `request` ("colazione qui vicino") is its own summary: the conversation keeps his."""
    talk(db, secrets_key, [("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY)])
    rows = db.execute("SELECT data->>'request' FROM research_findings").fetchall()
    assert rows and {r[0] for r in rows} == {"Dove posso fare colazione qui vicino?"}, rows


def test_e2e_a_tap_s_label_is_never_the_kept_request(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A tap's label is the model's text: "Colazione con Marco" on a chip never becomes their request,
    from which the next turn reads who is with them."""
    talk(
        db, secrets_key, [("[tap places:x] o1: Colazione con Marco qui vicino", BREAKFAST_ROUNDS, LONG_REPLY)]
    )
    rows = db.execute("SELECT data->>'request' FROM research_findings").fetchall()
    assert rows and not any("Marco" in (r[0] or "") for r in rows), rows


def test_a_quick_request_or_a_choice_to_buy_is_a_short_ask() -> None:
    asked = (
        "Un caffè al volo qui", "Una farmacia aperta adesso?", "A che ora apre domani il Forno Rondine?",
        "When's the next train to Selvamora?",
        "Che cuffie con cancellazione del rumore prendo sotto i 200 euro?",
        "Which headphones should I buy?", "Is there a pharmacy open now?",
    )  # fmt: skip
    for words in asked:
        assert research_rules.short_ask(words), words
    for words in ("Dov'è da Mario?", "Che ore sono?", "Che tempo fa domani?", "Aggiungi il dentista alle 10"):
        assert not research_rules.short_ask(words), words
    assert "get_agenda" not in research_rules.LOOKUPS and "send_email" not in research_rules.LOOKUPS


QUICK_LONG = (
    "Il più vicino è il Bar Centro Express, in piazza, aperto adesso. Se vuoi qualcosa di più curato c'è la "
    "Pasticceria Centro, poco più in là. Gli altri sono sul telefono, con le schede e le recensioni di tutti."
)


def test_e2e_a_quick_request_is_answered_short_the_rest_on_the_phone(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Depth run 2's d08 (40 words for "un caffè al volo"): two sentences said, the third on the phone."""
    rounds: list[list[Step]] = [
        [("map_search", {"query": "bar caffè", "near": "here", "provider": "google", "open_now": True})]
    ]
    t = talk(db, secrets_key, [("Un caffè al volo qui", rounds, QUICK_LONG)])
    said = t.turns[0].reply
    # A place this turn's map returned (a name no result wrote is never said)
    assert "Pasticceria Centro" in said and "recensioni" not in said, said
    assert any("recensioni" in line for note in t.phone.of("note") for line in note["lines"]), t.phone.of(
        "note"
    )


LONG_REST = (
    " ".join(
        f"Il piatto numero {n} è fatto con le verdure dell'orto e cotto piano, come si faceva una volta."
        for n in range(1, 16)
    )
    + " In tutto costa 55 euro."
)


def test_e2e_a_rest_longer_than_the_note_goes_on_a_sheet(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Review H4 (robust): past the note's six lines, the rest is a sheet of text blocks, never dropped."""
    t = talk(db, secrets_key, [
        ("Dove posso fare colazione qui vicino?", BREAKFAST_ROUNDS, LONG_REPLY),
        ("Spiegami il menu", [], LONG_REST),
    ])  # fmt: skip
    rest = [s for s in t.phone.of("sheet") if s.get("title") == "Dettagli"]
    assert rest, [s.get("title") for s in t.phone.of("sheet")]
    text = " ".join(b["text"] for b in rest[-1]["blocks"])
    assert text.endswith("In tutto costa 55 euro.") and "numero 15" in text, text
    assert research_rules.spoken_words(t.turns[1].reply) <= research_rules.ANSWER_WORDS, t.turns[1].reply


def test_a_full_stop_inside_a_word_or_before_a_comma_ends_no_sentence() -> None:
    """The real model's d14 (arm b): "… today at 3 p.m., four euros" was said as "… today at 3 p."."""
    one = "It has a kids' workshop today at 3 p.m., four euros, and children under 6 get in free."
    assert form.split_sentences(one + " Then go.") == [one + " ", "Then go."]
    assert form.split_sentences("Sul sito biglietteria.example costano 12 euro.") == [
        "Sul sito biglietteria.example costano 12 euro."
    ]
    said, _ = research_rules.brief(one, ["Museo del Colle is six minutes away. "])
    assert not said.rstrip().endswith(" p."), said


def test_the_screen_claim_of_a_question_is_a_statement() -> None:
    """ "Te l'ho messo sul telefono, vuoi che guardi anche gli orari?": the question is said, the claim (said
    only once the phone has it) ends with a full stop, never the question's mark."""
    from pisa.domain import claims

    kept, claim = claims.split_screen_claim("Te l'ho messo sul telefono, vuoi che guardi anche gli orari?")
    assert kept == "Vuoi che guardi anche gli orari?" and claim == "Te l'ho messo sul telefono.", (
        kept,
        claim,
    )
