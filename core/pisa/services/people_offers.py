# ruff: noqa: E501
"""What Pisa offers about the people they love, each at its moment, with the button ready.

- **Moments**: a person's own words (their email to them, their own calendar shared with them) that they
  are in their city, or free, on a day they are free too → "Laura è a Roma giovedì, ti va un caffè?", at most once
  a week per person. Never from a location (theirs or anyone's).
- **Thank-yous**: the morning after a meal or a party with someone, or when someone's gift reached them
  (their email, or a store's notice naming them) → a short thank-you in their own style.
- **Wishes**: at THEIR morning (their 9:00, when it is daytime for them and out of their quiet hours) →
  "A New York sono le 9: vuoi fare gli auguri a Marco?".

Each offer is one notice (kind info, `ritual: people`) naming a sheet (`GET /v1/sheets/{id}`) whose
button opens WhatsApp or Messages with Pisa's draft: **Pisa never sends it**. Never in their quiet hours, never
at night, never while Pisa is stopped (the minute step is `people`); never about someone they hid.
Logs carry ids and counts, never names, cities, days or words.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.db.settings import get_setting, set_setting
from pisa.domain import handoffs as links
from pisa.domain import people as rules
from pisa.domain import people_offers as offers
from pisa.domain.actions import addresses
from pisa.domain.errors import PisaError
from pisa.domain.mail import NEVER_TO_MODEL_LABELS
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.domain.sheet import SheetData
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services.handoffs import insert_prep
from pisa.services.notifications import enqueue

_log = get_logger("pisa.people.offers")
MOMENTS_KEY = "people_moments_at"  # app_settings: emails fetched up to here were read for moments
GIFTS_KEY = "people_gifts_at"  # … and for gifts received
RECENT = timedelta(days=7)  # an email older than this offers nothing (the first sync never floods them)
PACE = timedelta(minutes=30)  # one people offer at a time
STYLE_EMAILS = 20  # their last sent emails to someone, for their style
OFFERS_KEPT = timedelta(days=90)  # an offer's row (what Pisa said, its sheet) is kept this long
MOMENT_UNTIL = time(12)  # a moment is offered by that morning at the latest
TRIP_STAY_MAX = timedelta(days=30)  # a trip with no way back yet: they are there this long at most
Kind = Literal["moment", "thanks", "wish"]


class PeopleOffers:
    def __init__(
        self,
        pool: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        owner: Owner,
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._owner = owner

    def _today(self, now: datetime) -> date:
        return now.astimezone(self._owner.zone).date()

    # --- the sync's steps (hourly): what people said, and the offers to make -------------------------------

    async def moments_from_email(self, conn: AsyncConnection, now: datetime) -> int:
        """Their own words in their emails to them ("sarò a Roma giovedì"), each email once; never about
        someone they hid; kept while the email is (it goes with it)."""
        lang = await self._owner.lang(conn)
        since = await get_setting(conn, MOMENTS_KEY)
        start = datetime.fromisoformat(str(since)) if since else datetime.min.replace(tzinfo=UTC)
        rows = await (
            await conn.execute(
                "SELECT m.id, m.from_address, m.body, m.sent_at, m.fetched_at FROM email_messages m "
                "WHERE m.fetched_at > %s AND m.body IS NOT NULL AND NOT ('SENT' = ANY(m.labels)) "
                "AND NOT (m.labels && %s) ORDER BY m.fetched_at",
                (start, list(NEVER_TO_MODEL_LABELS)),
            )
        ).fetchall()
        known = await _known(conn)
        zones = {
            pid: tz for pid, tz in await (await conn.execute("SELECT id::text, tz FROM people")).fetchall()
        }
        kept, last, held = 0, start, False
        for mid, from_address, body, sent_at, fetched_at in rows:
            pid = known.get(str(from_address or "").lower())
            recent = now - sent_at <= RECENT
            # a recent email from someone not among their people yet (they write to them
            # tomorrow) is read again next time: the moment never hangs on which sync came first. The rows
            # are kept once each (`UNIQUE (person_id, source_ref)`), so reading again adds nothing twice.
            # only such an email that holds a moment holds the watermark (a newsletter never).
            held = held or (
                pid is None
                and recent
                and bool(offers.moments(str(body), sent_at.astimezone(self._owner.zone).date(), lang))
            )
            if not held:
                last = max(last, fetched_at)
            if pid is None or not recent:
                continue
            # their "domani" is the day after THEIR day of writing, when Pisa knows their zone
            written = sent_at.astimezone(rules.zone(zones.get(pid)) or self._owner.zone).date()
            for n, said in enumerate(offers.moments(str(body), written, lang)):
                if said.last < self._today(now) - timedelta(days=1):
                    continue  # over already (the sweep below would only take it out again)
                cur = await conn.execute(
                    "INSERT INTO person_moments (person_id, kind, city, word, first_day, last_day, source, "
                    "source_ref, at) VALUES (%s, %s, %s, %s, %s, %s, 'email', %s, %s) ON CONFLICT DO NOTHING",
                    (pid, said.kind, said.city, said.word, said.first, said.last, f"{mid}#{n}", sent_at),
                )
                kept += cur.rowcount
        if last > start:
            await set_setting(conn, MOMENTS_KEY, Jsonb(last.isoformat()))
        await conn.execute(  # an email's moment goes with its email
            "DELETE FROM person_moments n WHERE n.source = 'email' AND NOT EXISTS "
            "(SELECT 1 FROM email_messages m WHERE m.id = split_part(n.source_ref, '#', 1))"
        )
        await conn.execute(
            "DELETE FROM person_moments WHERE last_day < %s", (self._today(now) - timedelta(days=1),)
        )
        return kept

    async def moments_from_calendar(self, conn: AsyncConnection, now: datetime) -> int:
        """Their own calendar, shared with them (a calendar whose id is their address): an event naming one
        city Pisa knows, in the next two weeks. Kept while the event is."""
        known = await _known(conn)
        if not known:
            return 0
        rows = await (
            await conn.execute(
                "SELECT calendar_id, id, title, location, starts_at, ends_at, all_day, start_date, end_date "
                "FROM calendar_events WHERE lower(calendar_id) = ANY(%s) AND status <> 'cancelled' "
                "AND ends_at >= %s AND starts_at < %s",
                (list(known), now, now + timedelta(days=offers.MOMENT_AHEAD_DAYS + 1)),
            )
        ).fetchall()
        tz = self._owner.zone
        seen: list[str] = []
        kept = 0
        for cal, eid, title, location, starts, ends, all_day, start_day, end_day in rows:
            if all_day and start_day is not None:
                first, last = start_day, (end_day - timedelta(days=1)) if end_day else start_day
            else:
                first, last = (
                    starts.astimezone(tz).date(),
                    (ends - timedelta(seconds=1)).astimezone(tz).date(),
                )
            said = offers.calendar_moment(str(title), location, first, max(first, last))
            if said is None:
                continue
            ref = f"cal:{cal}/{eid}"
            seen.append(ref)
            cur = await conn.execute(
                "INSERT INTO person_moments (person_id, kind, city, first_day, last_day, source, source_ref, at) "
                "VALUES (%s, 'in_city', %s, %s, %s, 'calendar', %s, %s) ON CONFLICT (person_id, source_ref) DO "
                "UPDATE SET city = EXCLUDED.city, first_day = EXCLUDED.first_day, last_day = EXCLUDED.last_day",
                (known[str(cal).lower()], said.city, said.first, said.last, ref, now),
            )
            kept += cur.rowcount
        await conn.execute(  # an event gone (or no longer naming a city) takes its moment with it
            "DELETE FROM person_moments WHERE source = 'calendar' AND NOT (source_ref = ANY(%s))", (seen,)
        )
        return kept

    async def gifts(self, conn: AsyncConnection, now: datetime) -> int:
        """A gift that reached them: the person's own email saying they sent it, or a store's notice (its own
        domain) naming a known person by their whole name. Each email once; one thank-you per email."""
        lang = await self._owner.lang(conn)
        since = await get_setting(conn, GIFTS_KEY)
        start = datetime.fromisoformat(str(since)) if since else datetime.min.replace(tzinfo=UTC)
        rows = await (
            await conn.execute(
                "SELECT m.id, m.from_address, m.subject, m.body, m.sent_at, m.fetched_at FROM email_messages m "
                "WHERE m.fetched_at > %s AND m.body IS NOT NULL AND NOT ('SENT' = ANY(m.labels)) "
                "AND NOT (m.labels && %s) ORDER BY m.fetched_at",
                (start, list(NEVER_TO_MODEL_LABELS)),
            )
        ).fetchall()
        known = await _known(conn)
        names = await _names(conn)
        made, last = 0, start
        for mid, from_address, subject, body, sent_at, fetched_at in rows:
            last = max(last, fetched_at)
            if now - sent_at > RECENT:
                continue
            address = str(from_address or "").lower()
            pid = known.get(address) if offers.gift_from_sender(str(body)) else None
            if pid is None and offers.store_domain(address):
                giver = offers.gift_giver(str(subject or ""), str(body), list(names))
                pid = names.get(giver) if giver else None
            if pid is None:
                continue
            name = await _name(conn, pid)
            mine = await self._style(conn, pid, lang)
            line = offers.thanks_gift_line(name, lang)
            draft = offers.thanks_draft(mine, name, "gift", lang)
            due = offers.offer_at(now, self._owner.tz)
            made += await self._offer(
                conn, "thanks", pid, f"thanks:email:{mid}", name, line, draft, due, due + offers.THANKS_KEPT,
                now, lang, source=("email", sent_at),
            )  # fmt: skip
        if last > start:
            await set_setting(conn, GIFTS_KEY, Jsonb(last.isoformat()))
        return made

    async def plan(self, conn: AsyncConnection, now: datetime) -> int:
        """The offers to make now: a moment, yesterday's thank-you, today's wishes (each once)."""
        lang = await self._owner.lang(conn)
        await conn.execute("DELETE FROM people_offers WHERE created_at < %s", (now - OFFERS_KEPT,))
        made = 0
        for step in (self._plan_moments, self._plan_thanks, self._plan_wishes):
            made += await step(conn, now, lang)
        return made

    async def _plan_moments(self, conn: AsyncConnection, now: datetime, lang: str) -> int:
        today = self._today(now)
        tomorrow, horizon = today + timedelta(days=1), today + timedelta(days=offers.MOMENT_AHEAD_DAYS)
        rows = await (
            await conn.execute(
                "SELECT m.id, m.kind, m.city, m.word, m.first_day, m.last_day, m.source, m.at, p.id::text, "
                "p.name, p.city FROM person_moments m JOIN people p ON p.id = m.person_id "
                "WHERE NOT p.hidden AND p.orbit AND m.last_day >= %s AND m.first_day <= %s "
                "AND NOT EXISTS (SELECT 1 FROM people_offers o WHERE o.person_id = p.id AND o.kind = 'moment' "
                "AND o.created_at > %s) AND NOT EXISTS (SELECT 1 FROM people_offers o WHERE o.kind = 'moment' "
                "AND o.ref LIKE 'moment:' || m.id || ':%%') ORDER BY m.first_day, m.id",
                (tomorrow, horizon, now - offers.MOMENT_EVERY),
            )
        ).fetchall()
        made, done = 0, set()
        for mid, kind, city, word, first, last, source, at, pid, name, their_city in rows:
            if pid in done:
                continue
            said = offers.Moment(kind, city, first, last, word)
            for n in range((min(last, horizon) - max(first, tomorrow)).days + 1):
                day = max(first, tomorrow) + timedelta(days=n)
                his = await self._his_city(conn, day)
                where = said.city if kind == "in_city" else offers.city_key(their_city)
                if his is None or where != his:
                    continue  # never inferred: their words name their city, or they live in it and are free
                if not offers.free_window(await self._busy(conn, day), day, self._owner.tz):
                    continue
                mine = await self._style(conn, pid, lang)
                line = offers.moment_line(name, said, day, today, lang)
                draft = offers.moment_draft(mine, name, said, day, today, lang)
                due = offers.offer_at(now, self._owner.tz)
                until = datetime.combine(day, MOMENT_UNTIL, tzinfo=self._owner.zone)  # that morning at most
                made += await self._offer(
                    conn, "moment", pid, f"moment:{mid}:{day.isoformat()}", name, line, draft, due, until, now, lang,
                    source=(source, at),
                )  # fmt: skip
                done.add(pid)
                break
        return made

    async def _plan_thanks(self, conn: AsyncConnection, now: datetime, lang: str) -> int:
        """The morning after a meal or a party in their calendar (they took part; at most 8 guests) with someone they
        know: one thank-you a day at most, once per event (the line, now with its button)."""
        today = self._today(now)
        start, end = local_day_bounds(today - timedelta(days=1), self._owner.tz)
        if await (
            await conn.execute(
                "SELECT 1 FROM people_offers WHERE kind = 'thanks' AND ref LIKE 'thanks:event:%%' "
                "AND created_at >= %s",
                (end,),
            )
        ).fetchone():
            return 0
        events = await (
            await conn.execute(
                "SELECT id, title, guests FROM calendar_events WHERE NOT all_day AND guests IS NOT NULL "
                "AND accepted AND status <> 'cancelled' AND ends_at >= %s AND ends_at < %s ORDER BY starts_at",
                (start, end),
            )
        ).fetchall()
        known = await _known(conn)
        for event_id, title, guests in events:
            if not rules.thanks_event(str(title)) or len(guests or []) > rules.GUESTS_MAX:
                continue
            who = [known[a] for g in guests or [] if (a := str(g.get("email") or "").lower()) in known]
            if not who:
                continue
            pid = who[0]
            name = await _name(conn, pid)
            line, _ = rules.thanks_line(name, str(title).strip()[:60], lang)
            draft = offers.thanks_draft(await self._style(conn, pid, lang), name, "event", lang)
            due = max(now, datetime.combine(today, offers.THANKS_AT, tzinfo=self._owner.zone))
            if await self._offer(
                conn, "thanks", pid, f"thanks:event:{event_id}", name, line, draft, due,
                due + offers.THANKS_KEPT, now, lang, source=("yours", None),
            ):  # fmt: skip
                return 1
        return 0

    async def _plan_wishes(self, conn: AsyncConnection, now: datetime, lang: str) -> int:
        """A date on THEIR day: the wish offered at their morning, once per date and year."""
        settings = await self._owner.settings(conn)
        rows = await (
            await conn.execute(
                "SELECT d.id, d.kind, d.month, d.day, p.id::text, p.name, p.tz, p.city FROM person_dates d "
                "JOIN people p ON p.id = d.person_id WHERE NOT p.hidden AND d.kind <> 'other'"
            )
        ).fetchall()
        made = 0
        for date_id, kind, month, day, pid, name, tz, city in rows:
            theirs = tz if tz is not None and rules.zone(tz) is not None else self._owner.home  # at home
            found = offers.wish_moment(
                theirs, (month, day), self._owner.tz, now, lambda at: is_quiet(at, settings, self._owner.tz)
            )
            if found is None:
                continue
            due, until = found
            year = due.astimezone(ZoneInfo(theirs)).year
            same = (
                due.astimezone(ZoneInfo(theirs)).utcoffset() == due.astimezone(self._owner.zone).utcoffset()
            )
            line = offers.wish_line(name, kind, due.astimezone(ZoneInfo(theirs)), city, same, lang)
            draft = offers.wish_draft(await self._style(conn, pid, lang), kind, name, lang)
            made += await self._offer(
                conn, "wish", pid, f"wish:{date_id}:{year}", name, line, draft, due, until, now, lang
            )
        return made

    # --- the minute: one offer that is due goes out ----------------------------------------------------------

    async def deliver(self) -> int:
        """The offer whose moment came, one at a time (PACE), as a notice naming its sheet. Never in quiet
        hours, never at night, never for someone hidden since; past its `until` it lapses unsaid. A wish's
        line says their hour now."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            if is_quiet(now, settings, self._owner.tz) or not offers.daytime(now, self._owner.tz):
                return 0
            recent = await (
                await conn.execute("SELECT 1 FROM people_offers WHERE sent_at > %s", (now - PACE,))
            ).fetchone()
            if recent:
                return 0
            row = await (
                await conn.execute(
                    "SELECT o.id, o.kind, o.ref, o.line, o.data, p.name, p.tz, p.city, p.id::text "
                    "FROM people_offers o JOIN people p ON p.id = o.person_id "
                    "WHERE o.sent_at IS NULL AND o.due_at <= %s AND o.until > %s AND NOT p.hidden "
                    "AND (o.kind <> 'moment' OR p.orbit) "
                    "ORDER BY o.due_at LIMIT 1 FOR UPDATE OF o SKIP LOCKED",
                    (now, now),
                )
            ).fetchone()
            if row is None:
                return 0
            offer_id, kind, ref, line, data, name, tz, city, pid = row
            lang = str(settings["language"])
            if kind == "wish":
                fresh_line = await self._wish_line_now(conn, ref, name, tz, city, now, lang)
                if fresh_line is None:  # its date is gone (forgotten, Google disconnected)
                    await conn.execute("UPDATE people_offers SET until = %s WHERE id = %s", (now, offer_id))
                    return 0
                line = fresh_line
                data = {**data, "blocks": [{"type": "text", "text": line}, *data["blocks"][1:]]}
            elif (
                kind == "thanks"
                and ref.startswith("thanks:email:")
                and not await (
                    await conn.execute(
                        "SELECT 1 FROM email_messages WHERE id = %s", (ref.removeprefix("thanks:email:"),)
                    )
                ).fetchone()
            ):  # a gift's thank-you goes with its email
                await conn.execute("UPDATE people_offers SET until = %s WHERE id = %s", (now, offer_id))
                return 0
            elif (
                kind == "moment"
            ):  # "domani" said yesterday is "oggi" now: the line and the draft as they are now
                fresh = await self._moment_now(conn, ref, pid, name, data, now, lang)
                if fresh is None:  # what they said is gone (its email, its event): it lapses unsaid
                    await conn.execute("UPDATE people_offers SET until = %s WHERE id = %s", (now, offer_id))
                    return 0
                line, draft, data = fresh
                await conn.execute("UPDATE people_offers SET draft = %s WHERE id = %s", (draft, offer_id))
            buttons = [
                {k: b[k] for k in ("handoff_id", "kind", "title", "url") if k in b} | {"id": b["handoff_id"]}
                for b in data["blocks"]
                if b.get("type") == "handoff"
            ]
            await insert_prep(
                conn, device_id=None, purpose="message", target=rules.first_name(name), lang=lang,
                details={"people": kind}, buttons=buttons, object_id=f"sheet:{offer_id}", now=now,
            )  # fmt: skip
            notification_id, _ = await enqueue(
                conn,
                kind="info",
                text=line,
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                respect_quiet=False,
                path="people.offer",
                person_id=uuid.UUID(str(pid)) if pid else None,
            )  # quiet was checked first: an offer is never held for the morning
            await conn.execute(
                "UPDATE notifications SET ritual = 'people', sheet_id = %s WHERE id = %s",
                (offer_id, notification_id),
            )
            await conn.execute(
                "UPDATE people_offers SET sent_at = %s, notification_id = %s, line = %s, data = %s WHERE id = %s",
                (now, notification_id, line, Jsonb(data), offer_id),
            )
        _log.info("people_offer_sent", offer_id=str(offer_id), kind=kind, person_id=pid)
        return 1

    async def _moment_now(
        self,
        conn: AsyncConnection,
        ref: str,
        pid: str,
        name: str,
        data: dict[str, Any],
        now: datetime,
        lang: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        _, mid, day = ref.split(":")
        row = await (
            await conn.execute(
                "SELECT kind, city, word, first_day, last_day FROM person_moments WHERE id::text = %s", (mid,)
            )
        ).fetchone()
        if row is None:
            return None
        said, on, today = offers.Moment(*row), date.fromisoformat(day), self._today(now)
        line = offers.moment_line(name, said, on, today, lang)
        draft = offers.moment_draft(await self._style(conn, pid, lang), name, said, on, today, lang)
        fact = next((b["value"] for b in data["blocks"] if b.get("type") == "fact"), None)
        fresh = _sheet("moment", name, line, draft, fact, lang)
        return None if fresh is None else (line, draft, fresh)

    async def _wish_line_now(
        self,
        conn: AsyncConnection,
        ref: str,
        name: str,
        tz: str | None,
        city: str | None,
        now: datetime,
        lang: str,
    ) -> str | None:
        parts = ref.split(":")
        row = await (
            await conn.execute("SELECT kind FROM person_dates WHERE id::text = %s", (parts[1],))
        ).fetchone()
        if row is None:
            return None
        theirs = rules.zone(tz) or ZoneInfo(self._owner.home)
        their_now = now.astimezone(theirs)
        same = their_now.utcoffset() == now.astimezone(self._owner.zone).utcoffset()
        return offers.wish_line(name, row[0], their_now, city, same, lang)

    # --- the sheet (GET /v1/sheets/{id}) ----------------------------------------------------------------------

    async def sheet(self, sheet_id: str) -> dict[str, Any] | None:
        try:
            key = uuid.UUID(sheet_id.removeprefix("sheet:"))
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT o.data, o.created_at, o.sent_at FROM people_offers o JOIN people p ON "
                    "p.id = o.person_id WHERE o.id = %s AND o.sent_at IS NOT NULL AND NOT p.hidden",
                    (key,),
                )
            ).fetchone()
        if row is None:
            return None
        data, created, sent = row
        revision = int((sent or created).timestamp() * 1000)
        return {"object_id": f"sheet:{key}", "kind": "sheet", "revision": revision, "data": data}

    async def drafts(self, conn: AsyncConnection, today: date) -> list[tuple[str, str, str]]:
        rows = await (
            await conn.execute(
                "SELECT p.name, o.kind, o.draft FROM people_offers o JOIN people p ON p.id = o.person_id "
                "WHERE o.draft IS NOT NULL AND NOT p.hidden AND o.sent_at >= %s ORDER BY o.sent_at",
                (local_day_bounds(today, self._owner.tz)[0],),
            )
        ).fetchall()
        return [(str(a), str(b), str(c)) for a, b, c in rows]

    # --- helpers -----------------------------------------------------------------------------------------------

    async def _offer(
        self, conn: AsyncConnection, kind: Kind, pid: str, ref: str, name: str, line: str, draft: str,
        due: datetime, until: datetime, now: datetime, lang: str, *, source: tuple[str, datetime | None] | None = None,
    ) -> int:  # fmt: skip
        """Keep an offer (once per kind and thing) with its sheet: Pisa's line, where it comes from, and the
        buttons that open WhatsApp or Messages with the draft. Nothing is sent."""
        if until <= now or due >= until:
            return 0  # its moment can't come in their daytime: never planned to lapse
        fact = _source(source, lang) if source is not None else None
        data = _sheet(kind, name, line, draft, fact, lang)
        if data is None:
            return 0
        cur = await conn.execute(
            "INSERT INTO people_offers (id, kind, person_id, ref, line, draft, data, due_at, until, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (kind, ref) DO NOTHING",
            (uuid.uuid4(), kind, pid, ref, line[:300], draft[:1000], Jsonb(data), due, until, now),
        )
        if cur.rowcount:
            _log.info("people_offer_planned", kind=kind, person_id=pid)
        return cur.rowcount

    async def _style(self, conn: AsyncConnection, pid: str, lang: str) -> offers.Style:
        """Their style with them, from their own sent emails to their addresses (their words: never theirs)."""
        row = await (await conn.execute("SELECT emails FROM people WHERE id = %s", (pid,))).fetchone()
        theirs = set(row[0] or []) if row else set()
        if not theirs:
            return offers.style([], lang)
        rows = await (
            await conn.execute(
                "SELECT to_addresses, body FROM email_messages e WHERE 'SENT' = ANY(labels) AND body IS NOT NULL "
                # never a mail Pisa sent: Pisa's drafts would teach Pisa's next drafts
                "AND NOT EXISTS (SELECT 1 FROM pisa_sent_mail j WHERE j.gmail_message_id = e.id) "
                "ORDER BY sent_at DESC LIMIT 200"
            )
        ).fetchall()
        bodies = [str(b) for to, b in rows if any(a in theirs for _, a in addresses(*(to or [])))]
        return offers.style(bodies[:STYLE_EMAILS], lang)

    async def _his_city(self, conn: AsyncConnection, day: date) -> str | None:
        """Where they are that day: a trip they confirmed that has them there, else their home's city,
        when either names a city Pisa knows. Never a guess from the phone's location."""
        start, end = local_day_bounds(day, self._owner.tz)
        row = await (
            await conn.execute(
                "SELECT to_city, arrive_at, back_at FROM trips WHERE status = 'planned' AND doubt IS NULL "
                "AND arrive_at < %s AND (back_at IS NULL OR back_at > %s) ORDER BY arrive_at DESC LIMIT 1",
                (end, start),
            )
        ).fetchone()
        if row is not None and (row[2] is not None or row[1] + TRIP_STAY_MAX > start):
            return offers.city_key(row[0]) or offers.city_in(row[0])
        home = await home_store.read(conn)
        return offers.city_in(home.name) if home is not None else None

    async def _busy(self, conn: AsyncConnection, day: date) -> list[tuple[datetime, datetime]]:
        start, end = local_day_bounds(day, self._owner.tz)
        rows = await (
            await conn.execute(
                "SELECT starts_at, ends_at FROM calendar_events WHERE NOT all_day AND status <> 'cancelled' "
                "AND coalesce(accepted, true) AND starts_at < %s AND ends_at > %s",
                (end, start),
            )
        ).fetchall()
        return [(s, e) for s, e in rows]


def _sheet(
    kind: Kind, name: str, line: str, draft: str, fact: str | None, lang: str
) -> dict[str, Any] | None:
    """The offer's sheet: Pisa's line, where it comes from, and the buttons that open WhatsApp or Messages with
    the draft (they pick the contact there). None when a URL isn't one Pisa may open."""
    first = rules.first_name(name)
    en = lang == "en-US"
    title = {
        "moment": ("A coffee with {first}", "Un caffè con {first}"),
        "thanks": ("A thank-you to {first}", "Un grazie a {first}"),
        "wish": ("Wishes for {first}", "Auguri a {first}"),
    }[kind][0 if en else 1].format(first=first)
    blocks: list[dict[str, Any]] = [{"type": "text", "text": line}]
    if fact is not None:
        blocks.append({"type": "fact", "label": "Source" if en else "Fonte", "value": fact})
    pick = f"Pick {first} in the app" if en else f"Scegli {first} nell'app"
    for app, url in (("WhatsApp", links.whatsapp(None, draft)), ("iMessage", links.sms(None, draft))):
        if not links.safe_url(url):  # RAIL: the last check, whatever built it
            return None
        blocks.append({
            "type": "handoff", "handoff_id": str(uuid.uuid4()), "kind": "message",
            "title": f"{app} to {first}" if en else f"{app} a {first}", "subtitle": pick, "url": url,
            "text": draft,
        })  # fmt: skip
    data = {"title": title, "blocks": blocks}
    SheetData.model_validate(data)  # the contract's limits, before anything is kept
    return data


async def _known(conn: AsyncConnection) -> dict[str, str]:
    """Address → person, never someone they hid."""
    rows = await (await conn.execute("SELECT id::text, emails FROM people WHERE NOT hidden")).fetchall()
    return {address: pid for pid, emails in rows for address in emails or []}


async def _names(conn: AsyncConnection) -> dict[str, str]:
    rows = await (await conn.execute("SELECT name, id::text FROM people WHERE NOT hidden")).fetchall()
    return {str(n): str(i) for n, i in rows}


async def _name(conn: AsyncConnection, pid: str) -> str:
    row = await (await conn.execute("SELECT name FROM people WHERE id = %s", (pid,))).fetchone()
    if row is None:
        raise PisaError("not_found", "There is no such person.", "")
    return str(row[0])


def _source(source: tuple[str, datetime | None], lang: str) -> str:
    kind, at = source
    en = lang == "en-US"
    if kind == "calendar":
        return "their calendar" if en else "il suo calendario"
    if kind == "yours":
        return "your calendar" if en else "il tuo calendario"
    day = at.strftime("%d/%m") if at is not None else ""
    return f"their email of {day}" if en else f"la sua email del {day}"
