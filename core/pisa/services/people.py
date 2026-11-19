"""The people the user loves, and the care circle (stored only).

- **From what exists, never invented**: the addresses they wrote to and the people among them who wrote back
  (a display name that reads like a person: `person_sender`), and the guests of the events they took part in,
  merged by email; people they name themselves join from their words. Their corrections win: a field they set
  (`owner_fields`) or `hidden` is never overwritten by a sync.
- **Dates** from their words (the day and month read from them, never the model's) and from their calendar's
  yearly all-day events named for a birthday or an anniversary; reminders as pool tasks; on the day, a
  morning line with their local hour when it differs.
- **Gift ideas** from their words, or a person's own wish in an email to them: data, kept quietly, never acted
  on.
- **Orbits**: on Sunday, at most one person a week; **thank-you notes** the morning after a meal or a party.

Nothing here contacts anyone: lines go into their morning briefing, drafts to their phone's compose
screen.
Logs carry ids and counts, never names, addresses or their words.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.domain import people as rules
from pisa.domain import people_offers as offer_domain
from pisa.domain.actions import addresses
from pisa.domain.errors import PisaError
from pisa.domain.lexicon.months import MONTHS_SHORT
from pisa.domain.lexicon.weekdays import WEEKDAYS_SHORT
from pisa.domain.mail import NEVER_TO_MODEL_LABELS, person_sender
from pisa.domain.owner import Owner
from pisa.domain.profile import plain
from pisa.domain.schedule import on_sunday
from pisa.domain.tasks import end_of_day
from pisa.domain.text import people_name_key
from pisa.logging import get_logger
from pisa.services import people_offers as offer_rules
from pisa.services.intake import FREEMAIL_DOMAINS
from pisa.services.people_offers import PeopleOffers
from pisa.services.tasks import insert_task

_log = get_logger("pisa.people")
SYNC_EVERY = timedelta(hours=1)
# their mail is kept; the hourly sync reads the window it had while mail went after 90 days (its people,
# their names, last contact and rhythm), never the whole store every hour
MAIL_WINDOW = timedelta(days=90)
SYNCED_KEY = "people_synced_at"  # app_settings: the last sync
WISHES_KEY = "people_wishes_at"  # app_settings: emails fetched up to here were read for wishes
ORBITS_KEY = "people_orbits"  # app_settings: false once they said "non ricordarmi più di nessuno"
TASK_LABEL = "people"
PATCHABLE = ("name", "relation", "tz", "city", "birthday", "orbit", "hidden")
_PERSON = (
    "SELECT id::text, name, relation, tz, city, emails, source, owner_fields, hidden, orbit, "
    "last_contact_at, "
    "rhythm_days, created_at FROM people"
)


def _not_found(what: str = "person") -> PisaError:
    return PisaError("not_found", f"There is no such {what}.", "Refresh the people list")


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Correct the value")


def _uuid(text: str) -> uuid.UUID:
    try:
        return uuid.UUID(text)
    except ValueError:
        raise _not_found() from None


@dataclass(frozen=True)
class Found:
    """A person the user's words name, for the brain's tools."""

    id: str
    name: str
    relation: str | None
    tz: str | None
    city: str | None
    emails: tuple[str, ...]
    hidden: bool = False


class PeopleService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        # moments, thank-yous and wishes, each offered at its moment with its button
        self.offers = PeopleOffers(pool, clock=clock, owner=owner)

    # --- the sync (the worker's minute job, at most hourly) ------------------------------------------------

    async def tick(self) -> None:
        """Every minute: an offer whose moment came goes out; the sync itself at most hourly."""
        try:
            await self.offers.deliver()
        except Exception as exc:  # never the traceback (a row's DETAIL holds names)
            _log.warning("people_offer_failed", cause=exc.__class__.__name__)
        now = self._clock()
        async with self._pool.connection() as conn:
            last = await get_setting(conn, SYNCED_KEY)
        if last is not None and now - datetime.fromisoformat(str(last)) < SYNC_EVERY:
            return
        await self.sync()

    async def sync(self) -> dict[str, int]:
        """People from their mail and calendar, their dates from their calendar, last contact and rhythm,
        wishes in their emails, and the reminders due. Each step on its own: one failing never stops the
        others."""
        now = self._clock()
        counts: dict[str, int] = {}
        async with self._pool.connection() as conn:
            for name, step in (
                ("from_email", self._from_email),
                ("from_calendar", self._from_calendar),
                ("calendar_dates", self._calendar_dates),
                ("contacts", self._contacts),
                ("wishes", self._wishes),
                ("reminders", self._reminders),
                ("moments_email", self.offers.moments_from_email),  # their own words
                ("moments_calendar", self.offers.moments_from_calendar),
                ("gifts", self.offers.gifts),  # a gift that reached them
                ("offers", self.offers.plan),  # what to offer, and when
            ):
                try:
                    async with conn.transaction():
                        counts[name] = await step(conn, now)
                except Exception as exc:  # never the traceback (a row's DETAIL holds names)
                    _log.warning("people_step_failed", step=name, cause=exc.__class__.__name__)
            await set_setting(conn, SYNCED_KEY, Jsonb(now.isoformat()))
        _log.info("people_synced", **counts)
        return counts

    async def _owner_addresses(self, conn: AsyncConnection) -> set[str]:
        rows = await (await conn.execute("SELECT account FROM connector_accounts")).fetchall()
        return {str(a).lower() for (a,) in rows if a}

    async def _by_email(self, conn: AsyncConnection, *, hidden: bool = True) -> dict[str, str]:
        """Address → person. `hidden` False leaves out the people they hid (nothing more is learnt about
        them); True keeps them, so the sync never adds them again."""
        rows = await (
            await conn.execute("SELECT id::text, emails FROM people WHERE %s OR NOT hidden", (hidden,))
        ).fetchall()
        return {address: pid for pid, emails in rows for address in emails or []}

    async def _add(self, conn: AsyncConnection, name: str, address: str, source: str, now: datetime) -> str:
        pid = uuid.uuid4()
        clean = " ".join(name.replace('"', " ").split())[: rules.NAME_MAX]
        await conn.execute(
            "INSERT INTO people (id, name, name_key, emails, source, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (pid, clean, people_name_key(clean), [address], source, now, now),
        )
        return str(pid)

    async def _from_email(self, conn: AsyncConnection, now: datetime) -> int:
        """The addresses they wrote to, named by the name they used first (a person's, never a system's;
        a shared family address keeps its first name, never the latest sender's)."""
        mine = await self._owner_addresses(conn)
        kept = await (await conn.execute("SELECT address FROM email_correspondents")).fetchall()
        written = {str(a).lower() for (a,) in kept}
        rows = await (
            await conn.execute(
                "SELECT to_addresses, from_name, from_address, labels FROM email_messages "
                "WHERE sent_at >= %s ORDER BY sent_at",
                (now - MAIL_WINDOW,),
            )
        ).fetchall()
        named: dict[str, str] = {}
        for to, from_name, from_address, labels in rows:
            if "SENT" in (labels or []):
                for name, address in addresses(*(to or [])):
                    written.add(address)
                    if name:
                        named.setdefault(address, name)
            elif from_address and from_name:
                named.setdefault(str(from_address).lower(), str(from_name))
        known = await self._by_email(conn)
        added = 0
        for address in sorted(written - mine):
            name = named.get(address)
            if address in known or name is None or not person_sender(name, address, FREEMAIL_DOMAINS):
                continue
            known[address] = await self._add(conn, name, address, "email", now)
            added += 1
        return added

    async def _from_calendar(self, conn: AsyncConnection, now: datetime) -> int:
        """The named guests of the events they took part in (accepted or their own), never a big event's."""
        mine = await self._owner_addresses(conn)
        rows = await (
            await conn.execute(
                "SELECT guests FROM calendar_events WHERE guests IS NOT NULL AND accepted "
                "AND status <> 'cancelled'"
            )
        ).fetchall()
        known = await self._by_email(conn)
        added = 0
        for (guests,) in rows:
            if len(guests or []) > rules.GUESTS_MAX:
                continue
            for guest in guests or []:
                address = str(guest.get("email") or "").lower()
                name = str(guest.get("name") or "")
                if not address or address in mine or address in known:
                    continue
                if not person_sender(name, address, FREEMAIL_DOMAINS):
                    continue
                known[address] = await self._add(conn, name, address, "calendar", now)
                added += 1
        return added

    async def _calendar_dates(self, conn: AsyncConnection, now: datetime) -> int:
        """A yearly all-day event of HIS OWN (no guests: never someone's invite; or Google's `birthday`
        events from their contacts) named for a birthday or an anniversary: the date of the person it names.
        Yearly: Google's birthday type, or a series with one instance in the 31-day window (a weekly or
        monthly one has more). It joins a person only on the whole name (never "Laura" for "Laura Bianchi":
        they are asked in the app); a hidden one is never brought back."""
        rows = await (
            await conn.execute(
                "SELECT coalesce(recurring_event_id, id), min(title), min(start_date), count(*), "
                "bool_or(event_type = 'birthday') FROM calendar_events "
                "WHERE all_day AND start_date IS NOT NULL "
                "AND status <> 'cancelled' AND (event_type = 'birthday' OR (recurring_event_id IS NOT NULL "
                "AND guests IS NULL AND accepted IS NOT false)) GROUP BY 1"
            )
        ).fetchall()
        added = 0
        for ref, title, day, instances, birthday_type in rows:
            if not birthday_type and instances > 1:
                continue  # weekly or monthly: never a yearly date
            found = rules.calendar_date(str(title))
            if found is None:
                continue
            kind, name = found
            matches = await self._matching(conn, name, hidden=True)
            if matches and (len(matches) > 1 or plain(matches[0].name) != plain(name)):
                continue  # a partial or double match: never guessed
            if matches and matches[0].hidden:
                continue  # they hid them: never back
            pid = matches[0].id if matches else None
            if pid is None:
                pid = str(uuid.uuid4())
                await conn.execute(
                    "INSERT INTO people (id, name, name_key, source, created_at, updated_at) "
                    "VALUES (%s, %s, %s, 'calendar', %s, %s)",
                    (pid, name, people_name_key(name), now, now),
                )
            cur = await conn.execute(
                "INSERT INTO person_dates (person_id, kind, month, day, source, source_ref, created_at) "
                "VALUES (%s, %s, %s, %s, 'calendar', %s, %s) ON CONFLICT (person_id, kind) DO NOTHING",
                (pid, kind, day.month, day.day, str(ref), now),
            )
            added += cur.rowcount
        return added

    async def _contacts(self, conn: AsyncConnection, now: datetime) -> int:
        """Each person's last contact (their email to them, theirs to them, an event together) and their usual
        rhythm with them, from the last `MAIL_WINDOW` of mail; the last contact never goes back."""
        people = await (
            await conn.execute("SELECT id::text, emails FROM people WHERE emails <> '{}' AND NOT hidden")
        ).fetchall()
        if not people:
            return 0
        owner = {address: pid for pid, emails in people for address in emails}
        moments: dict[str, list[datetime]] = defaultdict(list)
        rows = await (
            await conn.execute(
                "SELECT sent_at, from_address, to_addresses, labels FROM email_messages WHERE sent_at >= %s",
                (now - MAIL_WINDOW,),
            )
        ).fetchall()
        for sent_at, from_address, to, labels in rows:
            if "SENT" in (labels or []):
                for _, address in addresses(*(to or [])):
                    if address in owner:
                        moments[owner[address]].append(sent_at)
            elif from_address and str(from_address).lower() in owner:
                moments[owner[str(from_address).lower()]].append(sent_at)
        events = await (
            await conn.execute(
                "SELECT ends_at, guests FROM calendar_events WHERE guests IS NOT NULL AND ends_at <= %s "
                "AND accepted AND status <> 'cancelled'",
                (now,),
            )
        ).fetchall()
        for ends_at, guests in events:
            for guest in guests or []:
                address = str(guest.get("email") or "").lower()
                if address in owner:
                    moments[owner[address]].append(ends_at)
        for pid, seen in moments.items():
            await conn.execute(
                "UPDATE people SET last_contact_at = greatest(last_contact_at, %s), "
                "rhythm_days = coalesce(%s, rhythm_days) WHERE id = %s",
                (max(seen), rules.rhythm_days(seen), pid),
            )
        return len(moments)

    async def _wishes(self, conn: AsyncConnection, now: datetime) -> int:
        """A person's own wish in an email to them ("mi piacerebbe tanto…"), kept as a gift idea from
        `email`: data, never acted on, never about money or Pisa. Each email read once; never about
        someone they hid. Kept while its email is (90 days at most), gone with it."""
        since = await get_setting(conn, WISHES_KEY)
        start = datetime.fromisoformat(str(since)) if since else datetime.min.replace(tzinfo=UTC)
        rows = await (
            await conn.execute(
                "SELECT m.id, m.from_address, m.body, m.sent_at, m.fetched_at FROM email_messages m "
                "WHERE m.fetched_at > %s AND m.body IS NOT NULL AND NOT ('SENT' = ANY(m.labels)) "
                "AND NOT (m.labels && %s) ORDER BY m.fetched_at",
                (start, list(NEVER_TO_MODEL_LABELS)),
            )
        ).fetchall()
        known = await self._by_email(conn, hidden=False)
        kept, last = 0, start
        for mid, from_address, body, sent_at, fetched_at in rows:
            last = max(last, fetched_at)
            pid = known.get(str(from_address or "").lower())
            if pid is None:
                continue
            for n, wish in enumerate(rules.wishes(str(body))):
                cur = await conn.execute(
                    "INSERT INTO person_notes (person_id, kind, text, source, source_ref, at) "
                    "VALUES (%s, 'gift', %s, 'email', %s, %s) ON CONFLICT DO NOTHING",
                    (pid, wish, f"{mid}#{n}", sent_at),
                )
                kept += cur.rowcount
        if last > start:
            await set_setting(conn, WISHES_KEY, Jsonb(last.isoformat()))
        await conn.execute(
            "DELETE FROM person_notes n WHERE n.source = 'email' AND NOT EXISTS "
            "(SELECT 1 FROM email_messages m WHERE m.id = split_part(n.source_ref, '#', 1))"
        )
        return kept

    async def _reminders(self, conn: AsyncConnection, now: datetime) -> int:
        """A pool task `max(remind_days_before)` days before a date, due `min(…)` days before
        (default: it appears a week before and is due the day before), once per date and year."""
        lang = await self._owner.lang(conn)
        today = now.astimezone(self._owner.zone).date()
        rows = await (
            await conn.execute(
                "SELECT d.id, d.kind, d.month, d.day, d.remind_days_before, d.source, p.name, "
                "EXISTS (SELECT 1 FROM person_notes n WHERE n.person_id = p.id AND n.kind = 'gift') "
                "FROM person_dates d JOIN people p ON p.id = d.person_id WHERE NOT p.hidden"
            )
        ).fetchall()
        made = 0
        for date_id, kind, month, day, remind, source, name, ideas in rows:
            days = rules.remind_days(remind)
            at = rules.next_day(month, day, today)
            if today < at - timedelta(days=days[0]):
                continue
            ref = f"people:date:{date_id}:{at.year}"
            if await (await conn.execute("SELECT 1 FROM tasks WHERE source_ref = %s", (ref,))).fetchone():
                continue
            due = max(today, at - timedelta(days=days[-1]))
            first = rules.first_name(name)
            title = _TASK_TITLES[kind][1 if lang == "en-US" else 0].format(name=first, on=_day(at, lang))
            notes = (
                (f'Gift ideas: ask Pisa "what should I give {first}?"' if lang == "en-US"
                 else f"Idee regalo: chiedi a Pisa «cosa regalo a {first}?»")
                if ideas else None
            )  # fmt: skip
            await insert_task(
                conn, title=title, now=now, notes=notes,
                source_kind="calendar" if source == "calendar" else "voice",
                due_at=end_of_day(due, self._owner.tz),
                due_kind="date", source_ref=ref, source_label=TASK_LABEL,
                deadline_source="owner" if source == "owner" else "pisa",
            )  # fmt: skip
            made += 1
        return made

    # --- finding someone ---------------------------------------------------------------------------------

    async def _matching(self, conn: AsyncConnection, person: str, *, hidden: bool = False) -> list[Found]:
        """The people the words name; `hidden` True includes the ones they hid (the sync must see
        them)."""
        rows = await (
            await conn.execute(
                "SELECT id::text, name, relation, tz, city, emails, hidden FROM people "
                "WHERE %s OR NOT hidden ORDER BY name",
                (hidden,),
            )
        ).fetchall()
        return [
            Found(pid, name, relation, tz, city, tuple(emails or ()), bool(gone))
            for pid, name, relation, tz, city, emails, gone in rows
            if rules.names(person, name, relation)
        ]

    async def find(self, person: str) -> list[Found]:
        async with self._pool.connection() as conn:
            return await self._matching(conn, person)

    async def _one(self, conn: AsyncConnection, person: str, lang: str, *, create: bool) -> Found | str:
        """The one person the words name; a new one from their words when none (only on their own words: the
        caller checked them); two alike → the question."""
        found = await self._matching(conn, person)
        if len(found) > 1:
            listed = ", ".join(f.name + (f" <{f.emails[0]}>" if f.emails else "") for f in found[:4])
            return f"Not done: two or more people match ({listed}). Ask the user which one."
        if found:
            return found[0]
        if not create:
            return (
                f"Not found: Pisa knows nobody called {person.strip()[:40]!r}. Say so; ask "
                f"who it is if needed."
            )
        rel = rules.relation_of(person)
        if rel is not None:
            name, relation = (rel[2] if lang == "en-US" else rel[1]), rel[0]
        else:
            words = rules.person_key(person)
            name, relation = " ".join(w.capitalize() for w in words.split()), None
        if not name:
            return "Not done: say who it is (a name or a relation)."
        now, pid = self._clock(), str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO people (id, name, name_key, relation, source, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, 'owner', %s, %s)",
            (pid, name[: rules.NAME_MAX], people_name_key(name), relation, now, now),
        )
        _log.info("person_added", person_id=pid, source="owner")
        return Found(pid, name, relation, None, None, ())

    # --- the brain's writes: the user's own words only ---------------------------------------------------

    async def remember_date(
        self, person: str, owner_said: str, words: str, lang: str, remind: list[int] | None
    ) -> str:
        """A date from the user's words: the day and month are read from `owner_said` (which must be their
        words this turn and name the person), never from the model or an email."""
        if not rules.in_said(person, owner_said) or not _quoted(owner_said, words):
            return (
                "Not kept: a date is kept only from the user's own words this turn naming the person and "
                "the day. Ask them to say it."
            )
        said = rules.said_date(owner_said, lang)
        if said is None:
            return "Not kept: their words hold no single day and month. Ask which day."
        kind = rules.date_kind(owner_said)
        async with self._pool.connection() as conn, conn.transaction():
            one = await self._one(conn, person, lang, create=True)
            if isinstance(one, str):
                return one
            await conn.execute(
                "INSERT INTO person_dates (person_id, kind, month, day, year, remind_days_before, source, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, 'owner', %s) ON CONFLICT (person_id, kind) DO "
                "UPDATE SET month = EXCLUDED.month, day = EXCLUDED.day, year = EXCLUDED.year, "
                "remind_days_before = EXCLUDED.remind_days_before, source = 'owner', source_ref = NULL",
                (
                    one.id,
                    kind,
                    said.month,
                    said.day,
                    said.year,
                    list(rules.remind_days(remind)),
                    self._clock(),
                ),
            )
        _log.info("person_date_kept", person_id=one.id, kind=kind)
        days = rules.remind_days(remind)
        return (
            f"Kept: {one.name}'s {kind.replace('_', ' ')} on {said.on} (from their words). Pisa reminds them "
            f"{days[0]} days before, as a task due {days[-1]} day(s) before, and says it on the morning."
        )

    async def remember_note(
        self, person: str, text: str, kind: str, owner_said: str, words: str, lang: str
    ) -> str:
        """A gift idea or a note about someone, from the user's own words (never sensitive: health, diet,
        belief, identity are never kept about others)."""
        text = " ".join(text.split())[: rules.NOTE_MAX]
        if kind not in ("gift", "note"):
            return "Not kept: kind is gift or note."
        if (
            not text
            or not _quoted(owner_said, words)
            or not rules.in_said(person, owner_said)
            or not rules.grounded_idea(text, owner_said)
        ):
            return (
                "Not kept: an idea or a note about someone is kept only from the user's own words this "
                "turn, "
                "naming the person. Never from an email or a page."
            )
        if rules.sensitive(text) or rules.sensitive(owner_said):
            return (
                "Not kept: Pisa keeps nothing sensitive about other people (health, diet, belief, identity). "
                "Say so."
            )
        async with self._pool.connection() as conn, conn.transaction():
            one = await self._one(conn, person, lang, create=True)
            if isinstance(one, str):
                return one
            await conn.execute(
                "INSERT INTO person_notes (person_id, kind, text, source, at) "
                "VALUES (%s, %s, %s, 'owner', %s)",
                (one.id, kind, text, self._clock()),
            )
        _log.info("person_note_kept", person_id=one.id, kind=kind)
        return f"Kept for {one.name}: {'gift idea' if kind == 'gift' else 'note'} (their words)."

    async def set_where(
        self, person: str, city: str | None, tz: str | None, owner_said: str, words: str
    ) -> str:
        """Where someone lives (their words): the city, and its zone from Pisa's city table; a zone the model
        gives only for a city not in the table and only when the zone's city is in their words too (
        "Portland" is Oregon or Maine: Pisa asks)."""
        if not _quoted(owner_said, words) or not rules.in_said(person, owner_said):
            return "Not kept: where someone lives is kept only from the user's own words this turn."
        city = " ".join((city or "").split())[: rules.CITY_MAX] or None
        if city is not None and plain(city) not in plain(owner_said):
            return "Not kept: the city must be in the user's words."
        tz_key = rules.tz_for_city(city) if city else None
        if tz_key is None and rules.zone(tz) is not None and rules.zone_said(str(tz), owner_said):
            tz_key = tz
        async with self._pool.connection() as conn, conn.transaction():
            one = await self._one(conn, person, self._owner.tag, create=False)
            if isinstance(one, str):
                return one
            await conn.execute(
                "UPDATE people SET city = coalesce(%s, city), tz = coalesce(%s, tz), updated_at = %s, "
                "owner_fields = (SELECT array_agg(DISTINCT f) FROM unnest(owner_fields || %s::text[]) f) "
                "WHERE id = %s",
                (city, tz_key, self._clock(), [f for f, v in (("city", city), ("tz", tz_key)) if v], one.id),
            )
        _log.info("person_place_kept", person_id=one.id, zone_known=tz_key is not None)
        if tz_key is None:
            return f"Kept: {one.name} lives in {city}. Pisa doesn't know that city's time zone: ask which."
        return f"Kept: {one.name} lives in {city or tz_key} ({tz_key})."

    async def stop_orbit(self, person: str | None, owner_said: str, words: str) -> str:
        """ "Non ricordarmi più di Paolo": no orbit line for them again; without a person, none for anyone."""
        if not _quoted(owner_said, words) or not rules.says_stop(owner_said):
            return "Not done: only the user's own words this turn saying to stop turn this off."
        async with self._pool.connection() as conn, conn.transaction():
            if not person or not person.strip():
                if not rules.says_everyone(owner_said):
                    return "Not done: say who (or that it's for everyone). Ask the user."
                await set_setting(conn, ORBITS_KEY, Jsonb(False))
                _log.info("people_orbits_off")
                return (
                    "Done: Pisa won't point out people they haven't heard from any more (People in the app "
                    "turns it on again)."
                )
            if not rules.in_said(person, owner_said):
                return "Not done: the person must be in the user's words."
            one = await self._one(conn, person, self._owner.tag, create=False)
            if isinstance(one, str):
                return one
            await conn.execute(
                "UPDATE people SET orbit = false, updated_at = %s, owner_fields = "
                "(SELECT array_agg(DISTINCT f) FROM unnest(owner_fields || '{orbit}'::text[]) f) "
                "WHERE id = %s",
                (self._clock(), one.id),
            )
        _log.info("person_orbit_off", person_id=one.id)
        return f"Done: Pisa won't remind them of {one.name} again (birthdays stay)."

    # --- the brain's reads ----------------------------------------------------------------------------------

    async def about(self, person: str, lang: str) -> tuple[str, str, bool]:
        """(Pisa's lines and their own ideas, an email's ideas, whether the name came from outside) for "cosa
        regalo a Laura?": the email's part, and a name from an email or an invite they never set, are
        outside text for the caller to mark as such."""
        async with self._pool.connection() as conn:
            one = await self._one(conn, person, lang, create=False)
            if isinstance(one, str):
                return one, "", False
            row = await (
                await conn.execute("SELECT source, owner_fields FROM people WHERE id = %s", (one.id,))
            ).fetchone()
            outside_name = row is not None and row[0] != "owner" and "name" not in (row[1] or [])
            notes = await (
                await conn.execute(
                    "SELECT kind, text, source, at FROM person_notes WHERE person_id = %s ORDER BY at DESC",
                    (one.id,),
                )
            ).fetchall()
            dates = await (
                await conn.execute(
                    "SELECT kind, month, day FROM person_dates WHERE person_id = %s", (one.id,)
                )
            ).fetchall()
        today = self._owner.day(self._clock())
        own = [f"{one.name}" + (f" ({one.relation})" if one.relation else "")]
        if one.city or one.tz:
            own.append(f"lives in: {one.city or ''} {f'({one.tz})' if one.tz else ''}".strip())
        own += [f"{k.replace('_', ' ')}: {rules.next_day(m, d, today).isoformat()}" for k, m, d in dates]
        own += [
            f"{'gift idea' if k == 'gift' else 'note'} (their words): {t}"
            for k, t, s, _ in notes
            if s == "owner"
        ]
        if not any(k == "gift" for k, *_ in notes):
            own.append("no gift ideas kept")
        email = [f"{at:%Y-%m-%d} in an email from {one.name}: {t}" for k, t, s, at in notes if s == "email"]
        return "\n".join(own), "\n".join(email), outside_name

    async def hour(self, person: str, lang: str) -> str:
        """Their hour, when Pisa proposes a wish there, and whether it's a decent hour to call:
        "da mamma sono le 23:40: domani?"."""
        async with self._pool.connection() as conn:
            one = await self._one(conn, person, lang, create=False)
        if isinstance(one, str):
            return one
        if rules.zone(one.tz) is None:
            return f'Pisa doesn\'t know where {one.name} lives: ask ("dove vive?").'
        assert one.tz is not None
        now = self._clock()
        found = offer_domain.clock(one.tz, self._owner.tz, now)
        call = found.line(offer_domain.their_place(one.name, one.relation, lang), lang) if found else ""
        hour = self._their(one.tz, one.city, now, None)
        if hour is None:
            return f"{one.name}: same time as the user.\nTo call: {call}"
        return f"{one.name}: {hour.line(lang)} (their zone {one.tz})\nTo call: {call}"

    # --- the morning (the briefing's people lines) --------------------------------------------------------

    async def morning_lines(self, lang: str) -> list[str]:
        """Pisa's lines for this morning, fixed for the day (the same on every briefing of the day): today's
        dates; on Sunday one orbit; one thank-you for yesterday's meal or party with someone. Hidden people
        never; at most one orbit a week, a thank-you once per event."""
        now = self._clock()
        tz = self._owner.zone
        today = now.astimezone(tz).date()
        async with self._pool.connection() as conn, conn.transaction():
            done = await (
                await conn.execute("SELECT kind, ref FROM people_nudges WHERE day = %s ORDER BY id", (today,))
            ).fetchall()
            made = {(k, r) for k, r in done}
            await self._date_nudges(conn, today, now, lang, made)
            if on_sunday(today):
                await self._orbit_nudge(conn, today, now, lang, made)
            # the thank-you is its own offer now, with its button (PeopleOffers._plan_thanks)
            rows = await (
                await conn.execute(
                    "SELECT n.line, n.kind, p.name, p.tz, p.city, d.kind, d.month, d.day "
                    "FROM people_nudges n LEFT JOIN people p ON p.id = n.person_id "
                    "LEFT JOIN person_dates d ON n.kind = 'date' AND d.id::text = split_part(n.ref, ':', 2) "
                    "WHERE n.day = %s AND NOT coalesce(p.hidden, false) ORDER BY n.kind, n.id",
                    (today,),
                )
            ).fetchall()
        # a date's line says their hour NOW, never the one of the first briefing
        return [
            rules.date_line(dkind, name, lang, self._their(tz, city, now, (month, day)))
            if kind == "date" and dkind is not None
            else line
            for line, kind, name, tz, city, dkind, month, day in rows
        ]

    def _their(
        self, tz: str | None, city: str | None, now: datetime, on: tuple[int, int] | None
    ) -> rules.TheirHour | None:
        """Their hour against their (the user's zone now). Someone with no zone is at home (the
        PISA_TIMEZONE), so the line comes when they are away: "da loro sono le 23"."""
        if tz is None:
            return rules.their_hour(self._owner.home, None, self._owner.tz, now, on)
        return rules.their_hour(tz, city, self._owner.tz, now, on)

    async def _nudge(
        self, conn: AsyncConnection, kind: str, pid: str, ref: str, today: date, line: str, draft: str | None,
        now: datetime, made: set[tuple[str, str]],
    ) -> None:  # fmt: skip
        if (kind, ref) in made:
            return
        await conn.execute(
            "INSERT INTO people_nudges (kind, person_id, ref, day, line, draft, at) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (kind, ref) DO NOTHING",
            (kind, pid, ref, today, line, draft, now),
        )
        made.add((kind, ref))
        _log.info("people_nudge", kind=kind, person_id=pid)

    async def _date_nudges(
        self, conn: AsyncConnection, today: date, now: datetime, lang: str, made: set[tuple[str, str]]
    ) -> None:
        rows = await (
            await conn.execute(
                "SELECT d.id, d.kind, d.month, d.day, p.id::text, p.name FROM person_dates d "
                "JOIN people p ON p.id = d.person_id WHERE NOT p.hidden"
            )
        ).fetchall()
        for date_id, kind, month, day, pid, name in rows:
            if rules.next_day(month, day, today) != today:
                continue
            line = rules.date_line(kind, name, lang, None)  # its hour is added when shown
            draft = rules.wish_draft(kind, name, lang)
            await self._nudge(
                conn, "date", pid, f"date:{date_id}:{today.year}", today, line, draft, now, made
            )

    async def _orbit_nudge(
        self, conn: AsyncConnection, today: date, now: datetime, lang: str, made: set[tuple[str, str]]
    ) -> None:
        if await get_setting(conn, ORBITS_KEY) is False:
            return
        # one a week: Sunday only (the caller), and one row per ISO week (UNIQUE (kind, ref))
        week = f"orbit:{today.isocalendar().year}-{today.isocalendar().week}"
        rows = await (
            await conn.execute(
                "SELECT id::text, name, last_contact_at, rhythm_days FROM people WHERE NOT hidden AND orbit"
            )
        ).fetchall()
        best: tuple[float, str, str, datetime] | None = None
        for pid, name, last, rhythm in rows:
            ratio = rules.orbit_gap(last, rhythm, now)
            if ratio is not None and (best is None or ratio > best[0]):
                best = (ratio, pid, name, last)
        if best is None:
            return
        _, pid, name, last = best
        days = (now - last).total_seconds() / 86400
        await self._nudge(
            conn, "orbit", pid, week, today, rules.orbit_line(name, days, lang), None, now, made
        )

    async def person_outside(self, person: str, lang: str) -> bool:
        """The person their words name holds a name from an email or an invite they never set
        (an unknown person: no name of theirs is said)."""
        async with self._pool.connection() as conn:
            one = await self._one(conn, person, lang, create=False)
        return not isinstance(one, str) and await self.names_outside([one.name])

    async def names_outside(self, names: list[str]) -> bool:
        """A name Pisa holds from an email's sender or an invite they never set."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM people WHERE name = ANY(%s) AND source <> 'owner' "
                    "AND NOT ('name' = ANY(owner_fields)) LIMIT 1",
                    (names,),
                )
            ).fetchone()
        return row is not None

    async def drafts(self) -> list[tuple[str, str, str]]:
        """Today's drafts Pisa offered (person, kind, text), for the brain: they send them themselves (the
        morning's, then the offers that went out today)."""
        today = self._owner.day(self._clock())
        async with self._pool.connection() as conn:
            offered = await self.offers.drafts(conn, today)
            rows = await (
                await conn.execute(
                    "SELECT p.name, n.kind, n.draft FROM people_nudges n JOIN people p ON p.id = n.person_id "
                    "WHERE n.day = %s AND n.draft IS NOT NULL AND NOT p.hidden ORDER BY n.id",
                    (today,),
                )
            ).fetchall()
        return [(str(a), str(b), str(c)) for a, b, c in rows] + offered

    # --- the app -----------------------------------------------------------------------------

    async def listed(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            people = await self._rows(conn, None)
            on = await get_setting(conn, ORBITS_KEY)
        return {"people": people, "orbits": on is not False}

    async def _rows(self, conn: AsyncConnection, pid: uuid.UUID | None) -> list[dict[str, Any]]:
        cur = conn.cursor(row_factory=dict_row)
        where = " WHERE NOT hidden" if pid is None else " WHERE id = %s"
        await cur.execute(_PERSON + where + " ORDER BY name", () if pid is None else (pid,))
        people = await cur.fetchall()
        if not people:
            return []
        ids = [p["id"] for p in people]
        notes = await (
            await conn.execute(
                "SELECT person_id::text, id, kind, text, source, at FROM person_notes "
                "WHERE person_id = ANY(%s::uuid[]) ORDER BY at DESC",
                (ids,),
            )
        ).fetchall()
        dates = await (
            await conn.execute(
                "SELECT person_id::text, id, kind, month, day, year, remind_days_before, source "
                "FROM person_dates "
                "WHERE person_id = ANY(%s::uuid[]) ORDER BY id",
                (ids,),
            )
        ).fetchall()
        tz = self._owner.zone
        lang = await self._owner.lang(conn)
        out = []
        for p in people:
            mine = [n for n in notes if n[0] == p["id"]]
            days = [
                {"id": d[1], "kind": d[2], "on": rules.SaidDate(d[3], d[4], d[5]).on,
                 "remind_days_before": list(rules.remind_days(d[6])), "source": d[7]}
                for d in dates if d[0] == p["id"]
            ]  # fmt: skip
            birthday = next((d["on"] for d in days if d["kind"] == "birthday"), None)
            out.append({
                "id": p["id"], "name": p["name"], "relation": p["relation"], "birthday": birthday,
                "tz": p["tz"], "city": p["city"], "emails": list(p["emails"] or []),
                "last_contact_at": p["last_contact_at"].astimezone(tz) if p["last_contact_at"] else None,
                "notes_count": len([n for n in mine if n[4] == "owner"]),
                "gift_ideas": [
                    {"id": n[1], "text": n[3], "from": n[4], "at": n[5].astimezone(tz)}
                    for n in mine if n[2] == "gift"
                ],
                "dates": days, "orbit": p["orbit"], "source": p["source"],
                "clock": self._clock_of(p["name"], p["relation"], p["tz"], lang),
            })  # fmt: skip
        return out

    def _clock_of(self, name: str, relation: str | None, tz: str | None, lang: str) -> dict[str, Any] | None:
        """Their hour now and whether it is a decent hour to call (the person's page)."""
        found = offer_domain.clock(tz, self._owner.tz, self._clock()) if tz else None
        if found is None:
            return None
        where = offer_domain.their_place(name, relation, lang)
        return {"at": found.their_now, "call": found.verdict, "call_from": found.next_good,
                "line": found.line(where, lang)}  # fmt: skip

    async def patch(self, person_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Their corrections, which win over every sync (`owner_fields`)."""
        pid = _uuid(person_id)
        sets: dict[str, Any] = {}
        for field, value in changes.items():
            if field not in PATCHABLE:
                raise _invalid(f"{field} can't be changed.")
            if field == "name":
                name = " ".join(str(value or "").split())
                if not 1 <= len(name) <= rules.NAME_MAX:
                    raise _invalid("A name is 1 to 80 characters.")
                sets["name"], sets["name_key"] = name, people_name_key(name)
            elif field == "tz":
                if value is not None and rules.zone(str(value)) is None:
                    raise _invalid("tz must be an IANA time zone (America/New_York).")
                sets["tz"] = value
            elif field in ("relation", "city"):
                cap = rules.CITY_MAX if field == "city" else rules.RELATION_MAX  # the columns' limits
                text = " ".join(str(value).split())[:cap] if value is not None else None
                sets[field] = text or None
                if field == "city" and text and "tz" not in changes and (key := rules.tz_for_city(text)):
                    sets["tz"] = key
            elif field in ("orbit", "hidden"):
                if not isinstance(value, bool):
                    raise _invalid(f"{field} is true or false.")
                sets[field] = value
            elif field != "birthday":
                raise _invalid(f"{field} can't be changed.")
        birthday = changes.get("birthday") if "birthday" in changes else ...
        said = None
        if birthday is not ... and birthday is not None:
            said = rules.parse_on(str(birthday))
            if said is None:
                raise _invalid("birthday is MM-DD or YYYY-MM-DD, a real day.")
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute("SELECT 1 FROM people WHERE id = %s", (pid,))).fetchone()
            if row is None:
                raise _not_found()
            fields = sorted(set(changes))
            assignments = sql.SQL("").join(
                sql.SQL(", {} = %s").format(sql.Identifier(k)) for k in sets
            )  # the names are PATCHABLE's, never the request's
            await conn.execute(
                sql.SQL(
                    "UPDATE people SET updated_at = %s{}, owner_fields = (SELECT array_agg(DISTINCT f) FROM "
                    "unnest(owner_fields || %s::text[]) f) WHERE id = %s"
                ).format(assignments),
                (now, *sets.values(), fields, pid),
            )
            if birthday is None:
                await conn.execute(
                    "DELETE FROM person_dates WHERE person_id = %s AND kind = 'birthday'", (pid,)
                )
            elif said is not None:
                await conn.execute(
                    "INSERT INTO person_dates (person_id, kind, month, day, year, source, created_at) VALUES "
                    "(%s, 'birthday', %s, %s, %s, 'owner', %s) ON CONFLICT (person_id, kind) DO UPDATE SET "
                    "month = EXCLUDED.month, day = EXCLUDED.day, year = EXCLUDED.year, source = 'owner', "
                    "source_ref = NULL",
                    (pid, said.month, said.day, said.year, now),
                )
            out = await self._rows(conn, pid)
        _log.info("person_patched", person_id=str(pid), fields=fields)
        return out[0]

    async def keep_note(self, person_id: str, note_id: int) -> dict[str, Any]:
        """They keep an email's gift idea as their own: it becomes
        `from: owner`, no longer tied to the email (so it outlives it)."""
        pid = _uuid(person_id)
        async with self._pool.connection() as conn, conn.transaction():
            kept = await (
                await conn.execute(
                    "UPDATE person_notes SET source = 'owner', source_ref = NULL "
                    "WHERE id = %s AND person_id = %s RETURNING id",
                    (note_id, pid),
                )
            ).fetchone()
            if kept is None:
                raise _not_found("note")
            out = await self._rows(conn, pid)
        _log.info("person_note_kept_by_owner", person_id=str(pid), note_id=note_id)
        return out[0]

    async def delete(self, person_id: str) -> None:
        """Forget someone: everything Pisa knew (notes, dates, lines, the care circle, where they live).
        Only their address stays, hidden, so the sync never brings them back; someone with no address is
        gone."""
        pid = _uuid(person_id)
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute("SELECT emails FROM people WHERE id = %s", (pid,))).fetchone()
            if row is None:
                raise _not_found()
            if not row[0]:
                await conn.execute("DELETE FROM people WHERE id = %s", (pid,))
            else:
                for table in (
                    "person_notes", "person_dates", "people_nudges", "care_circle", "person_moments",
                    "people_offers",
                ):  # fmt: skip
                    await conn.execute(
                        sql.SQL("DELETE FROM {} WHERE person_id = %s").format(sql.Identifier(table)), (pid,)
                    )
                await conn.execute(
                    "UPDATE people SET name = '-', name_key = '', relation = NULL, tz = NULL, city = NULL, "
                    "hidden = true, orbit = false, last_contact_at = NULL, rhythm_days = NULL, "
                    "owner_fields = '{hidden}', updated_at = %s WHERE id = %s",
                    (self._clock(), pid),
                )
        _log.info("person_deleted", person_id=str(pid))

    async def merge(self, person_id: str, into_id: str) -> dict[str, Any]:
        """Two rows that are one person (two addresses): their notes, dates, addresses and care into
        `into`; where both have one (a birthday, a care entry), `into`'s stays."""
        pid, into = _uuid(person_id), _uuid(into_id)
        if pid == into:
            raise _invalid("A person can't be merged into themselves.")
        async with self._pool.connection() as conn, conn.transaction():
            found = await (
                await conn.execute("SELECT id FROM people WHERE id = ANY(%s)", ([pid, into],))
            ).fetchall()
            if len(found) != 2:
                raise _not_found()
            await conn.execute("UPDATE person_notes SET person_id = %s WHERE person_id = %s", (into, pid))
            await conn.execute(  # what they said about their days goes with them
                "UPDATE person_moments m SET person_id = %s WHERE person_id = %s AND NOT EXISTS "
                "(SELECT 1 FROM person_moments o WHERE o.person_id = %s AND o.source_ref = m.source_ref)",
                (into, pid, into),
            )
            await conn.execute(
                "UPDATE person_dates d SET person_id = %s WHERE person_id = %s AND NOT EXISTS "
                "(SELECT 1 FROM person_dates o WHERE o.person_id = %s AND o.kind = d.kind)",
                (into, pid, into),
            )
            await conn.execute(
                "UPDATE care_circle SET person_id = %s WHERE person_id = %s AND NOT EXISTS "
                "(SELECT 1 FROM care_circle o WHERE o.person_id = %s)",
                (into, pid, into),
            )
            await conn.execute(
                "UPDATE people p SET "
                "emails = (SELECT array_agg(DISTINCT e) FROM unnest(p.emails || o.emails) e), "
                "last_contact_at = greatest(p.last_contact_at, o.last_contact_at), "
                "owner_fields = (SELECT array_agg(DISTINCT f) "
                "FROM unnest(p.owner_fields || '{emails}'::text[]) f), "
                "updated_at = %s FROM people o WHERE p.id = %s AND o.id = %s",
                (self._clock(), into, pid),
            )
            await conn.execute("DELETE FROM people WHERE id = %s", (pid,))
            out = await self._rows(conn, into)
        _log.info("person_merged", person_id=str(pid), into=str(into))
        return out[0]

    async def set_orbits(self, on: bool) -> dict[str, bool]:
        """Their switch for every orbit line (their "non dirmi più chi non sento" turns it off by
        voice)."""
        async with self._pool.connection() as conn:
            await set_setting(conn, ORBITS_KEY, Jsonb(on))
        _log.info("people_orbits_set", on=on)
        return {"orbits": on}

    async def delete_note(self, person_id: str, note_id: int) -> None:
        pid = _uuid(person_id)
        async with self._pool.connection() as conn:
            gone = await (
                await conn.execute(
                    "DELETE FROM person_notes WHERE id = %s AND person_id = %s RETURNING id", (note_id, pid)
                )
            ).fetchone()
        if gone is None:
            raise _not_found("note")
        _log.info("person_note_deleted", person_id=str(pid), note_id=note_id)

    async def care(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT c.person_id::text, p.name, c.shares, c.since FROM care_circle c "
                    "JOIN people p ON p.id = c.person_id ORDER BY c.since, p.name"
                )
            ).fetchall()
        tz = self._owner.zone
        return {
            "members": [
                {"person_id": pid, "name": name, "shares": shares, "since": since.astimezone(tz)}
                for pid, name, shares, since in rows
            ]
        }

    async def set_care(self, members: list[tuple[str, str]]) -> dict[str, Any]:
        """Replace the circle with what they entered (stored only: nothing is collected or shared)."""
        if len(members) > rules.CARE_MAX:
            raise _invalid(f"At most {rules.CARE_MAX} people in the care circle.")
        wanted = {_uuid(pid): shares for pid, shares in members}
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            if wanted:
                found = await (
                    await conn.execute("SELECT id FROM people WHERE id = ANY(%s)", (list(wanted),))
                ).fetchall()
                if len(found) != len(wanted):
                    raise _not_found()
            await conn.execute("DELETE FROM care_circle WHERE NOT (person_id = ANY(%s))", (list(wanted),))
            for pid, shares in wanted.items():
                await conn.execute(
                    "INSERT INTO care_circle (person_id, shares, since) VALUES (%s, %s, %s) ON CONFLICT "
                    "(person_id) DO UPDATE SET shares = EXCLUDED.shares, since = CASE WHEN "
                    "care_circle.shares = EXCLUDED.shares THEN care_circle.since ELSE EXCLUDED.since END",
                    (pid, shares, now),
                )
        _log.info("care_circle_set", members=len(wanted))
        return await self.care()


_TASK_TITLES = {
    "birthday": ("Compleanno di {name} ({on})", "{name}'s birthday ({on})"),
    "anniversary": ("Anniversario di {name} ({on})", "{name}'s anniversary ({on})"),
    "name_day": ("Onomastico di {name} ({on})", "{name}'s name day ({on})"),
    "other": ("Data importante: {name} ({on})", "A date for {name} ({on})"),
}


def _day(at: date, lang: str) -> str:
    key = "en-US" if lang == "en-US" else "it-IT"
    return f"{WEEKDAYS_SHORT[key][at.weekday()]} {at.day} {MONTHS_SHORT[key][at.month - 1]}"


def _quoted(owner_said: str, words: str) -> bool:
    """The user's words for a change are, word for word, in what they said this turn."""
    said = plain(owner_said)
    return len(said.split()) >= 2 and f" {said} " in f" {plain(words)} "


async def forget_google(conn: AsyncConnection) -> None:
    """On Google's disconnect, what Pisa learnt from their mail and calendar goes with them: an email's
    gift ideas, a calendar's dates and thank-you lines, the people the sync added that they never touched (no
    field, note, date or care entry of theirs), and the others' addresses and contact history. A person they
    hid keeps only that mark (and the address that makes it hold)."""
    await conn.execute("DELETE FROM person_notes WHERE source = 'email'")
    await conn.execute("DELETE FROM person_dates WHERE source = 'calendar'")
    await conn.execute("DELETE FROM people_nudges WHERE kind = 'thanks'")
    # their words about their days, and the offers built on their mail and calendar, go too
    await conn.execute("DELETE FROM person_moments")
    await conn.execute("DELETE FROM people_offers WHERE kind IN ('moment', 'thanks') OR sent_at IS NULL")
    await conn.execute(
        "DELETE FROM people p WHERE p.source IN ('email', 'calendar') AND p.owner_fields = '{}' "
        "AND NOT p.hidden "
        "AND NOT EXISTS (SELECT 1 FROM person_notes n WHERE n.person_id = p.id) "
        "AND NOT EXISTS (SELECT 1 FROM person_dates d WHERE d.person_id = p.id) "
        "AND NOT EXISTS (SELECT 1 FROM care_circle c WHERE c.person_id = p.id)"
    )
    await conn.execute(
        "UPDATE people SET emails = '{}', last_contact_at = NULL, rhythm_days = NULL "
        "WHERE NOT hidden AND source IN ('email', 'calendar')"
    )
    await delete_setting(conn, WISHES_KEY, offer_rules.MOMENTS_KEY, offer_rules.GIFTS_KEY)
