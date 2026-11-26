"""The owner model's rows: the addresses he writes from (`owner_identity`).

An alias is learned from the From header of a SENT message and waits, unconfirmed, until he confirms it
once. Only the account and the confirmed aliases make a message his (`domain/owner_model.py`). Confirming
an alias also marks the mail already stored from it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from psycopg import AsyncConnection

from pisa.domain.mail import MailMessage
from pisa.domain.owner_model import OwnerModel, alias_candidates, norm


async def load(conn: AsyncConnection, account: str) -> OwnerModel:
    """The account and its confirmed aliases."""
    rows = await (
        await conn.execute(
            "SELECT alias FROM owner_identity WHERE account = %s AND confirmed_at IS NOT NULL",
            (norm(account),),
        )
    ).fetchall()
    return OwnerModel(norm(account), frozenset(str(r[0]) for r in rows))


async def learn(conn: AsyncConnection, account: str, messages: Sequence[MailMessage]) -> int:
    """Note the From address of each SENT message that is not his account: a proposal for him to confirm.
    Returns how many aliases are new."""
    found = alias_candidates(
        account,
        [
            (m.labels, m.sender.address if m.sender else None, m.sender.name if m.sender else None)
            for m in messages
        ],
    )
    new = 0
    for alias, name in found.items():
        cur = await conn.execute(
            "INSERT INTO owner_identity (account, alias, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (account, alias) DO UPDATE SET display_name = coalesce(owner_identity.display_name, "
            "EXCLUDED.display_name)",
            (norm(account), alias, name),
        )
        new += cur.rowcount if cur.rowcount > 0 else 0
    return new


async def pending(conn: AsyncConnection, account: str) -> list[tuple[str, str | None]]:
    """The aliases he has not confirmed yet: (address, display name)."""
    rows = await (
        await conn.execute(
            "SELECT alias, display_name FROM owner_identity WHERE account = %s AND confirmed_at IS NULL "
            "ORDER BY first_seen_at, alias",
            (norm(account),),
        )
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


async def google_account(conn: AsyncConnection) -> str | None:
    """The connected Google account's address (the owner model's account), or None when none is connected."""
    row = await (
        await conn.execute("SELECT account FROM connector_accounts WHERE connector = 'google' LIMIT 1")
    ).fetchone()
    return norm(str(row[0])) if row and row[0] else None


async def listing(
    conn: AsyncConnection, account: str, state: str = "pending"
) -> list[tuple[str, str | None, datetime, datetime | None]]:
    """The aliases of the account: (address, display name, first seen, confirmed at). `state`: pending (not
    confirmed yet), confirmed, or all; the oldest first."""
    rows = await (
        await conn.execute(
            "SELECT alias, display_name, first_seen_at, confirmed_at FROM owner_identity WHERE account = %s "
            "AND (%s = 'all' OR (%s = 'pending') = (confirmed_at IS NULL)) ORDER BY first_seen_at, alias",
            (norm(account), state, state),
        )
    ).fetchall()
    return [(str(r[0]), r[1], r[2], r[3]) for r in rows]


async def confirm(conn: AsyncConnection, account: str, alias: str, now: datetime | None = None) -> bool:
    """He says this address is his. The mail already stored from it becomes his. False: no such alias."""
    key = norm(alias)
    cur = await conn.execute(
        "UPDATE owner_identity SET confirmed_at = coalesce(confirmed_at, %s) "
        "WHERE account = %s AND alias = %s",
        (now or datetime.now(UTC), norm(account), key),
    )
    if cur.rowcount < 1:
        return False
    await conn.execute(
        "UPDATE email_messages SET owner_authored = true "
        "WHERE lower(from_address) = %s AND NOT owner_authored AND 'SENT' = ANY(labels)",  # E1 SP-1
        (key,),
    )
    return True


async def forget(conn: AsyncConnection, account: str, alias: str) -> bool:
    """He says this address is not his (or no longer): the alias goes, and so does what it marked. False: no
    such alias."""
    key = norm(alias)
    gone = await conn.execute(
        "DELETE FROM owner_identity WHERE account = %s AND alias = %s", (norm(account), key)
    )
    await conn.execute(
        "UPDATE email_messages SET owner_authored = false WHERE lower(from_address) = %s AND owner_authored "
        "AND lower(from_address) <> %s",
        (key, norm(account)),
    )
    return gone.rowcount > 0
