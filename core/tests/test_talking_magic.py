# ruff: noqa: E501
"""Talking is magic, and Pisa Coder from the phone.
One test (or a few) per row; each fails when its fix is reverted (the handoff's table)."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pisa.domain import zone
from tests.conftest import pair
from tests.live_app import live
from tests.owner import owner_at
from tests.virtual_time import run_talk

NY = ZoneInfo("America/New_York")
ROME = ZoneInfo("Europe/Rome")


# --- row 4: Pisa follows the user's country ---------------------------------------------------------------


def test_the_phone_s_zone_comes_with_hello_and_device_context(client, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    with live(client, token, device_id, tz="America/New_York", region="US", hourCycle="h12") as app:
        assert db.execute("SELECT tz, region, hour_cycle FROM owner_context").fetchone() == (
            "America/New_York",
            "US",
            "h12",
        )
        app.custom(
            "pisa.device_context", {"tz": "Mars/Olympus"}
        )  # a zone Pisa can't load keeps the last known
        app.custom("pisa.device_context", {"region": "IT"})  # absent fields keep theirs
        assert app.ping() == []
        app.check()
    assert db.execute("SELECT tz, region, hour_cycle FROM owner_context").fetchone() == (
        "America/New_York",
        "IT",
        "h12",
    )
    assert client.app.state.runtime.tasks.tz == "America/New_York"  # every service's zone follows


def test_a_day_that_starts_in_rome_and_ends_in_new_york(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """The user flies on 3 Nov: the all-day event and the task due "3 Nov" stay on their 3 Nov in New York,
    never on Rome's (before the fix the task was overdue at 18:00 in New York and the event moved to the 2nd)."""
    from tests.assistant_env import add_event, add_task

    runtime = client.app.state.runtime
    task = add_task(db, "Pagare il bollo", date(2026, 11, 3))
    add_event(db, "Compleanno di Anna", datetime(2026, 11, 3, tzinfo=ROME), all_day=True)
    add_event(db, "Call con Milano", datetime(2026, 11, 3, 15, 0, tzinfo=ROME))  # an instant: stays one
    clock.now = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)  # 18:30 in New York, 00:30 of the 4th in Rome
    client.portal.call(
        lambda: runtime.zone.update(
            tz="America/New_York", region="US", hour_cycle=None, device_id=None, now=clock.now
        )
    )
    due = db.execute("SELECT due_at FROM tasks WHERE id = %s", (task,)).fetchone()[0]
    assert due.astimezone(NY) == datetime(2026, 11, 3, 23, 59, tzinfo=NY)
    agenda = client.portal.call(runtime.tasks.agenda)
    today = agenda["days"][0]
    assert today["date"] == "2026-11-03" and agenda["timezone"] == "America/New_York"
    titles = [e["title"] for e in today["events"]]
    assert "Compleanno di Anna" in titles
    call = next(e for e in today["events"] if e["title"] == "Call con Milano")
    assert call["start"] == "2026-11-03T09:00:00-05:00"  # its own instant, shown where they are
    assert [t["title"] for t in today["tasks_due"]] == ["Pagare il bollo"]
    second = client.portal.call(lambda: runtime.tasks.agenda(date(2026, 11, 2), 1))
    assert not [e for e in second["days"][0]["events"] if e["title"] == "Compleanno di Anna"]


def test_quiet_hours_and_the_briefing_hour_are_his_where_he_is() -> None:
    from pisa.domain.settings import briefing_time_on, is_quiet, with_defaults

    settings = with_defaults(
        None, language="it-IT"
    )  # quiet 22:30-07:00, briefing 07:30 (their wall clock, wherever they are)
    at = datetime(2026, 11, 4, 2, 0, tzinfo=UTC)  # 03:00 in Rome, 21:00 in New York
    owner = owner_at("Europe/Rome")
    owner.place = zone.PhoneContext("America/New_York")
    assert not is_quiet(at, settings, owner.tz)
    owner.place = None
    assert is_quiet(at, settings, owner.tz)
    owner.place = zone.PhoneContext("America/New_York")
    when = briefing_time_on(date(2026, 11, 4), settings, owner.tz)
    assert when.tzinfo is not None and when.astimezone(NY).date() == date(2026, 11, 4)
    assert when.astimezone(NY).strftime("%H:%M") == settings["briefing"]["time"]


def test_holidays_of_his_country_and_times_as_he_reads_them() -> None:
    from pisa.domain.holidays import easter, holiday

    assert easter(2026) == date(2026, 4, 5) and holiday(date(2026, 4, 6), "IT") == "Pasquetta"
    assert holiday(date(2026, 11, 26), "US") == "Thanksgiving" and holiday(date(2026, 11, 26), "IT") is None
    assert (
        holiday(date(2026, 5, 25), "US") == "Memorial Day"
        and holiday(date(2026, 11, 1), "IT") == "Ognissanti"
    )
    at = datetime(2026, 11, 26, 20, 5, tzinfo=UTC)
    assert zone.now_line(at, "America/New_York", "US", "h12") == (
        "Thursday 2026-11-26 3:05 PM America/New_York · holiday in US: Thanksgiving"
    )
    assert zone.now_line(at, "Europe/Rome", "IT", "h23") == "Thursday 2026-11-26 21:05 Europe/Rome"


def test_get_current_time_is_where_the_phone_is(client, db, fake_models, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.test_brain_loop import Script, play

    clock.now = datetime(2026, 11, 26, 20, 5, tzinfo=UTC)
    client.app.state.runtime.owner.place = zone.PhoneContext("America/New_York", "US", "h12")
    model = Script({"che ore sono": [[("get_current_time", {})]]})
    fake_models.default = model
    play(client, db, secrets_key, model, "che ore sono")
    assert any("3:05 PM America/New_York" in r and "Thanksgiving" in r for r in _returns(model))


def _returns(model: Any) -> list[str]:
    from pydantic_ai.messages import ToolReturnPart

    return [
        str(p.content)
        for sent in model.messages
        for m in sent
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]


def test_their_time_when_he_writes_home_in_their_night(client, db, google, auth, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.test_approvals import run

    db.execute("INSERT INTO email_correspondents (address, last_at) VALUES ('mamma@casa.example', now())")
    clock.now = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)  # 17:00 in New York, 23:00 in Rome
    client.app.state.runtime.owner.place = zone.PhoneContext("America/New_York", "US", "h23")
    said = run(
        client,
        client.app.state.runtime.actions.draft_email,
        "mamma@casa.example",
        "Ciao",
        "Ciao mamma.",
        said="scrivi alla mamma",
    )
    assert "It is 23:00 at home" in said
    client.app.state.runtime.owner.place = None  # at home: nothing to say
    said = run(
        client,
        client.app.state.runtime.actions.draft_email,
        "mamma@casa.example",
        "Ciao",
        "Ciao mamma.",
        said="scrivi alla mamma",
    )
    assert "at home" not in said


from tests.test_approvals import auth, google  # noqa: E402,F401  (fixtures)


@pytest.mark.parametrize(
    "tz, ok", [("America/New_York", True), ("Mars/Olympus", False), ("Europe/Rome", True)]
)
def test_only_a_zone_pisa_knows_is_taken(tz: str, ok: bool) -> None:
    assert zone.loadable(tz) is ok


# --- row 13 (carried from the views review)---------------------------------------------------------------


def test_the_shared_state_merges_a_growing_line_only_when_retried() -> None:
    from pisa.domain.state import ConversationState

    now = datetime(2026, 11, 3, 19, 59, tzinfo=UTC)
    state = ConversationState()
    state.pisa_said("Sono le", "voice", now)
    state.pisa_said(
        "Sono le 19:59, e domani piove.", "voice", now
    )  # a new line that happens to grow the last
    assert [s.text for s in state.said] == ["Sono le", "Sono le 19:59, e domani piove."]
    state.pisa_said("Ok", "voice", now)
    state.pisa_said(
        "Ok, ci penso io.", "voice", now, retried=True
    )  # the talker's retry of its interrupted line
    assert [s.text for s in state.said][-1:] == ["Ok, ci penso io."] and len(state.said) == 3


def test_the_talk_tells_the_shared_state_which_line_was_a_retry() -> None:
    import asyncio

    from pisa.ports.talker import PisaWords
    from tests.test_live_talk import _run, _until

    run, session, _, _ = _run()
    said: list[tuple[str, bool]] = []
    run.hooks.said = lambda text, backed, retried: said.append((text, retried))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(PisaWords("Sono le", final=True, interrupted=True))
        session.queue.put_nowait(PisaWords("Sono le 19:59.", final=True))
        await _until(lambda: len(said) >= 2)
        session.queue.put_nowait(PisaWords("Sono le 19:59. E poi?", final=True))  # grows, but no interruption
        await _until(lambda: len(said) >= 3)
        run.end("owner")
        await task

    run_talk(go())
    assert said == [("Sono le", False), ("Sono le 19:59.", True), ("Sono le 19:59. E poi?", False)]


@pytest.mark.parametrize(
    ("url", "said", "ok"),
    [
        ("https://a.example/menu", "leggi https://a.example/menu", True),
        ("https://a.example/menu?lang=it", "leggi https://a.example/menu?lang=it", True),
        ("https://a.example/menu?to=evil.example", "leggi https://a.example/menu", False),
        ("https://a.example/menu", "leggi https://a.example/menu?lang=it", False),
    ],
)
def test_said_page_compares_the_query_too(url: str, said: str, ok: bool) -> None:
    from pisa.domain.views import said_page

    assert said_page(url, said) is ok


@pytest.mark.parametrize(
    ("words", "ok"),
    [
        ("sì sostituiscila, non mi serve più", True),
        ("replace it, I don't need the old one", True),
        ("sostituiscila", True),
        ("non sostituirla", False),
        ("no, non la sostituire", False),
        ("don't replace it", False),
        ("non voglio sostituirla", False),
    ],
)
def test_asks_to_replace_ignores_a_negation_about_something_else(words: str, ok: bool) -> None:
    from pisa.domain.views import asks_to_replace

    assert asks_to_replace(words) is ok


# --- row 5: the clean-turn rate -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "now", "redone"),
    [
        ("metti la palestra domani alle 10", "no aspetta, intendevo sabato", True),
        ("add dinner tomorrow at 8", "I meant Saturday", True),
        ("sposta la cena alle 21", "sposta la cena alle 21.", True),  # said again
        ("sposta la cena alle 21", "e la palestra domani?", False),
        ("sì", "sì", False),  # a short word twice is no repeat
        ("cosa ho domani", "no grazie, a posto così", False),
    ],
)
def test_his_next_words_correct_or_repeat_the_turn(before: str, now: str, redone: bool) -> None:
    from pisa.domain import clean

    assert clean.redone(before, now) is redone


def test_the_rate_counts_turns_done_backed_and_not_redone() -> None:
    from pisa.domain import clean

    assert clean.rate([(0, 0, False), (1, 0, False), (0, 1, False), (0, 0, True), (0, 0, False)]) == (2, 5)


def test_a_talk_exchange_carries_the_brain_s_record_and_is_marked_redone() -> None:
    import asyncio

    from pisa.ports.talker import OwnerSpeaking, OwnerWords
    from pisa.services.talk import Kind, Thought
    from tests.test_live_talk import _run, _until

    run, session, _, seen = _run(
        Thought("Non l'ho spostato: non ci sono riuscito.", Kind.SAY, failed=1, unbacked=1)
    )
    redone: list[int] = []

    async def mark(n: int) -> None:
        redone.append(n)

    run.hooks.redone = mark

    async def go() -> None:
        task = asyncio.create_task(run.run())
        for words in ("sposta la cena alle 21", "sposta la cena alle 21 dai"):
            session.queue.put_nowait(OwnerSpeaking(True))
            session.queue.put_nowait(OwnerSpeaking(False))
            session.queue.put_nowait(OwnerWords(words, final=True))
            await _until(lambda: run._thought_for == run._finals and run._finals > 0)  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    exchanges = [e for kind, e in seen if kind == "exchange"]
    assert exchanges[0].failed == 1 and exchanges[0].unbacked == 1
    assert redone == [exchanges[0].n]  # their repeat marks the turn before, not this one
    assert any(e["kind"] == "redone" for e in run.timeline.events)


def test_the_turn_s_record_reaches_the_talk_and_talk_turns(
    client, db, fake_models, secrets_key, clock
) -> None:  # type: ignore[no-untyped-def]
    """A tool that says the change wasn't done counts as failed; the claim on top of it is rewritten and
    counted unbacked; `talk_turns` keeps both and `redone`."""
    import uuid

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall

    from pisa.services.talk import Exchange
    from tests.conftest import set_key

    set_key(db, secrets_key)
    device_id, _ = pair(db)

    async def model(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        if len(messages) == 1:
            yield {
                0: DeltaToolCall(
                    name="move_event",
                    json_args=json.dumps(
                        {"event": "cena", "start": "domani alle 21", "owner_said": "sposta la cena alle 21"}
                    ),
                )
            }
        else:
            yield "Fatto, l'ho spostata."

    fake_models.default = model
    runtime = client.app.state.runtime
    device = uuid.UUID(device_id)

    async def emit(delta: str, final: bool) -> None:
        return None

    reply = client.portal.call(
        lambda: runtime.conversations.respond(
            device, "sposta la cena alle 21", "it-IT", emit, trace_id="t-row5"
        )
    )
    assert reply == "Non l'ho spostata: non ci sono riuscito."
    failed, unbacked = runtime.conversations.record_of(device, "t-row5")
    assert failed == 1 and unbacked >= 1
    assert runtime.conversations.record_of(device, "another") == (0, 0)

    session_id = uuid.uuid4()
    db.execute(
        "INSERT INTO talk_sessions (id, device_id, talker, voice, trigger, lang, started_at) "
        "VALUES (%s, %s, 'gemini', 'v', 'press', 'it-IT', now())",
        (session_id, device_id),
    )
    e = Exchange(1, datetime.now(UTC), 0.0, final_at=1.0, failed=failed, unbacked=unbacked)
    client.portal.call(runtime.talk._store_exchange, session_id, e)  # pyright: ignore[reportPrivateUsage]
    client.portal.call(runtime.talk._store_redone, session_id, 1)  # pyright: ignore[reportPrivateUsage]
    assert db.execute("SELECT failed, unbacked, redone FROM talk_turns").fetchone() == (
        failed,
        unbacked,
        True,
    )


# --- row 14: a real-model replay's findings ----------------------------------------------------------------------


def test_with_no_location_the_weather_is_the_owner_s_home_city(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    from tests.test_learning import place
    from tests.weather_fake import FakeMeteoAPI

    http_handler["handler"] = FakeMeteoAPI(clock.now.astimezone(ROME).date())
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    assert (
        client.get("/v1/weather", headers=headers, params={"near": "here"}).status_code == 409
    )  # no home either
    casa = place(db, "Casa", (41.88, 12.47), "Via del Marmo Finto 1, 00153 Roma, Italy")
    assert client.put("/v1/profile/home", headers=headers, json={"place_id": casa}).status_code == 200
    here = client.get("/v1/weather", headers=headers, params={"near": "here"})
    assert here.status_code == 200, here.text
    assert here.json()["label"] in ("Casa", "Roma")  # the home's city (or its name when no provider can say)


def test_a_done_claim_about_the_turn_before_is_backed_by_its_record() -> None:
    from pisa.domain.claims import Record, backed, unbacked

    yes = "Sì, l'ho aggiunta ieri sera."
    added = frozenset({"create_event"})
    assert unbacked(yes, "it-IT", Record()) == [yes]  # nothing on record: unbacked
    assert unbacked(yes, "it-IT", Record(lately=added)) == []  # the recent turns' action log backs it
    assert unbacked("Li sposto al 30.", "it-IT", Record(lately=frozenset({"move_event"}))) == [
        "Li sposto al 30."
    ]
    assert backed("Li sposto al 30.", "it-IT", Record(done=frozenset({"move_event"})))
    assert backed("Ecco il meteo.", "it-IT", Record())  # no claim


def test_the_replay_reads_the_earlier_turns_writes() -> None:

    from tests.replay import harness

    turns = [
        harness.TurnLog(
            "add dinner with Sara tomorrow at 8", "Done: dinner with Sara tomorrow at 8.", False, wrote=True
        ),
        harness.TurnLog("did you actually add it?", "Yes, I added it.", False),
    ]

    class Conn:
        async def execute(self, *_: Any) -> "Conn":
            return self

        async def fetchall(self) -> list[Any]:
            return []

        async def fetchone(self) -> tuple[int]:
            return (0,)

    class Google:
        def __init__(self) -> None:
            self.events: dict[str, Any] = {}
            self.sent: list[Any] = []

    scenario = {"id": "x", "lang": "en-US", "expect": {}, "max_questions": 0}
    result = run_talk(harness.score(Conn(), Google(), scenario, turns))  # type: ignore[arg-type]
    assert result.false_claims == [] and result.passed
    turns[0].wrote = False  # nothing written before: the same "yes, I added it" is false
    result = run_talk(harness.score(Conn(), Google(), scenario, turns))  # type: ignore[arg-type]
    assert result.false_claims and not result.passed


def test_his_correction_of_the_day_grounds_the_replacement_with_the_turn_before() -> None:
    from pisa.domain.actions import Change, owner_said

    friday = date(2026, 10, 30)
    saturday = datetime(2026, 10, 31, 10, 0)
    before = "metti tutto sul calendario: domani palestra alle 10, pranzo con Nino alle 13 e dentista alle 17"
    gym = Change("calendar_create", "Palestra", saturday, None, "intendevo sabato")
    kw: dict[str, Any] = {"default_day": friday, "today": friday, "small_hours": True}
    assert owner_said(gym, "no aspetta, intendevo sabato", **kw) is not None  # alone: no time, held
    assert owner_said(gym, "no aspetta, intendevo sabato", earlier=before, **kw) is None
    lunch = Change(
        "calendar_create", "Pranzo con Nino", datetime(2026, 10, 31, 13, 0), None, "intendevo sabato"
    )
    assert owner_said(lunch, "no aspetta, intendevo sabato", earlier=before, **kw) is None
    wrong_time = Change(
        "calendar_create", "Palestra", datetime(2026, 10, 31, 11, 0), None, "intendevo sabato"
    )
    assert owner_said(wrong_time, "no aspetta, intendevo sabato", earlier=before, **kw) is not None
    wrong_day = Change("calendar_create", "Palestra", datetime(2026, 11, 1, 10, 0), None, "intendevo sabato")
    assert owner_said(wrong_day, "no aspetta, intendevo sabato", earlier=before, **kw) is not None
    nobody = Change("calendar_create", "Cena", saturday, None, "intendevo sabato")  # never said before
    assert owner_said(nobody, "no aspetta, intendevo sabato", earlier=before, **kw) is not None


# --- row 6: Pisa's voice bible -------------------------------------------------------------------------------


def test_the_talker_and_the_brain_both_cite_the_voice_bible() -> None:
    from pisa.brain.conversation import system_prompt
    from pisa.domain import bible
    from pisa.domain.whisper import Fresh, instructions

    rules = bible.rules(lang="it-IT")
    assert (
        "First person, always" in rules and '"I got that wrong" first' in rules and "come assistente" in rules
    )
    assert rules in system_prompt(lang="it-IT")
    assert rules in instructions(Fresh("adesso"), "", "it-IT", pisa_lang="it-IT")


@pytest.mark.parametrize(
    ("reply", "third"),
    [
        ("Pisa ci sta ancora lavorando.", True),
        ("Pisa ha spostato la cena alle 21.", True),
        ("Come assistente, non posso farlo.", True),
        ("As an AI, I can't book it.", True),
        ("Ho spostato la cena alle 21.", False),
        ("Ho sbagliato: la cena è alle 21.", False),
        ("Il J-pass è scaduto.", False),
    ],
)
def test_the_replay_finds_pisa_speaking_of_itself_in_the_third_person(reply: str, third: bool) -> None:

    from pisa.domain import bible
    from tests.replay import harness

    assert bool(bible.third_person(reply)) is third

    class Conn:
        async def execute(self, *_: Any) -> "Conn":
            return self

        async def fetchall(self) -> list[Any]:
            return []

        async def fetchone(self) -> tuple[int]:
            return (0,)

    class Google:
        def __init__(self) -> None:
            self.events: dict[str, Any] = {}
            self.sent: list[Any] = []

    turns = [harness.TurnLog("a che punto sei?", reply, False)]
    result = run_talk(harness.score(Conn(), Google(), {"id": "x", "expect": {}}, turns))  # type: ignore[arg-type]
    assert any("first person" in p for p in result.problems) is third


# --- row 1: heard in 1 s ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "lang", "line"),
    [
        (
            "trovami una pizza al taglio da guida, non la più vicina",
            "it-IT",
            "Pizza al taglio da guida, non la più vicina… cerco.",
        ),
        (
            "consigliami un ristorante vegano aperto adesso vicino a me",
            "it-IT",
            "Ristorante vegano aperto adesso vicino a te… cerco.",
        ),
        ("dove posso mangiare senza glutine stasera?", "it-IT", "Mangiare senza glutine stasera… cerco."),
        (
            "find me a quiet café with outlets near me, not a chain",
            "en-US",
            "Quiet café with outlets near you, not a chain… on it.",
        ),
        ("trova un bar", "it-IT", None),  # nothing unusual to say back
        ("cosa ho domani?", "it-IT", None),  # no search: the reflex or the brain answers it
        ("sposta la cena alle 21", "it-IT", None),  # a change is never said back as being done
    ],
)
def test_the_unusual_part_of_a_search_is_said_back(words: str, lang: str, line: str | None) -> None:
    from pisa.domain.claims import claim
    from pisa.domain.restate import heard

    assert heard(words, lang) == line
    assert line is None or claim(line) is None  # never a claim


def test_the_talk_says_it_back_first_and_measures_it_as_first_content() -> None:
    import asyncio

    from pisa.ports.talker import OwnerSpeaking, OwnerWords
    from tests.test_live_talk import _run, _until

    run, session, _, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(
            OwnerWords("trovami una pizza al taglio da guida, non la più vicina", final=True)
        )
        await _until(lambda: run._thought_for == 1)  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    first = session.whispers[0]
    assert first.id == "heard1" and first.text == "Pizza al taglio da guida, non la più vicina… cerco."
    (exchange,) = [e for kind, e in seen if kind == "exchange"]
    assert exchange.first_content_by == "heard" and exchange.first_content_ms is not None


# --- row 2: ready answers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "topic"),
    [
        ("cosa ho dopo?", "next"),
        ("what's next?", "next"),
        ("quando devo uscire?", "next"),
        ("cosa hai cambiato oggi?", "changes"),
        ("cosa dovrei fare adesso?", "pool"),
        ("what should I work on?", "pool"),
        ("a cosa stai lavorando?", "research"),
        ("sposta quello che ho dopo alle 18", None),  # a change: never a ready answer
        ("che tempo fa?", None),
    ],
)
def test_the_likely_questions_and_never_a_change(words: str, topic: str | None) -> None:
    from pisa.domain.ready import topic_of

    assert topic_of(words) == topic


def test_a_ready_answer_is_stale_once_pisa_s_data_changed_or_after_five_minutes() -> None:
    from datetime import timedelta

    from pisa.domain.ready import Answer, judge

    at = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    answer = Answer("pool", "In cima alla lista: Camicie.", at, ("v1",))
    assert judge(answer, at + timedelta(minutes=2), ("v1",)).hit
    assert not judge(answer, at + timedelta(minutes=2), ("v2",)).hit  # something changed since
    assert not judge(answer, at + timedelta(minutes=6), ("v1",)).hit  # older than 5 minutes
    assert judge(answer, at + timedelta(seconds=90), ("v1",)).age_s == 90


def test_the_service_answers_from_pisa_s_own_data_and_finds_a_change(client, db, clock) -> None:  # type: ignore[no-untyped-def]

    def task(title: str, source: str, priority: int) -> None:
        db.execute(
            "INSERT INTO tasks (id, title, title_key, status, source_kind, priority, created_at, updated_at) "
            "VALUES (gen_random_uuid(), %s, %s, 'open', %s, %s, now(), now())",
            (title, title.casefold(), source, priority),
        )

    task("Ritirare le camicie", "manual", 1)
    task("Pagare la bolletta (dall'email)", "email", 1)  # outside text: never in a ready answer
    ready = client.app.state.runtime.ready
    assert client.portal.call(ready.match, "cosa dovrei fare?", "it-IT") is None  # none ready yet: building
    client.portal.call(ready.wait)
    hit = client.portal.call(ready.match, "cosa dovrei fare?", "it-IT")
    assert hit is not None and hit.hit and hit.text == "In cima alla lista: Ritirare le camicie."
    task("Chiamare Nino", "voice", 1)  # Pisa's data changed since
    stale = client.portal.call(ready.match, "cosa dovrei fare?", "it-IT")
    assert stale is not None and not stale.hit
    client.portal.call(ready.wait)
    again = client.portal.call(ready.match, "cosa dovrei fare?", "it-IT")
    assert again is not None and again.hit and "Chiamare Nino" in again.text
    assert client.portal.call(ready.match, "sposta la cena", "it-IT") is None


@pytest.mark.parametrize("fresh", [True, False])
def test_the_talk_says_a_ready_answer_at_once_and_its_timeline_has_the_hit(fresh: bool) -> None:
    import asyncio

    from pisa.domain.ready import Hit
    from pisa.ports.talker import OwnerSpeaking, OwnerWords
    from tests.test_live_talk import _run, _until

    run, session, _, seen = _run()

    async def ready_answer(words: str) -> Hit | None:
        return Hit("pool", "In cima alla lista: Ritirare le camicie.", 40, fresh)

    run.hooks.ready_answer = ready_answer

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(OwnerWords("cosa dovrei fare?", final=True))
        await _until(lambda: run._thought_for == 1)  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    kinds = {e["kind"]: e for e in run.timeline.events}
    (exchange,) = [e for kind, e in seen if kind == "exchange"]
    if fresh:
        assert session.whispers[0].id == "ready1" and "Ritirare le camicie" in session.whispers[0].text
        hit = kinds["ready_hit"]
        assert (hit["topic"], hit["age_s"], hit["verified"]) == ("pool", 40, True)
        assert exchange.ready == "hit" and exchange.first_content_by == "ready"
    else:
        assert "ready_stale" in kinds and not any(w.id.startswith("ready") for w in session.whispers)
        assert exchange.ready == "stale"


# --- row 3: "done" only from records --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "lang", "waiting", "failed", "truth"),
    [
        ("Li sposto al 30.", "it-IT", False, False, "Non li ho spostati: non ho cambiato niente."),
        ("Fatto, l'ho spostata alle 21.", "it-IT", False, True, "Non l'ho spostata: non ci sono riuscito."),
        ("Ho mandato la mail a Marco.", "it-IT", True, False, "Non l'ho mandata: aspetta il tuo sì."),
        ("I've moved it to 9.", "en-US", False, False, "I haven't moved it: nothing has changed."),
        ("Fatto.", "it-IT", False, False, "Non l'ho ancora fatto: non ho cambiato niente."),
    ],
)
def test_an_unbacked_claim_is_rewritten_into_what_happened(
    sentence: str, lang: str, waiting: bool, failed: bool, truth: str
) -> None:
    from pisa.domain.claims import claim, truth_for

    said = truth_for(sentence, lang, waiting=waiting, failed=failed)
    assert said == truth and claim(said) is None  # the truth claims nothing


def test_a_tool_that_says_it_wasn_t_done_is_on_record() -> None:
    from pisa.domain.claims import failure

    assert (
        failure("move_event", "Not changed: no event matches «cena»")
        == "move_event: Not changed: no event matches «cena»"
    )
    assert failure("move_event", '{"status": "error", "receipt": "x"}') == "move_event: error"
    assert failure("move_event", '{"status": "done", "receipt": "x"}') is None


def test_several_claims_each_said_as_what_happened_never_dropped() -> None:
    import uuid

    from pisa.services.conversation import TurnState, _released  # pyright: ignore[reportPrivateUsage]

    turn = TurnState(uuid.uuid4(), "spostali")
    said, false = _released(["Sono sul 31.", "Li sposto al 30.", "Mando la mail a Marco."], turn, "it-IT")
    assert false == 3
    assert said == "Non li ho spostati: non ho cambiato niente. Non l'ho mandata: non ho cambiato niente."
    # a claim of its own with no verb to name keeps the general line
    said, false = _released(["Fatto."], turn, "it-IT")
    assert (said, false) == ("Non l'ho ancora fatto: non ho cambiato niente.", 1)


# --- the review's fixes ---------------------------------------------------------------


def _turn(**fields: Any) -> Any:
    import uuid

    from pisa.services.conversation import TurnState

    turn = TurnState(uuid.uuid4(), "spostala alle 21")
    for key, value in fields.items():
        setattr(turn, key, value)
    return turn


def test_the_past_never_backs_a_claim_when_this_turn_asked_a_yes_or_failed() -> None:
    from pisa.services.conversation import _released  # pyright: ignore[reportPrivateUsage]

    claim_ = "Fatto, l'ho spostata alle 21."
    moved = frozenset({"move_event"})
    assert _released([claim_], _turn(lately_tools=moved), "it-IT") == (claim_, 0)  # "l'hai spostata?": true
    waiting = _turn(lately_tools=moved, approvals=["a1"])
    assert _released([claim_], waiting, "it-IT") == ("Non l'ho spostata: aspetta il tuo sì.", 1)
    failed = _turn(lately_tools=moved, failures={("move_event", "cena"): "move_event: Not changed"})
    assert _released([claim_], failed, "it-IT") == ("Non l'ho spostata: non ci sono riuscito.", 1)


def test_only_a_change_of_the_same_kind_backs_a_claim() -> None:
    from pisa.domain.claims import Record, backed

    added, moved = frozenset({"create_event"}), frozenset({"move_event"})
    assert not backed("Sì, l'ho spostata.", "it-IT", Record(lately=added))  # an add never backs a move
    assert backed("Sì, l'ho spostata.", "it-IT", Record(lately=moved))
    assert not backed("Ho spostato la cena.", "it-IT", Record(done=added))  # nor in this turn
    # move A failed, move B done: "ho spostato A e B" stands on nothing
    assert not backed("Ho spostato la cena e la palestra.", "it-IT", Record(done=moved, failed=moved))
    assert backed("Ho spostato la palestra.", "it-IT", Record(done=moved))


def test_a_failure_is_cleared_only_by_the_same_change_done() -> None:
    from pisa.services.conversation import _target  # pyright: ignore[reportPrivateUsage]

    assert _target({"event": "Cena con Paola", "start": "21"}) == "cena con paola"
    assert _target({"event": "palestra"}) != _target({"event": "cena"})


def test_the_replay_backs_a_done_claim_only_by_an_earlier_write_of_its_kind() -> None:

    from tests.replay import harness

    class Conn:
        async def execute(self, *_: Any) -> "Conn":
            return self

        async def fetchall(self) -> list[Any]:
            return []

        async def fetchone(self) -> tuple[int]:
            return (0,)

    class Google:
        def __init__(self) -> None:
            self.events: dict[str, Any] = {}
            self.sent: list[Any] = []

    added = [("create_event", {"title": "Cena"})]
    turns = [
        harness.TurnLog("metti la cena alle 20", "Fatto: cena alle 20.", False, wrote=True, calls=added),
        harness.TurnLog("l'hai spostata?", "Sì, l'ho spostata alle 21.", False),
    ]
    result = run_talk(harness.score(Conn(), Google(), {"id": "x", "expect": {}}, turns))  # type: ignore[arg-type]
    assert result.false_claims == ["Sì, l'ho spostata alle 21."]  # an add never backs "spostata"
    turns[1] = harness.TurnLog("l'hai aggiunta davvero?", "Sì, l'ho aggiunta alle 20.", False)
    result = run_talk(harness.score(Conn(), Google(), {"id": "x", "expect": {}}, turns))  # type: ignore[arg-type]
    assert result.false_claims == []


def _approval(db: Any, said: str, *, at: datetime, undone: bool = False, outside: bool = False) -> None:
    import uuid as _uuid

    from psycopg.types.json import Jsonb

    payload = {"said": said, "title": "Cena"} | ({"after_outside": True} if outside else {})
    db.execute(
        "INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at, executed_at, undone_at) "
        "VALUES (%s, 'calendar_move', 'Sposto «Cena» a venerdì alle 21?', %s, 'done', %s, %s, %s, %s)",
        (_uuid.uuid4(), Jsonb(payload), at, at, at, at if undone else None),
    )


def _ask(client: Any, words: str, lang: str = "it-IT") -> Any:
    ready = client.app.state.runtime.ready
    hit = client.portal.call(ready.match, words, lang)
    if hit is None or not hit.hit:
        client.portal.call(ready.wait)
        hit = client.portal.call(ready.match, words, lang)
    return hit


def test_today_s_changes_as_pisa_said_them_never_undone_nor_after_outside_text(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _approval(db, "Ho spostato «Cena» a venerdì alle 21.", at=clock.now)
    _approval(db, "Ho spostato «Palestra» a sabato alle 10.", at=clock.now, undone=True)
    _approval(db, "Ho spostato «Riunione» a lunedì alle 9.", at=clock.now, outside=True)
    hit = _ask(client, "cosa hai cambiato oggi?")
    assert hit is not None and hit.hit and hit.text == "Oggi ho spostato «Cena» a venerdì alle 21."
    db.execute(
        "UPDATE approvals SET undone_at = %s WHERE payload->>'title' = 'Cena'", (clock.now,)
    )  # an undo
    stale = client.portal.call(client.app.state.runtime.ready.match, "cosa hai cambiato oggi?", "it-IT")
    assert stale is not None and not stale.hit  # the undo makes it stale
    client.portal.call(client.app.state.runtime.ready.wait)
    again = client.portal.call(client.app.state.runtime.ready.match, "cosa hai cambiato oggi?", "it-IT")
    assert again is not None and again.text == "Oggi non ho cambiato niente."


def test_with_no_plan_there_is_no_ready_next(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid as _uuid

    from psycopg.types.json import Jsonb

    assert _ask(client, "cosa dovrei fare?") is not None  # Pisa's ready answers were built…
    assert _ask(client, "cosa ho dopo?") is None  # … and with no plan for today the brain answers "next"
    day = clock.now.astimezone(ROME).date()
    email_task = db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Pagare la bolletta', 'pagare la bolletta', 'open', 'email', now(), now()) RETURNING id"
    ).fetchone()[0]
    start = clock.now + timedelta(hours=1)
    items = [
        {
            "kind": "task",
            "task_id": str(email_task),
            "title": "Pagare la bolletta",
            "start": start.isoformat(),
        },
        {"kind": "event", "title": "Dentista", "start": (start + timedelta(hours=1)).isoformat()},
    ]
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, %s, %s, 'x', 'x', %s)",
        (_uuid.uuid4(), day, clock.now, Jsonb({"items": items})),
    )
    hit = _ask(client, "cosa ho dopo?")
    assert (
        hit is not None and "Dentista" in hit.text and "bolletta" not in hit.text
    )  # L4: never an email's task


def test_a_new_zone_makes_the_ready_answers_stale_and_times_as_he_reads_them(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid as _uuid

    from psycopg.types.json import Jsonb

    clock.now = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)
    start = datetime(2026, 11, 3, 20, 30, tzinfo=UTC)  # 21:30 in Rome, 3:30 PM in New York
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, '2026-11-03', %s, 'x', 'x', %s)",
        (
            _uuid.uuid4(),
            clock.now,
            Jsonb({"items": [{"kind": "event", "title": "Cena", "start": start.isoformat()}]}),
        ),
    )
    hit = _ask(client, "cosa ho dopo?")
    assert hit is not None and "21:30" in hit.text
    runtime = client.app.state.runtime
    client.portal.call(
        lambda: runtime.zone.update(
            tz="America/New_York", region="US", hour_cycle="h12", device_id=None, now=clock.now
        )
    )
    client.portal.call(runtime.ready.wait)  # the zone's change rebuilt them
    moved = client.portal.call(runtime.ready.match, "cosa ho dopo?", "it-IT")
    assert moved is not None and moved.hit and "3:30 PM" in moved.text


@pytest.mark.parametrize(
    "words",
    [
        "I need to cancel the meeting with Marco, not the call",
        "voglio spostare la cena a dopo le 21",
        "I want you to delete all my emails, not the starred ones",
        "mi serve che sposti la palestra a dopo le 18",
        "trova il modo di cancellare la riunione senza avvisare",
        "cerca di spostare la cena dopo le 21",
        "vorrei mandare un messaggio a Marco senza emoji",
    ],
)
def test_a_command_is_never_said_back_as_a_search(words: str) -> None:
    from pisa.domain.restate import heard

    assert heard(words, "it-IT") is None and heard(words, "en-US") is None


def test_a_date_deadline_written_on_the_zone_before_keeps_its_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """NY → Rome, then a writer still on New York stamps "due 5 Nov" (23:59 NY = 6 Nov 05:59 in Rome): the
    day stays the 5th, at 23:59 where they are now."""
    runtime = client.app.state.runtime

    def update(tz: str) -> None:
        client.portal.call(
            lambda: runtime.zone.update(tz=tz, region=None, hour_cycle=None, device_id=None, now=clock.now)
        )

    update("America/New_York")
    clock.now += timedelta(minutes=15)
    update("Europe/Rome")
    stale = datetime(2026, 11, 5, 23, 59, tzinfo=NY)  # the writer's day, in its old zone
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, due_at, due_kind, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Rinnovo', 'rinnovo', 'open', 'manual', %s, 'date', now(), now())",
        (stale,),
    )
    due_at, due_date = db.execute("SELECT due_at, due_date FROM tasks").fetchone()
    assert due_date == date(2026, 11, 5) and due_at == datetime(2026, 11, 5, 23, 59, tzinfo=ROME)
    clock.now += timedelta(minutes=15)  # a real move, not a border flip
    update("America/New_York")  # and the day travels with them
    due_at, due_date = db.execute("SELECT due_at, due_date FROM tasks").fetchone()
    assert due_date == date(2026, 11, 5) and due_at == datetime(2026, 11, 5, 23, 59, tzinfo=NY)


def test_only_his_phone_says_where_he_is_and_a_border_flip_is_ignored(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid as _uuid

    runtime = client.app.state.runtime
    web, _ = pair(db, "Browser", "web")
    phone, _ = pair(db)

    def update(tz: str, device: str) -> None:
        client.portal.call(
            lambda: runtime.zone.update(
                tz=tz, region=None, hour_cycle=None, device_id=_uuid.UUID(device), now=clock.now
            )
        )

    update("America/New_York", web)
    assert db.execute("SELECT count(*) FROM owner_context").fetchone() == (0,)  # a web client: never
    update("America/New_York", phone)
    clock.now += timedelta(minutes=15)
    update("Europe/Rome", phone)
    clock.now += timedelta(minutes=2)
    update("America/New_York", phone)  # back to the zone of 2 minutes ago: the border
    assert db.execute("SELECT tz FROM owner_context").fetchone() == ("Europe/Rome",)
    clock.now += timedelta(minutes=15)
    update("America/New_York", phone)  # later: a real move
    assert db.execute("SELECT tz FROM owner_context").fetchone() == ("America/New_York",)


def test_a_zone_that_fails_never_fails_the_hello(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime

    async def broken(**_: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime.zone, "update", broken)
    device_id, token = pair(db)
    with live(client, token, device_id, tz="America/New_York") as app:
        app.custom("pisa.device_context", {"tz": "Asia/Tokyo"})
        assert app.ping() == []  # the socket goes on


@pytest.mark.parametrize(
    "words",
    [
        "cosa ho dopo? rimandalo", "cosa ho dopo? annullalo", "cosa ho dopo? cancellalo", "cosa ho dopo? toglilo",
        "cosa ho dopo? spostiamolo", "cosa ho dopo? e mettilo alle 5", "cosa devo fare? posticipa la palestra",
        "what's next? reschedule it", "what's next? push it to tomorrow", "what changed today? undo it",
        "cosa ho da fare? segnami il latte",
    ],
)  # fmt: skip
def test_a_change_in_any_form_is_never_a_ready_answer(words: str) -> None:
    from pisa.domain.ready import topic_of

    assert topic_of(words) is None
    assert topic_of("cosa hai cambiato oggi?") == "changes"  # a past participle asks, never changes


@pytest.mark.parametrize(
    ("words", "ok"),
    [
        ("non voglio che la sostituisci", False),
        ("I don't want you to replace it", False),
        ("I don't think you should replace it", False),
        ("no grazie, lasciala, non serve che la sostituisci", False),
        ("sostituiscila? no", False),
        ("sì sostituiscila, non mi serve più", True),
        ("replace it, I don't need the old one", True),
    ],
)
def test_a_negation_governing_the_replacing_or_a_question_never_replaces(words: str, ok: bool) -> None:
    from pisa.domain.views import asks_to_replace

    assert asks_to_replace(words) is ok


@pytest.mark.parametrize(
    ("before", "now", "redone"),
    [
        ("aggiungi latte alla lista", "aggiungi pane alla lista", False),
        ("chiama Marco stasera alle 8", "chiama Mario stasera alle 8", False),
        ("sposta la cena a domani", "sposta la cena a dopodomani", False),
        ("metti la palestra lunedì", "metti la palestra martedì", False),
        ("sposta la cena alle 21", "sposta la cena alle 21 dai", True),
        ("spostala alle nove stasera", "spostalo alle nove stasera", True),  # a transcription's slip
        ("metti la cena alle 20", "no, non è quello che ho detto", True),  # accents folded
    ],
)
def test_a_repeat_is_the_same_content_words(before: str, now: str, redone: bool) -> None:
    from pisa.domain import clean

    assert clean.redone(before, now) is redone


@pytest.mark.parametrize(
    ("sentence", "truth"),
    [
        ("Ho mandato le mail.", "Non le ho mandate: non ho cambiato niente."),
        ("Ho aggiunto gli eventi.", "Non li ho aggiunti: non ho cambiato niente."),
        ("Le ho spostate a domani.", "Non le ho spostate: non ho cambiato niente."),
        ("L'ho spostata alle 21.", "Non l'ho spostata: non ho cambiato niente."),
    ],
)
def test_the_pronoun_agrees_with_what_was_claimed(sentence: str, truth: str) -> None:
    from pisa.domain.claims import truth_for

    assert truth_for(sentence, "it-IT", waiting=False, failed=False) == truth


def test_only_the_claim_s_own_failure_says_it_failed() -> None:
    from pisa.services.conversation import _released  # pyright: ignore[reportPrivateUsage]

    turn = _turn(failures={("move_event", "cena"): "move_event: Not changed"})
    said, _ = _released(["L'ho cancellata."], turn, "it-IT")
    assert said == "Non l'ho cancellata: non ho cambiato niente."  # the move failed, not a cancel


def test_not_shown_once_and_ecco_il_link_is_a_screen_claim() -> None:
    from pisa.services.conversation import _screen_sentences, _with_screen_claims  # pyright: ignore[reportPrivateUsage]

    rest, claims = _screen_sentences("Ecco il link.")
    assert (
        claims
        and _with_screen_claims(rest, claims, False, False, "it-IT")
        == "Non sono riuscito a mostrartelo sul telefono."
    )
    already = "Chiude alle 19. Non sono riuscito a mostrartelo sul telefono."
    assert (
        _with_screen_claims(already, ["Ti ho messo la mappa sul telefono."], False, False, "it-IT") == already
    )


def test_away_from_home_the_weather_never_falls_back_to_home(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    from tests.test_learning import place
    from tests.weather_fake import FakeMeteoAPI

    http_handler["handler"] = FakeMeteoAPI(clock.now.astimezone(ROME).date())
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    casa = place(db, "Casa", (41.88, 12.47), "Via del Marmo Finto 1, 00153 Roma, Italy")
    assert client.put("/v1/profile/home", headers=headers, json={"place_id": casa}).status_code == 200
    client.app.state.runtime.owner.place = zone.PhoneContext(
        "America/New_York", "US", "h12"
    )  # they are in New York
    assert client.get("/v1/weather", headers=headers, params={"near": "here"}).status_code == 409


def test_said_page_compares_the_port() -> None:
    from pisa.domain.views import said_page

    assert not said_page("https://a.example:8443/menu", "leggi https://a.example/menu")
    assert said_page("https://a.example:8443/menu", "leggi https://a.example:8443/menu")


def test_a_calendar_change_rebuilds_the_ready_answers(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    called: list[str] = []

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "sync_calendar", nothing)
    monkeypatch.setattr(runtime.planner, "plan", nothing)
    monkeypatch.setattr(runtime.ready, "changed", lambda: called.append("changed"))
    client.portal.call(runtime.after_calendar_change)
    assert called == ["changed"]


# --- the real-model replay's findings --------------------


def test_replay_a_plan_proposal_is_never_done_and_is_said_as_a_proposal() -> None:
    from pisa.domain.claims import PROPOSES, proposal_truth
    from pisa.services.conversation import _released  # pyright: ignore[reportPrivateUsage]

    turn = _turn(proposals=[("Ritirare le camicie in lavanderia", "10:00")])
    said, false = _released(["Ho messo il ritiro delle camicie in lavanderia oggi alle 10."], turn, "it-IT")
    assert false == 1 and said == "Ti propongo «Ritirare le camicie in lavanderia» alle 10:00: va bene?"
    assert (
        "plan_task" in PROPOSES
        and proposal_truth("en-US", "Gym", "18:00") == "I suggest «Gym» at 18:00: does that work?"
    )
    from datetime import date as _date

    from pisa.domain import decisions

    with decisions.collecting("trovami un momento per le camicie", _date(2026, 10, 31)):
        decisions.note("act", "plan_task", "owner_words")
        assert "plan_task" not in _turn().record().done  # Pisa's act of proposing backs no "Fatto"


def test_replay_a_bare_fatto_is_about_this_turn_unless_he_asked() -> None:
    from pisa.domain.claims import ANY, Record, backed

    fatto = "Fatto: continuo da qui, call con Anna alle 15 e palestra alle 16."
    assert not backed(
        fatto, "it-IT", Record(lately=frozenset({ANY, "create_event"}))
    )  # the one-question-one-yes scenario: nothing done now
    assert backed(fatto, "it-IT", Record(lately=frozenset({"create_event"}), asked=True))  # "l'hai fatto?"
    assert backed(fatto, "it-IT", Record(done=frozenset({"create_event"})))


def test_replay_a_table_is_asked_for_with_show_sheet() -> None:
    from pisa.brain.skills import Library, load, named

    found = Library(load()).search("mostrami il meteo di Roma dei prossimi 3 giorni come tabella")
    assert found and found[0][1].name == "presenting-on-phone"
    skill = named("presenting-on-phone")
    assert (
        skill is not None
        and "`show_sheet` with a `table` block" in skill.body
        and "never `show_note`" in skill.body
    )
    assert "come tabella" in skill.key_text and "as a table" in skill.key_text  # its triggers find it


def test_only_the_primary_phone_moves_the_zone_and_no_region_keeps_the_country(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid as _uuid

    runtime = client.app.state.runtime
    ipad, _ = pair(db, "iPad", "ios")
    db.execute("UPDATE devices SET created_at = now() - interval '1 day' WHERE id = %s", (ipad,))
    iphone, _ = pair(db, "iPhone", "ios")  # the most recently paired iPhone: the primary phone

    def report(device: str, tz: str | None, region: str | None) -> None:
        client.portal.call(
            lambda: runtime.zone.update(
                tz=tz, region=region, hour_cycle=None, device_id=_uuid.UUID(device), now=clock.now
            )
        )

    report(ipad, "America/New_York", "US")
    assert db.execute("SELECT count(*) FROM owner_context").fetchone() == (0,)  # the iPad's NY moves nothing
    report(iphone, "Europe/Rome", "IT")
    clock.now += timedelta(minutes=15)
    report(iphone, "Europe/London", None)  # no region: the country stays
    assert db.execute("SELECT tz, region FROM owner_context").fetchone() == ("Europe/London", "IT")
    report(ipad, "America/New_York", "US")
    assert db.execute("SELECT tz, region FROM owner_context").fetchone() == ("Europe/London", "IT")
