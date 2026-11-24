# ruff: noqa: E501
"""Perceive then ask, Pisa's own states, the character file and Pisa's opinions. One test (or a few) per behaviour;
each fails when its part is reverted. Invented names."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from pisa.domain import talk
from pisa.domain import voice2 as rules
from pisa.domain.decisions import Decision
from tests.conftest import pair
from tests.native import native

TZ = ZoneInfo("Europe/Rome")
DAY = "2026-11-11"
FAST = {"pace": "fast", "loudness": "usual", "gaps": "few", "place": "normal"}
SLOW = {"pace": "slow", "loudness": "soft", "gaps": "many", "place": "normal"}


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _at(clock: Any, text: str) -> None:
    clock.now = local(text).astimezone(UTC)


def _phone(db: Any) -> uuid.UUID:
    row = db.execute("SELECT id FROM devices").fetchone()
    return row[0] if row else uuid.UUID(pair(db, "iPhone")[0])


def _event(db: Any, title: str, start: datetime, attendees: int = 0, event_id: str | None = None) -> str:
    db.execute("INSERT INTO calendars (id, name, access_role) VALUES ('me@example.invalid', 'Personale', 'owner') "
               "ON CONFLICT DO NOTHING")  # fmt: skip
    event_id = event_id or str(uuid.uuid4())
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, attendees) "
        "VALUES ('me@example.invalid', %s, %s, %s, %s, false, 'confirmed', %s)",
        (event_id, title, start, start + timedelta(minutes=30), attendees),
    )
    native(db, event_id)  # Their own Google event (the attendees still decide)
    return event_id


def _full_day(db: Any, attendees: int = 0) -> None:
    for hour in (9, 10, 11, 14, 16, 17):
        _event(db, f"Lavoro {hour}", local(f"{DAY}T{hour:02d}:00"), attendees)


def _v2(client: Any) -> Any:
    return client.app.state.runtime.conversations.deps.perceive


def _after(
    client: Any, device: uuid.UUID, text: str, *, heard: Any = None, reply: str = "Fatto.", **kw: Any
) -> tuple[str | None, object]:
    """The end of one model turn: (Pisa's line, or None; the turn its question is bound to)."""
    turn = object()
    line = client.portal.call(
        lambda: _v2(client).after_turn(device, text, "it-IT", reply, turn=turn, heard=heard, **kw)
    )
    return line, turn


def _key(db: Any, secrets_key: str) -> None:
    from tests.conftest import set_key

    set_key(db, secrets_key)


def _say(
    client: Any, device: uuid.UUID, text: str, heard: Any = None, lang: str = "it-IT"
) -> tuple[str, bool]:
    """His words through Pisa's conversation; (Pisa's reply, answered by code: no model round)."""
    conversations = client.app.state.runtime.conversations
    before = conversations._last_turn.get(device)

    async def emit(delta: str, final: bool) -> None:
        return None

    said = client.portal.call(
        lambda: conversations.respond(device, text, lang, emit, trace_id=uuid.uuid4().hex, heard=heard)
    )
    state = conversations._last_turn.get(device)
    return str(said or ""), state is None or state is before or state.loop.rounds == 0


# --- row 5: no_label (design-2 §2.3's three unit cases, IT and EN) --------------------------------------


def test_no_label_the_three_cases_it_and_en() -> None:
    assert talk.no_label("Sei stressato, ti sposto la cena.") == ["label"]
    assert talk.no_label("Stanco morto: stasera hai solo la cena.", "sono stanco morto") == []
    assert talk.no_label("Parli veloce.") == ["observable without a question"]
    assert talk.no_label("You're stressed, I'll move dinner.") == ["label"]
    assert talk.no_label("Exhausted: tonight it's just dinner.", "I'm exhausted") == []
    assert talk.no_label("You're talking fast.") == ["observable without a question"]
    # LOW 7: the question forms are labels too
    for asked in ("Are you stressed?", "Ti senti stressato?", "Feeling stressed?", "You sound down: everything ok?",
                  "Sei stressato?", "Do you feel anxious?"):  # fmt: skip
        assert talk.no_label(asked) == ["label"], asked
    for fine in ("Is the market down?", "Feeling good about the plan?", "Ti senti pronto per domani?"):
        assert talk.no_label(fine) == [], fine


def test_every_question_pisa_can_ask_holds_no_label_and_names_an_observable() -> None:
    for lang in ("it-IT", "en-US"):
        for tempo in ("up", "down"):
            for channels in (("voice", "day"), ("voice", "words"), ("words", "day"), ("words",)):
                if tempo == "down" and "day" in channels:
                    continue  # the day says only "full"
                ask = rules.Ask(tempo, "two_channels", channels)  # type: ignore[arg-type]
                q = rules.question(ask, lang, events=6)
                assert rules.holds(q) and q.count("?") == 1, q
                assert "lascio stare" in q or "leave it" in q  # they can wave it away
                assert rules.holds(rules.question(ask, lang, his="cotto"), "cotto")
    assert not rules.holds("Ti sento stressato: giornata pesante?")  # a label is never asked


# --- row 1: the guard (two channels, their reason, never from outside, never with others) ----------------


def test_one_channel_alone_never_asks() -> None:
    for channel in rules.CHANNELS:
        assert rules.decide(rules.Context(cues={channel: "up"})) is None
    ask = rules.decide(rules.Context(cues={"voice": "up", "day": "up"}))
    assert ask == rules.Ask("up", "two_channels", ("voice", "day"))
    assert rules.decide(rules.Context(cues={"voice": "up", "day": "down"})) is None  # they disagree
    # a fact Pisa is missing for something they asked: one channel is enough
    assert rules.decide(rules.Context(cues={"voice": "down"}, asked_for=True)) == rules.Ask(
        "down", "missing_fact", ("voice",)
    )
    assert rules.asks_for_fit("cosa faccio stasera?") and not rules.asks_for_fit("cosa ho stasera?")


def test_the_guard_s_rails() -> None:
    two: dict[Any, Any] = {"voice": "up", "words": "up"}
    assert rules.decide(rules.Context(cues=two)) is not None
    for blocked in (
        rules.Context(cues=two, outside=True),  # A turn that read outside text asks nothing
        rules.Context(cues=two, others=True),
        rules.Context(cues=two, floor=True),
        rules.Context(cues=two, open_question=True),
        rules.Context(cues=two, quiet=frozenset({"up"})),
        rules.Context(cues=two, asked_today=("up",)),  # never twice a kind a day
        rules.Context(cues=two, asked_today=("down", "down")),  # the ceiling against bugs: 2 a day
    ):
        assert rules.decide(blocked) is None, blocked
    # only their three channels count: anything else (a mail, a page, a tool) is not a channel
    assert rules.decide(rules.Context(cues={"voice": "up", "mail": "up"})) is None  # type: ignore[dict-item]
    # a mail saying the opposite never cancels (or makes) their own cue either
    assert rules.decide(rules.Context(cues={"voice": "up", "mail": "down"}, asked_for=True)) == rules.Ask(  # type: ignore[dict-item]
        "up", "missing_fact", ("voice",)
    )


def test_the_voice_is_observables_only_and_a_whisper_or_noise_means_others() -> None:
    assert rules.voice_tempo(rules.Heard.of(FAST)) == "up"
    assert rules.voice_tempo(rules.Heard.of(SLOW)) == "down"
    assert rules.voice_tempo(rules.Heard.of({"pace": "usual"})) is None
    assert rules.Heard.of({"emotion": "stressed"}) is None  # no such field: nothing heard
    assert rules.Heard.of({"pace": "angry"}) is None
    for place in ("whisper", "noisy_place"):
        heard = rules.Heard.of({**FAST, "place": place})
        assert rules.others_maybe(heard) and rules.voice_tempo(heard) is None


def test_two_channels_ask_with_the_observable_and_one_channel_does_not(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _full_day(db)
    _at(clock, f"{DAY}T18:30")
    line, _ = _after(client, device, "spostami la palestra", heard=None)
    assert line is None  # the day alone (6 of their events) never asks
    line, _ = _after(client, device, "ok", heard={"pace": "fast"}, reply="Ok.")
    assert line == "Ti sento di corsa: giornata pesante? O lascio stare."  # voice + day
    row = db.execute("SELECT tempo, channels, reason, status FROM pisa_cue_asks").fetchone()
    assert row == ("up", ["voice", "day"], "two_channels", "open")
    assert not talk.no_label(line)


def test_a_question_that_would_label_him_is_refused(client, db, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _full_day(db)
    _at(clock, f"{DAY}T18:30")
    monkeypatch.setattr(
        rules, "question", lambda *a, **kw: "Sei stressato: giornata pesante? O lascio stare."
    )
    assert _after(client, device, "ok", heard=FAST, reply="Ok.")[0] is None
    assert db.execute("SELECT count(*) FROM pisa_cue_asks").fetchone()[0] == 0


def test_good_days_too(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T19:00")
    line, _ = _after(client, device, "finalmente a casa", heard=SLOW)  # their voice + their words
    assert line == "Parli più disteso del solito: è andata bene? O lascio stare."


def test_outside_text_never_causes_or_fills_a_question(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T18:30")
    _full_day(db, attendees=3)  # six invites with other people: a stranger's titles, never their day
    db.execute("INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) VALUES "
               "('me@example.invalid', 'inv', 'Sei stressato? Giornata pesante', %s, %s, false, 'confirmed')",
               (local(f"{DAY}T12:00"), local(f"{DAY}T12:30")))  # fmt: skip
    line, _ = _after(client, device, "leggimi la mail", heard=FAST)
    assert line is None  # the invites don't make a day: one channel (the voice) alone
    # a turn that read outside text (a mail subject "sei stressato?") asks nothing, two channels or not
    line, _ = _after(client, device, "di corsa, leggimi la mail", heard=FAST, outside=True,
                     reply="Ti ha scritto Paolo: «sei stressato?».")  # fmt: skip
    assert line is None
    assert db.execute("SELECT count(*) FROM pisa_cue_asks").fetchone()[0] == 0


def test_no_cue_question_with_others_present(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _full_day(db)  # their words and their day agree: only people near them stop the question
    _at(clock, f"{DAY}T18:30")
    for place in ("whisper", "noisy_place"):
        line, _ = _after(client, device, "di corsa, cosa ho dopo?", heard={**FAST, "place": place})
        assert line is None, place
    _event(db, "Riunione", local(f"{DAY}T18:15"), attendees=2)  # a meeting with other people, now
    line, _ = _after(client, device, "di corsa, cosa ho dopo?", heard=FAST)
    assert line is None
    assert db.execute("SELECT count(*) FROM pisa_cue_asks").fetchone()[0] == 0


def test_never_inside_a_floor_message_or_after_a_question(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T18:30")
    assert _after(client, device, "di corsa, cosa ho?", heard=FAST, floor=True)[0] is None
    assert (
        _after(client, device, "di corsa, sposta la cena", heard=FAST, reply="La sposto alle 21?")[0] is None
    )
    assert _after(client, device, "di corsa, sposta la cena", heard=FAST, open_question=True)[0] is None
    assert _after(client, device, "di corsa, cosa ho?", heard=FAST)[0] is not None


def test_a_waved_away_kind_stays_quiet_until_he_raises_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T18:30")
    line, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    assert line
    said = client.portal.call(_v2(client).heard, device, "lascia stare", "it-IT", turn)
    assert said == "Va bene, lascio stare."
    assert db.execute("SELECT status FROM pisa_cue_asks").fetchone()[0] == "waved"
    _at(clock, "2026-11-12T18:30")  # the next day, the same cues
    assert _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)[0] is None
    assert _after(client, device, "di corsa anche oggi", heard=FAST)[0] is None
    # they raise it themselves: the kind is their again (not asked in that very turn: they said it)
    assert _after(client, device, "giornata pesante oggi, cosa ho domani?", heard=FAST)[0] is None
    _at(clock, "2026-11-13T18:30")
    assert _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)[0] is not None


def test_an_unanswered_question_goes_quiet_too(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T18:30")
    line, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    assert line
    assert client.portal.call(_v2(client).heard, device, "e domani cosa ho?", "it-IT", turn) is None
    _after(client, device, "e domani cosa ho?", heard=None)  # the model answered; they went on
    assert db.execute("SELECT status FROM pisa_cue_asks").fetchone()[0] == "unanswered"
    _at(clock, "2026-11-12T18:30")
    assert _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)[0] is None


def test_his_state_is_kept_only_on_his_yes_and_said_back_in_his_word(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    _at(clock, f"{DAY}T18:30")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    said = client.portal.call(v2.heard, device, "sì, cotto", "it-IT", turn)
    assert said == "Capito. Te lo ricordo con la tua parola, «cotto»?"
    assert db.execute("SELECT count(*) FROM pisa_his_states").fetchone()[0] == 0  # named, never kept yet
    turn2 = object()
    v2.bind(device, turn2)
    assert (
        client.portal.call(v2.heard, device, "sì, ricordalo", "it-IT", turn2) == "Ok, «cotto»: me lo ricordo."
    )
    assert db.execute("SELECT tempo, word FROM pisa_his_states").fetchall() == [("up", "cotto")]
    _at(clock, "2026-11-12T18:30")
    line, _ = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    assert line == "Ti sento di corsa: cotto anche oggi? O lascio stare."  # their word, never Pisa's


def test_a_no_to_keeping_it_keeps_nothing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    _at(clock, f"{DAY}T18:30")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    client.portal.call(v2.heard, device, "sono a pezzi", "it-IT", turn)
    turn2 = object()
    v2.bind(device, turn2)
    assert client.portal.call(v2.heard, device, "no", "it-IT", turn2) == "Ok, non lo tengo."
    assert db.execute("SELECT count(*) FROM pisa_his_states").fetchone()[0] == 0
    # and a word in the same breath as their "ricordalo" is kept at once, as they said it
    _at(clock, "2026-11-12T18:30")
    db.execute("DELETE FROM pisa_cue_quiet")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    assert client.portal.call(v2.heard, device, "sì, stanco morto, ricordalo", "it-IT", turn) == (
        "Ok, «stanco morto»: me lo ricordo."
    )
    assert db.execute("SELECT word FROM pisa_his_states").fetchall() == [("stanco morto",)]


def test_his_word_is_his_exactly() -> None:
    assert rules.his_word("sì, cotto") == "cotto"
    assert rules.his_word("sono stanco morto") == "stanco morto"
    assert rules.his_word("Abbastanza, sono a pezzi") == "a pezzi"
    assert (
        rules.his_word("sì") is None
        and rules.his_word("no") is None
        and rules.his_word("sì, ricordalo") is None
    )
    assert rules.answer("lascia stare", "up") == "wave" and rules.answer("tutto ok", "up") == "wave"
    assert rules.answer("sì, abbastanza", "up") == "yes"
    assert rules.answer("e domani cosa ho?", "up") is None


def test_the_ceiling_against_bugs_two_a_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T19:00")
    _full_day(db)
    assert _after(client, device, "ok", heard=FAST, reply="Ok.")[0]  # up
    db.execute("DELETE FROM calendar_events")
    db.execute("DELETE FROM pisa_cue_quiet")
    assert _after(client, device, "finalmente", heard=SLOW)[0]  # down
    db.execute("DELETE FROM pisa_cue_quiet")
    db.execute("DELETE FROM pisa_cue_asks WHERE tempo = 'up'")
    db.execute("INSERT INTO pisa_cue_asks (id, tempo, channels, reason, status, asked_at) VALUES (%s, 'down', "
               "'{voice}', 'two_channels', 'answered', %s)", (uuid.uuid4(), clock.now))  # fmt: skip
    _full_day(db)
    assert _after(client, device, "ok", heard=FAST, reply="Ok.")[0] is None  # two today already


# --- row 1 through the conversation: the question at the end of a turn, the answer by code ---------------


def test_through_the_conversation_the_question_ends_the_reply_and_his_answer_is_code_s(  # type: ignore[no-untyped-def]
    client, db, clock, fake_models, secrets_key
) -> None:
    _key(db, secrets_key)
    device = _phone(db)
    _full_day(db)
    _at(clock, f"{DAY}T18:30")
    fake_models.default = "Dopo hai Lavoro 17 alle 17:00."
    said, by_code = _say(client, device, "cosa ho dopo?", heard=FAST)
    assert not by_code and said.endswith("Ti sento di corsa: giornata pesante? O lascio stare.")
    said, by_code = _say(client, device, "lascia stare")
    assert by_code and said == "Va bene, lascio stare."
    assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0  # nothing done or sent
    fake_models.default = "Ok."
    said, _ = _say(client, device, "cosa ho domani?", heard=FAST)
    assert "giornata pesante" not in said  # quiet now


def test_a_typed_turn_with_no_voice_asks_only_on_words_and_day(
    client, db, clock, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    _key(db, secrets_key)
    device = _phone(db)
    _full_day(db)
    _at(clock, f"{DAY}T18:30")
    fake_models.default = "Dopo hai Lavoro 17."
    said, _ = _say(client, device, "cosa ho dopo?")  # the day alone
    assert "?" not in said
    said, _ = _say(client, device, "di corsa: cosa ho dopo?")  # their words + their day
    assert said.endswith("Oggi sei a 6 impegni: giornata pesante? O lascio stare.")


# --- row 2: Pisa's own states ---------------------------------------------------------------------------


def test_pisa_s_state_says_only_its_records_wrong_first_never_needy(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    _at(clock, f"{DAY}T18:30")
    said = client.portal.call(v2.state, device, "it-IT")
    assert said.startswith(
        "Nei miei registri, niente di nuovo. Ci sono, se serve."
    )  # no record: nothing invented
    db.execute("INSERT INTO understand_corrections (id, heard, meant, lang, created_at) VALUES (%s, 'Anna', 'Ana', "
               "'it-IT', %s)", (uuid.uuid4(), clock.now - timedelta(days=1)))  # fmt: skip
    for n in range(3):
        db.execute("INSERT INTO profile_claims (id, kind, statement, provenance, state, created_at, updated_at) "
                   "VALUES (%s, 'like', %s, 'said', 'active', %s, %s)",
                   (uuid.uuid4(), f"Gli piace {n}", clock.now, clock.now))  # fmt: skip
    db.execute("INSERT INTO profile_claims (id, kind, statement, provenance, state, created_at, updated_at) "
               "VALUES (%s, 'routine', 'Forse il venerdì sushi', 'inferred', 'candidate', %s, %s)",
               (uuid.uuid4(), clock.now, clock.now))  # fmt: skip
    said = client.portal.call(v2.state, device, "it-IT")
    assert said.startswith(
        "Ho sbagliato una cosa: avevo capito «Anna», era «Ana». L'ho corretto. "  # wrong first, out loud
        "Questa settimana ho imparato 3 cose su di te. Ho una cosa su di te che vorrei capire meglio. "
        "Ci sono, se serve."
    )
    assert "Gli piace" not in said and "sushi" not in said  # counts of its records, never their claims' words
    assert not rules.needy(said) and not talk.no_label(said)


def test_needy_lines_are_caught() -> None:
    for line in ("Mi manchi.", "Mi sento solo oggi.", "I miss you", "I'm lonely without you", "Sono felice!"):
        assert rules.needy(line), line
    assert not rules.needy(rules.state_answer(rules.StateRecords(("a", "b"), 2, 2), "en-US"))


def test_come_stai_is_pisa_s_by_code_and_a_trait_is_his_to_approve(
    client, db, clock, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    _key(db, secrets_key)
    device = _phone(db)
    _at(clock, f"{DAY}T18:30")
    said, by_code = _say(client, device, "e tu, come stai?")
    assert by_code
    assert said == (
        "Nei miei registri, niente di nuovo. Ci sono, se serve. Una cosa su di me: vorrei tenere un tratto mio, "
        "«dire prima quando sbaglio». Ti va?"
    )
    said, by_code = _say(client, device, "sì")
    assert by_code and said == "Grazie: lo tengo."
    assert db.execute("SELECT trait, status FROM pisa_character").fetchall() == [("wrong_first", "approved")]
    said, _ = _say(client, device, "com'è il tuo carattere?")
    assert said == "Il mio carattere, finora: dire prima quando sbaglio."
    said, _ = _say(client, device, "come stai?")
    assert said.endswith("«parlare corto, la cosa prima». Ti va?")  # the next trait, one at a time
    said, _ = _say(client, device, "no")
    assert said == "Va bene, lo lascio."
    said, _ = _say(client, device, "how are you, Pisa?", lang="en-US")
    assert said.endswith("“keeping an eye on your days adding up”. Is that ok?")
    fake_models.default = "Ok."
    _say(client, device, "cosa ho domani?")  # they go on: that trait stays unanswered, never proposed again
    said, _ = _say(client, device, "come stai?")
    assert said == "Nei miei registri, niente di nuovo. Ci sono, se serve."
    assert {r[0]: r[1] for r in db.execute("SELECT trait, status FROM pisa_character")} == {
        "wrong_first": "approved", "short": "declined", "days_add_up": "proposed",
    }  # fmt: skip


# --- row 3: opinions, once, with the rails ----------------------------------------------------------


def test_the_opinion_rails_health_tax_other_people() -> None:
    assert rules.opinion_rails("Dormi poco: non fa bene alla salute.") == ["health"]
    assert rules.opinion_rails("You should sleep more.") == ["health"]
    assert rules.opinion_rails("Conviene detrarlo nel 730.") == ["tax"]
    assert rules.opinion_rails("Put it as a tax write-off.") == ["tax"]
    assert rules.opinion_rails("Con Marco perdi tempo.", ["Marco"]) == ["other people"]
    for kind in ("disagree", "habit"):
        for lang in ("it-IT", "en-US"):
            line = rules.opinion(kind, lang, n=3)  # type: ignore[arg-type]
            assert rules.opinion_rails(line) == [], line
            assert "una volta sola" in line or "just once" in line  # once, with its reason


def _moved(db: Any, event_id: str, title: str, at: datetime, notified: list[str] | None = None) -> uuid.UUID:
    approval_id = uuid.uuid4()
    payload = {"event_id": event_id, "title": title, "notified": notified or []}
    db.execute("INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at) VALUES "
               "(%s, 'calendar_move', 's', %s, 'done', %s, %s)", (approval_id, Jsonb(payload), at, at))  # fmt: skip
    return approval_id


def test_pisa_disagrees_once_on_a_decision_then_lets_it_go(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T10:00")
    gym = _event(db, "Palestra", local(f"{DAY}T19:00"))
    for days in (3, 1):
        _moved(db, gym, "Palestra", clock.now - timedelta(days=days))
    move = Decision(
        "act", "move_event", "owner_words", ref=str(_moved(db, gym, "Palestra", clock.now)), outcome="done"
    )
    line, _ = _after(
        client, device, "sposta la palestra a domani", reply="Spostata a domani alle 19.", moves=[move]
    )
    assert line == ("Un parere, una volta sola: è la 3ª volta in una settimana che spostiamo questo impegno. Forse "
                    "gli va dato un orario fisso; decidi tu.")  # fmt: skip
    assert "Palestra" not in line  # MEDIUM 5: the title is never quoted
    move = Decision(
        "act", "move_event", "owner_words", ref=str(_moved(db, gym, "Palestra", clock.now)), outcome="done"
    )
    assert _after(client, device, "spostala ancora", reply="Spostata.", moves=[move])[0] is None  # let go


def test_never_an_opinion_about_other_people_or_from_outside(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T10:00")
    db.execute("INSERT INTO people (id, name, name_key, source, created_at, updated_at) VALUES (%s, 'Nino', 'nino', "
               "'owner', now(), now())", (uuid.uuid4(),))  # fmt: skip
    lunch = _event(db, "Pranzo con Nino", local(f"{DAY}T13:00"))
    call = _event(db, "Call", local(f"{DAY}T15:00"))  # their event; the move notified someone
    for event, title, notified in (
        (lunch, "Pranzo con Nino", None),
        (call, "Call", ["anna@example.invalid"]),
    ):
        for days in (3, 2, 1):
            _moved(db, event, title, clock.now - timedelta(days=days), notified)
        ref = str(_moved(db, event, title, clock.now, notified))
        for provenance in ("owner_words", "outside_text"):
            move = Decision("act", "move_event", provenance, ref=ref, outcome="done")  # type: ignore[arg-type]
            assert _after(client, device, "spostalo", reply="Spostato.", moves=[move])[0] is None
    assert db.execute("SELECT count(*) FROM pisa_opinions").fetchone()[0] == 0


def test_a_habit_view_once_at_night(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    conversation = uuid.uuid4()
    db.execute("INSERT INTO conversations (id, device_id) VALUES (%s, %s)",
               (conversation, device))  # fmt: skip
    for night in ("2026-11-09T01:20", "2026-11-10T02:05"):
        db.execute("INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'user', "
                   "'x', 'it-IT', %s)", (conversation, local(night)))  # fmt: skip
    _at(clock, f"{DAY}T01:40")
    line, _ = _after(client, device, "cosa ho domani?", reply="Domani hai il dentista alle 9.")
    assert line == ("Un parere, una volta sola: è la 3ª notte questa settimana che parliamo dopo l'una. Se vuoi, la "
                    "sera ti preparo il giorno dopo prima delle 23; decidi tu.")  # fmt: skip
    _at(clock, "2026-11-12T01:40")
    assert _after(client, device, "cosa ho domani?", reply="Niente.")[0] is None  # once, then let go


# --- the Sunday card counts them ----------------------------------------------------------------------


def test_the_sunday_card_counts_the_cue_questions(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import ledger as ledger_rules

    for status in ("answered", "named", "waved", "unanswered"):
        db.execute("INSERT INTO pisa_cue_asks (id, tempo, channels, reason, status, asked_at) VALUES (%s, 'up', "
                   "'{voice,day}', 'two_channels', %s, %s)", (uuid.uuid4(), status, local(f"{DAY}T18:00")))  # fmt: skip
    counts = client.portal.call(_v2(client).week, local("2026-11-09T00:00"), local("2026-11-16T00:00"))
    assert counts == {"asked": 4, "answered": 2, "waved": 1, "unanswered": 1}
    line = ledger_rules.cue_line(counts, "it-IT")
    assert line == "Domande su come stai: 4 (risposte 2, lasciate stare 1, senza risposta 1)."
    sheet = ledger_rules.sheet("it-IT", total=0, undone=0, top=[], undoable=[], proposals=[], subscriptions=[],
                               cue_line=line)  # fmt: skip
    assert {"type": "text", "text": line} in sheet["blocks"]
    assert ledger_rules.cue_line({"asked": 0}, "it-IT") is None


# --- the contract: utterance.voice, observables only, optional ---------------------------------------


def test_the_utterance_carries_only_observables_and_old_builds_send_none() -> None:
    import json

    import pytest
    from pydantic import ValidationError

    from pisa.services.session_messages import Utterance
    from tests.live_app import message_of

    msg = message_of(json.dumps({"v": 1, "id": str(uuid.uuid4()), "ts": "2026-11-05T18:40:00+01:00",
                                    "type": "utterance", "text": "ciao", "lang": "it-IT", "final": True,
                                    "source": "phone_mic", "voice": FAST}))  # fmt: skip
    assert isinstance(msg, Utterance) and msg.voice is not None and msg.voice.pace == "fast"
    assert rules.Heard.of(msg.voice) == rules.Heard("fast", "usual", "few", "normal")
    old = Utterance(text="ciao", lang="it-IT", final=True, source="phone_mic")
    assert old.voice is None and "voice" not in old.model_dump()
    with pytest.raises(ValidationError):
        Utterance.model_validate({"text": "ciao", "lang": "it-IT", "final": True, "source": "phone_mic",
                                  "voice": {"mood": "sad"}})  # fmt: skip


def test_the_gateway_hands_the_observables_to_the_turn(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.live_app import live

    _key(db, secrets_key)
    device_id, token = pair(db)
    seen: list[Any] = []
    conversations = client.app.state.runtime.conversations
    real = conversations.respond

    async def respond(*args: Any, **kwargs: Any) -> str:
        seen.append(kwargs.get("heard"))
        return await real(*args, **kwargs)

    conversations.respond = respond
    with live(client, token, device_id) as app:
        app.say("ciao", lang="it-IT", source="phone_mic", voice={"pace": "fast"})
        list(app.reply())
    assert seen and seen[0] is not None and seen[0].pace == "fast"


# --- the prompt: unchanged with the new tables empty or full ----------------------------------------


def test_a_turn_s_prompt_is_byte_identical_with_the_tables_empty_or_full(
    client, db, clock, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    _key(db, secrets_key)
    device = _phone(db)
    _at(clock, f"{DAY}T10:00")
    fake_models.default = "Ok."

    def prompt() -> str:
        fake_models.seen_messages.clear()
        _say(client, device, "cosa ho domani?")
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM conversations")
        client.app.state.runtime.conversations._last_turn.clear()
        messages = fake_models.seen_messages[0]
        out = []
        for m in messages:
            for p in m.parts:
                out.append(f"{type(p).__name__}:{getattr(p, 'content', '')}")
        return "\n".join(out)

    empty = prompt()
    now = clock.now
    db.execute("INSERT INTO pisa_cue_asks (id, tempo, channels, reason, status, asked_at) VALUES (%s, 'up', "
               "'{voice,day}', 'two_channels', 'waved', %s)", (uuid.uuid4(), now))  # fmt: skip
    db.execute("INSERT INTO pisa_cue_quiet (tempo, since, why) VALUES ('up', %s, 'waved')", (now,))
    db.execute(
        "INSERT INTO pisa_his_states (id, tempo, word, kept_at) VALUES (%s, 'up', 'cotto', %s)",
        (uuid.uuid4(), now),
    )
    db.execute(
        "INSERT INTO pisa_character (trait, status, proposed_at) VALUES ('wrong_first', 'approved', %s)",
        (now,),
    )
    db.execute("INSERT INTO pisa_opinions (key, kind, said_at) VALUES ('habit:late', 'habit', %s)", (now,))
    assert prompt() == empty


# --- the review -------------------------------------------------------------------


NOT_ANSWERS = (
    "no, spostami la palestra alle 7", "chiama Marco", "perché?", "cosa?", "dopo", "meteo domani", "sto guidando",
    "ricordalo: compra il latte", "ricordamelo domani", "sì, malato, ricordalo", "depresso", "sì, sono malato",
    "sì ma dimmi il meteo", "no aspetta", "Marco", "sì, Marco",
)  # fmt: skip


def test_his_next_turn_is_never_taken_unless_it_answers_from_the_closed_set() -> None:
    for words in NOT_ANSWERS:
        assert rules.answer(words, "up") is None, words
        assert rules.his_word(words) is None, words
        assert rules.remember_answer(words) is None or words.startswith("no"), words
    assert rules.answer("no", "up") == rules.answer("lascia stare", "up") == "wave"
    assert rules.answer("sì", "up") == "yes" and rules.answer("sì, cotto", "up") == "named"
    assert rules.remember_answer("ricordalo: compra il latte") is None
    assert rules.remember_answer("ricordamelo domani") is None


def test_the_reflex_lets_every_other_sentence_go_to_the_model_and_keeps_nothing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    for n, words in enumerate(NOT_ANSWERS):
        _at(clock, f"2026-11-{11 + n // 2:02d}T18:{30 + (n % 2) * 10}")
        db.execute("DELETE FROM pisa_cue_quiet")
        db.execute("DELETE FROM pisa_cue_asks")
        line, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
        assert line, words
        assert client.portal.call(v2.heard, device, words, "it-IT", turn) is None, words
        assert client.portal.call(v2.heard, device, "sì", "it-IT", turn) is not None  # still theirs to answer
    assert db.execute("SELECT count(*) FROM pisa_his_states").fetchone()[0] == 0


def test_a_name_of_his_people_or_a_health_word_is_never_kept() -> None:
    assert rules.keepable("cotto") and rules.keepable("stanco morto")
    for word in ("malato", "depresso", "ansia", "febbre", "chiama Marco", "Marco"):
        assert not rules.keepable(word), word
    assert rules.keepable("serena") and not rules.keepable("serena", ["Serena"])


def test_a_name_of_his_people_is_never_kept_through_the_reflex(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    db.execute("INSERT INTO people (id, name, name_key, source, created_at, updated_at) VALUES (%s, 'Serena', "
               "'serena', 'owner', now(), now())", (uuid.uuid4(),))  # fmt: skip
    _at(clock, f"{DAY}T19:00")
    _, turn = _after(client, device, "finalmente a casa", heard=SLOW)
    assert client.portal.call(v2.heard, device, "sì, serena, ricordalo", "it-IT", turn) == (
        "Capito. Ci sono, se serve."
    )
    assert db.execute("SELECT count(*) FROM pisa_his_states").fetchone()[0] == 0


def test_newest_wins_a_question_else_and_a_talk_s_yes(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    _at(clock, f"{DAY}T18:30")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    # a check-in ("Sei passato in farmacia?") said since: their "sì" is its, never the cue's
    assert client.portal.call(lambda: v2.heard(device, "sì", "it-IT", turn, newer=True)) is None
    # in a talk, a bare yes or no is not decided by code
    assert client.portal.call(lambda: v2.heard(device, "sì", "it-IT", turn, yes_by_code=False)) is None
    assert client.portal.call(lambda: v2.heard(device, "no", "it-IT", turn, yes_by_code=False)) is None
    assert db.execute("SELECT status FROM pisa_cue_asks").fetchone()[0] == "open"
    # Pisa asked them something else ("Cosa gli scrivo?"): "e tu, come stai?" is the message's text
    assert (
        client.portal.call(lambda: v2.heard(device, "e tu, come stai?", "it-IT", object(), asked_else=True))
        is None
    )


def test_a_check_in_between_takes_his_yes_through_the_conversation(
    client, db, clock, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    _key(db, secrets_key)
    device = _phone(db)
    _full_day(db)
    _at(clock, f"{DAY}T18:30")
    fake_models.default = "Dopo hai Lavoro 17."
    said, _ = _say(client, device, "cosa ho dopo?", heard=FAST)
    assert said.endswith("O lascio stare.")
    client.app.state.runtime.conversations.unasked.said(device, None)  # Pisa's reminder, unasked, since
    fake_models.default = "Ok."
    said, _ = _say(client, device, "sì")
    assert said != "Capito. Ci sono, se serve."
    assert db.execute("SELECT status FROM pisa_cue_asks").fetchone()[0] != "answered"


def test_a_named_word_and_a_question_expire(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    _at(clock, f"{DAY}T18:30")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    client.portal.call(v2.heard, device, "sì, cotto", "it-IT", turn)
    turn2 = object()
    v2.bind(device, turn2)
    clock.now += timedelta(seconds=rules.REMEMBER_WITHIN_S + 1)
    assert client.portal.call(v2.heard, device, "sì", "it-IT", turn2) is None  # too late: nothing kept
    assert db.execute("SELECT count(*) FROM pisa_his_states").fetchone()[0] == 0
    _at(clock, "2026-11-12T18:30")
    db.execute("DELETE FROM pisa_cue_quiet")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    clock.now += timedelta(seconds=rules.REMEMBER_WITHIN_S + 1)
    assert client.portal.call(v2.heard, device, "lascia stare", "it-IT", turn) is None  # the question expired


def test_no_view_on_an_event_with_a_medical_word_or_someone_in_it() -> None:
    for title in ("Psicologa", "Cardiologo", "Fisioterapia", "Chemio", "Therapy", "Pranzo con mamma",
                  "Cena con Nino", "Dentista", "Visita dal dottore", "Lunch with Sam"):  # fmt: skip
        assert rules.private_title(title), title
    for title in ("Palestra", "Corsa", "Spesa", "Gym"):
        assert not rules.private_title(title), title
    assert rules.private_title("Calcetto Nino", ["Nino"])


def test_never_a_view_on_a_private_title_through_the_service(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    _at(clock, f"{DAY}T10:00")
    for title in ("Psicologa", "Cena con Nino"):
        event = _event(db, title, local(f"{DAY}T19:00"))
        for days in (3, 2):
            _moved(db, event, title, clock.now - timedelta(days=days))
        move = Decision("act", "move_event", "owner_words", ref=str(_moved(db, event, title, clock.now)),
                        outcome="done")  # fmt: skip
        assert _after(client, device, "spostalo", reply="Spostato.", moves=[move])[0] is None, title
    assert db.execute("SELECT count(*) FROM pisa_opinions").fetchone()[0] == 0


def test_an_answered_question_is_never_swept_quiet(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = _phone(db)
    v2 = _v2(client)
    _at(clock, f"{DAY}T18:30")
    _, turn = _after(client, device, "di corsa: cosa ho dopo?", heard=FAST)
    assert client.portal.call(v2.heard, device, "sì", "it-IT", turn) == "Capito. Ci sono, se serve."
    ask_id = db.execute("SELECT id FROM pisa_cue_asks").fetchone()[0]

    async def sweep_late() -> None:
        async with v2._pool.connection() as conn:
            await v2._go_quiet(conn, ask_id, "up", "unanswered")

    client.portal.call(sweep_late)
    assert db.execute("SELECT status FROM pisa_cue_asks").fetchone()[0] == "answered"
    assert db.execute("SELECT count(*) FROM pisa_cue_quiet").fetchone()[0] == 0
