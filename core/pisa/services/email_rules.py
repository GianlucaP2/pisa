"""The user teaches Pisa which emails are never tasks: "never from this sender" or "never this kind".

A rule is the user's own word (a tap on Dismiss), kept as a claim of kind `source` in their profile:
`subject_key` `email_sender:<address>` or `email_kind:<kind>`, provenance said, a hard limit. About you lists
it; forgetting it there removes the rule. Intake applies it to every new email, and making it dismisses the
open tasks it covers (never deletes them: the user can reopen one).

The kind of an email is its account notice when it is one (security, storage, auto_renewal, developer,
contact_check), else its triage category."""

import uuid
from datetime import datetime, timedelta
from typing import Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.domain.learning import clean_name
from pisa.domain.reasons import NOTICE_KINDS, kind_word
from pisa.logging import get_logger

SENDER = "email_sender:"
KIND = "email_kind:"
Never = Literal["sender", "kind"]
_log = get_logger("pisa.email_rules")
# Undoing a rule reopens what it dismissed, this soon after making it.
REOPEN_WITHIN = timedelta(hours=24)


def email_kind(category: str | None, notice: str | None) -> str | None:
    """An email's kind for the user's rules: its account notice, else its triage category."""
    return notice if notice and notice != "none" else category


async def rules(conn: AsyncConnection) -> tuple[set[str], set[str]]:
    """(senders, kinds) the user said never to make tasks from."""
    rows = await (
        await conn.execute(
            "SELECT subject_key FROM profile_claims WHERE state <> 'retired' AND kind = 'source' "
            "AND (subject_key LIKE 'email_sender:%%' OR subject_key LIKE 'email_kind:%%')"
        )
    ).fetchall()
    keys = [k for (k,) in rows]
    senders = {k.removeprefix(SENDER) for k in keys if k.startswith(SENDER)}
    kinds = {k.removeprefix(KIND) for k in keys if k.startswith(KIND)}
    return senders, kinds


async def add(conn: AsyncConnection, never: Never, value: str, lang: str, now: datetime) -> tuple[str, str]:
    """The user's rule as a `source` claim (once): (its statement in their language, its id: the claim's,
    which `DELETE /v1/rules/{id}` undoes)."""
    value = value.strip().lower() if never == "sender" else value
    key = (SENDER if never == "sender" else KIND) + value
    if never == "sender":
        statement = (
            f"Nessun task dalle email di {clean_name(value)}"
            if lang == "it-IT"
            else f"No tasks from the emails of {clean_name(value)}"
        )
    else:
        word = kind_word(value, lang)
        statement = f"Nessun task dalle email: {word}" if lang == "it-IT" else f"No tasks from emails: {word}"
    exists = await (
        await conn.execute(
            "SELECT id::text, statement FROM profile_claims WHERE subject_key = %s AND state <> 'retired'",
            (key,),
        )
    ).fetchone()
    if exists is not None:
        return exists[1], exists[0]
    rule_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, support, "
        "against, confidence, state, sensitive, hard, created_at, updated_at, subject_key, "
        "last_confirmed_at) VALUES (%s, 'source', %s, %s, -1, '{}', 'said', 1, 0, 1, 'active', false, "
        "true, %s, %s, %s, %s)",
        (rule_id, statement, Jsonb({"email_rule": never}), now, now, key, now),
    )
    _log.info("email_rule_added", never=never)
    return statement, str(rule_id)


async def is_rule(conn: AsyncConnection, rule_id: str) -> bool:
    """An email rule of the user's (a `source` claim about a sender or a kind), by its id."""
    try:
        parsed = uuid.UUID(rule_id)
    except ValueError:
        return False
    row = await (
        await conn.execute(
            "SELECT 1 FROM profile_claims WHERE id = %s AND kind = 'source' "
            "AND (subject_key LIKE 'email_sender:%%' OR subject_key LIKE 'email_kind:%%')",
            (parsed,),
        )
    ).fetchone()
    return row is not None


async def dismiss_covered(conn: AsyncConnection, never: Never, value: str, now: datetime) -> list[str]:
    """The open email tasks a new rule covers, dismissed (never deleted). Their ids."""
    if never == "sender":
        query = (
            "UPDATE tasks t SET status = 'dismissed', updated_at = %s FROM email_messages m "
            "WHERE t.status = 'open' AND t.source_kind = 'email' AND m.id = t.source_ref "
            "AND lower(m.from_address) = %s RETURNING t.id::text"
        )
        params = (now, value.strip().lower())
    elif value in NOTICE_KINDS:
        query = (
            "UPDATE tasks t SET status = 'dismissed', updated_at = %s FROM email_messages m "
            "WHERE t.status = 'open' AND t.source_kind = 'email' AND m.id = t.source_ref "
            "AND m.intake_notice = %s RETURNING t.id::text"
        )
        params = (now, value)
    else:
        # A triage category (bills, requests…) can't tell a utility's email from a person's: the rule
        # applies to new automated mail only, and no open task is swept with it.
        return []
    return [tid for (tid,) in await (await conn.execute(query, params)).fetchall()]


async def log_made(conn: AsyncConnection, rule_id: str, dismissed: list[str], now: datetime) -> None:
    """The rule's id and the tasks it dismissed, kept for its undo. A rule made again (the
    same claim) adds the tasks it dismissed this time."""
    await conn.execute(
        "INSERT INTO email_rule_log (rule_id, made_at, dismissed) VALUES (%s, %s, %s::uuid[]) "
        "ON CONFLICT (rule_id) DO UPDATE SET made_at = EXCLUDED.made_at, "
        "dismissed = email_rule_log.dismissed || EXCLUDED.dismissed, forgotten_at = NULL",
        (uuid.UUID(rule_id), now, dismissed),
    )


async def was_forgotten(conn: AsyncConnection, rule_id: str) -> bool:
    """A rule of the user's that is already undone: a repeated DELETE is done, not "no such rule"."""
    try:
        parsed = uuid.UUID(rule_id)
    except ValueError:
        return False
    row = await (
        await conn.execute(
            "SELECT 1 FROM email_rule_log WHERE rule_id = %s AND forgotten_at IS NOT NULL", (parsed,)
        )
    ).fetchone()
    return row is not None


async def undone(conn: AsyncConnection, rule_id: str, now: datetime) -> list[str]:
    """The rule is forgotten: the tasks it dismissed reopen when it was made within 24 h (and they are still
    dismissed: the user may have finished or deleted one since). Their ids."""
    parsed = uuid.UUID(rule_id)
    row = await (
        await conn.execute(
            "UPDATE email_rule_log SET forgotten_at = %s WHERE rule_id = %s RETURNING made_at, dismissed",
            (now, parsed),
        )
    ).fetchone()
    if row is None or now - row[0] > REOPEN_WITHIN or not row[1]:
        return []
    reopened = await (
        await conn.execute(
            "UPDATE tasks SET status = 'open', updated_at = %s WHERE id = ANY(%s) AND status = 'dismissed' "
            "RETURNING id::text",
            (now, row[1]),
        )
    ).fetchall()
    _log.info("email_rule_undone", reopened=len(reopened))
    return [tid for (tid,) in reopened]
