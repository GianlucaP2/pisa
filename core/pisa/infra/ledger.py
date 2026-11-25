"""Cost ledger (llm_calls) and spend caps.

Per call: reserve an estimate → call → settle with the real usage. A reservation that would pass the daily or
monthly cap is refused with budget_exceeded. "Today" and "this month" are the user's
(PISA_TIMEZONE, Europe/Rome).
Reservations left behind by crashed calls ("ghosts") are swept after 10 minutes.

The evals' calls (run inside `eval_budget()`) go on their own budget: the eval cap (€0.50 a day by
default, the operator's setting `eval_cap`), never the user's caps or the budget guard. The hard limit
 still counts them: it is every paid service together.
"""

import calendar
import functools
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.db.settings import get_setting, set_setting
from pisa.domain import learn as learn_rules
from pisa.domain import night as night_rules
from pisa.domain.errors import PisaError
from pisa.infra import hard_limit, locks, spend
from pisa.infra.fx import usd_to_eur
from pisa.logging import get_logger

GHOST_AFTER = timedelta(minutes=10)
_log = get_logger("pisa.models.ledger")


@dataclass(frozen=True)
class Caps:
    daily_eur: Decimal
    monthly_eur: Decimal


DEFAULT_CAPS = Caps(daily_eur=Decimal("1.00"), monthly_eur=Decimal("20.00"))
DEFAULT_EVAL_CAP = Decimal("0.50")  # FACT: the operator's daily eval budget
Budget = str  # "owner", "eval" or "night"
_budget: ContextVar[Budget] = ContextVar("ledger_budget", default="owner")


@contextmanager
def eval_budget() -> Iterator[None]:
    """Every call made inside goes on the eval budget (the tasks it starts too: they copy the context)."""
    token = _budget.set("eval")
    try:
        yield
    finally:
        _budget.reset(token)


@contextmanager
def night_budget() -> Iterator[None]:
    """Every call made inside goes on the night's budget, capped at `night.NIGHT_CAP_EUR` a
    night: never spent from the user's caps, so a heavy night never refuses their morning turns; but a night
    stops once their AI caps are reached (their spend and the night's together). The hard limit counts it."""
    token = _budget.set("night")
    try:
        yield
    finally:
        _budget.reset(token)


def on_eval_budget[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
    """An eval's run: every call it makes goes on the eval budget."""

    @functools.wraps(fn)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with eval_budget():
            return await fn(*args, **kwargs)

    return run


def budget() -> Budget:
    return _budget.get()


# Lane: the worker's job a call is made in, on its ledger row (the per-job
# cost line). None outside a job: a turn, a REST request.
_job: ContextVar[str | None] = ContextVar("ledger_job", default=None)


@contextmanager
def job_scope(name: str) -> Iterator[None]:
    """Every call made inside (the tasks it starts too: they copy the context) is that job's on the ledger."""
    token = _job.set(name)
    try:
        yield
    finally:
        _job.reset(token)


def job() -> str | None:
    return _job.get()


async def job_spend(conn: AsyncConnection, name: str, since: datetime) -> tuple[int, Decimal]:
    """A job's model calls since a moment, and what they count for against the caps (its cost line)."""
    row = await (
        await conn.execute(
            f"SELECT count(*), {spend.SPEND} FROM llm_calls WHERE job = %s AND created_at >= %s",  # noqa: S608
            (name, since),
        )
    ).fetchone()
    return (int(row[0]), Decimal(row[1])) if row else (0, Decimal(0))


def eur(amount: Decimal) -> str:
    return f"€{amount:.2f}" if amount >= Decimal("0.01") or amount == 0 else f"€{amount:.4f}"


DAY_RESET_KEY = "spend_day_reset_at"  # set by maintenance spend-reset (the operator, on the user's ask)


async def cap_day_start(conn: AsyncConnection, day_start: datetime) -> datetime:
    """Where the user's daily cap starts counting: the local midnight, or today's reset when there is one.
    The ledger keeps every call, and the hard limit still counts the whole day."""
    stored = await get_setting(conn, DAY_RESET_KEY)
    if isinstance(stored, str):
        reset = datetime.fromisoformat(stored)
        if reset > day_start:
            return reset
    return day_start


async def get_caps(conn: AsyncConnection) -> Caps:
    stored = await get_setting(conn, "spend_caps")
    if isinstance(stored, dict):
        return Caps(Decimal(str(stored["daily_eur"])), Decimal(str(stored["monthly_eur"])))
    return DEFAULT_CAPS


async def set_caps(conn: AsyncConnection, caps: Caps) -> None:
    await set_setting(
        conn, "spend_caps", Jsonb({"daily_eur": str(caps.daily_eur), "monthly_eur": str(caps.monthly_eur)})
    )


async def get_eval_cap(conn: AsyncConnection) -> Decimal:
    stored = await get_setting(conn, "eval_cap")
    if isinstance(stored, dict) and "daily_eur" in stored:
        return Decimal(str(stored["daily_eur"]))
    return DEFAULT_EVAL_CAP


async def get_eval_month_cap(
    conn: AsyncConnection, now: datetime, tz: str, hard: hard_limit.HardLimits
) -> Decimal:
    """The evals' month, never more than what the hard month leaves once the user's whole
    monthly cap is spent (eval month <= hard month - owner month): evals never use the user's month up.
    The setting's `monthly_eur` when set, else the daily cap times the month's days; clamped either way."""
    stored = await get_setting(conn, "eval_cap")
    if isinstance(stored, dict) and "monthly_eur" in stored:
        wanted = Decimal(str(stored["monthly_eur"]))
    else:
        local = now.astimezone(ZoneInfo(tz))
        days = calendar.monthrange(local.year, local.month)[1]
        wanted = await get_eval_cap(conn) * days
    room = hard.monthly_eur - (await get_caps(conn)).monthly_eur
    return max(Decimal(0), min(wanted, room))


async def spent_since(conn: AsyncConnection, since: datetime, which: Budget = "owner") -> Decimal:
    """What `which` budget spent since then (the user's: never an eval's)."""
    row = await (
        await conn.execute(
            f"SELECT {spend.SPEND} FROM llm_calls WHERE created_at >= %s AND budget = %s",  # noqa: S608
            (since, which),
        )
    ).fetchone()
    return Decimal(row[0]) if row else Decimal(0)


async def _slots_spent(conn: AsyncConnection, since: datetime, which: Budget, slot_prefix: str) -> Decimal:
    """What `which` budget's calls on the slots starting `slot_prefix` spent since then."""
    row = await (
        await conn.execute(
            f"SELECT {spend.SPEND} FROM llm_calls WHERE created_at >= %s AND budget = %s "  # noqa: S608
            "AND slot LIKE %s",
            (since, which, slot_prefix + "%"),
        )
    ).fetchone()
    return Decimal(row[0]) if row else Decimal(0)


# When a cap's period starts: the user's local day, the same day from today's reset (the user's daily cap,
# see `cap_day_start`), or their local month. One night is the local day it runs in (03:30 their zone).
Period = Literal["day", "day_from_reset", "month"]
# Where a cap's amount comes from: (conn, now, tz, the hard limit) → EUR.
CapSource = Callable[[AsyncConnection, datetime, str, hard_limit.HardLimits], Awaitable[Decimal]]
# The error a cap gives when `extra` more would pass it: (spent, extra, cap, slot) → error.
Refused = Callable[[Decimal, Decimal, Decimal, str], PisaError]


@dataclass(frozen=True)
class BudgetCap:
    """One cap of a budget: the rows it counts (its budget's, on the slots starting `slot_prefix`; "" is every
    slot), over which period, against which amount."""

    name: str
    slot_prefix: str
    period: Period
    cap: CapSource
    refused: Refused
    log_event: str | None  # the warning logged when hit; None: the once-a-day cap notification instead
    # the budgets whose rows it counts; empty: its own budget's only
    counts: tuple[Budget, ...] = ()


@dataclass(frozen=True)
class BudgetRules:
    """A budget's caps, checked in order (the first one passed refuses), then the hard limit over every paid
    service. `notify`: a refusal by one of its caps with no log event, or by the hard limit, sends the day's
    one cap notification."""

    caps: tuple[BudgetCap, ...]
    notify: bool


async def _night_cap(conn: AsyncConnection, now: datetime, tz: str, hard: hard_limit.HardLimits) -> Decimal:
    return night_rules.NIGHT_CAP_EUR


async def _eval_day_cap(
    conn: AsyncConnection, now: datetime, tz: str, hard: hard_limit.HardLimits
) -> Decimal:
    return await get_eval_cap(conn)


async def _learn_cap(conn: AsyncConnection, now: datetime, tz: str, hard: hard_limit.HardLimits) -> Decimal:
    return (learn_rules.MONTH_CAP_USD * await usd_to_eur(conn)).quantize(Decimal("0.01"))


async def _owner_day_cap(
    conn: AsyncConnection, now: datetime, tz: str, hard: hard_limit.HardLimits
) -> Decimal:
    return (await get_caps(conn)).daily_eur


async def _owner_month_cap(
    conn: AsyncConnection, now: datetime, tz: str, hard: hard_limit.HardLimits
) -> Decimal:
    return (await get_caps(conn)).monthly_eur


def _night_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return PisaError(
        "budget_exceeded",
        f"The night's cap is reached: {eur(spent)} spent tonight, and this call (about {eur(extra)}) "
        f"would pass the {eur(cap)} night cap (slot {slot}). "
        "The user's budget is untouched.",
        "The night stops here and goes on tomorrow",
    )


def _night_owner_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return PisaError(
        "budget_exceeded",
        f"The user's AI cap is reached: {eur(spent)} spent by them and the night together, and this call "
        f"(about {eur(extra)}) would pass the {eur(cap)} cap (slot {slot}). Their turns are untouched.",
        "The night stops here and goes on tomorrow",
    )


def _eval_day_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return PisaError(
        "budget_exceeded",
        f"The evals' daily cap is reached: {eur(spent)} spent today, and this call "
        f"(about {eur(extra)}) would pass the {eur(cap)} eval cap (slot {slot}). "
        "The user's budget is untouched.",
        "Run the eval tomorrow, or raise the eval cap (setting eval_cap)",
    )


def _eval_month_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return PisaError(
        "budget_exceeded",
        f"The evals' monthly cap is reached: {eur(spent)} spent this month, and this call "
        f"(about {eur(extra)}) would pass the {eur(cap)} eval month (slot {slot}). "
        "The user's budget is untouched.",
        "Run the eval next month, or raise eval_cap.monthly_eur (within the hard month)",
    )


def _learn_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return PisaError(
        "budget_exceeded",
        f"Pisa Learn's monthly cap is reached: {eur(spent)} spent on Pisa Learn this month, and "
        f"this call (about {eur(extra)}) would pass its {eur(cap)} cap "
        f"(${learn_rules.MONTH_CAP_USD} a month, slot {slot}). The rest of Pisa is untouched.",
        "Pisa Learn answers again next month",
    )


def _owner_day_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return _cap_error("Daily", spent, "today", extra, cap, slot)


def _owner_month_refused(spent: Decimal, extra: Decimal, cap: Decimal, slot: str) -> PisaError:
    return _cap_error("Monthly", spent, "this month", extra, cap, slot)


# The budgets, by the ledger's `budget` column. The evals' and the night's never spend the user's caps (the
# night stops once they are reached); Pisa Learn's month sits inside the user's caps; the hard limit counts
# them all.
BUDGETS: dict[Budget, BudgetRules] = {
    # the night: its own cap, then the user's AI caps counted over their spend and the night's together, so
    # a night stops once their caps are reached (its spend never refuses their turns: those count theirs)
    "night": BudgetRules(
        (
            BudgetCap("night", "", "day", _night_cap, _night_refused, "night_cap_hit"),
            BudgetCap(
                "night owner day",
                "",
                "day_from_reset",
                _owner_day_cap,
                _night_owner_refused,
                "night_owner_cap_hit",
                counts=("owner", "night"),
            ),
            BudgetCap(
                "night owner month",
                "",
                "month",
                _owner_month_cap,
                _night_owner_refused,
                "night_owner_cap_hit",
                counts=("owner", "night"),
            ),
        ),
        notify=False,
    ),
    "eval": BudgetRules(
        (
            BudgetCap("eval day", "", "day", _eval_day_cap, _eval_day_refused, "eval_cap_hit"),
            BudgetCap(
                "eval month", "", "month", get_eval_month_cap, _eval_month_refused, "eval_month_cap_hit"
            ),
        ),
        notify=False,
    ),
    "owner": BudgetRules(
        (
            BudgetCap(
                "learn month", learn_rules.SLOT_PREFIX, "month", _learn_cap, _learn_refused, "learn_cap_hit"
            ),
            BudgetCap("owner day", "", "day_from_reset", _owner_day_cap, _owner_day_refused, None),
            BudgetCap("owner month", "", "month", _owner_month_cap, _owner_month_refused, None),
        ),
        notify=True,
    ),
}


async def _refusal(
    conn: AsyncConnection,
    extra: Decimal,
    slot: str,
    now: datetime,
    tz: str,
    which: Budget,
    hard: hard_limit.HardLimits,
) -> PisaError | None:
    """The cap `extra` more would pass, inside the ledger's lock: the caps of `which` budget that count this
    slot (`BUDGETS`), then the hard limit over every paid service."""
    day_start, month_start = spend.period_starts(now, tz)
    rules = BUDGETS[which]
    refusal: PisaError | None = None
    for cap in rules.caps:
        if not slot.startswith(cap.slot_prefix):
            continue
        if cap.period == "month":
            since = month_start
        elif cap.period == "day_from_reset":
            since = await cap_day_start(conn, day_start)
        else:
            since = day_start
        if cap.counts:
            spent = sum([await spent_since(conn, since, b) for b in cap.counts], Decimal(0))
        elif cap.slot_prefix:
            spent = await _slots_spent(conn, since, which, cap.slot_prefix)
        else:
            spent = await spent_since(conn, since, which)
        amount = await cap.cap(conn, now, tz, hard)
        if spent + extra > amount:
            if cap.log_event is not None:
                _log.warning(cap.log_event, spent_eur=str(spent), cap_eur=str(amount))
                return cap.refused(spent, extra, amount, slot)
            refusal = cap.refused(spent, extra, amount, slot)
            break
    if refusal is None:
        refusal = await hard_limit.refusal(conn, extra_eur=extra, now=now, tz=tz, hard=hard)
    if refusal is not None and rules.notify:
        await _notify_cap_once(conn, day_start, refusal)
    return refusal


# RAIL: every paid call is reserved against the day's and month's caps first, atomically.
async def reserve(
    conn: AsyncConnection,
    *,
    slot: str,
    model_ref: str,
    estimate_eur: Decimal,
    price_known: bool,
    trace_id: str,
    now: datetime,
    tz: str,
    hard: hard_limit.HardLimits,
    call_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """`call_id`: the row's id, when the caller must know it before this returns (a cancel can land after the
    commit, before the return: the caller settles the row by it). `hard`: the server's hard limit (its
    settings, `hard_limit.limits`)."""
    call_id = call_id or uuid.uuid4()
    which = budget()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.LEDGER,))
        refusal = await _refusal(conn, estimate_eur, slot, now, tz, which, hard)
        if refusal is None:
            await conn.execute(
                "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, price_known, "
                "trace_id, created_at, budget, job) "
                "VALUES (%s, %s, %s, 'reserved', %s, %s, %s, %s, %s, %s)",
                (call_id, slot, model_ref, estimate_eur, price_known, trace_id, now, which, job()),
            )
    if refusal is not None:
        raise refusal
    return call_id


async def grow(
    conn: AsyncConnection,
    call_id: uuid.UUID,
    *,
    extra_eur: Decimal,
    slot: str,
    now: datetime,
    tz: str,
    hard: hard_limit.HardLimits,
) -> None:
    """Reserve `extra_eur` more for a call still running (a long research turn; a talk session's next
    minute): the same caps as `reserve`, checked again. Refused: PisaError budget_exceeded, and the
    reservation stays as it was. RAIL (1): a row that isn't reserved any more (swept, settled)
    can't grow: PisaError internal, never a silent no-op that leaves the caps blind."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.LEDGER,))
        row = await (await conn.execute("SELECT budget FROM llm_calls WHERE id = %s", (call_id,))).fetchone()
        refusal = await _refusal(conn, extra_eur, slot, now, tz, row[0] if row else budget(), hard)
        if refusal is None:
            cur = await conn.execute(
                "UPDATE llm_calls SET reserved_eur = reserved_eur + %s, grown_at = %s "
                "WHERE id = %s AND status = 'reserved'",
                (extra_eur, now, call_id),
            )
            if cur.rowcount == 0:
                raise PisaError(
                    "internal",
                    f"The spend ledger has no open reservation for this call (slot {slot}): it can't grow.",
                    "Try again",
                )
    if refusal is not None:
        raise refusal


def _cap_error(
    which: str, spent: Decimal, when: str, estimate: Decimal, cap: Decimal, slot: str
) -> PisaError:
    return PisaError(
        "budget_exceeded",
        f"{which} AI spend cap reached: {eur(spent)} spent {when}, and this call (about {eur(estimate)}) "
        f"would pass the {eur(cap)} cap (slot {slot}).",
        "Raise the cap in Settings → AI spend",
    )


async def _notify_cap_once(conn: AsyncConnection, day_start: datetime, error: PisaError) -> None:
    """At most one cap notification per (local) day. It is a log event."""
    today = day_start.date().isoformat()
    if await get_setting(conn, "budget_cap_notified_on") == today:
        return
    await set_setting(conn, "budget_cap_notified_on", Jsonb(today))
    _log.warning("budget_cap_hit", message=error.message)


async def settle(
    conn: AsyncConnection,
    call_id: uuid.UUID,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_eur: Decimal,
    price_known: bool,
    latency_ms: int,
    now: datetime,
    error_code: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE llm_calls SET status = 'settled', input_tokens = %s, output_tokens = %s, "
        "cache_read_tokens = %s, cache_write_tokens = %s, cost_eur = %s, price_known = price_known AND %s, "
        "latency_ms = %s, error_code = %s, settled_at = %s WHERE id = %s",
        (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            cost_eur,
            price_known,
            latency_ms,
            error_code,
            now,
            call_id,
        ),
    )


async def fail(
    conn: AsyncConnection, call_id: uuid.UUID, *, error_code: str, latency_ms: int, now: datetime
) -> None:
    await conn.execute(
        "UPDATE llm_calls SET status = 'failed', cost_eur = 0, latency_ms = %s, error_code = %s, "
        "settled_at = %s "
        "WHERE id = %s",
        (latency_ms, error_code, now, call_id),
    )


async def sweep_ghosts(conn: AsyncConnection, now: datetime) -> int:
    cur = await conn.execute(
        "UPDATE llm_calls SET status = 'swept', settled_at = %s WHERE status = 'reserved' AND "
        "coalesce(grown_at, created_at) < %s",  # a call that grew within the window is alive (a talk)
        (now, now - GHOST_AFTER),
    )
    if cur.rowcount:
        _log.warning("ledger_ghosts_swept", count=cur.rowcount)
    return cur.rowcount


@dataclass(frozen=True)
class SlotSpend:
    slot: str
    today_eur: Decimal
    month_eur: Decimal
    calls_today: int


@dataclass(frozen=True)
class Spend:
    today_eur: Decimal
    month_eur: Decimal
    caps: Caps
    by_slot: list[SlotSpend]


async def spend_summary(conn: AsyncConnection, now: datetime, tz: str) -> Spend:
    """The user's spend (the evals' is their own)."""
    day_start, month_start = spend.period_starts(now, tz)
    rows = await (
        await conn.execute(
            f"SELECT slot, {spend.SPEND_SINCE}, {spend.SPEND}, "  # noqa: S608
            "count(*) FILTER (WHERE created_at >= %s) FROM llm_calls WHERE created_at >= %s "
            "AND budget = 'owner' GROUP BY slot ORDER BY slot",
            (day_start, day_start, month_start),
        )
    ).fetchall()
    by_slot = [SlotSpend(r[0], Decimal(r[1]), Decimal(r[2]), int(r[3])) for r in rows]
    return Spend(
        today_eur=sum((s.today_eur for s in by_slot), Decimal(0)),
        month_eur=sum((s.month_eur for s in by_slot), Decimal(0)),
        caps=await get_caps(conn),
        by_slot=by_slot,
    )


async def slot_daily_usage(conn: AsyncConnection, slot: str, now: datetime) -> tuple[Decimal, Decimal] | None:
    """Average daily input and output tokens of a slot over the last 7 days; None when it has no usage."""
    row = await (
        await conn.execute(
            "SELECT count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) FROM llm_calls "
            "WHERE slot = %s AND status = 'settled' AND created_at >= %s AND budget = 'owner'",
            (slot, now - timedelta(days=7)),
        )
    ).fetchone()
    if not row or row[0] == 0:
        return None
    return Decimal(row[1]) / 7, Decimal(row[2]) / 7
