"""Thread fetch: the threads that new mail touched are read, only the messages Pisa lacks, within the caps.
A fake Gmail client; synthetic mail only."""

from datetime import timedelta
from typing import Any

import psycopg

from pisa.domain.mail import Address, MailChanges, MailMessage, ThreadFetch
from pisa.services import google_sync
from pisa.services.google_sync import THREAD_MESSAGES_MAX, THREADS_PER_SYNC, sync_gmail
from tests.gateway_env import run
from tests.google_fake import FakeGoogle
from tests.test_google import T0, google_env

ACCOUNT = "owner@example.org"


def msg(
    mid: str, thread: str, minute: int, *, sender: str = "shop@example.com", sent: bool = False
) -> MailMessage:
    return MailMessage(
        id=mid,
        thread_id=thread,
        sender=Address(None, sender),
        to=(Address(None, "x@example.com"),),
        subject="refund",
        sent_at=T0 + timedelta(minutes=minute),
        labels=("SENT",) if sent else ("INBOX",),
        snippet="",
        body=f"text of {mid}",
    )


class FakeMail:
    """A mailbox: `threads` holds every message of each thread; `added` is what the history says is new."""

    def __init__(self, threads: dict[str, list[MailMessage]], added: list[MailMessage]) -> None:
        self.threads = threads
        self.added = added
        self.thread_calls: list[tuple[str, frozenset[str], int]] = []
        self.broken: set[str] = set()

    async def history_point(self) -> str:
        return "h0"

    async def recent(self, **_: Any) -> list[MailMessage]:
        return []

    async def changes_since(self, history_point: str) -> MailChanges:
        return MailChanges(history_id="h2", added=tuple(self.added))

    async def thread(self, thread_id: str, *, skip: frozenset[str], limit: int) -> ThreadFetch:
        self.thread_calls.append((thread_id, skip, limit))
        if thread_id in self.broken:
            from pisa.connectors.google.errors import ConnectorError

            raise ConnectorError("Gmail said no.")
        everything = self.threads.get(thread_id, [])
        unknown = [m for m in everything if m.id not in skip]
        return ThreadFetch(len(everything), tuple(unknown[-limit:]))


def test_thread_fetch(db: psycopg.Connection, secrets_key: str, clock: Any) -> None:
    # the thread already holds m1 (the shop's) and m2 (the owner's answer, unseen before); m3 is new
    thread = [msg("m1", "t1", 0), msg("m2", "t1", 5, sender=ACCOUNT, sent=True), msg("m3", "t1", 10)]
    mail = FakeMail({"t1": thread}, added=[thread[2]])
    db.execute(
        "INSERT INTO email_messages (id, thread_id, sent_at, body) VALUES ('m1', 't1', %s, 'old')", (T0,)
    )

    async def go() -> None:
        async with google_env(db, secrets_key, FakeGoogle(), clock) as (google, pool):
            async with pool.connection() as conn:
                await conn.execute("UPDATE connector_accounts SET gmail_history_id = 'h1'")
            await sync_gmail(pool, google, source=mail, now=T0)  # type: ignore[arg-type]

    run(go)
    # one call for the one touched thread, asking only for what is missing, within the cap
    assert [(t, sorted(skip), limit) for t, skip, limit in mail.thread_calls] == [
        ("t1", ["m1", "m3"], THREAD_MESSAGES_MAX)
    ]
    rows = db.execute(
        "SELECT id, intake_status, owner_authored FROM email_messages ORDER BY sent_at"
    ).fetchall()
    # m2 came as context and is the owner's own, as it is from his account: his reply is read (it moves a
    # storyline), where another sender's context message is stored and not read
    assert rows == [("m1", "pending", False), ("m2", "pending", True), ("m3", "pending", False)]
    assert db.execute("SELECT message_count, truncated FROM email_threads WHERE id = 't1'").fetchone() == (
        3,
        False,
    )
    assert db.execute("SELECT owner_last_at FROM email_threads WHERE id = 't1'").fetchone() == (
        T0 + timedelta(minutes=5),
    )


def test_thread_fetch_caps(db: psycopg.Connection, secrets_key: str, clock: Any) -> None:
    assert (THREAD_MESSAGES_MAX, THREADS_PER_SYNC) == (20, 50)
    # 60 threads touched in one run: only 50 are read, the most recent first
    added = [msg(f"n{i}", f"t{i}", i) for i in range(60)]
    threads = {f"t{i}": [added[i]] for i in range(60)}
    # one thread is long: 30 messages the owner has not got besides the new one
    threads["t59"] = [msg(f"old{i}", "t59", -100 + i) for i in range(30)] + [added[59]]
    mail = FakeMail(threads, added=added)
    mail.broken.add("t58")  # a thread that cannot be read does not stop the others

    async def go() -> None:
        async with google_env(db, secrets_key, FakeGoogle(), clock) as (google, pool):
            async with pool.connection() as conn:
                await conn.execute("UPDATE connector_accounts SET gmail_history_id = 'h1'")
            result = await sync_gmail(pool, google, source=mail, now=T0)  # type: ignore[arg-type]
            assert result.error is None

    run(go)
    called = [t for t, _, _ in mail.thread_calls]
    assert len(called) == THREADS_PER_SYNC
    assert called[0] == "t59" and "t9" not in called  # the newest first; the oldest ten wait
    assert all(limit == THREAD_MESSAGES_MAX for _, _, limit in mail.thread_calls)
    # t59: the new message plus the newest 20 of its old ones; the thread says it holds more
    t59 = db.execute("SELECT count(*) FROM email_messages WHERE thread_id = 't59'").fetchone()
    assert t59 == (1 + THREAD_MESSAGES_MAX,)
    assert db.execute("SELECT truncated FROM email_threads WHERE id = 't59'").fetchone() == (True,)
    # the broken thread kept its new message and has no thread row
    assert db.execute("SELECT count(*) FROM email_messages WHERE thread_id = 't58'").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM email_threads WHERE id = 't58'").fetchone() == (0,)
    # a run with nothing new reads no thread
    quiet = FakeMail(threads, added=[])
    run_quiet = google_sync.sync_threads
    assert run(lambda: run_quiet(None, quiet, set())) == 0  # type: ignore[arg-type]
    assert quiet.thread_calls == []


def test_an_inbound_mail_spoofing_the_account_is_never_the_owner_s(  # E1 SP-1
    db: psycopg.Connection, secrets_key: str, clock: Any
) -> None:
    """From the user's own address but not filed as SENT: a stranger's mail, never the user's words."""
    spoofed = msg("s1", "t9", 0, sender=ACCOUNT, sent=False)
    sent = msg("s2", "t9", 5, sender=ACCOUNT, sent=True)
    mail = FakeMail({"t9": [spoofed, sent]}, added=[spoofed, sent])

    async def go() -> None:
        async with google_env(db, secrets_key, FakeGoogle(), clock) as (google, pool):
            async with pool.connection() as conn:
                await conn.execute("UPDATE connector_accounts SET gmail_history_id = 'h1'")
            await sync_gmail(pool, google, source=mail, now=T0)  # type: ignore[arg-type]

    run(go)
    rows = dict(db.execute("SELECT id, owner_authored FROM email_messages").fetchall())
    assert rows == {"s1": False, "s2": True}
