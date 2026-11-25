"""One conversation turn's research and its memory, shared by the research and place tools.

no research budget. The effort is the question's; the only limit is a **loop guard**, so Pisa never
spins on itself: the same call made twice, rounds of model requests that bring no new evidence (no new place,
page or result), or a high safety ceiling (calls, seconds). Then the tools refuse and tell the model to answer
with what is solid and say what is missing. A quick request never meets the guard: it is no invitation to
research. The first slow call says "Cerco, un attimo" at once, and each call grows the turn's AI reservation
by one more model request (the AI caps, bound everything).
"""

import asyncio
import ipaddress
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pisa.brain.spoken import Round
from pisa.brain.untrusted import OWN_ERROR, block
from pisa.domain.connections import ACK_DETAIL
from pisa.domain.errors import PisaError
from pisa.domain.tools import named

# RAIL: the loop guard. Not a budget: rounds with nothing new, a repeated call, or the ceiling.
STALE_ROUNDS = 3  # rounds in a row whose research calls brought no new place, page or result
CEILING_CALLS = 60  # a safety ceiling far past any real question (a deep comparison is ~25-40 calls)
CEILING_SECONDS = 180.0
PLACES_COUNTED = 15  # past the skill's 15 candidates, a new place is no new evidence
PER_CALL_S = 20.0  # one call's own limit (a hung provider never holds the turn to the ceiling)
GRACE_SECONDS = 5.0  # a call started near the ceiling may run this much past it, never Tavily's 30 s timeout
STOP = "Stop searching: answer now with what is solid, and say plainly what you couldn't check."
WORKING = {"it-IT": "Cerco, un attimo.", "en-US": "Let me look, one moment."}
SLOW_TOOLS = named(lambda t: t.slow)
# voice6: a web call Pisa itself refused before any provider answered (no provider for the web is
# ready, or the month's cap is reached): its error is Pisa's own words (`PlaceProviders.choose`), never a
# provider's. A provider's own failure (`place_provider_error`, its cause quoted) stays outside text.
# every tool's error Pisa writes itself for a provider
# that isn't configured or is capped, and Pisa's own "I don't know where you are" (`location_unknown`). Each
# such error goes back in a block of its own kind (`OWN_ERROR`): the block itself carries the marker, so
# `voice6.outside_kinds` never pairs counts with blocks.
OWN_FAILURE_CODES = frozenset({"place_provider_not_configured", "places_cap_reached", "location_unknown"})


def working(lang: str, words: str) -> str:
    """Pisa's interim line in their language: the turn's (their Settings', the language they spoke), never
    their words' guess: the reply is in the turn's language "even when their words read as another language",
    so the interim line is too (the words' guess said
    "Let me look, one moment." before an Italian reply)."""
    del words
    return WORKING.get(lang, WORKING["en-US"])


def failed_kind(err: Any) -> str:
    """The block kind of a tool's error: `OWN_ERROR` when Pisa wrote it itself (`OWN_FAILURE_CODES`), else
    `tool-error` (a provider's words may be in it: outside text)."""
    return OWN_ERROR if getattr(err, "code", None) in OWN_FAILURE_CODES else "tool-error"


def failed_block(
    what: str, err: PisaError, *, kind: str | None = None, why: str = "why the tool could not answer"
) -> str:
    """A tool's failure, as data: its message can carry a provider's words or the user's query. The block's
    kind is `failed_kind` unless the tool names its own. No provider set up on this server is no failure: the
    user is told so plainly ("La ricerca di luoghi non è ancora configurata su questo server.")."""
    if getattr(err, "detail", "") == "provider_not_configured":  # no provider has a key: not set up
        return block(kind or failed_kind(err), f"Not set up on this server yet: {err.message}", UNCONFIGURED)
    if getattr(err, "detail", "") == ACK_DETAIL:  # a paid provider set up, waiting for the user's OK
        return block(kind or failed_kind(err), f"Waiting for the user's OK: {err.message}", NEEDS_ACK)
    return block(kind or failed_kind(err), f"{what}: {err.message}", why)


# what the model reads of a provider not set up on this server: no failure
UNCONFIGURED = "not set up on this server yet, which is no failure: tell the user so plainly in one line"
# what the model reads of a paid provider set up but not acknowledged (Collegamenti): no failure either
NEEDS_ACK = (
    "a paid provider waits for the user's OK of its monthly cap in Collegamenti (Settings → Connections), "
    "which is no failure: tell the user so plainly in one line"
)


Grow = Callable[[int], Awaitable[None]]
"""Reserve one more model request of this many prompt characters (raises PisaError budget_exceeded)."""
SayNow = Callable[[str], Awaitable[None]]
ShowPlaces = Callable[[dict[str, Any], str, str], Awaitable[str]]
"""Show the `places` object (card, focus, purpose): the device's status (present_tools.Ack.status)."""
ShowSheet = Callable[[dict[str, Any], str, str], Awaitable[str]]
"""Show a `sheet` (data, focus, purpose): the device's status."""
SaveMenu = Callable[[str, str, list[str]], Awaitable[None]]
"""Keep a menu Pisa read this turn: (place id, page url, dishes)."""


SOURCES_MAX = 6  # links on a `sources` card
SOURCE_URL_MAX = 2000  # the card's limits (protocol): a longer link or host is dropped, never cut
SOURCE_HOST_MAX = 255


DOING_MAX = 80  # characters of the model's progress line


@dataclass
class ResearchTurn:
    """One turn's research: the budget, what the tools found (places, pages), and the hooks into the turn."""

    lang: str
    prompt_chars: int  # what the turn's first model request carried
    said: str = ""  # the user's words this turn: an address in them may be read (read_page)
    loop: Round | None = None  # the turn's loop: what its tools showed counts for `finish`
    say_now: SayNow | None = None
    grow: Grow | None = None
    show: ShowPlaces | None = None
    save_menu: SaveMenu | None = None
    show_sheet: ShowSheet | None = None
    earlier: dict[str, Any] | None = None  # the conversation's kept findings (its last research)
    earlier_at: str = ""  # when they were kept, as Pisa says it ("14:05")
    clock: Callable[[], float] = time.monotonic
    started: float = field(default=0.0)
    calls: int = 0
    made: set[str] = field(default_factory=set)  # the loop guard: each call's tool and arguments, made once
    # the checks made of places and pages (details, hours, travel, a page read): new evidence
    facts: set[tuple[str, ...]] = field(default_factory=set)
    known_urls: set[str] = field(default_factory=set)  # pages the earlier research found
    on_shown: Callable[[], None] | None = None  # the complete answer is on the screen now
    rounds: int = 0  # model requests of this turn (`next_round`)
    round_calls: int = 0  # research calls of the current round
    round_evidence: int = 0  # the evidence when the current round began
    stale: int = 0  # rounds in a row whose calls brought nothing new
    stopped: str | None = None  # why the guard stopped the research (a word, for the log)
    stop_round: int = -1  # the round in which the guard first said stop
    # a research call in a later round than the stop: the model didn't stop, so the next
    # request may use no tool (`spoken.Loop`): it answers with what is solid
    searched_past_stop: bool = False
    reviews: dict[str, str] = field(default_factory=dict)  # place id → its reviews and attributes, as read
    snippets: dict[str, str] = field(default_factory=dict)  # url → the search result's title and snippet
    candidates: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # url → reader (name, quote)
    criteria: list[str] = field(default_factory=list)  # the criteria the answer was built on (show_findings)
    findings: dict[str, Any] | None = None  # what show_findings showed: kept for the conversation
    context_chars: int = 0  # the tool results so far: each next model request carries them too
    working_said: bool = False
    places: dict[str, dict[str, Any]] = field(default_factory=dict)  # place id → facts seen this turn
    urls: dict[str, str] = field(default_factory=dict)  # url → title, seen this turn
    read: set[str] = field(default_factory=set)  # pages read_page read this turn (a menu comes only from one)
    pages: dict[str, str] = field(default_factory=dict)  # url → the text read_page read (menu dishes from it)
    taste: list[str] = field(default_factory=list)  # the user's likes: what `fits` may name
    avoid: list[str] = field(default_factory=list)  # dislikes, hard limits: named only negated
    different_allowed: bool = False  # this card may show one "something different" place (~1 in 5)
    shown: bool = False
    # the research guide joins the turn's instructions once a research tool is called
    # (or from the start, in a follow-up to a research turn), never by the user's words.
    researched: bool = False
    # what the model says it is doing (a research tool's `saying`): the progress line after 6 s
    # of silence uses it, and the template only when the model gave none.
    on_doing: Callable[[str], None] | None = None
    # a pick (their words ask to find, recommend or compare), what Pisa speaks after
    # the sheet (`lead`), their situation for "why for you now" (`now`, their next event: title and start),
    # and whether Pisa asked once for the guides before showing a pick
    pick: bool = False
    lead: str = ""
    now: Any = None
    event: tuple[str, Any] | None = None
    web_tried: bool = False
    guides_asked: bool = False
    widen_asked: bool = False  # Pisa asked once for more candidates before the pick
    # the pick Pisa held to ask for the guides: shown at the turn's end if the model never shows it again
    pending: Callable[[], Awaitable[str]] | None = None
    # who is with them, from their own words in this conversation ("con Giulia")
    company: list[str] = field(default_factory=list)
    # their hard requirements (the asked time, the budget, the party, seated, to work),
    # each finalist's verdict against them, the ones excluded with their reason and source, and the opening
    # states this turn read (place id, the asked time as ISO → open_at's answer)
    asked: Any = None
    # the time where they are (a find that isn't a pick checks too)
    local_now: Callable[[], Any] | None = None
    verdicts: dict[str, Any] = field(default_factory=dict)
    excluded: list[dict[str, str]] = field(default_factory=list)
    opening: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    all_failed_said: bool = False
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)  # place id → its times
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.started = self.started or self.clock()
        if self.earlier:  # a page the last research found may be read again ("the menu?")
            self.known_urls |= {str(p.get("url")) for p in self.earlier.get("pages") or [] if p.get("url")}
            self.known_urls |= {str(r.get("url")) for r in self.earlier.get("results") or [] if r.get("url")}
            self.known_urls |= {
                str(c.get("url")) for r in self.earlier.get("shown") or [] for c in r.get("cited_by") or []
                if c.get("url")
            }  # fmt: skip

    def doing(self, saying: str | None) -> None:
        line = " ".join((saying or "").split())[:DOING_MAX]
        if line and self.on_doing is not None:
            self.on_doing(line)

    def evidence(self) -> int:
        """How much this turn has found: places (at most PLACES_COUNTED), results, pages read, reader
        candidates, and the checks made (details, hours, travel of a known place are evidence too)."""
        return (
            min(len(self.places), PLACES_COUNTED) + len(self.urls) + len(self.read)
            + sum(map(len, self.candidates.values())) + len(self.facts)
        )  # fmt: skip

    def next_round(self) -> None:
        """A model request begins (the history processor calls it): the round before it is judged. A round
        whose research calls brought nothing new counts towards the guard; one that found something resets
        it."""
        if self.round_calls:
            found = self.evidence()
            self.stale = 0 if found > self.round_evidence else self.stale + 1
        self.rounds += 1
        self.round_calls = 0
        self.round_evidence = self.evidence()

    def _stop(self) -> str | None:
        elapsed = self.clock() - self.started
        if self.stale >= STALE_ROUNDS:
            self.stopped = "stale"
            return f"Your last {self.stale} rounds of searching found nothing new. {STOP}"
        if self.calls >= CEILING_CALLS or elapsed >= CEILING_SECONDS:
            self.stopped = "ceiling"
            return (
                f"Pisa's safety ceiling for one turn is reached ({self.calls} calls, {elapsed:.0f} s). {STOP}"
            )
        return None

    async def gate(self, tool: str, args: dict[str, Any] | None = None) -> str | None:
        """Before a research call: None to go ahead, else what the tool answers instead (the loop guard). The
        model's parallel calls of one response pass through here one at a time, and each takes its slot
        before anything awaits (20 parallel calls all passed a check made before the
        count)."""
        async with self._lock:
            self.researched = True
            self.web_tried = self.web_tried or tool in ("web_search", "read_page")
            key = tool + json.dumps(
                {k: v for k, v in (args or {}).items() if k != "saying"}, sort_keys=True, default=str
            )
            if (stop := self._stop()) is not None:  # before a repeat, so repeats stop too
                if self.stop_round < 0:
                    self.stop_round = self.rounds
                elif self.rounds > self.stop_round:
                    self.searched_past_stop = True
                return stop
            if key in self.made:
                self.round_calls += 1  # a repeat is a call of its round, bringing nothing
                return (
                    f"Not called again: {tool} with these same arguments already ran this turn; its "
                    "result is above. Use it, or search with other words."
                )
            self.made.add(key)
            self.calls += 1
            self.round_calls += 1
            if self.grow is not None:
                try:
                    await self.grow(self.prompt_chars + self.context_chars)
                except PisaError as err:
                    self.calls -= 1
                    self.round_calls -= 1
                    self.made.discard(key)
                    if err.code != "budget_exceeded":
                        raise
                    return f"{err.message} Stop searching: answer now with what you have."
            say = tool in SLOW_TOOLS and not self.working_said and self.say_now is not None
            self.working_said = self.working_said or say
        if say and self.say_now is not None:
            await self.say_now(working(self.lang, self.said))
        return None

    async def bounded[T](self, call: Awaitable[T]) -> T:
        """A research call cut at the turn's safety ceiling (plus a short grace): the user gets an answer,
        never a wait past it."""
        left = min(PER_CALL_S, CEILING_SECONDS - (self.clock() - self.started) + GRACE_SECONDS)
        try:
            async with asyncio.timeout(max(left, 1.0)):
                return await call
        except TimeoutError:
            raise PisaError(
                "provider_error", "The turn's research time ran out before this answered.", ""
            ) from None

    def may_read(self, url: str) -> str | None:
        """None when read_page may fetch `url`, else why not: only pages this turn's web_search found, pages
        this conversation's earlier research found ("explain the menu"), or an address in the
        user's own words, and never a private or numeric host. A page or review that asks to
        open a link (with the user's data in it) is data, not a reason."""
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return "only http(s) pages can be read."
        try:
            ipaddress.ip_address(parts.hostname)
            return "a page at a numeric address isn't read."
        except ValueError:
            pass
        host = parts.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
            return "a local address isn't read."
        if (
            url.strip() not in self.urls
            and url.strip() not in self.known_urls
            and url.strip() not in self.said
        ):
            return (
                "only pages web_search found (this turn or the last research), or their own address, are "
                "read."
            )
        return None

    def seen(self, result: str) -> str:
        self.context_chars += len(result)
        return result

    def sources(self, limit: int = SOURCES_MAX, *, read_only: bool = False) -> list[dict[str, str]]:
        """The links behind this turn's web facts, for the `sources` card: the pages it read first,
        then the search results it saw; only addresses this turn's tools returned (never one Pisa made up).
        `read_only`, the pages read_page read (the sheet takes images and links from them
        only)."""
        ordered = [u for u in self.urls if u in self.read]
        if not read_only:
            ordered += [u for u in self.urls if u not in self.read]
        out: list[dict[str, str]] = []
        for url in ordered:
            parts = urlsplit(url)
            host = (parts.hostname or "").removeprefix("www.")
            # A link too long to keep whole, or a host past DNS's 255, is dropped: never cut into a broken
            # link, never a card that fails its schema after the answer was spoken.
            if (
                not url.startswith(
                    ("http://", "https://")
                )  # the card's schema is case-sensitive ("HTTPS://")
                or not host
                or len(url) > SOURCE_URL_MAX
                or len(host) > SOURCE_HOST_MAX
                or any(o["url"] == url for o in out)
            ):
                continue
            title = (self.urls.get(url) or "").strip()
            out.append({"title": (title if title and title != url else host)[:120], "url": url, "host": host})
            if len(out) == limit:
                break
        return out

    def own_page(self, url: str) -> bool:
        """A page a place publishes itself (its menu, its site): never a guide."""
        from pisa.domain.research import own_page

        facts = self.places.values()
        return own_page(
            url, [str(f["website"]) for f in facts if f.get("website")],
            [str(f["name"]) for f in facts if f.get("name")],
        )  # fmt: skip

    def guide_read(self) -> bool:
        """A guide, award or press page was read this turn (a place's own menu page is none)."""
        return any(not self.own_page(u) for u in self.read)

    def likes(self, place_id: str) -> list[str]:
        """Their likes (their profile) that this place's own facts show (its kind,
        its name, its reviews as read): only their data, only what the place has."""
        from pisa.domain.profile import stems

        facts = self.places.get(place_id) or {}
        have = stems(" ".join([str(facts.get("name") or ""), str(facts.get("category") or ""),
                               self.reviews.get(place_id, "")]))  # fmt: skip
        # a profile statement is a sentence ("Ama i cornetti integrali", "Gli piacciono i cornetti"):
        # what they like is its object, matched and said alone ("cornetti integrali, come piace a te")
        from pisa.domain.research import liked

        return [w for t in self.taste if (w := liked(t)) and (mine := stems(w)) and mine <= have]

    def note_place(self, place_id: str, **facts: Any) -> None:
        known = self.places.setdefault(place_id, {})
        known.update({k: v for k, v in facts.items() if v is not None})

    def note_travel(self, place_id: str, minutes: int) -> None:
        """The travel time to a place this turn already found; a place only resolved by travel_time isn't
        found (show_places shows found places only)."""
        if place_id in self.places:
            self.places[place_id]["travel_min"] = minutes
