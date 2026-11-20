"""Pisa's CEO desk, as steps of the frame's night.

**Shadow only**: nothing here reaches them (no notification, message, candidate, task or job)
and nothing here enters any prompt: no turn, intake, profile, grounding or style path reads `pisa_desk*` (only
this module names them). **Pisa never contacts anyone or anything outward**: the desk reads public pages
and searches through the frame, and writes readings, drafts and scores; it never sends, applies, books or
pays.

The steps (`night.STEPS`, after the ideas; the frame's stop, budget `night` and search gate hold over each):
- **d1_windows**, the window calendar (IT, EU, US): official pages from a fixed list (and a discovery search
  when the night has one left), each read by a model that **points at dates**; code keeps a date only when its
  words are on the fetched page, name the same day and the page is an official host. A date that fails is
  stored as failed and never shown.
- **d2_dossiers**: one per idea made ready (≤ 3), with its neighbours, its **best window** (in
  code), **the acquirer lens** (names found on their fetched pages), **the customer's voice** (public reviews
  only, quotes found on their pages), **the money desk** as a draft capital stack (eligibility in code against
  their legal form) and **the decision card** (Pisa's p; their p only from their tap).
- **d3_arbitrage**, four ways (product, regulation, cost, entry), one way a dossier a night: each gap's
  name is found on its fetched page.
- **d4_journal**: the decisions resolved, Brier-scored per party and domain.

**Caps**: the desk's calls ≤ €0.04 a night (summed by code from the ledger by trace), on budget `night`; its
searches only what the ideas and the curiosity left of the frame's 40 (≤ 6), through `night.search` and its
filter; ≤ 16 pages a night. Fetched pages are data. Logs carry ids and counts only.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import desk as brain
from pisa.domain import desk as rules
from pisa.domain import ideas as ideas_rules
from pisa.domain import taint
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.night_step import Capped, NightStep, Read, Stopped, no_read

if TYPE_CHECKING:
    from pisa.services.ideas import IdeasService
    from pisa.services.night import NightService

_log = get_logger("pisa.desk")
EST_CALL_EUR = Decimal("0.003")  # what one desk call is assumed to cost before it runs (the cap's margin)


def _serves(country: rules.Country, owner: Owner) -> bool:
    """Italy's official pages and searches are read for a user in Italy only (PISA_COUNTRY); the EU's and
    the US's for anyone."""
    return country != "it" or owner.in_country("IT")


def _about(idea: dict[str, Any]) -> str:
    """An idea comes from their own problem, so its words go to a model as theirs, as data."""
    from pisa.services.ideas import block_owner

    return block_owner(
        f"{idea['statement']}\nMechanism: {idea['mechanism']}", "Pisa's idea from the user's problem"
    )


class DeskService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        night: "NightService",
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        ideas: "IdeasService | None" = None,
        think: brain.Think | None = None,
        read: Read = no_read,
    ) -> None:
        self._pool = pool
        self._night = night
        self._owner = owner
        self._clock = clock
        self._ideas = ideas
        self._think = think
        # the frame's stop, budget, call, read and search
        self.step = NightStep(pool, name="desk", trace="desk", owner=owner, night=night, read=read)

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {
            "d1_windows": self.windows_step,
            "d2_dossiers": self.dossiers,
            "d3_arbitrage": self.arbitrage,
            "d4_journal": self.journal,
        }

    # --- the gates every call, fetch and search goes through ---

    async def _ask[T](self, night: date, task: str, instructions: str, prompt: str, out: type[T]) -> T | None:
        """One model call through the frame (its stop, its failure policy), never past the desk's € cap
        tonight (RAIL (caps): the next call assumed to cost as much as the dearest so far)."""
        return await self.step.ask(
            self._think, task, instructions, prompt, out,
            trace=f"desk-{night.isoformat()}-{task}",
            cap=(self.step.tonight(night), rules.DESK_CAP_EUR, EST_CALL_EUR),
        )  # fmt: skip

    async def _ready(self) -> list[dict[str, Any]]:
        """The ideas ready for the dossiers (≤ 3); on a tie the ones that have a dossier stay."""
        assert self._ideas is not None
        async with self._pool.connection() as conn:
            kept = [
                r[0] for r in await (await conn.execute("SELECT idea_id FROM pisa_desk_dossiers")).fetchall()
            ]
        return await self._ideas.for_desk(rules.DOSSIERS_MAX, kept)

    async def _names(self) -> list[taint.Personal]:
        """What the frame's filter strips besides its own list: the proper nouns of their problems and
        fragments (the idea engine's), of the ideas the dossiers follow, and their own company and legal names
        from their `desk` setting."""
        names: list[str] = [*await self._ideas.his_names()] if self._ideas is not None else []
        if self._ideas is not None:
            for idea in await self._ready():
                names += ideas_rules.proper_nouns(f"{idea['statement']} {idea['mechanism']}")
        from pisa.db.settings import get_setting

        async with self._pool.connection() as conn:
            mine = await get_setting(conn, "desk") or {}
        names += (
            [str(v) for k, v in mine.items() if k != "legal_forms" and isinstance(v, str)]
            if isinstance(mine, dict)
            else []
        )
        return [taint.Personal(n) for n in names]

    async def _search(
        self, night: date, query: str, kind: str, *, model: bool = True
    ) -> list[dict[str, str]] | None:
        """RAIL (caps): only through the frame's `search` (its filter, its 40, its stop), and only
        the desk's own 6, counted on the frame's rows, never past what is left of the 40. A
        model-written query first loses every capitalised word that is not generic; their names go to
        the filter."""
        if await self.step.sent(night, "d") >= rules.DESK_SEARCHES or await self._night.left() <= 0:
            return None
        sent = ideas_rules.generic(query) if model else query
        return await self.step.search(sent, kind=kind, extra=await self._names(), link=rules.link)

    async def _page(
        self, night: date, url: str, country: rules.Country | None = None
    ) -> tuple[str, str] | None:
        """A page fetched by Pisa (`read`): (the URL it ended on, its text), or None when the link doesn't
        resolve, the stop is on or tonight's pages are spent (counted in the database). After
        its redirects, a window's page is still an official host of its country, and any other page still the
        cited site. Its words are data, always."""
        if not rules.link(url):
            return None
        await self.step.halt_if_stopped()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO pisa_desk_nights (night, pages) VALUES (%s, 1) ON CONFLICT (night) DO "
                    "UPDATE SET pages = pisa_desk_nights.pages + 1 WHERE pisa_desk_nights.pages "
                    "< %s RETURNING pages",
                    (night, rules.PAGES_MAX),
                )
            ).fetchone()
        if row is None:  # tonight's pages are spent
            return None
        got = await self.step.read(url)
        if not got:
            return None
        final = str(got[0] or "")
        if country is not None:
            ok = rules.official(final, country) == country
        else:
            ok = rules.link(final) and ideas_rules.same_site(url, final)
        return (final, got[1]) if ok else None

    # --- d1: the window calendar ---

    def _tonight(self, night: date) -> list[tuple[rules.Country, rules.WindowKind | None, str]]:
        start = night.toordinal() % len(rules.SOURCES)
        n = min(rules.SOURCES_PER_NIGHT, len(rules.SOURCES))
        return [rules.SOURCES[(start + i) % len(rules.SOURCES)] for i in range(n)]

    async def windows_step(self, night: date) -> int:
        """The night's official pages, read for dates; every date checked by code on its fetched page.
        Returns the windows that passed tonight."""
        pages: list[tuple[rules.Country, rules.WindowKind | None, str]] = [
            p for p in self._tonight(night) if _serves(p[0], self._owner)
        ]
        passed = 0
        try:
            # one discovery search a night, fixed in code (nothing of theirs); through the filter all the same
            country, query = rules.DISCOVERY[night.toordinal() % len(rules.DISCOVERY)]
            found = await self._search(night, query, "d1_discover") if _serves(country, self._owner) else None
            for r in (found or [])[:2]:
                if r["url"] not in {u for *_, u in pages}:  # its host is checked before it is read
                    pages.append((country, None, r["url"]))
            read = {u for *_, u in pages}
            for country, _kind, url in pages:
                passed += await self._read_windows(night, country, url, read)
            # every window still shown, on a page not read tonight, is re-checked on it tonight, in code alone
            await self._recheck(night, read)
        except (Stopped, Capped):
            pass
        _log.info("desk_windows", pages=len(pages), passed=passed)
        return passed

    async def _recheck(self, night: date, read: set[str]) -> None:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, country, url, day, quote, title FROM pisa_desk_windows "
                    "WHERE passed AND day >= %s "
                    "ORDER BY day, id",
                    (night,),
                )
            ).fetchall()
        pages: dict[str, str | None] = {}
        for wid, country, url, day, quote, title in rows:
            if url in read:
                continue
            if url not in pages:
                got = await self._page(night, url, country)
                pages[url] = got[1] if got else None
            ok = rules.window_checked(
                day, quote, pages[url], country, title
            )  # RAIL: still on its page tonight
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pisa_desk_windows SET passed = %s, checked_on = %s WHERE id = %s",
                    (ok, night, wid),
                )

    async def _read_windows(self, night: date, country: rules.Country, url: str, read: set[str]) -> int:
        if rules.official(url, country) != country:  # RAIL: an official host, or nothing is read from it
            return 0
        got = await self._page(night, url, country)
        if got is None:  # tonight's check failed: the page's dates are not shown until it passes again
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pisa_desk_windows SET passed = false, checked_on = %s WHERE url = %s",
                    (night, url),
                )
            return 0
        url, text = got  # the page it ended on is the one cited (an official host of its country)
        read.add(url)
        out = await self._ask(
            night, "windows", brain.WINDOWS, brain.page_block(url, text, rules.PAGE_CHARS), brain.Windows
        )
        passed = 0
        for w in (out.items if out else [])[: rules.WINDOWS_PER_PAGE]:
            try:
                day = date.fromisoformat(w.day)
            except ValueError:
                continue
            ok = rules.window_checked(
                day, w.quote, text, country, w.title
            )  # RAIL: the date is on the fetched page
            forms = rules.forms_on_page(w.forms, text)
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO pisa_desk_windows (id, country, kind, title, url, day, quote, forms, "
                    "passed, first_seen, checked_on) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (url, kind, day, title) DO UPDATE SET quote = excluded.quote, "
                    "forms = excluded.forms, passed = excluded.passed, checked_on = excluded.checked_on",
                    (uuid.uuid4(), country, w.kind, w.title, url, day, w.quote, forms, ok, night, night),
                )
            passed += ok
        return passed

    async def windows(
        self, today: date, countries: tuple[rules.Country, ...] = ("it", "eu", "us")
    ) -> list[dict[str, Any]]:
        """The windows a sheet may show (the morning sheet's; never a turn's): checked by code on an official
        page this week, ahead within six months, soonest first, each with its link and its words."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, country, kind, title, url, day, forms, passed, checked_on FROM "
                    "pisa_desk_windows "
                    "WHERE passed AND country = ANY(%s) ORDER BY day, id",
                    (list(countries),),
                )
            ).fetchall()
        return [
            {
                "id": wid,
                "country": c,
                "kind": k,
                "url": u,
                "day": d,
                "forms": list(f or ()),
                "words": rules.window_words(k, t, d, today),
            }
            for wid, c, k, t, u, d, f, p, checked in rows
            if rules.visible(p, u, c, d, checked, today)
        ]

    # --- d2: dossiers, with the best window, the lens, the voice, the money and the decision ---

    async def dossiers(self, night: date) -> int:
        """A dossier for each idea made ready (≤ 3). Returns the dossiers written tonight."""
        if self._ideas is None:
            return 0
        made = 0
        deep: list[tuple[uuid.UUID, dict[str, Any]]] = []
        try:
            for idea in await self._ready():
                deep.append((await self._dossier(night, idea), idea))
                made += 1
            # one dossier a night is deepened: the first with no acquirer lens yet, the first with no voice
            async with self._pool.connection() as conn:
                lensed = {
                    r[0]
                    for r in await (
                        await conn.execute("SELECT dossier_id FROM pisa_desk_acquirers")
                    ).fetchall()
                }
                heard = {
                    r[0]
                    for r in await (await conn.execute("SELECT dossier_id FROM pisa_desk_voices")).fetchall()
                }
            for did, idea in deep:
                if did not in lensed:
                    await self._lens(night, did, _about(idea))
                    break
            for did, idea in deep:
                if did not in heard and idea["competitors"]:
                    await self._voices(night, did, idea["competitors"])
                    break
        except (Stopped, Capped):
            pass
        _log.info("desk_dossiers", dossiers=made)
        return made

    async def _dossier(self, night: date, idea: dict[str, Any]) -> uuid.UUID:
        assert self._ideas is not None
        now = self._clock()
        neighbours = await self._ideas.neighbours(idea["id"], rules.NEIGHBOURS)
        windows = await self.windows(night, rules.markets(idea["market"]))
        from pisa.db.settings import get_setting

        async with self._pool.connection() as conn:
            forms = (await get_setting(conn, "desk") or {}).get("legal_forms")
        grants: list[tuple[dict[str, Any], rules.Eligible]] = [
            (w, rules.eligible(rules.his_form(forms, w["country"]), w["forms"]))
            for w in windows
            if w["kind"] == "grant"
        ]
        # the best window, in code: the soonest grant they may be eligible for, else the soonest window
        best = next((w for w, ok in grants if ok != "not_eligible"), None) or (
            windows[0] if windows else None
        )
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO pisa_desk_dossiers (id, idea_id, night, market, neighbours, window_id, "
                    "created_at, "
                    "updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (idea_id) DO UPDATE SET "
                    "night = excluded.night, neighbours = excluded.neighbours, "
                    "window_id = excluded.window_id, "
                    "updated_at = excluded.updated_at RETURNING id",
                    (
                        uuid.uuid4(),
                        idea["id"],
                        night,
                        idea["market"],
                        neighbours,
                        best["id"] if best else None,
                        now,
                        now,
                    ),
                )
            ).fetchone()
            did: uuid.UUID = row[0] if row else uuid.uuid4()
            # the money desk: a draft, never sent, applied for or paid
            stack = rules.capital_stack(
                [(str(w["id"]), ok) for w, ok in grants], idea["buyers"], idea["revenue_year_eur"]
            )
            await conn.execute(
                "INSERT INTO pisa_desk_money (dossier_id, stack, updated_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (dossier_id) DO UPDATE SET stack = excluded.stack, updated_at = "
                "excluded.updated_at",
                (did, Jsonb(stack), now),
            )
        about = _about(idea)
        await self._decision(night, did, about)
        return did

    async def _decision(self, night: date, did: uuid.UUID, about: str) -> None:
        out = await self._ask(night, "decision", brain.DECISION, about, brain.Decision)
        if out is None:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_desk_decisions (id, dossier_id, domain, question, pisa_p, resolves_by, "
                "created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dossier_id, domain) DO NOTHING",
                (
                    uuid.uuid4(),
                    did,
                    out.domain,
                    out.question,
                    min(max(out.p, 0.01), 0.99),
                    night + timedelta(days=out.days),
                    self._clock(),
                ),
            )

    async def _lens(self, night: date, did: uuid.UUID, about: str) -> None:
        """The acquirer lens: who might buy it and what they lack, each name found on its fetched page."""
        plan = await self._ask(night, "lens", brain.LENS, about, brain.Lens)
        if plan is None:
            return
        found = await self._search(night, plan.query, "d2_lens")
        if not found:
            return
        out = await self._ask(
            night,
            "lens",
            brain.ACQUIRERS,
            f"{about}\n\n{brain.results_block(found)}",
            brain.Acquirers,
        )
        for a in (out.items if out else [])[: rules.ACQUIRERS_MAX]:
            if not 0 <= a.index < len(found):  # an index the results have; never a link the model wrote
                continue
            url = found[a.index]["url"]
            got = await self._page(night, url)
            ok = got is not None and rules.quoted(a.name, got[1])
            url = got[0] if got else url
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO pisa_desk_acquirers (id, dossier_id, name, lacks, url, filing, passed, "
                    "checked_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4(),
                        did,
                        a.name,
                        a.lacks,
                        url,
                        rules.host(url).endswith("sec.gov"),
                        ok,
                        self._clock(),
                    ),
                )

    async def _voices(self, night: date, did: uuid.UUID, competitors: list[tuple[str, str]]) -> None:
        """The customer's voice: the first competitor's public reviews only, each quote on its
        fetched page."""
        if not competitors:
            return
        # the query is code's, from a competitor's name read on its fetched page: generic() would strip
        # it; their names still go to the filter
        found = await self._search(night, f"{competitors[0][0]} recensioni reviews", "d2_voice", model=False)
        reviews = [r for r in found or [] if rules.public_review(r["url"])]  # RAIL: public reviews only
        if not reviews:
            return
        out = await self._ask(night, "voices", brain.VOICES, brain.results_block(reviews), brain.Voices)
        for v in (out.items if out else [])[: rules.VOICES_MAX]:
            if not 0 <= v.index < len(reviews):
                continue
            url = reviews[v.index]["url"]
            got = await self._page(night, url)
            ok = got is not None and rules.quoted(v.quote, got[1])
            url = got[0] if got else url
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO pisa_desk_voices (id, dossier_id, theme, quote, url, passed, checked_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), did, v.theme, v.quote[: rules.QUOTE_MAX], url, ok, self._clock()),
                )

    # --- d3: the arbitrage desk, four ways ---

    async def arbitrage(self, night: date) -> int:
        """One gap a night (the sheet's "one Italy/US gap"): one dossier and one way, both rotating; an
        Italian and a US search; each gap's name found on its fetched page. Returns the gaps that passed.
        Off unless the user is in Italy (PISA_COUNTRY): the two countries are Italy and the US."""
        if not self._owner.in_country("IT"):
            _log.info("desk_arbitrage_off", country=self._owner.country)
            return 0
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, idea_id FROM pisa_desk_dossiers WHERE night = %s ORDER BY id", (night,)
                )
            ).fetchall()
        about = {i["id"]: i for i in await self._ready()} if self._ideas else {}
        passed = 0
        try:
            if rows:
                did, iid = rows[night.toordinal() % len(rows)]
                idea = about.get(iid)
                way = rules.WAYS[night.toordinal() % len(rules.WAYS)]
                if idea is not None:
                    passed += await self._gaps(night, did, way, _about(idea))
        except (Stopped, Capped):
            pass
        _log.info("desk_arbitrage", dossiers=len(rows), passed=passed)
        return passed

    async def _gaps(self, night: date, did: uuid.UUID, way: rules.Way, about: str) -> int:
        dossier = about
        plan = await self._ask(
            night,
            "arbitrage",
            brain.ARBITRAGE.format(way=way, language="{language}"),
            dossier,
            brain.Arbitrage,
        )
        if plan is None:
            return 0
        it = await self._search(night, plan.query_it, "d3_arbitrage")
        us = await self._search(night, plan.query_us, "d3_arbitrage")
        if it is None or us is None:  # both sides looked at, or no claim about either
            return 0
        found = it + us
        if not found:
            return 0
        out = await self._ask(
            night,
            "arbitrage",
            brain.GAPS.format(way=way, language="{language}"),
            f"{dossier}\n\nItalia: [0-{len(it) - 1}], USA: "
            f"[{len(it)}-{len(found) - 1}]\n{brain.results_block(found)}",
            brain.Gaps,
        )
        passed = 0
        for g in out.items if out else []:
            side_ok = (0 <= g.index < len(it)) if g.has == "it" else (len(it) <= g.index < len(found))
            if not side_ok:  # the index must be on the side the model says has it
                continue
            other = g.other_index if g.other_index is not None and 0 <= g.other_index < len(found) else None
            url = found[g.index]["url"]
            got = await self._page(night, url)
            ok = got is not None and rules.quoted(g.name, got[1])
            url = got[0] if got else url
            if ok and other is not None:
                ok = await self._page(night, found[other]["url"]) is not None
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO pisa_desk_arbitrage (id, night, dossier_id, way, has, name, claim, url, "
                    "other_url, "
                    "passed, checked_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4(),
                        night,
                        did,
                        way,
                        g.has,
                        g.name,
                        g.claim,
                        url,
                        found[other]["url"] if other is not None else None,
                        ok,
                        self._clock(),
                    ),
                )
            passed += ok
        return passed

    # --- d4: the decision journal ---

    async def journal(self, night: date) -> int:
        """Brier per party and domain over every decision resolved so far. Returns the decisions scored."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT domain, pisa_p, his_p, outcome FROM pisa_desk_decisions WHERE outcome IS NOT NULL"
                )
            ).fetchall()
            pairs: list[tuple[rules.Party, rules.Domain, float | None, bool | None]] = []
            for domain, jp, hp, outcome in rows:
                pairs.append(("pisa", domain, float(jp), outcome))
                pairs.append(("owner", domain, float(hp) if hp is not None else None, outcome))
            for (party, domain), score in rules.scores(pairs).items():
                await conn.execute(
                    "INSERT INTO pisa_desk_scores (night, party, domain, brier) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (night, party, domain) DO UPDATE SET brier = excluded.brier",
                    (night, party, domain, score),
                )
        return len(rows)

    async def his_p(self, decision: uuid.UUID, p: float) -> None:
        """Their p, from their one tap on the sheet (the morning sheet's): the only way `his_p` is ever
        written."""
        if not 0 <= p <= 1:
            raise ValueError("a probability is between 0 and 1")
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_desk_decisions SET his_p = %s WHERE id = %s AND outcome IS NULL", (p, decision)
            )

    async def resolve(self, decision: uuid.UUID, outcome: bool) -> None:
        """A decision settled (their tap, or the public fact its question names): once."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_desk_decisions SET outcome = %s, resolved_at = %s "
                "WHERE id = %s AND outcome IS NULL",
                (outcome, self._clock(), decision),
            )

    async def scored(self) -> str:
        """The journal in words ("Su questioni di mercato tu 0.18, io 0.22"), from the latest
        night's scores."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT party, domain, brier FROM pisa_desk_scores WHERE night = "
                    "(SELECT max(night) FROM pisa_desk_scores)"
                )
            ).fetchall()
        return rules.journal_words({(p, d): float(b) for p, d, b in rows})
