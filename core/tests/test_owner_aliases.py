"""The owner-alias routes: the addresses the user writes from, listed while pending, confirmed or forgotten
once (`GET /v1/owner/aliases`, `POST .../{address}/confirm`, `POST .../{address}/forget`). A confirmed alias
makes the mail stored from it the user's own; a forgotten one takes that back."""

import psycopg
import pytest

from tests.conftest import pair
from tests.mail_world import OWNER, add_mail, google_account
from tests.phone_signing import device_headers, register

ALIAS = "ada.studio@example.com"
OTHER = "ada.old@example.com"


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


def seed(db: psycopg.Connection) -> None:
    google_account(db)
    db.execute(
        "INSERT INTO owner_identity (account, alias, display_name, first_seen_at) VALUES "
        "(%s, %s, 'Ada Demo', now() - interval '2 days'), (%s, %s, NULL, now() - interval '1 day')",
        (OWNER, ALIAS, OWNER, OTHER),
    )
    add_mail(db, "s1", sender=ALIAS, subject="Re: invoice", text="Sent it.", thread="t-1")
    db.execute("UPDATE email_messages SET owner_authored = false, labels = '{SENT}' WHERE id = 's1'")


def test_alias_confirm_routes(client, auth, db) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/v1/owner/aliases").status_code == 401  # a paired device only
    # nothing connected: no account, nothing to list, nothing to confirm
    assert client.get("/v1/owner/aliases", headers=auth).json() == {"account": None, "aliases": []}
    private = register(client, auth)

    def signed(address: str) -> dict[str, str]:  # confirming is always signed (A1 on E1 AL-1)
        return device_headers(client, auth, private, "alias_confirm", address.strip().lower())

    assert client.post(f"/v1/owner/aliases/{ALIAS}/confirm", headers=signed(ALIAS)).status_code == 404
    seed(db)
    listed = client.get("/v1/owner/aliases", headers=auth)
    assert listed.status_code == 200
    body = listed.json()
    assert body["account"] == OWNER
    assert [(a["address"], a["display_name"], a["confirmed"]) for a in body["aliases"]] == [
        (ALIAS, "Ada Demo", False),
        (OTHER, None, False),
    ]
    assert all(a["first_seen_at"] for a in body["aliases"])
    # the user says the first is theirs: it is confirmed, and the mail already stored from it becomes theirs
    sent = client.post(f"/v1/owner/aliases/{ALIAS}/confirm", headers=signed(ALIAS))
    assert sent.status_code == 200 and sent.json()["confirmed"] is True and sent.json()["address"] == ALIAS
    assert db.execute("SELECT owner_authored FROM email_messages WHERE id = 's1'").fetchone() == (True,)
    assert [a["address"] for a in client.get("/v1/owner/aliases", headers=auth).json()["aliases"]] == [OTHER]
    confirmed = client.get("/v1/owner/aliases?state=confirmed", headers=auth).json()["aliases"]
    assert [(a["address"], a["confirmed"]) for a in confirmed] == [(ALIAS, True)]
    assert len(client.get("/v1/owner/aliases?state=all", headers=auth).json()["aliases"]) == 2
    # the address as the user types it (case, spaces) is the same address
    again = client.post("/v1/owner/aliases/ADA.STUDIO@example.com/confirm", headers=signed(ALIAS))
    assert again.status_code == 200
    # the other is not theirs: forgotten; the confirmed one forgotten takes its mail back
    assert client.post(f"/v1/owner/aliases/{OTHER}/forget", headers=auth).status_code == 204
    assert client.post(f"/v1/owner/aliases/{OTHER}/forget", headers=auth).status_code == 404
    assert client.post(f"/v1/owner/aliases/{ALIAS}/forget", headers=auth).status_code == 204
    assert db.execute("SELECT owner_authored FROM email_messages WHERE id = 's1'").fetchone() == (False,)
    assert client.get("/v1/owner/aliases?state=all", headers=auth).json()["aliases"] == []
    assert client.post(f"/v1/owner/aliases/{ALIAS}/confirm", headers=signed(ALIAS)).status_code == 404
    bad = client.get("/v1/owner/aliases?state=maybe", headers=auth)
    assert bad.status_code in (400, 422)


def test_an_unsigned_alias_confirm_is_refused(client, auth, db) -> None:  # type: ignore[no-untyped-def]
    """A1 on E1 AL-1: confirming widens what counts as the user's words, so it needs the device's signature;
    unsigned, or signed for another address, it is refused and nothing changes. Forgetting stays unsigned."""
    seed(db)
    private = register(client, auth)
    unsigned = client.post(f"/v1/owner/aliases/{ALIAS}/confirm", headers=auth)
    assert unsigned.status_code == 403 and unsigned.json()["error"]["code"] == "biometric_required"
    other = device_headers(client, auth, private, "alias_confirm", OTHER)
    assert client.post(f"/v1/owner/aliases/{ALIAS}/confirm", headers=other).status_code == 403
    assert db.execute("SELECT owner_authored FROM email_messages WHERE id = 's1'").fetchone() == (False,)
    assert client.get("/v1/owner/aliases?state=confirmed", headers=auth).json()["aliases"] == []
    assert client.post(f"/v1/owner/aliases/{OTHER}/forget", headers=auth).status_code == 204
