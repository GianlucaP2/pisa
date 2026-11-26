"""The owner model: the account and the aliases he confirmed make a message his. Synthetic mail only."""

import asyncio
from datetime import UTC, datetime

import psycopg
from psycopg import AsyncConnection

from pisa.domain.mail import Address, MailMessage
from pisa.domain.owner_model import OwnerModel, alias_candidates
from pisa.services import owner_identity
from pisa.services.google_sync import _upsert_messages  # pyright: ignore[reportPrivateUsage]

ACCOUNT = "Dana.Test@example.org"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def mail(mid: str, sender: str, name: str | None = None, labels: tuple[str, ...] = ("INBOX",)) -> MailMessage:
    return MailMessage(
        id=mid,
        thread_id="t1",
        sender=Address(name, sender),
        to=(Address(None, "x@example.com"),),
        subject="s",
        sent_at=NOW,
        labels=labels,
        snippet="",
        body="hello",
    )


def test_owner_model_rules() -> None:
    model = OwnerModel(ACCOUNT, frozenset({"dana@alias.example.net"}))
    assert model.authored("dana.test@example.org")  # the account, whatever its case
    assert model.authored(" DANA@alias.example.net ")  # a confirmed alias
    assert not model.authored("shop@example.com")
    assert not model.authored(None) and not model.authored("")
    found = alias_candidates(
        ACCOUNT,
        [
            (("SENT",), "Dana@Alias.example.net", "Dana T."),  # sent from an alias: a candidate
            (("SENT",), "dana.test@example.org", None),  # his account: not one
            (("INBOX",), "shop@example.com", "Shop"),  # received, not sent: not one
            (("SENT",), "dana@alias.example.net", "Other Name"),  # the first name seen stays
            (("SENT",), "no-at-sign", None),
        ],
    )
    assert found == {"dana@alias.example.net": "Dana T."}


def test_owner_model(db: psycopg.Connection, test_database_url: str) -> None:
    async def go() -> None:
        async with await AsyncConnection.connect(test_database_url, autocommit=True) as conn:
            # a sent message from an alias: learned, not yet his
            await _upsert_messages(
                conn,
                [
                    mail("m1", "dana.test@example.org", "Dana", ("SENT",)),
                    mail("m2", "dana@alias.example.net", "Dana T.", ("SENT",)),
                    mail("m3", "shop@example.com", "Shop"),
                ],
                ACCOUNT,
            )
            assert await owner_identity.pending(conn, ACCOUNT) == [("dana@alias.example.net", "Dana T.")]
            assert (await owner_identity.load(conn, ACCOUNT)).aliases == frozenset()
            # a message from the same alias is not his before he confirms it
            await _upsert_messages(
                conn, [mail("m4", "dana@alias.example.net", "Dana T.", ("SENT",))], ACCOUNT
            )
            # he confirms once: the stored mail from it becomes his, and the next too
            assert await owner_identity.confirm(conn, ACCOUNT, "Dana@Alias.example.net", NOW)
            assert not await owner_identity.confirm(conn, ACCOUNT, "nobody@example.net", NOW)
            await _upsert_messages(
                conn, [mail("m5", "dana@alias.example.net", "Dana T.", ("SENT",))], ACCOUNT
            )
            # E1 SP-1: an inbound mail from that address (not SENT) is never theirs, confirmed or not
            await _upsert_messages(conn, [mail("m6", "dana@alias.example.net", "Dana T.")], ACCOUNT)
            assert await owner_identity.pending(conn, ACCOUNT) == []
            assert (await owner_identity.load(conn, ACCOUNT)).aliases == frozenset({"dana@alias.example.net"})

    asyncio.run(go())
    authored = dict(db.execute("SELECT id, owner_authored FROM email_messages").fetchall())
    assert authored == {"m1": True, "m2": True, "m3": False, "m4": True, "m5": True, "m6": False}

    async def forget() -> None:
        async with await AsyncConnection.connect(test_database_url, autocommit=True) as conn:
            await owner_identity.forget(conn, ACCOUNT, "dana@alias.example.net")

    asyncio.run(forget())
    authored = dict(db.execute("SELECT id, owner_authored FROM email_messages").fetchall())
    assert authored["m2"] is False and authored["m1"] is True
    assert db.execute("SELECT count(*) FROM owner_identity").fetchone() == (0,)
