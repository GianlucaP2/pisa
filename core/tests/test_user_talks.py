"""The user's talks of 3-4 Nov, one test per row that
fails on revert. Invented names, places and times only."""

from typing import Any, cast

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from pisa.services.session_messages import Utterance
from tests.assistant_env import assistant_data
from tests.owner import owner_at


def _done_write() -> ToolReturnPart:
    return ToolReturnPart("create_event", '{"status": "done", "receipt": "e1 done: Cena"}', tool_call_id="w")


# --- #2: one reply, once ----------------------------------------------------------------------------------


def test_2_finish_never_says_again_what_its_response_wrote() -> None:
    from pisa.brain.spoken import Round

    for written, say, want in [
        # the text is the reply: finish's paraphrase is never said after it
        ("Ho messo la cena con Bruno venerdì alle 20.", "Fatto: venerdì alle 20 sei a cena con Bruno.", " "),
        ("La mostra è domani alle 10.", "Hai detto che la mostra è domani alle 10.", " "),
        # a lead only ("Ecco.") leaves finish's words to say
        (
            "Ecco.",
            "Ho messo la cena con Bruno venerdì alle 20.",
            "Ho messo la cena con Bruno venerdì alle 20.",
        ),
        # no text: finish's words, as given
        ("", "Fatto.", "Fatto."),
    ]:
        loop = Round(lang="it-IT")
        loop.say = say
        calls = [
            ToolCallPart("create_event", {}, tool_call_id="w"),
            ToolCallPart("finish", {}, tool_call_id="f"),
        ]
        response = ModelResponse(parts=[TextPart(written), *calls] if written else calls)
        returns = [_done_write(), ToolReturnPart("finish", "ok", tool_call_id="f")]
        messages = [ModelRequest(parts=[]), response, ModelRequest(parts=returns)]
        assert loop.ready(messages) == want, (written, say)


def test_2_a_later_round_says_only_what_it_adds() -> None:
    from pisa.domain import talk

    said = ["Domani la mostra al museo civico è alle 10. "]
    assert not talk.adds("Domani alle 10 hai la mostra al museo civico.", said, [], ask=False)
    assert not talk.adds("La mostra è alle 10, domani.", said, [], ask=False)
    # a number, a "non" or a new thing is something it adds
    assert talk.adds("Il pranzo con Bruno è alle 13.", said, [], ask=False)
    assert talk.adds("Domani la mostra non è alle 10.", said, [], ask=False)
    assert talk.adds("Domani la mostra al museo civico è alle 11.", said, [], ask=False)
    # the question for a change that waits for their yes is always said
    assert talk.adds("Domani la mostra è alle 10, la sposto?", said, [], ask=True)


def test_2_finish_sees_the_writes_of_its_own_streamed_response() -> None:
    """Text and tool calls in one response end the stream run, and its tools run without it: finish read "no
    write holds their words", handed them back, and the model answered a second time."""
    from pisa.brain.conversation import unsaid_once
    from pisa.brain.spoken import Round, with_streamed

    words = "metti cena con Bruno venerdì alle 20"
    response = ModelResponse(parts=[
        TextPart("Ho messo la cena."),
        ToolCallPart("create_event", {"title": "Cena con Bruno", "start": "2026-11-13T20:00",
                                      "owner_said": words}, tool_call_id="w"),
        ToolCallPart("finish", {"say": "Fatto."}, tool_call_id="f"),
    ])  # fmt: skip
    from pydantic_ai.messages import UserPromptPart

    messages: list[Any] = [ModelRequest(parts=[UserPromptPart(words)])]
    assert unsaid_once(words, messages)  # the run's own messages lack the response
    loop = Round(lang="it-IT")
    loop.response = response
    assert unsaid_once(words, with_streamed(loop, messages, "f")) == []
    assert with_streamed(loop, messages, "other") == messages  # another response's call: left as it was


# --- #3: Pisa never hears its own last line as their words
# ---------------------------------------------------


def test_3_pisa_s_own_line_heard_back_after_its_speech_is_its_echo() -> None:
    from pisa.domain import self as self_rules
    from pisa.domain.actions import is_short_no
    from pisa.domain.tasks import is_short_yes

    def bare(text: str) -> bool:
        return is_short_yes(text) or is_short_no(text)

    line = self_rules.Line(0.0, "Ho sbagliato, non so cosa intendi. Me lo ripeti con altre parole?")
    # 12 words: about 6 s of speech; heard back 14 s after it was sent (10 s from the line's start missed it)
    said = "ho sbagliato non so cosa intendi me lo ripeti con altre parole"
    assert self_rules.own_echo(said, [line], 14.0, bare=bare)
    short = self_rules.Line(0.0, "Ho sbagliato, non so cosa intendi.")
    assert self_rules.own_echo("Ho sbagliato, non so cosa intendi.", [short], 1.0, bare=bare)
    assert not self_rules.own_echo(said, [line], 30.0, bare=bare)  # long gone
    # review H2: under 60% of Pisa's line, or fewer than 5 words, is theirs
    assert not self_rules.own_echo("ho sbagliato non so cosa intendi", [line], 1.0, bare=bare)
    assert not self_rules.own_echo("sì", [line], 1.0, bare=bare)  # their bare yes
    assert not self_rules.own_echo("no non hai sbagliato, intendo la mostra", [line], 1.0, bare=bare)
    assert not self_rules.own_echo("ripeti?", [line], 1.0, bare=bare)  # too short to be Pisa's alone


def test_3_the_voice_path_never_answers_pisa_s_own_line(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from tests.conftest import pair, set_key
    from tests.live_app import live

    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Ho sbagliato, non so cosa intendi. Me lo ripeti?"
    with live(client, token, device_id) as app:
        app.say("cosa mi dici della mostra", lang="it-IT", source="phone_mic")  # fmt: skip
        list(app.reply())
        echo = Utterance(
            text="Ho sbagliato, non so cosa intendi", lang="it-IT", final=True, source="phone_mic"
        )
        app.utterance(echo)
        chunks = list(app.reply())
        assert [(c["type"], c["delta"], c["final"]) for c in chunks] == [("say", "", True)]
        # typed, the same words are theirs
        typed = Utterance(text="Ho sbagliato, non so cosa intendi", lang="it-IT", final=True, source="typed")
        app.utterance(typed)
        assert "".join(c["delta"] for c in app.reply()).strip()
    users = db.execute("SELECT content FROM messages WHERE role = 'user' ORDER BY id").fetchall()
    assert users == [("cosa mi dici della mostra",), ("Ho sbagliato, non so cosa intendi",)]


# --- #4: their thinking aloud is one turn ----------------------------------------------------------------


def test_4_a_plainly_unfinished_fragment_waits_for_the_rest(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, monkeypatch
) -> None:
    from pisa.services import session
    from tests.conftest import pair, set_key
    from tests.live_app import live

    monkeypatch.setattr(session, "FRAGMENT_WAIT_S", 1.0)
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Va bene."
    with live(client, token, device_id) as app:
        first = Utterance(text="Dobbiamo sicuramente…", lang="it-IT", final=True, source="phone_mic")
        app.utterance(first)
        app.say("spostare la palestra a domani", lang="it-IT", source="phone_mic")  # fmt: skip
        closed = list(app.reply())  # the fragment's wait ends, empty
        assert [(c["reply_to"], c["delta"], c["final"]) for c in closed] == [(first.id, "", True)]
        assert "".join(c["delta"] for c in app.reply()).strip() == "Va bene."
        # a fragment alone is answered once the wait is over, as it is
        app.say("e poi", lang="it-IT", source="phone_mic")
        assert "".join(c["delta"] for c in app.reply()).strip() == "Va bene."
    users = db.execute("SELECT content FROM messages WHERE role = 'user' ORDER BY id").fetchall()
    assert users == [("Dobbiamo sicuramente… spostare la palestra a domani",), ("e poi",)]
    assert len(fake_models.seen_messages) == 2  # never answered piece by piece


# --- #5: their "sì" applies every change the question named with its time ---------------------------------


def _ev(title: str, start: str, end: str) -> dict[str, Any]:
    return {"title": title, "when": f"gio 12 nov alle {start}",
            "body": {"start": {"dateTime": f"2026-11-12T{start}:00+01:00"},
                     "end": {"dateTime": f"2026-11-12T{end}:00+01:00"}}}  # fmt: skip


def test_5_one_question_binds_each_change_it_names_with_its_time() -> None:
    from pisa.domain import actions

    held = [("calendar_cancel", _ev("Riunione in ufficio", "09:00", "13:00")),
            ("calendar_create", _ev("Riunione in ufficio", "09:00", "12:00")),
            ("calendar_create", _ev("Pranzo", "12:00", "12:45")),
            ("calendar_create", _ev("Partenza per Torino", "13:30", "14:00"))]  # fmt: skip
    for reply in (
        "Cancello la riunione in ufficio e la ricreo fino alle 12, aggiungo il pranzo alle 12 e la partenza "
        "per Torino alle 13:30. Confermi?",
        "Cancello la riunione in ufficio e la ricreo fino alle 12. Aggiungo il pranzo alle 12. Poi la "
        "partenza per Torino alle 13:30. Confermi?",
        "Ok: annullo la riunione in ufficio e la ricreo dalle 9 alle 12, metto pranzo alle 12, e partenza "
        "per Torino alle 13:30. Va bene?",
    ):
        assert [actions.says_change(k, p, reply) for k, p in held] == [True] * 4, reply


def test_5_m6_still_holds_no_time_no_bind_no_cancel_without_its_verb() -> None:
    from pisa.domain import actions

    lunch, trip = _ev("Pranzo", "12:00", "12:45"), _ev("Partenza per Torino", "13:30", "14:00")
    meeting = _ev("Riunione in ufficio", "09:00", "13:00")
    no_time = "Aggiungo il pranzo alle 12 e poi la partenza per Torino. Confermi?"
    assert actions.says_change("calendar_create", lunch, no_time)
    assert not actions.says_change("calendar_create", trip, no_time)  # nothing binds without its time
    # a noun phrase after Pisa's "aggiungo" is never a cancel; a cancel needs its own cancel verb
    assert not actions.says_change(
        "calendar_cancel", meeting, "Aggiungo il pranzo alle 12 e la riunione in ufficio. Confermi?"
    )
    # an article is no pronoun: "la cena alle 13:30" after the meeting never binds the meeting's move
    move = _ev("Riunione in ufficio", "13:30", "14:30")
    assert not actions.says_change(
        "calendar_move", move, "Sposto la riunione in ufficio alle 10 e la cena alle 13:30. Va bene?"
    )
    # a time said in the past, or a change set aside, binds nothing
    assert not actions.says_change("calendar_create", lunch, "Ieri alle 12 avevi il pranzo. Confermi?")
    assert not actions.says_change(
        "calendar_create", lunch, "Invece di aggiungere il pranzo alle 12, sposto la call. Va bene?"
    )


def test_5_the_quiet_summary_never_delivers_an_expired_ask(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid
    from datetime import timedelta

    from pisa.services.notifications import release_held

    now = clock.now
    night = now - timedelta(hours=6)
    held = [("Pranzo alle 12: scade tra due minuti.", "approval_lapsing", night),
            ("Dentista alle 9:00", None, night),
            ("Partenza alle 13:30: scade tra due minuti.", "approval_lapsing", now)]  # fmt: skip
    for text, speak, at in held:
        db.execute(
            "INSERT INTO notifications (id, kind, text, lang, priority, created_at, queued_at, "
            "delivered_via, speak) VALUES (%s, 'info', %s, 'it-IT', 'normal', %s, %s, 'held', %s)",
            (uuid.uuid4(), text, at, at, speak),
        )
    runtime = client.app.state.runtime
    client.portal.call(lambda: release_held(runtime.pool, now=now, owner=owner_at()))
    (summary,) = db.execute(
        "SELECT text FROM notifications WHERE delivered_via IS DISTINCT FROM 'held' AND kind = 'info'"
    ).fetchone()
    assert "Dentista" in summary and "Partenza" in summary  # the one still open goes
    assert "Pranzo" not in summary  # its approval expired in the night
    left = db.execute(
        "SELECT count(*) FROM notifications WHERE delivered_at IS NULL AND delivered_via = 'held'"
    )
    assert left.fetchone() == (0,)


# --- #8: recurring events and their routine ----------------------------------------------------------------


def test_8_the_series_rule_and_its_words() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import pytest

    from pisa.domain import actions

    rome = ZoneInfo("Europe/Rome")
    thursday = datetime(2026, 11, 12, 13, 0, tzinfo=rome)
    assert actions.rrule("weekdays", thursday) == "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    assert actions.rrule("weekly", thursday) == "RRULE:FREQ=WEEKLY;BYDAY=TH"
    until = datetime(2026, 12, 18, 0, 0, tzinfo=rome)
    assert actions.rrule("weekly", thursday, until) == "RRULE:FREQ=WEEKLY;BYDAY=TH;UNTIL=20261218T225959Z"
    assert actions.repeat_words("weekdays", thursday, "it-IT") == "tutti i giorni lavorativi"
    assert actions.repeat_words("weekly", thursday, "it-IT", until) == "ogni giovedì, fino al 18 dicembre"
    with pytest.raises(ValueError):
        actions.rrule("weekdays", datetime(2026, 11, 14, 10, 0, tzinfo=rome))  # a Saturday
    with pytest.raises(ValueError):
        actions.rrule("weekly", thursday, datetime(2026, 11, 1, tzinfo=rome))


def test_8_his_yes_to_keep_it_is_his_words_from_the_message_before() -> None:
    from pisa.domain import profile

    for words in ("sì, ricordalo", "Sì ricordatelo pure", "tienilo a mente", "yes, remember it"):
        assert profile.keeps_it(words), words
    for words in ("ricordami di comprare il latte", "sì", "ricordalo e spostalo alle 10", "non ricordarlo"):
        assert not profile.keeps_it(words), words


# --- #9, #10: the team's note, and no rail words on a reply that claimed nothing ---------------------------


def test_9_a_promise_to_the_team_is_a_claim_only_the_note_backs() -> None:
    from pisa.domain import claims, talk, tool_choice

    empty = claims.Record()
    noted = claims.Record(done=frozenset({claims.TEAM_TOOL}))
    promises = ["Lo segnalo al team di sviluppo.", "La propongo al team.",
                "L'ho segnalato agli sviluppatori.", "I'll flag it to the team."]  # fmt: skip
    for said in promises:
        assert claims.claim(said) is not None, said
        assert not claims.backed(said, "it-IT", empty) and claims.backed(said, "it-IT", noted), said
        assert claims.truth_for(said, "it-IT", waiting=False, failed=False).startswith(
            "Non l'ho ancora segnalato"
        )
    for said in (
        "Non posso segnalarlo al team.",
        "Propongo call alle 16 e palestra alle 17.",
        "Vuoi che lo segnali al team?",
    ):
        assert claims.team_claim(said) is None, said
    # their words that ask it offer the tool; their work team, a question or another person never do
    for words in ("segnalalo al team", "dillo agli sviluppatori", "proponilo al team di sviluppo, è un bug"):
        assert talk.asks_team(words) and "tell_the_team" in tool_choice.tools_for(words), words
    for words in (
        "manda il report al team marketing",
        "puoi segnalarlo al team?",
        "segnala a Marco che arrivo",
    ):
        assert not talk.asks_team(words), words


def test_10_a_bare_acknowledgement_is_never_rewritten_beside_a_reply() -> None:
    import uuid

    from pisa.services.conversation import TurnState, _released

    turn = TurnState(uuid.uuid4(), "mi piace quando mi rispondi breve")
    assert _released(["Segnato."], turn, "it-IT") == ("", 1)  # dropped: the reply's other words stand
    # alone, or after words that asked a change, the truth is still said
    assert _released(["Segnato."], turn, "it-IT", others=False)[0].startswith("Non l'ho segnato")
    asked = TurnState(uuid.uuid4(), "sposta la cena alle 21")
    assert _released(["Fatto."], asked, "it-IT")[0] != ""
    # a claim that names a change is rewritten as before
    assert _released(["Ho spostato la cena alle 21."], turn, "it-IT")[0].startswith("Non l'ho spostat")


# --- #12: the email's whole text, one email at a time ------------------------------------------------------


def test_12_an_email_is_read_whole_by_its_id_and_never_one_for_another(client, db) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    body = "Da domani il pacco è al punto di ritiro Bottega Verde, via Fittizia Omega Terza 3. " * 3
    for mid, labels, text in [("pk1000001", "{INBOX}", body), ("pk1000002", "{INBOX}", "Altro."),
                              ("pr3000003", "{CATEGORY_PROMOTIONS}", "Sconti!")]:  # fmt: skip
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, subject, sent_at, labels, snippet, body) "
            "VALUES (%s, %s, 'Corriere Rapido', 'Pacco', %s, %s, %s, %s)",
            (mid, f"t-{mid}", datetime(2026, 11, 11, 9, tzinfo=UTC), labels, text[:40], text),
        )
    data = assistant_data(
        client.app.state.runtime.pool, MagicMock(), clock=MagicMock(), settings=MagicMock(), brief=MagicMock()
    )
    hit, text = client.portal.call(data.email_text, "pk1000001")
    assert hit.id == "pk1000001" and "via Fittizia Omega Terza 3" in text and len(text) > len(hit.snippet)
    assert client.portal.call(data.email_text, "pk1000") is None  # two match: never one for the other
    assert client.portal.call(data.email_text, "pk1") is None  # too short to name one
    assert client.portal.call(data.email_text, "pr3000003") is None  # a promotion never reaches a model


# --- #13: something different, never a hotel, never far ---------------------------------------------------


def test_13_an_idea_is_near_where_he_is_and_never_a_place_to_sleep(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    import uuid
    from datetime import UTC

    from pisa.domain import explore
    from pisa.services.rituals import place_ideas
    from tests.test_day_story import HOME, _home, _phone, _place, _Places, _stay, local

    _home(db)
    phone = _phone(db)
    abroad = (48.82, 2.371)  # an invented trip abroad: their "most stayed at" place is its hotel
    hotel = _place(db, "Hotel Viaggio", abroad, "lodging")
    cafe = _place(db, "Bar Solito", (HOME[0] + 0.004, HOME[1]), "cafe")
    for n in range(5):
        _stay(db, phone, abroad, local(f"2026-11-0{n + 1}T22:00"), local(f"2026-11-0{n + 2}T08:00"), hotel)
    _stay(db, phone, (HOME[0] + 0.004, HOME[1]), local("2026-11-10T09:00"), local("2026-11-10T09:30"), cafe)
    near_cafe = {"id": str(uuid.uuid4()), "name": "Caffè Vicino", "address": "via Inventata 1",
                 "category": "cafe", "lat": HOME[0] + 0.01, "lon": HOME[1]}  # fmt: skip
    far_cafe = {"id": str(uuid.uuid4()), "name": "Café Lointain", "address": "rue Imaginaire 2",
                "category": "cafe", "lat": abroad[0], "lon": abroad[1]}  # fmt: skip
    near_hotel = {"id": str(uuid.uuid4()), "name": "Albergo Vicino", "address": "via Finta 3",
                  "category": "lodging", "lat": HOME[0] + 0.002, "lon": HOME[1]}  # fmt: skip
    fake = _Places([near_hotel, far_cafe, near_cafe])
    clock.now = local("2026-11-18T19:00").astimezone(UTC)
    pool, places = client.app.state.runtime.pool, cast(Any, fake)
    ideas = client.portal.call(place_ideas(pool, places, lambda: clock.now), set(), set())
    assert [i["text"] for i in ideas] == ["Caffè Vicino, via Inventata 1"]
    assert explore.lodging("lodging") and explore.lodging("Hotel") and not explore.lodging("cafe")
    # where they are now (a fresh fix abroad) decides "near": home's café is far from there, so nothing
    db.execute(
        "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
        "VALUES (%s, %s, %s, 12, %s)",
        (phone, abroad[0], abroad[1], clock.now),
    )
    fake.found = [near_cafe, near_hotel]
    assert client.portal.call(place_ideas(pool, places, lambda: clock.now), set(), set()) == []  # type: ignore[arg-type]


# --- #14: Pisa knows its own screen -------------------------------------------------------------------------


def test_14_his_question_about_the_screen_offers_pisa_s_own_help() -> None:
    from pisa.domain import app_help, tool_choice

    asked = ["che cosa fa il pulsante in alto a sinistra?", "a cosa serve l'onda?", "cos'è il cerchio blu?",
             "what does the top left button do?"]  # fmt: skip
    for words in asked:
        assert app_help.asks(words) and "app_help" in tool_choice.tools_for(words), words
    for words in ("metti la cena alle 20", "il pulsante dell'ascensore è rotto"):
        assert "app_help" not in tool_choice.tools_for(words) or app_help.asks(words) is False, words
    text = app_help.text("it-IT")
    for part in ("In alto a sinistra", "«Scrivi a Pisa»", "modalità vocale", "Approva / Rifiuta", "Face ID"):
        assert part in text, part
    assert len(text) < 2000 and len(app_help.text("en-US")) < 2000  # short: only on the turn that asks


# --- #15: a voice with no credits rests for the day; the phone speaks; they are told once
# ---------------------


def test_15_a_provider_out_of_credit_is_named_so() -> None:
    from pisa.connectors.speech import _failure

    openai = _failure("OpenAI", 429, "speech", '{"error": {"code": "insufficient_quota", "message": "..."}}')
    eleven = _failure("ElevenLabs", 401, "speech", '{"detail": {"status": "quota_exceeded"}}')
    assert openai.no_credit and eleven.no_credit and _failure("X", 402, "speech").no_credit
    assert "no credits left" in openai.message and "Settings → Voice" in openai.message
    busy = _failure("OpenAI", 429, "speech", '{"error": {"code": "rate_limit_exceeded"}}')
    assert not busy.no_credit and not _failure("OpenAI", 401, "speech", "invalid key").no_credit


def test_15_no_credit_stops_the_voice_for_the_day_and_tells_him_once(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    from datetime import UTC, datetime, timedelta

    from pydantic import SecretStr

    from pisa.db.pool import open_pool
    from pisa.domain import voice as voice_rules
    from pisa.infra.keys import KeyStore
    from pisa.ports.speech import SpeechError
    from pisa.services.voice import VoiceService
    from tests.conftest import FAKE_KEY, dsn
    from tests.test_voice import Provider, speak

    class Empty(Provider):
        async def stream(self, text, **kwargs):  # type: ignore[no-untyped-def, override]
            if "credito" in text:
                self.calls += 1
                raise SpeechError("OpenAI has no credits left (HTTP 429).", 429, no_credit=True)
            async for chunk in super().stream(text, **kwargs):
                yield chunk

    now = [datetime(2026, 11, 11, 9, 0, tzinfo=UTC)]

    async def go() -> tuple[Any, Any, Any, int, Any]:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        keys = KeyStore(SecretStr(secrets_key))
        provider = Empty()
        try:
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            providers = cast(Any, {"openai": provider})
            voice = VoiceService(pool, keys, providers, owner=owner_at(), clock=lambda: now[0])
            first, _ = await speak(voice, "Uno. ", "credito. ", "Tre. ")
            calls = provider.calls
            second, _ = await speak(voice, "Ancora. ")  # the same day: never called again
            third, _ = await speak(voice, "credito ancora. ")
            calls = provider.calls - calls  # calls after the rest began
            now[0] += timedelta(days=1)  # tomorrow: the chosen voice is tried again
            fourth, _ = await speak(voice, "Domani. ")
            return first, second, third, calls, fourth
        finally:
            await pool.close()

    first, second, third, calls, fourth = asyncio.run(go())
    assert first == [(0, b"Uno. ", False, None), (1, b"", True, 5)]  # the phone speaks from "credito" on
    assert calls == 0  # rested: the provider is never called again today ("credito" itself never retried)
    assert second == [] and third == []  # no audio from the provider: the phone's voice
    assert fourth[0][1] == b"Domani. "
    notices = db.execute("SELECT text FROM notifications").fetchall()
    assert notices == [(voice_rules.NO_CREDIT_NOTICE["it-IT"].format(name="OpenAI"),)]  # told once


# --- the review of f0bf7b3b: each sentence fails on revert ------------------------------------


def test_a_negated_verb_carries_into_nothing() -> None:
    from pisa.domain import actions

    def ev(title: str, start: str) -> dict[str, Any]:
        return _ev(title, start, "23:00")

    for kind, payload, reply in [
        ("calendar_move", ev("Palestra", "17:00"), "Non sposto la call e la palestra alle 17. Va bene?"),
        ("calendar_move", ev("Call con Anna", "18:00"),
         "Non sposto la palestra alle 17. La call con Anna alle 18. Va bene?"),
        ("calendar_move", ev("Call con Anna", "18:00"),
         "Per ora non sposto niente. La call con Anna alle 18 e la palestra alle 19. Va bene?"),
        ("calendar_create", ev("Pranzo", "12:00"),
         "Non aggiungo la cena alle 20. Il pranzo alle 12. Confermi?"),
        ("calendar_move", ev("Call con Anna", "18:00"),
         "Sposto la palestra alle 17 e nemmeno la call con Anna alle 18. Va bene?"),
        ("calendar_move", ev("Call con Anna", "18:00"),
         "Non sposto la palestra alle 17 né la call con Anna alle 18. Va bene?"),
    ]:  # fmt: skip
        assert not actions.says_change(kind, payload, reply), reply
    assert actions.says_change("calendar_move", ev("Palestra", "17:00"),
                               "Sposto la call alle 16 e la palestra alle 17. Va bene?")  # fmt: skip


def test_his_pick_of_one_option_is_never_an_echo() -> None:
    from pisa.domain import self as self_rules
    from pisa.domain.actions import is_short_no
    from pisa.domain.tasks import is_short_yes

    def bare(text: str) -> bool:
        return is_short_yes(text) or is_short_no(text)

    asked = [self_rules.Line(0.0, "Vuoi la palestra domani alle 17 o venerdì alle 18?")]
    for his in ("domani alle 17", "palestra venerdì alle 18", "la palestra venerdì alle 18 grazie"):
        assert not self_rules.own_echo(his, asked, 1.0, bare=bare), his
    assert self_rules.own_echo("vuoi la palestra domani alle 17 o venerdì alle 18", asked, 1.0, bare=bare)


def test_a_long_lead_never_hides_what_was_done() -> None:
    from pisa.brain.spoken import Round

    loop = Round(lang="it-IT")
    loop.say = "Ho spostato la palestra alle 17, annullabile per 24 ore."
    calls = [ToolCallPart("move_event", {}, tool_call_id="w"), ToolCallPart("finish", {}, tool_call_id="f")]
    response = ModelResponse(parts=[TextPart("Ok, ci penso io subito."), *calls])
    returns = [ToolReturnPart("move_event", '{"status": "done", "receipt": "m1 done"}', tool_call_id="w"),
               ToolReturnPart("finish", "ok", tool_call_id="f")]  # fmt: skip
    said = loop.ready([ModelRequest(parts=[]), response, ModelRequest(parts=returns)])
    assert said == "Ho spostato la palestra alle 17, annullabile per 24 ore."


def test_a_series_binds_only_where_it_is_said_to_repeat() -> None:
    from pisa.domain import actions

    work = {"title": "Lavoro", "when": "lun 16 nov alle 13:00", "repeat_said": "tutti i giorni lavorativi"}
    assert not actions.says_change("calendar_create", work, "Aggiungo il lavoro lunedì alle 13?")
    assert actions.says_change(
        "calendar_create", work, "Aggiungo il lavoro alle 13, tutti i giorni lavorativi?"
    )
    assert actions.says_change(
        "calendar_create", work, "Aggiungo il lavoro alle 13, tutti i giorni lavorativi. Va bene?"
    )
    assert not actions.series_fits(work, "Li aggiungo entrambi?")


def test_an_end_alone_binds_only_a_recreate() -> None:
    from pisa.domain import actions

    lunch = _ev("Pranzo", "12:00", "13:00")
    assert not actions.says_change(
        "calendar_create", lunch, "Aggiungo il pranzo prima della call alle 13. Va bene?"
    )
    gym = _ev("Palestra", "17:00", "18:00")
    assert not actions.says_change("calendar_move", gym, "Sposto la palestra fino alle 18. Va bene?")
    meeting = _ev("Riunione in ufficio", "09:00", "12:00")
    assert not actions.says_change(
        "calendar_create", meeting, "Cancello la riunione in ufficio e la ricreo alle 12. Confermi?"
    )  # a bare "alle": never its end
    assert actions.says_change(
        "calendar_create", meeting, "Cancello la riunione in ufficio e la ricreo fino alle 12. Confermi?"
    )


def test_his_yes_never_joins_a_waiting_fragment(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, monkeypatch
) -> None:
    from pisa.services import session
    from tests.conftest import pair, set_key
    from tests.live_app import live

    monkeypatch.setattr(session, "FRAGMENT_WAIT_S", 1.0)
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Va bene."
    with live(client, token, device_id) as app:
        first = Utterance(text="e poi", lang="it-IT", final=True, source="phone_mic")
        app.utterance(first)
        app.say("sì", lang="it-IT", source="phone_mic")
        closed = list(app.reply())
        assert [(c["reply_to"], c["delta"]) for c in closed] == [(first.id, "")]
        list(app.reply())
    users = db.execute("SELECT content FROM messages WHERE role = 'user' ORDER BY id").fetchall()
    assert users == [("sì",)]  # their yes alone, never "e poi sì"


def test_only_the_credit_codes_and_the_team_note_points_back() -> None:
    from pisa.connectors.speech import _failure
    from pisa.domain import talk

    assert not _failure("OpenAI", 429, "speech", '{"error": {"message": "see billing docs"}}').no_credit
    assert talk.points_back("segnalalo al team") and talk.points_back("dillo agli sviluppatori")
    assert not talk.points_back("segnala al team che rispondi due volte")
