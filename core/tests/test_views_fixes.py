# ruff: noqa: E501
"""The review's fixes to Pisa's own views. One test a fix, each fails when its fix is
reverted."""

import asyncio
from typing import Any

import pytest

from pisa.domain import views as rules

# --- H1 (row 7): a retry is merged only after the talker's own `interrupted`, and always checked ------------


@pytest.mark.parametrize("case", ["interrupted_claim", "no_interruption", "owner_final_between"])
def test_a_retried_line_is_merged_only_after_interrupted_and_always_checked(case: str) -> None:
    from pisa.ports.talker import OwnerWords, PisaWords
    from tests.test_live_talk import _run, _until

    run, session, _, _ = _run()

    def lines() -> list[str]:
        return [e["text"] for e in run.timeline.events if e["kind"] == "pisa_said"]

    def checked() -> list[str]:
        return [e["said"] for e in run.timeline.events if e["kind"] == "grounding"]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        if case == "interrupted_claim":
            session.queue.put_nowait(PisaWords("Ho", final=True, interrupted=True))
            session.queue.put_nowait(PisaWords("Ho spostato il dentista alle 15.", final=True))
            await _until(lambda: checked())
        elif case == "no_interruption":
            session.queue.put_nowait(PisaWords("Hai tre cose:", final=True))
            session.queue.put_nowait(
                PisaWords("Hai tre cose: il dentista alle 9, la palestra alle 18.", final=True)
            )
            await _until(lambda: len(checked()) == 2)
        else:
            session.queue.put_nowait(PisaWords("Sono le", final=True, interrupted=True))
            session.queue.put_nowait(OwnerWords("che ore sono", final=True))
            session.queue.put_nowait(PisaWords("Sono le 19:59.", final=True))
            await _until(lambda: len(lines()) == 2)
        run.end("owner")
        await task

    asyncio.run(go())
    if case == "interrupted_claim":  # one line, and its false claim is caught (no change was made)
        assert lines() == ["Ho spostato il dentista alle 15."]
        assert checked() == ["Ho spostato il dentista alle 15."]
    elif case == "no_interruption":  # two lines, both checked
        assert lines() == ["Hai tre cose:", "Hai tre cose: il dentista alle 9, la palestra alle 18."]
        assert checked() == lines()  # "tre" and the times: each line judged on its own
    else:  # the user's final between: two turns, never merged
        assert lines() == ["Sono le", "Sono le 19:59."]


# --- the views: M2 (pages), M4 (find), L8 (non-finite), L9 (whole URLs), L10 (days), L11 (replace) -----------


def _views(client: Any, results: list[Any]) -> Any:
    from tests.test_views import _views as views_with  # pyright: ignore[reportPrivateUsage]

    return views_with(client, results)


async def _save(views: Any, name: str, recipe: dict[str, Any], said: str = "salvala") -> str:
    return await views.save(name, "", recipe, hosts={"ilmeteo.it", "evil.example"}, said=said, turn_id=None,
                            device_id=None)  # fmt: skip


def test_a_view_s_links_come_from_the_pages_it_read_never_from_any_string(client, db) -> None:  # type: ignore[no-untyped-def]
    page = "https://www.ilmeteo.it/roma"
    recipe = {"reads": [{"tool": "web_search", "args": {"query": "meteo roma"}},
                        {"tool": "read_page", "args": {"url": page}}],
              "blocks": [{"type": "link", "url": "{{0.results.0.url}}", "title": "{{0.results.0.title}}"},
                         {"type": "image", "url": "{{0.results.1.url}}", "alt": "pixel"},
                         {"type": "link", "url": "{{1.url}}", "title": "Il meteo"}]}  # fmt: skip
    found = {"results": [{"title": "Phish", "url": "https://evil.example/phish"},
                         {"title": "Pixel", "url": "https://evil.example/p.gif"}]}  # fmt: skip
    # The save checks today's reads first, then the view runs them again
    views = _views(client, [found, {"url": page, "text": "sole"}, found, {"url": page, "text": "sole"}])
    assert client.portal.call(_save, views, "meteo", recipe).startswith("Saved")
    _, data, dropped = client.portal.call(views.run, "meteo")
    assert [b.get("url") for b in data["blocks"]] == [page]  # the read page's own address only
    assert len([d for d in dropped if "not from a page read this turn" in d]) == 2


def test_a_view_is_found_by_its_whole_name_and_two_alike_are_asked(client, db) -> None:  # type: ignore[no-untyped-def]
    recipe = {"reads": [], "blocks": [{"type": "text", "text": "x"}]}
    views = _views(client, [])
    assert client.portal.call(_save, views, "a", recipe).startswith("Not saved")  # too short a name
    for name in ("meteo", "meteo roma", "spesa", "treni roma", "treni rome"):
        assert client.portal.call(_save, views, name, recipe).startswith("Saved")
    assert client.portal.call(views.run, "mostrami il meteo")[0] == "meteo"
    assert client.portal.call(views.run, "il meteo roma")[0] == "meteo roma"  # the longer name, whole
    assert client.portal.call(views.run, "fammi vedere la spesa")[0] == "spesa"
    assert client.portal.call(views.run, "metro")[1] is None  # never "meteo"
    assert client.portal.call(views.run, "la metro di oggi")[0].startswith("No view")
    asked, data, _ = client.portal.call(views.run, "treni rom")  # two alike: Pisa asks which
    assert data is None and '"treni roma"' in asked and '"treni rome"' in asked and "which" in asked


def test_a_non_finite_number_is_never_shown() -> None:
    from pisa.brain.present_tools import sheet_blocks

    recipe = rules.Recipe([{"tool": "get_weather", "args": {}}],
                          [{"type": "stat", "label": "Max", "value": "{{0.max}}"},
                           {"type": "text", "text": "Min {{0.min}}"},
                           {"type": "stat", "label": "Vento", "value": "{{0.wind}}"}])  # fmt: skip
    blocks, missing = rules.fill(recipe, [{"max": float("nan"), "min": float("inf"), "wind": 3}])
    assert blocks == [{"type": "stat", "label": "Vento", "value": 3}] and missing == 2
    (table,), _ = sheet_blocks(
        [{"type": "table", "columns": ["a", "b"], "rows": [[float("nan"), 2.5]]}],
        [],
        results=["2.5"],
        lang="it-IT",
    )
    assert table["rows"] == [["", 2.5]]


def test_read_page_takes_the_whole_address_the_owner_said() -> None:
    def recipe(url: str) -> dict[str, Any]:
        return {
            "reads": [{"tool": "read_page", "args": {"url": url}}],
            "blocks": [{"type": "text", "text": "x"}],
        }

    said = "salvala, la pagina è https://example.org/meteo/roma"
    assert isinstance(rules.validate(recipe("https://example.org/meteo/roma"), set(), said), rules.Recipe)
    assert isinstance(rules.validate(recipe("https://example.org/meteo"), set(), said), str)  # a piece of it
    assert isinstance(rules.validate(recipe("https://example.org/m"), set(), said), str)
    assert isinstance(rules.validate(recipe("https://example.org/meteo/roma/x"), set(), said), str)


def test_search_my_email_days_are_one_to_ninety() -> None:
    def days(value: int | str) -> Any:
        recipe = {"reads": [{"tool": "search_my_email", "args": {"query": "bonifico", "days": value}}],
                  "blocks": [{"type": "text", "text": "x"}]}  # fmt: skip
        checked = rules.validate(recipe, set())
        return checked.reads[0]["args"]["days"] if isinstance(checked, rules.Recipe) else checked

    assert (days(3650), days(0), days(-5), days(30), days("7")) == (90, 1, 1, 30, 7)


def test_a_view_of_the_same_name_is_replaced_only_when_he_says_so(client, db) -> None:  # type: ignore[no-untyped-def]
    one = {"reads": [], "blocks": [{"type": "text", "text": "uno"}]}
    two = {"reads": [], "blocks": [{"type": "text", "text": "due"}]}
    views = _views(client, [])
    assert client.portal.call(_save, views, "meteo", one).startswith("Saved")
    asked = client.portal.call(_save, views, "Meteo", two, "salvala come meteo")
    assert asked.startswith("Not saved") and "replace" in asked
    assert client.portal.call(views.run, "meteo")[1]["blocks"] == [{"type": "text", "text": "uno"}]
    assert client.portal.call(_save, views, "meteo", two, "salvala, sostituisci quella vecchia").startswith(
        "Saved"
    )
    assert client.portal.call(views.run, "meteo")[1]["blocks"] == [{"type": "text", "text": "due"}]


# --- M3 (row 2): set_task_start after outside text: their own command, never "alle" alone ---------------------


def test_a_start_after_outside_text_is_his_command_never_a_question_or_someone_else_s() -> None:
    from datetime import date

    from pisa.domain.actions import grounded_change

    today = date(2026, 10, 20)

    def at(hour: int, words: str) -> bool:
        start = {"when": f"2026-10-20T{hour}:00"}
        return grounded_change("set_task_start", start, words, today, title="Chiamare il notaio")

    for words in ("il notaio alle 18?", "non chiamare il notaio alle 18", "il notaio ha detto che alle 18 chiude",
                  "chiamare il notaio: l'email dice alle 18", "chiamare il notaio alle 18? no, alle 15",
                  "il notaio alle 18"):  # fmt: skip
        assert not at(18, words), words
    assert not at(15, "chiamare il notaio alle 18? no, alle 15")  # no verb of theirs: it holds for a yes
    assert at(18, "sposta il notaio alle 18") and at(18, "chiamo il notaio alle 18")
    assert at(15, "il notaio: lo faccio alle 15") and not at(15, "il notaio: non lo faccio alle 15")


# --- M5 (row 9): "by meaning" never swallows a real general ban -----------------------------------------------


def test_a_general_ban_stays_a_candidate_the_owner_s_by_meaning_rule_doesn_t() -> None:
    from pisa.domain.profile import no_email_tasks

    for ban in ("Never create tasks from any email", "Non creare task da nessuna email",
                "Non creare task dalle email, solo se te lo dico io",
                "Don't create tasks from email, I'll judge myself"):  # fmt: skip
        assert no_email_tasks(ban), ban
    assert not no_email_tasks(
        "Non trasformare ogni email in un task: valuta contenuto e semantica, e crea task per le email mirate "
        "che richiedono davvero un'azione."
    )
    assert not no_email_tasks("Non creare task da tutte le email, solo da quelle importanti")  # "not all"


# --- M6: the claim's clause, a shown thing as subject, never a lead word alone ---------------------------


@pytest.mark.parametrize(
    "reply, kept",
    [
        # (a) never a split inside "15:30"; "e" only before a clause with its own verb
        # The claim's clause goes and the answer stands, never a plumbing confession;
        # only a reply that was nothing but the claim says what happened
        ("Il dentista è alle 15:30, ti ho messo la mappa sul telefono.", "Il dentista è alle 15:30."),
        ("Ti ho messo il link e la mappa sul telefono.", "Non sono riuscito a mostrartelo sul telefono."),
        ("Te l'ho mandato sul telefono e chiude alle 19.", "Chiude alle 19."),
        # (b) è/sono only with a shown thing; another object is no screen; the new phrasings
        (
            "Il numero di Marco è nel telefono, nella rubrica.",
            "Il numero di Marco è nel telefono, nella rubrica.",
        ),
        ("Ho messo la sveglia sul telefono alle 7.", "Ho messo la sveglia sul telefono alle 7."),
        (
            "Ti ho mandato l'indirizzo sul telefono.",
            "Non sono riuscito a mostrartelo sul telefono.",
        ),  # to them
        ("La mappa è sul telefono.", "Non sono riuscito a mostrartelo sul telefono."),
        ("Sul telefono trovi la mappa.", "Non sono riuscito a mostrartelo sul telefono."),
        ("L'ho messa sul telefono.", "Non sono riuscito a mostrartelo sul telefono."),
        ("Guarda il telefono.", "Non sono riuscito a mostrartelo sul telefono."),
        ("Trovi tutto nell'app.", "Non sono riuscito a mostrartelo sul telefono."),
        # (c) a lead word alone is no answer
        ("Ecco, ti ho messo la mappa sul telefono.", "Non sono riuscito a mostrartelo sul telefono."),
        ("Ecco. Ti ho messo la mappa sul telefono.", "Non sono riuscito a mostrartelo sul telefono."),
    ],
)
def test_the_screen_claim_s_clause_and_never_a_lead_word_alone(reply: str, kept: str) -> None:
    from pisa.services.conversation import _screen_sentences, _with_screen_claims  # pyright: ignore[reportPrivateUsage]

    rest, claims = _screen_sentences(reply)
    assert _with_screen_claims(rest, claims, False, False, "it-IT") == kept


# --- M7 (row 8): an either/or that holds back is code's yes/no, a real either/or stays -----------------------


def test_an_either_or_that_holds_back_goes_a_real_one_stays() -> None:
    from pisa.services.conversation import _without_questions  # pyright: ignore[reportPrivateUsage]

    for held in ("Lo sposto alle 15 o preferisci di no?", "Lo sposto o lo lasci dov'è?",
                 "Lo sposto adesso o aspettiamo domani?", "Vuoi spostarlo o cambiare giorno?",
                 "Should I move it or leave it?", "Move it now or wait?"):  # fmt: skip
        assert _without_questions(f"Ok. {held}") == "Ok.", held
    for choice in ("Sabato o domenica?", "Alle 15 o alle 16?", "Lo sposti o lo cancelli?"):
        assert _without_questions(f"Ok. {choice}") == f"Ok. {choice}", choice


# --- L12: the replay's scripted model finds a rewritten turn by its recognised words ------------------------


def test_the_scripted_model_finds_a_turn_the_understanding_rewrote() -> None:
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from pisa.domain.understand import Result, marked
    from tests.replay.harness import script

    raw = "sposta il dentista alle 15"
    model = script({"turns": [{"owner": raw}], "script": [["Spostato alle 15."]]})
    rewritten = marked(Result(meant="sposta il dentista Rossi alle 15", corrections=[]), raw, "it-IT")

    async def first(words: str) -> Any:
        async for out in model([ModelRequest(parts=[UserPromptPart(content=words)])], None):
            return out

    assert asyncio.run(first(raw)) == "Spostato alle 15."
    assert asyncio.run(first(rewritten)) == "Spostato alle 15."  # never the "Va bene." past the script
