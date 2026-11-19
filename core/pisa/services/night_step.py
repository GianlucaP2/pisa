"""The night step's frame: what every night engine goes through for a call, a page and a search.

One `NightStep` an engine (ideas, curious, desk, episodes, memory, portrait): the stop ("ferma tutto" said
mid-step ends its work), its spend so far on the ledger (`infra.spend`, its rows found by trace), its own €
cap checked before a call (the next call assumed as dear as its dearest so far), the one model call
(`ask`: never when stopped, a refusal of the night's budget goes up to the frame, any other failure is that
call's only), the page read (`read`: the page or None, a reader's failure logged, never raised) and the web
search (through the night's own `NightService.search`: its filter, its 40, its stop).

What each engine checks besides (its share of the 40, its own sites, its pages a night) stays its own.
Logs carry ids and counts only.
"""

from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg_pool import AsyncConnectionPool

from pisa.brain.call import Think
from pisa.domain import prompt_examples
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.infra import spend
from pisa.logging import get_logger
from pisa.services.stop import stopped_now

if TYPE_CHECKING:
    from pisa.services.night import NightService

_log = get_logger("pisa.night")
Read = Callable[[str], Awaitable[tuple[str, str] | None]]  # a page: (the URL it ended on, its text)
Step = Callable[[date], Awaitable[int]]


class Stopped(Exception):
    """ "Ferma tutto" said mid-step: no further call, fetch, search or row."""


class Capped(Exception):
    """A step's own € cap is reached: its work ends here with what it did so far; the night goes on."""


async def no_read(_: str) -> None:
    return None


def reader(places: Any) -> Read:
    """Pisa's own signed reader (`places.reader`, set once Pisa is published), looked up at each read: no
    reader, no page."""

    async def read(url: str) -> tuple[str, str] | None:
        found = getattr(places, "reader", None)
        return await found(url) if found is not None else None

    return read


def result(r: Any) -> dict[str, str]:
    """One search result as the engines keep it: title, url and the snippet on one line."""
    get = r.get if isinstance(r, dict) else lambda k, d="": getattr(r, k, d)
    return {
        "title": str(get("title", "") or ""),
        "url": str(get("url", "") or ""),
        "snippet": " ".join(str(get("content", "") or "").split()),
    }


class NightStep:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        name: str,
        trace: str,
        owner: Owner | None = None,
        night: "NightService | None" = None,
        read: Read = no_read,
    ) -> None:
        self._pool = pool
        self._name = name  # the engine's log prefix (`ideas_call_failed`)
        self._trace = trace  # its calls' trace prefix: `<trace>-<night>-<task>`
        self._owner = owner
        self._night = night
        self.reader = read

    # --- the stop -------------------------------------------------------------------------------------------

    async def stopped(self) -> bool:
        async with self._pool.connection() as conn:
            return await stopped_now(conn)

    async def halt_if_stopped(self) -> None:
        """RAIL (c): a stop said mid-night, no further call, fetch or search."""
        if await self.stopped():
            raise Stopped

    # --- spend and the budget -------------------------------------------------------------------------------

    def tonight(self, night: date) -> str:
        """Tonight's calls of this engine, as a trace pattern."""
        return f"{self._trace}-{night.isoformat()}-%"

    async def spent(self, like: str) -> tuple[Decimal, Decimal]:
        """What the ledger's calls whose trace matches `like` have cost, and the dearest of them."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"SELECT {spend.SPEND}, {spend.DEAREST} "  # noqa: S608
                    "FROM llm_calls WHERE trace_id LIKE %s",
                    (like,),
                )
            ).fetchone()
        return (Decimal(row[0]), Decimal(row[1])) if row else (Decimal(0), Decimal(0))

    async def cost(self, trace: str) -> Decimal:
        """What the ledger's calls of exactly this trace have cost."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"SELECT {spend.SPEND} FROM llm_calls WHERE "  # noqa: S608
                    "trace_id = %s",
                    (trace,),
                )
            ).fetchone()
        return Decimal(row[0]) if row else Decimal(0)

    async def over(self, like: str, cap: Decimal, est: Decimal) -> bool:
        """RAIL (caps): would the next call pass `cap`? It is assumed to cost `est`, or as much as the
        dearest call so far if dearer."""
        spent, dearest = await self.spent(like)
        return spent + max(est, dearest) > cap

    async def within(self, like: str, cap: Decimal, est: Decimal) -> None:
        """`over`, as the engines' gate: past the cap, `Capped`."""
        if await self.over(like, cap, est):
            raise Capped

    # --- the call -------------------------------------------------------------------------------------------

    async def ask[T](
        self,
        think: Think | None,
        task: str,
        instructions: str,
        prompt: str,
        out: type[T],
        *,
        trace: str,
        cap: tuple[str, Decimal, Decimal] | None = None,
    ) -> T | None:
        """One model call: never without a model, never when stopped (`Stopped`), never past the engine's
        own `cap` (its calls' trace pattern, the cap, the next call's assumed cost: `Capped`); in Pisa's
        language. A refusal of the night's own budget goes up to the frame (it ends the night as `capped`);
        any other failure is this call's only (None)."""
        if think is None:
            return None
        await self.halt_if_stopped()
        if cap is not None:
            await self.within(*cap)
        try:
            if self._owner is not None:
                instructions = prompt_examples.in_language(instructions, self._owner.tag)
            return await think(task, instructions, prompt, out, trace=trace)
        except PisaError as err:
            if err.code == "budget_exceeded":
                raise
            _log.info(f"{self._name}_call_failed", task=task, code=err.code)
            return None
        except Exception as exc:  # a model's bad answer ends this call only
            _log.info(f"{self._name}_call_failed", task=task, error=exc.__class__.__name__)
            return None

    def guarded(self, step: Step) -> Step:
        """A stop said mid-step, or the engine's own cap, ends the step with what it did so far (0); the
        frame's own refusal (`budget_exceeded`, the night's €0.25) goes up and ends the night as `capped`."""

        async def run(night: date) -> int:
            try:
                return await step(night)
            except Stopped:
                _log.info(f"{self._name}_stopped")
                return 0
            except Capped:
                _log.info(f"{self._name}_capped")
                return 0

        return run

    # --- the page and the search ----------------------------------------------------------------------------

    async def read(self, url: str) -> tuple[str, str] | None:
        """A page fetched by Pisa: (the URL it ended on, its text), or None when there is none. A reader's
        failure is this page's only: logged (its class, never the URL), never raised."""
        try:
            got = await self.reader(url)
        except Exception as exc:
            _log.warning("night_read_failed", step=self._name, error=exc.__class__.__name__)
            return None
        return got or None

    async def sent(self, night: date, prefix: str) -> int:
        """The searches tonight's steps of kind `prefix*` have sent (an engine's own share)."""
        assert self._night is not None
        return await self._night.sent_tonight(night, prefix)

    async def search(
        self, query: str, *, kind: str, extra: Iterable[str], link: Callable[[str], bool]
    ) -> list[dict[str, str]] | None:
        """RAIL: only through the frame's `search` (its filter, its 40, its stop): the results whose link
        `link` accepts, or None when refused."""
        assert self._night is not None
        found = await self._night.search(query, kind=kind, extra=extra)
        if found is None:
            return None
        return [r for r in (result(x) for x in found) if link(r["url"])]
