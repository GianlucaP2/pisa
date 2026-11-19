"""One consent card for every outward act. Every name,
address and text here is synthetic. Each item's tests fail on revert."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pisa.domain import approvals as approval_rules
from pisa.domain import claims, decisions, results, yescard
from pisa.services.conversation import TurnState, current_turn
from tests.actions_fake import ActionsGoogle
from tests.conftest import pair
from tests.replay import harness
from tests.test_approvals import auth, decide, dinner, google, pending, thread  # noqa: F401
from tests.test_task_pool import _sent_to  # pyright: ignore[reportPrivateUsage]

T0 = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
MOVE = {
    "calendar_id": "cal-1", "event_id": "ev-1", "title": "Cena", "lang": "it-IT",
    "start": {"dateTime": "2026-10-20T21:00:00+02:00"}, "end": {"dateTime": "2026-10-20T22:30:00+02:00"},
    "when": "mar 20 ott alle 21:00", "send_updates": "all", "notified": ["Paola"],
}  # fmt: skip


def run_on(client: Any, device: str | None, fn: Any, *args: Any, said: str = "sì") -> Any:
    """A runtime coroutine in the app's loop, as one turn of `device` where the user said `said`."""

    async def turn() -> Any:
        with decisions.collecting():
            current_turn.set(TurnState(uuid.uuid4(), said, device=uuid.UUID(device) if device else None))
            return await fn(*args)

    return client.portal.call(turn)


# --- 1. one Action record for every outward act -------------------------------------------------------------


def test_every_approval_kind_maps_onto_one_action_record() -> None:
    assert set(yescard.KINDS) == set(approval_rules.KINDS)  # no second system: every kind is a card
    action = yescard.of("calendar_move", MOVE, ["Paola"])
    assert (action.kind, action.tier, action.reversible, action.cost, action.data_out) == (
        "calendar_move", 1, True, None, ("paola",),
    )  # fmt: skip
    # the hash is the digest: the voice, the tap and Face ID bind the same thing
    assert action.hash == approval_rules.approval_digest("calendar_move", MOVE, ["Paola"])
    assert yescard.of("email_send", {"draft_id": "d"}, ["a@x.example"]).reversible is False


@pytest.mark.parametrize(
    "change",
    [
        {"start": {"dateTime": "2026-10-20T21:30:00+02:00"}},  # the time
        {"title": "Cena con tutti"},  # the text
        {"amount": "12.50 EUR"},  # an amount
    ],
)
def test_a_changed_time_text_or_amount_is_another_hash(change: dict[str, Any]) -> None:
    assert (
        yescard.of("calendar_move", MOVE | change, ["Paola"]).hash
        != yescard.of("calendar_move", MOVE, ["Paola"]).hash
    )


def test_a_changed_recipient_is_another_hash() -> None:
    same = yescard.of("email_send", {"draft_id": "d"}, ["Anna@X.example", "bo@y.example"])
    assert same.hash == yescard.of("email_send", {"draft_id": "d"}, ["bo@y.example", "anna@x.example"]).hash
    assert same.hash != yescard.of("email_send", {"draft_id": "d"}, ["anna@x.example", "bo@z.example"]).hash


# --- 3. tiers -----------------------------------------------------------------------------------------------


def test_the_tiers() -> None:
    assert yescard.tier_of("calendar_create", {}) == 1  # send / book / contact
    assert yescard.tier_of("email_send", {"amount": "40 EUR"}) == 2  # money
    assert yescard.tier_of("email_send", {"merge": True}) == 2  # Pisa's own code
    assert yescard.tier_of("weather", {}) == 0  # information: never a card
    money = yescard.of("calendar_create", {"cost": "30 EUR"}, [])
    assert money.face_id and yescard.needs_face_id(money, requires_biometric=False)
    assert yescard.needs_face_id(yescard.of("email_send", {}, []), requires_biometric=True)


def test_tier_two_is_approved_only_with_face_id(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    approvals = client.app.state.runtime.approvals
    paid = MOVE | {"cost": "45 EUR"}
    run_on(client, None, approvals.create, "calendar_move", "Sposto «Cena»?", [], paid)
    (card,) = pending(client, auth)
    assert card["tier"] == 2 and card["requires_biometric"] is True
    db.execute("UPDATE approvals SET requires_biometric = false")  # the tier alone still asks Face ID
    tap = decide(client, auth, card["id"])
    assert tap.status_code == 401 and tap.json()["error"]["code"] == "biometric_required"
    assert "money" in tap.json()["error"]["message"] and not google.writes
    with pytest.raises(Exception, match="Face ID"):  # never on words alone either
        run_on(
            client,
            None,
            lambda: approvals.act("calendar_move", "Sposto", [], paid, device_id=None, lang="it-IT"),
        )


# --- 2. a single-use yes bound to the hash ------------------------------------------------------------------


def test_a_changed_time_voids_the_yes(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    actions = client.app.state.runtime.actions
    run_on(client, None, actions.move_event, "la cena", "oggi alle 21", None)
    (card,) = pending(client, auth)
    db.execute(  # anything that changes the act after the card was shown: here, its time
        "UPDATE approvals SET payload = "
        "jsonb_set(payload, '{start,dateTime}', '\"2026-10-20T23:00:00+02:00\"')"
    )
    void = decide(client, auth, card["id"])
    assert void.status_code == 409 and void.json()["error"]["code"] == "approval_changed"
    assert not google.writes and pending(client, auth) == []
    row = db.execute("SELECT status FROM approvals").fetchone()
    assert row == ("superseded",)  # single use: the void yes can't be given again
    again = decide(client, auth, card["id"])
    assert again.status_code == 409 and not google.writes


def test_a_changed_recipient_voids_the_yes(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    thread(google, db)
    actions = client.app.state.runtime.actions
    run_on(client, None, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    (card,) = pending(client, auth)
    db.execute("UPDATE email_drafts SET cc_addresses = array['Ospite <ospite@z.example>']")
    void = decide(client, auth, card["id"], how="voice")
    assert void.status_code == 409 and void.json()["error"]["code"] == "approval_changed"
    assert google.sent == []


def test_the_yes_holds_ten_seconds_after_the_read_out_and_two_minutes_by_voice() -> None:
    first = T0
    until = yescard.voice_until(first, T0)
    at = {"read_out_at": first, "until": until, "created_at": T0}
    assert yescard.voice_yes(first + timedelta(seconds=10), **at) == "ok"
    assert yescard.voice_yes(first + timedelta(seconds=11), **at) == "late"  # Pisa reads it again
    again = first + timedelta(seconds=100)  # read again: 10 s more, never past the first read-out's 120 s
    assert (
        yescard.voice_yes(again + timedelta(seconds=5), read_out_at=again, until=until, created_at=T0) == "ok"
    )
    assert yescard.voice_yes(first + timedelta(seconds=121), read_out_at=first + timedelta(seconds=118),
                             until=until, created_at=T0) == "over"  # fmt: skip
    assert yescard.PHONE_TTL == timedelta(minutes=10) and yescard.VOICE_TTL == timedelta(seconds=120)


def test_a_late_si_is_refused_and_pisa_reads_the_card_again(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(
        google,
        db,
        attendees=[
            {"email": "owner@example.org", "self": True},
            {"email": "paola@y.example", "displayName": "Paola"},
        ],
    )
    device, _ = pair(db)
    runtime = client.app.state.runtime
    actions = runtime.actions
    run_on(client, device, actions.move_event, "la cena", "oggi alle 21", None)
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    reply = "Sposto «Cena» a oggi alle 21 e avviso Paola?"
    assert run_on(client, device, actions.bundle, uuid.UUID(device), [card_id], reply) == [card_id]
    # The read-out ends once the reply has been said aloud, at 14 characters a second
    ended = clock.now + yescard.spoken_for(reply)
    assert db.execute("SELECT read_out_at FROM approvals").fetchone() == (ended,)
    clock.now = ended + timedelta(seconds=11)
    turn = TurnState(uuid.uuid4(), "sì")

    async def answer() -> str | None:
        current_turn.set(turn)
        return await actions.answer(uuid.UUID(device), "sì", "it-IT", [card_id], [], [card_id])

    said = client.portal.call(answer)
    assert said is not None and said.startswith("Te lo rileggo: Sposto «Cena»")
    assert "avviso Paola" in said and "si può annullare per 24 ore" in said and said.endswith("Confermi?")
    assert not google.writes and db.execute("SELECT status FROM approvals").fetchone() == ("pending",)
    assert turn.approvals == [card_id]  # their next yes answers the same card, by code
    clock.now += timedelta(seconds=3)
    done = client.portal.call(answer)
    assert done is not None and "spostato" in done and google.writes
    assert db.execute("SELECT status, verified FROM approvals").fetchone() == ("done", "verified")


def test_a_spoken_yes_past_two_minutes_goes_to_the_phone(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    device, _ = pair(db)
    actions = client.app.state.runtime.actions
    run_on(client, device, actions.move_event, "la cena", "oggi alle 21", None)
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    run_on(client, device, actions.bundle, uuid.UUID(device), [card_id], "Sposto «Cena» alle 21?")
    clock.now += timedelta(seconds=116)
    approvals = client.app.state.runtime.approvals
    run_on(client, device, lambda: approvals.read_out([card_id], renew=False))  # Pisa reads it again
    clock.now += timedelta(seconds=6)  # 6 s after the latest read-out, past 120 s after the first

    async def answer() -> str | None:
        current_turn.set(TurnState(uuid.uuid4(), "sì"))
        return await actions.answer(uuid.UUID(device), "sì", "it-IT", [card_id], [], [card_id])

    assert client.portal.call(answer) == yescard.on_the_phone("it-IT")
    assert not google.writes and db.execute("SELECT status FROM approvals").fetchone() == ("pending",)


def test_the_server_refuses_a_late_spoken_yes_itself(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """Whatever path brings it (the model's `answer_proposal` too), a spoken yes in the conversation more
    than 10 s after the read-out runs nothing."""
    from pisa.domain.errors import PisaError

    dinner(google, db)
    device, _ = pair(db)
    runtime = client.app.state.runtime
    run_on(client, device, runtime.actions.move_event, "la cena", "oggi alle 21", None)
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    run_on(client, device, runtime.approvals.read_out, [card_id])
    clock.now += timedelta(seconds=11)
    with pytest.raises(PisaError, match="10 seconds"):
        run_on(
            client,
            device,
            lambda: runtime.approvals.decide(card_id, "approve", "voice", in_conversation=True),
        )
    assert not google.writes


def test_the_talk_s_spoken_yes_window_is_the_read_out_grace() -> None:
    from pisa.services import talk

    assert talk.YES_WINDOW_S == 10.0


# --- 4. one renderer per surface ----------------------------------------------------------------------------


def test_the_spoken_card_says_who_what_when_how_much_and_what_cant_be_undone() -> None:
    action = yescard.of("calendar_move", MOVE | {"cost": "45 EUR"}, ["Paola"])
    said = yescard.spoken(action, "Sposto «Cena» a mar 20 ott alle 21:00?", "it-IT")
    assert said == (
        "Sposto «Cena» a mar 20 ott alle 21:00, avviso Paola, costa 45 EUR, si può annullare per 24 ore. "
        "Confermi?"
    )
    mail = yescard.of("email_send", {"names": "Claudio"}, ["claudio@x.example"])
    assert yescard.spoken(mail, "Invio la risposta a Claudio?", "en-US").endswith(
        "it can't be undone. Shall I?"
    )


def test_the_phone_card(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    thread(google, db)
    actions = client.app.state.runtime.actions
    run_on(client, None, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    (card,) = pending(client, auth)
    detail = client.get(f"/v1/approvals/{card['id']}", headers=auth).json()
    shown = detail["card"]
    assert shown["who"] == "Claudio, Paola" and shown["reversible"] is False
    assert shown["irreversible_line"] == "non si può annullare" and shown["tier"] == 1
    assert shown["data_out"] == ["claudio@x.example", "paola@y.example"] and shown["verified"] is None
    stored = db.execute("SELECT action_hash FROM approvals").fetchone()
    assert len(shown["action_hash"]) == 64 and (shown["action_hash"],) == stored


# --- 5. read-back -------------------------------------------------------------------------------------------


def test_after_acting_the_provider_s_state_is_read_back(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    thread(google, db)
    actions = client.app.state.runtime.actions
    run_on(client, None, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    (card,) = pending(client, auth)
    done = decide(client, auth, card["id"]).json()
    assert (done["status"], done["verified"], done["result"]) == (
        "done", "verified", "Risposta inviata a Claudio, Paola."
    )  # fmt: skip


def test_an_unconfirmed_send_is_never_said_done(client, db, google: ActionsGoogle, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    thread(google, db)
    google.sent_lost = True  # Gmail answered the send, but never holds the message as sent
    actions = client.app.state.runtime.actions
    run_on(client, None, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    (card,) = pending(client, auth)
    done = decide(client, auth, card["id"]).json()
    assert (done["status"], done["verified"]) == ("done", "not_verified")
    assert done["result"] == yescard.unverified("email_send", {}, "it-IT")
    assert claims.claim(done["result"]) is None  # no "fatto", no "inviata"


def test_the_calendar_read_back() -> None:
    live = {"status": "confirmed", "start": {"dateTime": "2026-10-20T19:00:00Z"}}
    assert yescard.calendar_confirms("calendar_move", MOVE, live)  # the same moment, in another zone
    assert not yescard.calendar_confirms(
        "calendar_move", MOVE, live | {"start": {"dateTime": "2026-10-20T18:00:00Z"}}
    )
    assert not yescard.calendar_confirms("calendar_move", MOVE, None)
    assert yescard.calendar_confirms("calendar_cancel", MOVE, {"status": "cancelled"})
    assert not yescard.calendar_confirms("calendar_cancel", MOVE, live)


def test_the_claim_rail_backs_no_fatto_without_a_confirming_read_back() -> None:
    result = results.done("ab12cd34", "ab12cd34 NOT verified: …", verified=False)
    assert results.unverified(result) and not results.unverified(results.done("x", "x done: ok"))
    # the act ran (it is in Pisa's recent changes) but its read-back didn't confirm it: no claim stands on it
    record = claims.Record(lately=frozenset({"move_event"}), unverified=frozenset({"move_event"}))
    assert not claims.backed("Ho spostato la cena alle 21.", "it-IT", record)
    assert claims.backed(
        "Ho spostato la cena alle 21.", "it-IT", claims.Record(lately=frozenset({"move_event"}))
    )
    assert not claims.backed("Fatto.", "it-IT", claims.Record(unverified=frozenset({"send_draft"})))
    assert claims.backed(
        "Ho spostato la cena alle 21.", "it-IT", claims.Record(done=frozenset({"move_event"}))
    )
    line = claims.truth_for("Ho spostato la cena.", "it-IT", waiting=False, failed=False, unverified=True)
    assert (
        line == "Non posso dirti che l'ho spostata: non riesco a verificarlo." and claims.claim(line) is None
    )


# --- 6. an ambiguous target asks, never guesses -------------------------------------------------------------


def test_two_marcos_make_a_question_never_a_draft(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _sent_to(db, "Marco Rossi <marco.rossi@x.example>")
    _sent_to(db, "Marco Bianchi <marco.b@y.example>")
    db.execute(  # one of their people, named by them; the other is only a sent mail's display name
        "INSERT INTO people (id, name, name_key, emails, source, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Marco del calcetto', 'marco del calcetto', '{marco.rossi@x.example}', "
        "'owner', now(), now())"
    )
    actions = client.app.state.runtime.actions
    said = run_on(client, None, actions.draft_email, "Marco", "Cena", "Ciao Marco.", said="mandalo a Marco")
    ask = said.split("«", 1)[1]
    assert ask.startswith("Quale Marco: ") and ask.endswith("?»")
    assert "Marco del calcetto <marco.rossi@x.example>" in ask and " o " in ask and "marco.b@y.example" in ask
    assert "Bianchi" not in said and "Rossi <" not in said  # Never a header's display name
    assert google.drafts == {} and pending(client, auth) == []
    assert yescard.which("Marco", ["A", "B", "C"], "en-US") == "Which Marco: A, B or C?"


# --- 7. pass^k per action kind ------------------------------------------------------------------------------


def test_the_replay_reports_pass_k_per_action_kind() -> None:
    move = harness.TurnLog("sposta la cena", "Sposto?", False, calls=[("move_event", {})])
    send = harness.TurnLog("mandala", "Mando?", False, calls=[("send_draft", {}), ("get_agenda", {})])
    ok_move = harness.Result("a", [], [move], 1, [])
    bad_move = harness.Result("b", ["no"], [move], 1, [])
    ok_send = harness.Result("c", [], [send], 1, [])
    text = harness.table([[ok_move, ok_move], [ok_move, bad_move], [ok_send, ok_send]])
    assert "calendar_move        pass^2: 1/2" in text and "email_send           pass^2: 1/1" in text
    assert harness.kind_of_tool("get_agenda") is None
    # A draft is an email's card, Pisa's option a move
    assert [harness.kind_of_tool(t) for t in ("draft_reply", "draft_email", "apply_option")] == [
        "email_send", "email_send", "calendar_move",
    ]  # fmt: skip


# --- The review's fixes ------------------------------------------------------------------


class Says:
    """A conversation model: for each of the user's words, the tools it calls, then exactly what it says."""

    def __init__(self, script: dict[str, tuple[list[tuple[str, dict[str, Any]]], str]]) -> None:
        self.script = script

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        import json

        from pydantic_ai.messages import ToolReturnPart, UserPromptPart
        from pydantic_ai.models.function import DeltaToolCall

        at = max(i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts))
        words = [str(p.content) for p in messages[at].parts if isinstance(p, UserPromptPart)][-1]
        calls, reply = next((v for k, v in self.script.items() if k in words), ([], "Va bene."))
        done = any(isinstance(p, ToolReturnPart) for m in messages[at + 1 :] for p in m.parts)
        if calls and not done:
            yield {n: DeltaToolCall(name=c, json_args=json.dumps(a)) for n, (c, a) in enumerate(calls)}
            return
        yield reply


def turns(client: Any, db: Any, secrets_key: str, clock: Any, *said: tuple[str, str, float]) -> list[str]:
    """The user's turns over one socket: (words, source, seconds after the turn before); Pisa's replies."""
    from tests.conftest import set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    out: list[str] = []
    with live(client, token, device_id) as app:
        for words, source, after in said:
            clock.now += timedelta(seconds=after)
            app.say(words, lang="it-IT", source=source)  # type: ignore[arg-type]
            frames: list[dict[str, Any]] = []
            while not frames or not (frames[-1]["type"] == "say" and frames[-1]["final"]):
                frames.append(app.next())
                assert frames[-1]["type"] != "error", frames[-1]
            out.append("".join(f.get("delta") or "" for f in frames if f["type"] == "say"))
    return out


def test_no_fatto_on_his_words_when_the_read_back_does_not_confirm(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key, clock,  # noqa: F811
) -> None:  # fmt: skip
    """Their own words move it at once, Google takes the write but the read-back shows it unmoved: Pisa's
    "Fatto, ho spostato…" is rewritten in that turn, and next turn "l'hai spostata?" → "Sì, l'ho spostata"
    too."""
    dinner(google, db)
    google.stale_reads = True
    fake_models.default = Says({
        "sposta la cena alle 21": ([("move_event", {"event": "la cena", "start": "oggi alle 21"})],
                                   "Fatto, ho spostato la cena alle 21."),
        "l'hai spostata": ([], "Sì, l'ho spostata alle 21."),
    })  # fmt: skip
    first, second = turns(
        client, db, secrets_key, clock, ("sposta la cena alle 21", "earbuds_mic", 0),
        ("l'hai spostata?", "earbuds_mic", 30),
    )  # fmt: skip
    assert db.execute("SELECT status, verified FROM approvals").fetchone() == ("done", "not_verified")
    assert "Fatto" not in first and "ho spostato" not in first.lower() and "non riesco a verificarlo" in first
    assert "l'ho spostata alle 21" not in second and "non riesco a verificarlo" in second
    logged = db.execute("SELECT outcome FROM pisa_decisions WHERE tool = 'move_event'").fetchone()
    assert logged == ("unverified",)


def test_a_yes_by_code_never_says_an_unverified_act_done(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    google.stale_reads = True
    device, _ = pair(db)
    actions = client.app.state.runtime.actions
    run_on(client, device, actions.move_event, "la cena", "oggi alle 21", None)
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    run_on(client, device, actions.bundle, uuid.UUID(device), [card_id], "Sposto «Cena» alle 21?")

    async def answer() -> str | None:
        current_turn.set(TurnState(uuid.uuid4(), "sì"))
        return await actions.answer(uuid.UUID(device), "sì", "it-IT", [card_id], [], [card_id])

    said = client.portal.call(answer)
    assert said == yescard.unverified("calendar_move", {"title": "Cena"}, "it-IT")


def test_a_typed_yes_keeps_the_card_s_ten_minutes(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key, clock,  # noqa: F811
) -> None:  # fmt: skip
    dinner(
        google,
        db,
        attendees=[
            {"email": "owner@example.org", "self": True},
            {"email": "paola@y.example", "displayName": "Paola"},
        ],
    )
    fake_models.default = Says({
        "sposta la cena": ([("move_event", {"event": "la cena", "start": "oggi alle 21"})],
                           "Sposto «Cena» a oggi alle 21 e avviso Paola?"),
    })  # fmt: skip
    asked, answered = turns(
        client, db, secrets_key, clock, ("sposta la cena", "typed", 0), ("sì", "typed", 150),
    )  # fmt: skip
    assert asked.endswith("?") and "spostato" in answered and "telefono" not in answered
    assert db.execute("SELECT status FROM approvals").fetchone() == ("done",)


def test_the_talk_stamps_the_read_out_when_the_talker_says_it(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from types import SimpleNamespace

    from tests.test_live_talk import _run  # pyright: ignore[reportPrivateUsage]

    run, _, _, _ = _run()
    heard: list[str] = []

    async def aloud() -> None:
        heard.append("aloud")

    run.hooks.asked_aloud = aloud
    run._ask = SimpleNamespace(text="Sposto la cena alle 21?")  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    client.portal.call(lambda: run._pisa_said("Sposto la cena alle 21?", 0))  # pyright: ignore[reportPrivateUsage]
    assert heard == ["aloud"]
    runtime = client.app.state.runtime
    assert runtime.talk.asked_aloud == runtime.conversations.asked_aloud  # wired
    dinner(google, db)
    device, _ = pair(db)
    run_on(client, device, runtime.actions.move_event, "la cena", "oggi alle 21", None)
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    runtime.conversations._last_turn[uuid.UUID(device)] = TurnState(uuid.uuid4(), "x", approvals=[card_id])  # pyright: ignore[reportPrivateUsage]
    clock.now += timedelta(seconds=40)
    client.portal.call(lambda: runtime.conversations.asked_aloud(uuid.UUID(device)))
    assert db.execute("SELECT read_out_at FROM approvals").fetchone() == (clock.now,)


def test_a_fresh_question_renews_the_two_minutes(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    device, _ = pair(db)
    actions = client.app.state.runtime.actions
    run_on(client, device, actions.move_event, "la cena", "oggi alle 21", None)
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    run_on(client, device, actions.bundle, uuid.UUID(device), [card_id], "Sposto «Cena» alle 21?")
    clock.now += timedelta(seconds=150)  # a side question, then Pisa asks it again
    run_on(client, device, actions.bundle, uuid.UUID(device), [card_id], "Allora, sposto «Cena» alle 21?")
    clock.now += timedelta(seconds=5)

    async def answer() -> str | None:
        current_turn.set(TurnState(uuid.uuid4(), "sì"))
        return await actions.answer(uuid.UUID(device), "sì", "it-IT", [card_id], [], [card_id])

    said = client.portal.call(answer)
    assert said is not None and "spostato" in said and google.writes


def test_late_and_over_cards_are_read_again_never_half_decided(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    device, _ = pair(db)
    runtime = client.app.state.runtime
    run_on(client, device, runtime.actions.move_event, "la cena", "oggi alle 21", None)
    run_on(client, device, runtime.actions.create_event, "Dentista", "domani alle 10", None, 45, None, None)
    over, late = (r[0] for r in db.execute("SELECT id::text FROM approvals ORDER BY created_at, kind DESC"))
    db.execute("UPDATE approvals SET voice_until = %s, read_out_at = %s WHERE id = %s",
               (clock.now - timedelta(seconds=1), clock.now - timedelta(seconds=5), over))  # fmt: skip
    db.execute("UPDATE approvals SET voice_until = %s, read_out_at = %s WHERE id = %s",
               (clock.now + timedelta(seconds=60), clock.now - timedelta(seconds=20), late))  # fmt: skip

    async def answer() -> str | None:
        current_turn.set(TurnState(uuid.uuid4(), "sì"))
        return await runtime.actions.answer(uuid.UUID(device), "sì", "it-IT", [over, late], [], [over, late])

    said = client.portal.call(answer)
    assert said is not None and said.startswith(yescard.on_the_phone("it-IT")) and "Te lo rileggo:" in said
    assert "spoken yes" not in said and not google.writes  # never English, never half run
    assert {r[0] for r in db.execute("SELECT status FROM approvals")} == {"pending"}


def test_send_draft_s_late_yes_reads_the_card_again(client, db, google, auth, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.test_approvals import feedback

    thread(google, db)
    device, _ = pair(db)
    actions = client.app.state.runtime.actions
    run_on(client, device, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    db.execute("UPDATE approvals SET payload = payload || '{\"heard\": true}'::jsonb, read_out_at = %s",
               (clock.now - timedelta(seconds=30),))  # fmt: skip

    async def send() -> str:
        turn = TurnState(uuid.uuid4(), "sì mandala", heard_at=clock.now)
        turn.asked_before = frozenset({card_id})
        turn.device = uuid.UUID(device)
        current_turn.set(turn)
        return await actions.send_draft(None)

    said = client.portal.call(send)
    assert feedback(said).startswith("Te lo rileggo:") and google.sent == []


def test_the_read_back_runs_after_the_status_is_saved(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    approvals = client.app.state.runtime.approvals
    seen: list[Any] = []

    async def verify(payload: dict[str, Any], _: Any) -> bool:
        seen.append(db.execute("SELECT status, result IS NOT NULL FROM approvals").fetchone())
        return True

    approvals._verify["calendar_move"] = verify  # pyright: ignore[reportPrivateUsage]
    run_on(client, None, client.app.state.runtime.actions.move_event, "la cena", "oggi alle 21", None)
    (card,) = pending(client, auth)
    assert decide(client, auth, card["id"]).json()["verified"] == "verified"
    assert seen == [("done", True)]


def test_the_phone_card_shows_the_stored_hash(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    dinner(google, db)
    run_on(client, None, client.app.state.runtime.actions.move_event, "la cena", "oggi alle 21", None)
    (card,) = pending(client, auth)
    db.execute("UPDATE approvals SET action_hash = %s", ("ab" * 32,))
    assert client.get(f"/v1/approvals/{card['id']}", headers=auth).json()["card"]["action_hash"] == "ab" * 32


def test_an_unverified_result_keeps_its_undo() -> None:
    import json

    from pisa.services.actions import _done  # pyright: ignore[reportPrivateUsage]

    view = {"id": "ab12cd34-0000", "status": "done", "result": "…", "verified": "not_verified",
            "undo_until": "2026-10-21T13:00:00+02:00"}  # fmt: skip
    out = json.loads(_done(view))
    assert out["verified"] is False and "undo" in out


def test_a_changed_text_voids_the_yes(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """The email's hash binds the draft's version (its payload) and Gmail's held text (the send's
    fingerprint): an edit of the text makes a new version, so the old card's yes runs nothing."""
    thread(google, db)
    actions = client.app.state.runtime.actions
    run_on(client, None, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    (old,) = pending(client, auth)
    run_on(
        client, None, actions.edit_draft, None, "Ciao, la porta Paola.", None, None, said="cambia il testo"
    )
    late = decide(client, auth, old["id"])
    assert late.status_code == 409 and google.sent == []


def test_a_companion_phone_reading_the_card_stamps_the_read_out(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.live_app import live

    device, token = pair(db)
    approvals = client.app.state.runtime.approvals
    with live(client, token, device) as app:
        db.execute(  # in companion mode, earbuds in: the phone reads the card out itself
            "INSERT INTO device_presence (device_id, app, companion, earbuds, updated_at) "
            "VALUES (%s, 'background', 'on', 'connected', now()) ON CONFLICT (device_id) DO UPDATE "
            "SET app = 'background', companion = 'on', earbuds = 'connected', updated_at = now()",
            (device,),
        )
        run_on(client, device, lambda: approvals.create(
            "calendar_move", "Sposto «Cena»?", [], MOVE, device_id=uuid.UUID(device)))  # fmt: skip
        while app.next()["type"] != "interrupt":
            pass
    stamped = db.execute("SELECT read_out_at FROM approvals").fetchone()
    assert stamped == (clock.now + yescard.spoken_for("Sposto «Cena»?"),)


# --- the merge (the review's fixes) ------------------------------------------------------------


def test_another_confirmed_act_never_backs_the_unconfirmed_one() -> None:
    """ "sposta la cena e metti il dentista": the create confirmed, the move's read-back failed. Pisa's
    record held ANY for the create, which matched every tool: "Ho spostato la cena" and "Fatto." were
    backed."""
    record = claims.Record(done=frozenset({"create_event", claims.ANY}), unverified=frozenset({"move_event"}))
    assert not claims.backed("Ho spostato la cena alle 21.", "it-IT", record)
    assert not claims.backed("Fatto.", "it-IT", record)
    assert claims.backed("Ho messo il dentista domani alle 10.", "it-IT", record)


def test_an_approved_act_s_failed_read_back_backs_no_claim_next_turn(client, db) -> None:  # type: ignore[no-untyped-def]
    """An act they approved by a yes or a tap logs no `act` row, so its `not_verified` read-back never
    reached the recent log: next turn "Sì, l'ho spostata" was backed through what_did_i_do."""
    device, _ = pair(db)
    conversations = client.app.state.runtime.conversations
    now = conversations._clock()  # pyright: ignore[reportPrivateUsage]
    db.execute(
        "INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at, decided_at, "
        "executed_at, device_id, verified) VALUES (gen_random_uuid(), 'calendar_move', 's', '{}', 'done', "
        "%s, %s, %s, %s, %s, 'not_verified')",
        (now, now + timedelta(minutes=10), now, now, device),
    )
    confirmed, unconfirmed = client.portal.call(
        conversations._acted_lately,
        uuid.UUID(device),  # pyright: ignore[reportPrivateUsage]
    )
    assert "move_event" in unconfirmed and "move_event" not in confirmed


def test_the_spoken_length_counts_to_the_question_and_is_capped() -> None:
    """A 2,843-character reply that asks first gave a 203 s read-out and a 323 s window, and a "sì"
    200 s later ran the move. Timed to the question, never more than 60 s."""
    question = "Sposto la cena alle 21?"
    long_after = question + " " + "Poi ti racconto tutto il resto della giornata. " * 60
    assert yescard.spoken_for(long_after) == yescard.spoken_for(question)
    assert yescard.spoken_for("parola " * 2000) == yescard.SPOKEN_MAX


def test_send_draft_s_yes_is_timed_from_his_words(client, db, google, auth, clock) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """Review: the send's timing test passed whether the yes was timed from his words or from now. His "sì"
    heard 5 s after the read-out, the tool running 30 s after it: the yes is on time, and the mail goes."""
    thread(google, db)
    device, _ = pair(db)
    actions = client.app.state.runtime.actions
    run_on(client, device, actions.draft_reply, "Claudio cover", "Ciao, la porto io.")
    ((card_id,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    db.execute("UPDATE approvals SET payload = payload || '{\"heard\": true}'::jsonb, read_out_at = %s",
               (clock.now - timedelta(seconds=30),))  # fmt: skip

    async def send() -> str:
        turn = TurnState(uuid.uuid4(), "sì mandala", heard_at=clock.now - timedelta(seconds=25))
        turn.asked_before = frozenset({card_id})
        turn.device = uuid.UUID(device)
        current_turn.set(turn)
        return await actions.send_draft(None)

    client.portal.call(send)
    assert google.sent != []
