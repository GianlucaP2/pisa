"""Postgres advisory lock ids, in one place so services that must exclude each other agree on them.

Every numeric id lives here; the string keys (`hashtext(key)`) that more than one statement takes are named
here too. A key spelled inline at its only site (`hashtext('pisa.stop')`) belongs to that one service.

Order: a lock taken while another is held is taken after it, always in the same order, so two callers never
wait on each other. The one nesting today: `HARD_LIMIT` is taken after a ledger's own lock (`LEDGER` for the
AI ledger, `PLACES_CAP` for the places ledger), never before.

The single-key ids (`pg_advisory_lock(id)`) and the two-key classes (`pg_advisory_lock(class,
hashtext(name))`) are separate lock spaces in Postgres; they are numbered apart anyway.

A worker step already runs under its own job lock (`JOB`, its name); a step takes a second lock only when it
must also exclude something else (another step, an API route, a turn), and takes it with `held`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

# --- single-key ids -----------------------------------------------------------------------------------------
PAIRING = 7_001
# serializes the AI ledger's cap checks and reservations (every budget)
LEDGER = 7_002
# the hard limit over every paid service: taken after LEDGER or PLACES_CAP, never before
HARD_LIMIT = 7_044
# serializes the night's search count
NIGHT_SEARCH = 7_077
GMAIL_SYNC = 7_101
CALENDAR_SYNC = 7_102
INTAKE = 7_103
BRIEFING = 7_104
# 7_105 (reminders) and 7_109 (check-ins): retired, their steps' job locks were the same exclusion.
QUIET_RELEASE = 7_106
PLAN = 7_107
DONE = 7_110
# RAIL: one API process: the open questions Pisa asked and the "newest wins" sequence live in its
# memory; a second API process (more uvicorn workers) would split them, so it refuses to start.
API_PROCESS = 7_111
# RAIL: deliveries count and mark the unprompted voice rate one at a time.
VOICE_RATE = 7_112
# one worker decides the pending candidates at a time.
ATTEND = 7_113
# one trip reader at a time (two would split one journey into two trips).
TRIPS = 7_164

# --- two-key classes: (class, hashtext(name)) ---------------------------------------------------------------
# the worker's jobs: one run of a job at a time, across minutes and processes (name: the job)
JOB = 7_082
# one device's stays at a time (name: the device id)
STAYS = 7_108

# --- string keys: hashtext(key) -----------------------------------------------------------------------------
APPROVALS_CREATE = "pisa.approvals.create"
# the night's episode segmentation and the user's "dimentica" never interleave
EPISODES = "pisa.episodes"
NOTE = "pisa.note"
# the places ledger's cap check and reservation (HARD_LIMIT is taken after it)
PLACES_CAP = "pisa.places.cap"
PROFILE = "pisa.profile"


@asynccontextmanager
async def held(pool: AsyncConnectionPool, key: int, *, wait: bool = False) -> AsyncIterator[bool]:
    """The single-key lock `key` for the block, on a connection of its own from `pool`. Yields whether it is
    held: `wait` waits for it (always True), else it is tried once (False: someone else holds it)."""
    async with pool.connection() as conn:
        if wait:
            await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
            got = True
        else:
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (key,))).fetchone()
            got = bool(row and row[0])
        try:
            yield got
        finally:
            if got:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
