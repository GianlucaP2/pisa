"""Fixes at the merge (F1 to F4), each failing without its fix."""

from typing import Any

from tests.live_app import device as device  # the fixture
from tests.test_interface_tools import SHOW_PLAN, _calls, _phone, _returned, _said


def test_a_result_that_is_not_a_show_is_said_after_a_round_that_spoke_and_showed(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    """The model spoke, looked something up and showed the plan in one response: what it looked up is said."""
    model = _calls(
        ("Guardo e te lo metto sul telefono. ", [("get_current_time", {}), *SHOW_PLAN]), "Sono le 9."
    )
    fake_models.default = model
    _, (frames,) = _phone(client, db, secrets_key, ["che ore sono? e fammi vedere il piano"])
    assert len(model.seen) == 2 and _said(frames).endswith("Sono le 9.")


def test_a_show_the_model_got_wrong_goes_back_to_it(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    """A section that doesn't exist: the call is retried by the model, never silently dropped."""
    bad: Any = [("show_setting", {"purpose": "le notifiche", "section": "notifications"})]
    model = _calls(("Ti apro le impostazioni. ", bad), "Quella impostazione non c'è.")
    fake_models.default = model
    _, (frames,) = _phone(client, db, secrets_key, ["apri le impostazioni delle notifiche"])
    assert len(model.seen) == 2 and _said(frames).endswith("Quella impostazione non c'è.")


def test_a_show_that_was_seen_still_ends_with_one_model_call(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    model = _calls(("Te lo metto sul telefono. ", SHOW_PLAN), "Non dovrei essere chiamato.")
    fake_models.default = model
    _phone(client, db, secrets_key, ["fammi vedere il piano"])
    assert len(model.seen) == 1 and "Not shown" not in _returned(model.seen[0])


def test_everyday_words_are_not_an_ask_to_see_after_outside_text() -> None:
    from pisa.brain.present_tools import needs_words

    everyday = ["guarda se Marco mi ha scritto", "look at the email", "is it open?",
                "phone number in the invite", "list of attendees", "vedo che hai letto la mail"]  # fmt: skip
    for words in everyday:
        assert needs_words("note", words), words
    asks = ["mostrami il piano", "fammi vedere le modifiche", "fammelo vedere sul telefono",
            "show me the plan", "open the settings", "apri le impostazioni", "[tap] o2: Il bar"]  # fmt: skip
    for words in asks:
        assert not needs_words("plan", words), words


def test_items_delivered_together_never_pass_the_voice_rate(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Four unprompted items released at once (a breakpoint) in companion mode: three spoken, one shown.
    (CI under load) the app's own outbox listener sweeps whatever is pending when it connects,
    and on a slow box it connected after these rows were written and delivered one itself (no socket: "none").
    Not a dispatcher race: one process has one dispatcher. So the test uses the app's dispatcher and clock,
    and waits on the rows (whoever delivers them), never on who got there first."""
    import asyncio
    import time
    import uuid
    from datetime import datetime
    from typing import cast
    from zoneinfo import ZoneInfo

    from tests.conftest import pair

    device_id = pair(db)[0]
    db.execute(
        "INSERT INTO device_presence (device_id, app, companion, earbuds, updated_at) "
        "VALUES (%s, 'foreground', 'on', 'connected', now())",
        (device_id,),
    )
    sent: list[Any] = []

    class Live:
        async def notify(self, device: object, outgoing: Any) -> bool:
            await asyncio.sleep(0.05)  # the socket send: the others start meanwhile
            sent.append(outgoing)
            return True

    now = datetime(2026, 10, 31, 15, 0, tzinfo=ZoneInfo("Europe/Rome"))
    clock.now = now
    dispatcher = client.app.state.sessions.outbox  # the app's one dispatcher, its listener's too
    dispatcher._live = cast(Any, Live())
    ids = [uuid.uuid4() for _ in range(4)]
    for nid in ids:
        db.execute(
            "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, queued_at, "
            "urgency) VALUES (%s, 'info', 'Il pacco è arrivato.', 'it-IT', 'normal', %s, %s, %s, "
            "'breakpoint')",
            (nid, device_id, now, now),
        )

    async def together() -> list[Any]:
        return list(await asyncio.gather(*(dispatcher.deliver(nid) for nid in ids)))

    assert set(client.portal.call(together)) <= {"ws", None}  # None: the listener's sweep had it already
    for _ in range(400):  # the condition: all four delivered, whoever delivered them
        via = [r[0] for r in db.execute("SELECT delivered_via FROM notifications").fetchall()]
        if via == ["ws"] * 4 and len(sent) == 4:
            break
        time.sleep(0.05)
    assert via == ["ws"] * 4  # never dropped
    assert sorted(str(o.voice) for o in sent) == ["False", "None", "None", "None"]
