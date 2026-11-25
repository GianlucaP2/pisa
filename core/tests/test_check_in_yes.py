"""Fixes at the merge, each failing without its fix."""

from tests.conftest import pair
from tests.test_acting import Model
from tests.test_acting_fixes import _talk
from tests.test_changes_feed import _check_in, _say_check_in
from tests.test_talking_like_a_person import _deliver, _queue


def test_a_yes_answers_a_check_in_by_code_only_when_it_is_the_newest_thing_pisa_said(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    """MED: after the check-in, a reminder went out: the user's "sì" answered the check-in (the task marked
    done) though the newest thing Pisa said was the reminder; and a check-in never delivered was answered
    too."""
    from tests.conftest import set_key
    from tests.live_app import live

    model = Model("x", [])
    fake_models.default = model
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        unseen = _check_in(db, client, "Sei passato in farmacia?")  # never delivered
        _talk(client, db, secrets_key, app, "sì")
        assert model.seen == 1
        assert db.execute("SELECT answer FROM check_ins WHERE id = %s", (unseen,)).fetchone() == (None,)
        db.execute("UPDATE check_ins SET dropped_at = now() WHERE id = %s", (unseen,))
        asked = _check_in(db, client, "Sei passato dal tabaccaio?")
        _say_check_in(client, app, device_id, asked, "Sei passato dal tabaccaio?")
        _deliver(client, _queue(client, "reminder", "Tra 15 minuti: Dentista.", device=device_id))
        assert app.next()["type"] == "notify"
        _talk(client, db, secrets_key, app, "sì")  # the newest thing Pisa said is the reminder
    assert model.seen == 2
    assert db.execute("SELECT answer FROM check_ins WHERE id = %s", (asked,)).fetchone() == (None,)
