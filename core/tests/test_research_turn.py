"""The research turn's guards (review fixes; the loop guard
replaces the budget): parallel calls take their slots one at a time, the same call is never made twice, rounds
that find nothing new stop the research, the ceiling holds, a slow call is cut, and read_page reads only this
turn's search results or the user's own addresses."""

import asyncio

import pytest

from pisa.brain import research_turn
from pisa.brain.research_turn import CEILING_CALLS, STALE_ROUNDS, ResearchTurn
from pisa.domain.errors import PisaError


def test_parallel_calls_cannot_pass_the_ceiling() -> None:
    """H1: many calls in one model response, with a reservation that waits like a database: exactly the
    ceiling goes ahead (each takes its slot before anything awaits)."""

    async def go() -> list[str | None]:
        async def grow(chars: int) -> None:
            await asyncio.sleep(0.001)

        turn = ResearchTurn(lang="it-IT", prompt_chars=100, grow=grow)
        results = await asyncio.gather(
            *(turn.gate("web_search", {"query": f"q{i}"}) for i in range(CEILING_CALLS + 5))
        )
        assert turn.calls == CEILING_CALLS
        return list(results)

    results = asyncio.run(go())
    assert results.count(None) == CEILING_CALLS
    assert all(r and "safety ceiling" in r and "answer now with what is solid" in r for r in results if r)


def test_no_budget_twenty_parallel_calls_all_go_ahead() -> None:
    """The old 12-call budget is gone: a pick's wide first round runs whole."""

    async def go() -> list[str | None]:
        turn = ResearchTurn(lang="it-IT", prompt_chars=100)
        turn.next_round()
        return list(await asyncio.gather(*(turn.gate("map_search", {"query": f"q{i}"}) for i in range(20))))

    assert asyncio.run(go()) == [None] * 20


def test_the_same_call_twice_is_refused_and_not_counted() -> None:
    async def go() -> tuple[str | None, str | None, int]:
        turn = ResearchTurn(lang="it-IT", prompt_chars=100)
        first = await turn.gate("web_search", {"query": "colazione monti", "saying": "Cerco"})
        again = await turn.gate("web_search", {"query": "colazione monti", "saying": "Ancora"})
        return first, again, turn.calls

    first, again, calls = asyncio.run(go())
    assert first is None and calls == 1
    assert again is not None and again.startswith("Not called again: web_search with these same arguments")


def test_rounds_that_find_nothing_new_stop_the_research() -> None:
    """Rounds of calls that bring no new place, page or result stop a spinning turn; a round that
    finds something resets the count."""

    async def go() -> list[str | None]:
        turn = ResearchTurn(lang="it-IT", prompt_chars=100)
        out: list[str | None] = []
        for i in range(STALE_ROUNDS + 2):
            turn.next_round()
            out.append(await turn.gate("web_search", {"query": f"nulla {i}"}))
            if i == 0:
                turn.urls[f"https://found{i}.example/"] = "x"  # the first round found something
        return out

    out = asyncio.run(go())
    assert out[: STALE_ROUNDS + 1] == [None] * (STALE_ROUNDS + 1)
    assert out[-1] is not None and "found nothing new" in out[-1] and "answer now" in out[-1]


def test_a_refused_reservation_gives_the_slot_back() -> None:
    async def go() -> int:
        async def grow(chars: int) -> None:
            raise PisaError("budget_exceeded", "The daily AI cap is reached.", "")

        turn = ResearchTurn(lang="it-IT", prompt_chars=100, grow=grow)
        assert (await turn.gate("map_search", {"query": "bar"}) or "").startswith(
            "The daily AI cap is reached."
        )
        return turn.calls

    assert asyncio.run(go()) == 0


def test_a_slow_call_is_cut_at_the_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """M1: a page started near the end can't run to the provider's 30 s timeout."""
    monkeypatch.setattr(research_turn, "CEILING_SECONDS", 0.05)
    monkeypatch.setattr(research_turn, "GRACE_SECONDS", 0.0)

    async def go() -> None:
        turn = ResearchTurn(lang="it-IT", prompt_chars=100)
        with pytest.raises(PisaError, match="research time ran out"):
            await turn.bounded(asyncio.sleep(5))

    asyncio.run(go())  # cut after the 1 s floor, not the 5 s sleep


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.timeout.com/amsterdam/coffee", True),  # from this turn's web_search
        ("https://owner-said.example/menu", True),  # in the user's own words
        ("https://evil.example/?q=Cena+con+Paola+alle+20", False),  # a page asked for it: data, not a reason
        ("http://127.0.0.1:8000/v1/devices", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("http://localhost/", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
    ],
)
def test_read_page_reads_only_found_or_said_pages(url: str, allowed: bool) -> None:
    """M2: injected text can't make Pisa fetch an address that carries the user's data to someone else."""
    turn = ResearchTurn(
        lang="it-IT", prompt_chars=100, said="leggi https://owner-said.example/menu per favore"
    )
    turn.urls["https://www.timeout.com/amsterdam/coffee"] = "Time Out"
    assert (turn.may_read(url) is None) is allowed
