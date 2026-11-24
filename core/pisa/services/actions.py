"""Pisa's actions outside Pisa: calendar changes and email replies.

- **Pisa acts**: a calendar change whose values are in the user's own words this turn, on their own event
  with nobody else to notify, in a turn that read no outside text, is done at once (`act`, origin `words`)
  with Undo for 24 hours, and the device gets `action_done`. Otherwise one yes (`proposal` names who is
  notified; `held` came from outside text or isn't in the user's words), which code hears in their next turn
  (`answer`) or the phone gives. Never a second question after it.

- **Calendar**: `create_event`, `move_event` (keeps the length), `cancel_event`, in the user's own Google
  calendars. The live event is read first: an event the user doesn't organise is refused with that reason,
  an all-day one isn't moved, and the approval names who Google will notify (`sendUpdates` = all when there
  are other attendees). The executor checks the event didn't change since the proposal; after it, the calendar
  is synced again and the day re-planned. Undo (10 minutes): delete what was created, put a moved event back,
  restore a cancelled one.
- **Email**: `draft_reply` saves a Gmail draft in the thread (nothing leaves: no approval) and returns the
  text to read back; `edit_draft` changes the text or the recipients; `send_draft` asks for the approval.
  Recipients come from the thread; one the user names who isn't in it is an outsider (Face ID). An
  address found only in an email's text is refused: the content of an email never chooses who Pisa writes to.
- Without the Google permission (calendar.events, gmail.compose), each tool says what is missing.
- Logs carry ids and kinds only: never titles, addresses or an email's text.
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import formataddr
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain.untrusted import carried_lines, carried_now, current, data_block
from pisa.connectors.google import oauth
from pisa.connectors.google.calendar_write import GoogleCalendarWriter
from pisa.connectors.google.drafts import GmailDrafts
from pisa.connectors.google.errors import ConnectorError
from pisa.domain import actions as rules
from pisa.domain import claims as claim_rules
from pisa.domain import decisions, quarantine, results, wrist, yescard, zone
from pisa.domain.approvals import EXPIRES_AFTER, TOOL_OF, UNANSWERED_KEPT, outcome_sentence
from pisa.domain.errors import PisaError
from pisa.domain.mail import NEVER_TO_MODEL_LABELS
from pisa.domain.owner import Owner
from pisa.domain.tasks import (
    CLEAR_MARGIN,
    MATCH_THRESHOLD,
    in_small_hours,
    is_short_yes,
    match_score,
    parse_when,
    title_key,
)
from pisa.logging import get_logger
from pisa.ports.actions import CalendarWriter, DraftMail
from pisa.services import foreign
from pisa.services.approvals import ApprovalsService, Outcome
from pisa.services.conversation import TurnState, current_turn, turn_device
from pisa.services.google import GoogleService

DEFAULT_EVENT_MIN = 60
EVENT_MAX = timedelta(hours=24)
FIND_EVENTS_DAYS = 14  # move/cancel look at the next two weeks (and what started in the last 12 hours)
FIND_EMAIL_DAYS = 30
CORRECTED = "corrected-proposal"  # the mark a re-made proposal held after outside text carries
WAITING = "NOT sent, waiting for their yes."  # a draft is never "done" before their yes
MAIL_KINDS = frozenset({"email", "email-results"})  # outside text that can ask them for a change
_log = get_logger("pisa.actions")
# "annulla" right after Pisa added a task: only a task that new, untouched since (an older one, or one
# changed since, is never taken for "the last change": delete_task names it)
UNDO_TASK_WITHIN = timedelta(minutes=15)


def _waiting(view: dict[str, Any], question: str | None = None, reason: str = "") -> str:
    """One question, then the user's yes acts (spoken in their next message, or on the phone).
    a `needs_yes` result with the reason and the question to ask, never a silent hold."""
    ident, ask = results.short(view["id"]), question or view["summary"]
    why = reason or ("it sends an email: the read-back is its one question" if view["kind"] == "email_send"
                     else "it needs the user's yes")  # fmt: skip
    return results.needs_yes(
        ident, f"{ident} proposed, NOT done: {ask}", reason=why, ask=ask, biometric=view["requires_biometric"]
    )


def _done(view: dict[str, Any], said: str | None = None, stored: dict[str, Any] | None = None) -> str:
    """The user's words were the decision: it is done; Undo for 24 hours. The result carries
    its receipt and the object as stored, read back after the write."""
    ident = results.short(view["id"])
    if view["status"] != "done":
        reason = view["result"] or "it failed"
        return results.error(f"{ident} failed: {reason}", f"Not done: {reason}. Say why.")
    if view.get("verified") == "not_verified":  # the provider didn't confirm it: no "fatto"
        return results.done(  # its Undo and the object stay known
            ident,
            f"{ident} NOT verified: {view['result']}",
            stored=stored,
            undo=bool(view["undo_until"]),
            verified=False,
        )
    return results.done(
        ident, f"{ident} done: {said or view['result']}", stored=stored, undo=bool(view["undo_until"])
    )


def _outcome(view: dict[str, Any]) -> str:
    """The decision log's outcome: a done act the read-back didn't confirm is `unverified`, so
    Pisa's later turns never stand a claim on it."""
    return (
        "unverified"
        if view["status"] == "done" and view.get("verified") == "not_verified"
        else view["status"]
    )


def _sentence(result: str, lang: str) -> str:
    """What Pisa says from a result by code (a correction answered without the model): the change made, with
    its Undo, or the one question."""
    try:
        out = json.loads(result)
    except ValueError:
        return result
    if out.get("status") == results.NEEDS_YES:
        return str(out["ask"])
    said = str(out.get("receipt", "")).split(": ", 1)[-1]
    if out.get("status") == results.DONE and out.get("undo"):
        said = f"{said} {rules.word(lang, 'undo_hint')}"
    return said


class ActionsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        google: GoogleService,
        approvals: ApprovalsService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        after_calendar_change: Callable[[], Awaitable[None]] | None = None,
        calendar: Callable[[], CalendarWriter] | None = None,
        mail: Callable[[], DraftMail] | None = None,
        apply_option: Callable[[str, str, str], Awaitable[Any]] | None = None,
        undo_option: Callable[[str], Awaitable[Any]] | None = None,
        autonomy: Callable[..., Awaitable[str]] | None = None,
    ) -> None:
        self._pool = pool
        self._google = google
        self._approvals = approvals
        self._owner = owner
        self._clock = clock
        self.after_calendar_change = after_calendar_change  # sync the calendar, re-plan
        # an option of Pisa's applied after the user's yes to it, and
        # Pisa's plan put back when that change is undone.
        self.apply_option = apply_option
        self.undo_option = undo_option
        # the user's level for a kind → ask | tell | silent
        self.autonomy = autonomy
        self._calendar = calendar or (lambda: GoogleCalendarWriter(google.api("Google Calendar")))
        self._mail = mail or (lambda: GmailDrafts(google.api("Gmail")))
        # each kind's read-back asks the provider whether the act is really there
        approvals.register("calendar_create", self._do_create, self._undo, self._read_calendar)
        approvals.register("calendar_move", self._do_move, self._undo, self._read_calendar)
        approvals.register("calendar_cancel", self._do_cancel, self._undo, self._read_cancel)
        approvals.register("email_send", self._do_send, verify=self._read_sent)

    def _small_hours(self) -> bool:
        """Before 05:00: the user's "domani" is today's date."""
        return in_small_hours(self._owner.local(self._clock()))

    def _local(self, text: str) -> datetime:
        moment = datetime.fromisoformat(text)
        return moment if moment.tzinfo else moment.replace(tzinfo=self._owner.zone)

    def _at(self, text: str, what: str) -> datetime:
        """A moment from the user's words: "domani alle 10", "venerdì 21:00", "2026-10-23T10:00"."""
        when, kind = parse_when(
            rules.with_minutes(text), self._owner.day(self._clock()), small_hours=self._small_hours()
        )
        if when is None or kind != "datetime":
            raise PisaError("validation_failed", f'The {what} needs a time too ("domani alle 10").', "")
        return self._local(when)

    # --- calendar: propose --------------------------------------------------------------------------------

    async def create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        duration_min: int | None = None,
        place: str | None = None,
        calendar: str | None = None,
        owner_said: str | None = None,
        replaces: str | None = None,
        *,
        grounded_by: str | None = None,
        repeat: rules.Repeat | None = None,
        until: str | None = None,
    ) -> str:
        """`repeat`: every week on its weekday, or every weekday, through Google Calendar's
        RRULE, to `until` (a day) or with no end; a series always waits for their yes.
        `replaces`: the id of the proposal still waiting that this one corrects, even under
        another title ("Call Marco" for "Call con Marco"): it is withdrawn, never left for a later yes.
        `grounded_by` (code only, never the model's): their word on what Pisa prepared ("sì, fatto" after the
        booking Pisa asked about), checked by the handoff tool: that word is the one yes."""
        await self._google.require(oauth.SCOPE_CALENDAR_EVENTS)
        title = rules.clean(title)[:200]
        if not title:
            raise PisaError("validation_failed", "The event needs a title.", "")
        # the place as the title is, one line with no
        # control or format characters, so it can never forge a line of a prompt that lists it
        place = (rules.clean(" ".join(place.split()))[:300] or None) if place else None
        turn = current_turn.get()
        if replaces and turn is not None and turn.corrects and turn.named_events:
            # "no aspetta, intendevo sabato" right after Pisa made the event: the
            # replacement is that event moved, never a second one beside it
            found = await self._find_event(title, None, since=self._clock() - timedelta(hours=12))
            if not isinstance(found, str) and found[1] in turn.named_events:
                return await self.move_event(title, start, known=(found[0], found[1]), owner_said=owner_said)
        begin = self._at(start, "start")
        if end:
            finish = self._end(begin, end)
        else:
            finish = begin + timedelta(minutes=duration_min or DEFAULT_EVENT_MIN)
        if not timedelta(0) < finish - begin <= EVENT_MAX:
            raise PisaError("validation_failed", "The event must end after it starts, within 24 hours.", "")
        if finish <= self._clock():
            # one in progress is real ("resto a casa dalle 12 alle 16", said at 12:37);
            # one already over is a wrong day or time
            raise PisaError("validation_failed", "That start is in the past.", "Say a later time")
        last = None
        if until:  # a day, never a time: its end of day
            clock = until if re.search(r"\d{1,2}[:.]\d{2}", until) else f"{until} 23:59"
            last = self._at(clock, "end of the series").astimezone(self._owner.zone)
        if repeat is not None and repeat not in rules.REPEATS:
            raise PisaError(
                "validation_failed", f"A series repeats weekly or on weekdays, not {repeat!r}.", ""
            )
        try:
            series = rules.rrule(repeat, begin.astimezone(self._owner.zone), last) if repeat else None
        except ValueError as err:
            raise PisaError(
                "validation_failed", f"Not a series: {err}.", "Say another start or end day"
            ) from None
        named = await self._calendar_named(calendar)
        if isinstance(named, str):
            return results.error(f"not done: {named}", named)
        calendar_id, calendar_name = named
        lang = await self._owner.lang(self._pool)
        # The event's id derives from the approval's: a create retried after a lost answer finds it (409).
        approval_id = uuid.uuid4()
        body: dict[str, Any] = {
            "id": approval_id.hex,
            "summary": title,
            "start": {"dateTime": begin.isoformat(), "timeZone": self._owner.tz},
            "end": {"dateTime": finish.isoformat(), "timeZone": self._owner.tz},
            "extendedProperties": {"private": _tag(approval_id)},
        }
        if place:
            body["location"] = place
        repeat_said = ""
        if repeat is not None and series is not None:
            body["recurrence"] = [series]
            repeat_said = rules.repeat_words(repeat, begin.astimezone(self._owner.zone), lang, last)
        details = [
            f"{rules.word(lang, 'calendar')}: {calendar_name}",
            f"{rules.word(lang, 'when')}: {rules.when_text(begin, finish, lang, self._owner.tz)}",
            *([f"{rules.word(lang, 'where')}: {rules.clean(place)}"] if place else []),
            *([f"{rules.word(lang, 'repeats')}: {repeat_said}"] if repeat_said else []),
            rules.notified_line([], lang),
        ]
        at_said = rules.at_text(begin, lang, self._owner.tz) + (f", {repeat_said}" if repeat_said else "")
        summary = rules.word(lang, "create", title=title, when=at_said)
        payload = {"calendar_id": calendar_id, "body": body, "title": title, "lang": lang,
                   "when": rules.at_text(begin, lang, self._owner.tz), "send_updates": "none"}  # fmt: skip
        if repeat_said:
            payload["repeat_said"] = repeat_said
        # a correction of a proposal still waiting ("per oggi", "alle 5 invece") re-makes it: the
        # old question is withdrawn, never left to be answered; its user's words may ground the new one.
        earlier, outside = await self._replace_pending("calendar_create", title, replaces)
        today = self._owner.day(self._clock())
        mode, why = self._mode(
            "calendar_create", begin, today, attendees=False, title=title, end=finish,
            quote=owner_said, earlier=earlier, place=place, end_said=bool(end),
        )  # fmt: skip
        left_out = ""
        if mode == "held" and place and not outside:
            # their own command is done with their own words;
            # the place only the email gave is never written without their yes: it is left out
            bare, bare_why = self._mode(
                "calendar_create", begin, today, attendees=False, title=title, end=finish,
                quote=owner_said, earlier=earlier, place=None, end_said=bool(end),
            )  # fmt: skip
            # never when the place reaches the event through its title ("Ritiro pacco
            # Bottega Verde"): a word of the left-out place in the title keeps the hold
            place_words = {w for w in re.findall(r"\w{3,}", place.casefold()) if not w.isdigit()}
            in_title = place_words & set(re.findall(r"\w{3,}", title.casefold()))
            if bare != "held" and not in_title:
                mode, why, left_out = bare, bare_why, rules.clean(place)
                body.pop("location", None)
                where = f"{rules.word(lang, 'where')}: "
                details = [d for d in details if not d.startswith(where)]
        # an earlier turn's email still in the talk counts too (read_outside)
        if grounded_by and mode == "held" and not current().outside() and not outside:
            mode, why = (
                "words",
                "",
            )  # their yes to Pisa's own question about what they did (this turn read nothing)
        if outside:
            # a proposal held because outside text was read stays held when it is re-made, and
            # so does every correction of it after
            payload["after_outside"] = True
            if mode != "held":
                mode, why = "held", "it corrects a proposal made after outside text: the user's yes decides"
        if repeat_said and mode != "held":
            # a series writes many days at once: it always waits for their yes, like any write held
            mode, why = "held", "a series of events: the user's yes decides"
        said = self._spoken(begin, lang) + (f", {repeat_said}" if repeat_said else "")
        result = await self._propose_or_act(
            mode, "calendar_create", summary, details, payload, lang, approval_id=approval_id,
            ask=rules.word(lang, "create", title=title, when=said),
            did=rules.word(lang, "did_create", title=title, when=said), reason=why,
        )  # fmt: skip
        if left_out:
            result += (
                f" Written WITHOUT the place {left_out!r}: only the earlier email gave it, and Pisa never "
                "writes an email's words without their yes. Never say the event has that place."
            )
        return result

    def _end(self, begin: datetime, end: str) -> datetime:
        """An end time: "11:30" (the start's day), or a day and a time."""
        text = end.strip()
        if len(text) <= 5 and (":" in text or "." in text):
            hours, minutes = text.replace(".", ":").split(":")
            local = begin.astimezone(self._owner.zone)
            return local.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)
        return self._at(text, "end")

    async def _calendar_named(self, name: str | None) -> tuple[str, str] | str:
        """(id, name) of the calendar the user named, the primary one by default; or why not. Pisa writes only
        to calendars the user owns (accessRole owner), never to Pisa's own plan calendar."""
        account = await self._google.account_email()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT id, name, access_role FROM calendars ORDER BY name")
            ).fetchall()
        if not name:
            primary = next((n for i, n, _ in rows if i == account), None)
            return "primary", primary or account or "primary"
        scored = sorted(((match_score(name, n), i, n, role) for i, n, role in rows), reverse=True)
        if not scored or scored[0][0] < MATCH_THRESHOLD:
            known = ", ".join(n for _, n, role in rows if role == "owner") or "none synced yet"
            raise PisaError("validation_failed", f"No calendar is called {name!r} (calendars: {known}).", "")
        _, calendar_id, calendar_name, role = scored[0]
        if role != "owner":
            return _not_owned(calendar_name)
        return calendar_id, calendar_name

    async def move_event(
        self,
        event: str,
        start: str,
        day: str | None = None,
        *,
        known: tuple[str, str] | None = None,
        owner_said: str | None = None,
    ) -> str:
        """`known`: (calendar id, event id) of the event Pisa already holds (a correction of its own move)."""
        await self._google.require(oauth.SCOPE_CALENDAR_EVENTS)
        found = (
            await self._known_event(*known)
            if known
            else await self._find_event(event, day, since=self._clock() - timedelta(hours=12))
        )
        if isinstance(found, str):
            # the user's "spostala" may be about a proposal still waiting: it is re-made at the
            # new time (one yes if still needed), never "No event matches".
            proposal = None if known else await self._proposal_named(event)
            if proposal is not None:
                return await self._remake(proposal, start, owner_said)
            return results.error(f"not done: {found}", found)
        calendar_id, event_id, calendar_name = found
        live = await self._owned_event(calendar_id, event_id)
        if isinstance(live, str):
            return results.error(f"not done: {live}", live)
        if "dateTime" not in (live.get("start") or {}):
            said = "That is an all-day event: Pisa moves only events with a time. Nothing was asked."
            return results.error(f"not done: {said}", said)
        old_start = self._local(live["start"]["dateTime"])
        old_end = self._local(live["end"]["dateTime"])
        begin = self._at(start, "new start")
        if begin < self._clock():
            raise PisaError("validation_failed", "That start is in the past.", "Say a later time")
        finish = begin + (old_end - old_start)
        lang = await self._owner.lang(self._pool)
        title = rules.clean(str(live.get("summary") or "?"))
        notified = rules.attendees_to_notify(live)
        tz = self._owner.zone
        change = f"{rules.at_text(old_start, lang, self._owner.tz)} → {begin.astimezone(tz):%H:%M}"
        if begin.astimezone(tz).date() != old_start.astimezone(tz).date():
            change = f"{rules.at_text(old_start, lang, tz.key)} → {rules.at_text(begin, lang, tz.key)}"
        details = [
            f"{rules.word(lang, 'calendar')}: {calendar_name}",
            f"{title}: {change} ({rules.word(lang, 'same')})",
            *(
                [f"{rules.word(lang, 'where')}: {rules.clean(live['location'])}"]
                if live.get("location")
                else []
            ),
            rules.notified_line(notified, lang),
        ]
        summary = rules.word(lang, "move", title=title, when=rules.at_text(begin, lang, self._owner.tz))
        approval_id = uuid.uuid4()
        payload = {
            "calendar_id": calendar_id, "event_id": event_id, "title": title, "lang": lang,
            "start": {"dateTime": begin.isoformat(), "timeZone": self._owner.tz},
            "end": {"dateTime": finish.isoformat(), "timeZone": self._owner.tz},
            "before": {"start": live["start"], "end": live["end"]}, "updated": live.get("updated"),
            "etag": live.get("etag"), "when": rules.at_text(begin, lang, self._owner.tz),
            "old_when": rules.at_text(old_start, lang, self._owner.tz),
            "send_updates": "all" if notified else "none", "notified": notified,
        }  # fmt: skip
        mode, why = self._mode(
            "calendar_move", begin, old_start.astimezone(tz).date(), attendees=bool(notified), title=title,
            event_id=event_id, old_start=old_start, quote=owner_said,
        )  # fmt: skip
        said = self._spoken(begin, lang)
        return await self._propose_or_act(
            mode, "calendar_move", summary, details, payload, lang,
            approval_id=approval_id, notified=notified, ask=rules.word(lang, "move", title=title, when=said),
            did=rules.word(lang, "did_move", title=title, when=said), reason=why,
        )  # fmt: skip

    async def move_on_yes(
        self,
        calendar_id: str,
        event_id: str,
        start: datetime,
        auth: str,
        was: datetime | None = None,
        *,
        device: uuid.UUID | None = None,
    ) -> tuple[bool, str]:
        """The Ghost Run's fix, after the user's one yes to it (a proposal of Pisa's takes
        one yes, then Pisa acts): their own event, with nobody invited, moves to `start` (same length), stored
        as approved (origin `proposal`), run now, with its 24 h Undo. (moved, what to say): an event that
        changed since (`was`: its start when Pisa proposed it; people invited; no time) is never moved.
        """
        await self._google.require(oauth.SCOPE_CALENDAR_EVENTS)
        live = await self._owned_event(calendar_id, event_id)
        if isinstance(live, str):
            return False, live
        if "dateTime" not in (live.get("start") or {}) or rules.attendees_to_notify(live):
            return (
                False,
                "Not moved: the event changed since Pisa proposed it (it has people invited, or no time).",
            )
        old_start = self._local(live["start"]["dateTime"])
        old_end = self._local(live["end"]["dateTime"])
        if was is not None and old_start != was:
            return False, "Not moved: the event was moved since Pisa proposed it."
        if start < self._clock():
            return False, "Not moved: that time has passed."
        finish = start + (old_end - old_start)
        lang = await self._owner.lang(self._pool)
        title = rules.clean(str(live.get("summary") or "?"))
        tz = self._owner.zone
        details = [f"{title}: {rules.at_text(old_start, lang, tz.key)} → {start.astimezone(tz):%H:%M}",
                   rules.notified_line([], lang)]  # fmt: skip
        said = rules.word(lang, "did_move", title=title, when=self._spoken(start, lang))
        approval_id = uuid.uuid4()
        payload = {
            "calendar_id": calendar_id, "event_id": event_id, "title": title, "lang": lang,
            "start": {"dateTime": start.isoformat(), "timeZone": self._owner.tz},
            "end": {"dateTime": finish.isoformat(), "timeZone": self._owner.tz},
            "before": {"start": live["start"], "end": live["end"]}, "updated": live.get("updated"),
            "etag": live.get("etag"), "when": rules.at_text(start, lang, self._owner.tz),
            "old_when": rules.at_text(old_start, lang, tz.key), "send_updates": "none", "notified": [],
            "approval_id": str(approval_id), "said": said,
        }  # fmt: skip
        summary = rules.word(lang, "move", title=title, when=rules.at_text(start, lang, self._owner.tz))
        view = await self._approvals.act(
            "calendar_move", summary, details, payload, device_id=device, lang=lang,
            approval_id=approval_id, origin="proposal", auth="tap" if auth == "tap" else "voice",
        )  # fmt: skip
        decisions.note_if_collecting(
            "act", _TOOLS["calendar_move"], "pisa_proposal", ref=view["id"], outcome=_outcome(view)
        )
        return (True, said) if view["status"] == "done" else (False, str(view.get("result") or "Not moved."))

    async def cancel_event(self, event: str, day: str | None = None, owner_said: str | None = None) -> str:
        await self._google.require(oauth.SCOPE_CALENDAR_EVENTS)
        found = await self._find_event(event, day, since=self._clock())  # an event that ended stays
        if isinstance(found, str):
            # cancelling a proposal still waiting withdraws its question: nothing was created.
            proposal = await self._proposal_named(event)
            if proposal is not None:
                await self._approvals.supersede(str(proposal["id"]))
                ident = results.short(proposal["id"])
                return results.done(
                    ident, f"{ident} withdrawn: the proposal «{proposal['payload'].get('title')}» was never "
                    "created; nothing is in the calendar",
                )  # fmt: skip
            return results.error(f"not done: {found}", found)
        calendar_id, event_id, calendar_name = found
        live = await self._owned_event(calendar_id, event_id)
        if isinstance(live, str):
            return results.error(f"not done: {live}", live)
        lang = await self._owner.lang(self._pool)
        title = rules.clean(str(live.get("summary") or "?"))
        notified = rules.attendees_to_notify(live)
        start = live.get("start") or {}
        begin = (
            self._local(start["dateTime"]) if "dateTime" in start else self._local(f"{start['date']}T00:00")
        )
        details = [
            f"{rules.word(lang, 'calendar')}: {calendar_name}",
            f"{rules.word(lang, 'when')}: {rules.at_text(begin, lang, self._owner.tz)}",
            *(
                [f"{rules.word(lang, 'where')}: {rules.clean(live['location'])}"]
                if live.get("location")
                else []
            ),
            rules.notified_line(notified, lang),
        ]
        summary = rules.word(lang, "cancel", title=title, when=rules.at_text(begin, lang, self._owner.tz))
        payload = {"calendar_id": calendar_id, "event_id": event_id, "title": title, "lang": lang,
                   "updated": live.get("updated"), "etag": live.get("etag"),
                   "send_updates": "all" if notified else "none", "notified": notified}  # fmt: skip
        mode, why = self._mode(
            "calendar_cancel", None, None, attendees=bool(notified), title=title, event_id=event_id,
            quote=owner_said,
        )  # fmt: skip
        return await self._propose_or_act(
            mode, "calendar_cancel", summary, details, payload, lang, notified=notified,
            ask=rules.word(lang, "ask_cancel", title=title, when=self._spoken(begin, lang)),
            did=rules.word(lang, "did_cancel", title=title), reason=why,
        )  # fmt: skip

    # --- an option the user chose moves the real event too ------------------------------------

    async def write_through(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
        auth: str,
        *,
        invited: int = 0,
        option_id: str | None = None,
        check: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        """The option the user chose moves «event» in Pisa's plan: the Google event moves with it when it is
        the user's own (organised by them, in a calendar they own, not Pisa's plan calendar, with a time).
        Choosing it is the one yes (origin `proposal`; its Undo with the plan's), the people invited included
        when the option named how many (`invited`, "e avviso 2 invitati") and that is still so. One yes first:
        a choice by voice after outside text, or not in the user's words ("la seconda"),
        and invited people the option didn't name. (The approval's id or None when nothing goes to Google,
        what the user should hear about the calendar or None.)

        `check` (the option: options_id, option_id), before anything is applied: when
        the move needs a yes, the whole option waits for it (one approval carrying the option: plan and
        calendar together on the yes, one Undo); otherwise (None, None) and nothing is done. `auth`
        "approved": the user's yes to such an option, so nothing more is asked; "approved_option": another
        event of that option, whose people that yes didn't name (they take their own yes)."""
        try:
            await self._google.require(oauth.SCOPE_CALENDAR_EVENTS)
        except PisaError:
            return None, None  # no calendar permission: the move stays in Pisa's plan
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT e.calendar_id, c.name, c.access_role FROM calendar_events e JOIN calendars c "
                    "ON c.id = e.calendar_id WHERE e.id = %s AND e.status <> 'cancelled' AND NOT e.all_day",
                    (event_id,),
                )
            ).fetchone()
        if row is None or row[2] != "owner":
            return None, None
        calendar_id, calendar_name = row[0], row[1]
        try:
            live = await self._owned_event(calendar_id, event_id)
        except PisaError as err:
            return None, f"The calendar event was not moved ({err.message}); only Pisa's plan changed."
        if isinstance(live, str) or "dateTime" not in (live.get("start") or {}):
            return None, None
        old_start, old_end = self._local(live["start"]["dateTime"]), self._local(live["end"]["dateTime"])
        if old_start == start and old_end == end:
            return None, None
        lang = await self._owner.lang(self._pool)
        tz = self._owner.zone
        title = rules.clean(str(live.get("summary") or "?"))
        notified = rules.attendees_to_notify(live)
        when = rules.at_text(start, lang, self._owner.tz)
        details = [
            f"{rules.word(lang, 'calendar')}: {calendar_name}",
            f"{title}: {rules.at_text(old_start, lang, self._owner.tz)} → {start.astimezone(tz):%H:%M}",
            rules.notified_line(notified, lang),
        ]
        approval_id = uuid.uuid4()
        payload = {
            "calendar_id": calendar_id, "event_id": event_id, "title": title, "lang": lang,
            "start": {"dateTime": start.astimezone(tz).isoformat(), "timeZone": self._owner.tz},
            "end": {"dateTime": end.astimezone(tz).isoformat(), "timeZone": self._owner.tz},
            "before": {"start": live["start"], "end": live["end"]}, "updated": live.get("updated"),
            "etag": live.get("etag"), "when": when, "old_when": rules.at_text(old_start, lang, tz.key),
            "send_updates": "all" if notified else "none", "notified": notified,
            "approval_id": str(approval_id), "plan": True,
        }  # fmt: skip
        summary = rules.word(lang, "move", title=title, when=when)
        device, turn = turn_device(), current_turn.get()
        # RAIL: a choice after outside text takes one yes (the idea may not be Pisa's own); so do
        # people the option didn't name. Otherwise the option is Pisa's own proposal
        # on the user's own things: Pisa applies it with Undo, even on a vague "va bene"; the user's yes
        # ("approved") is it too.
        offered = {"option_id": option_id, "options_id": (check or {}).get("options_id")}
        named = turn is not None and rules.grounded_change(
            "apply_option", offered, turn.words, self._owner.day(self._clock())
        )
        # RAIL: Pisa's own proposal is applied on vague words only when Pisa offered these
        # options in this turn or the device's previous one (from its own tools); options seen only in the
        # day's context, next to invite titles, take one yes.
        own = (
            turn is not None
            and check is not None
            and str(check.get("options_id")) in (set(turn.offered) | set(turn.named_options))
        )
        held = auth not in ("tap", "approved", "approved_option") and (
            bool(current().outside()) or turn is None or not (named or own or check is None)
        )
        # RAIL: other people are notified only when the user said or tapped the
        # option that named them; never on vague words, and never by a yes to another event's question.
        needs = held or (
            bool(notified)
            and auth != "approved"
            and (auth == "approved_option" or len(notified) != invited or not (named or auth == "tap"))
        )
        # Provenance: the user chose Pisa's option (pisa_proposal), or Pisa applied its own on vaguer words.
        said = (
            decisions.provenance(said=False, outside=bool(current().outside()))
            if named or auth in ("tap", "approved")
            else decisions.provenance(said=False, outside=bool(current().outside()), unprompted=True)
        )
        why = _RAIL["held" if held else ""]
        if check is not None:
            if not needs:
                return None, None
            question = rules.with_notified(summary, notified, lang) if notified else summary
            view = await self._approvals.create(
                "calendar_move", question, details, payload | {"option": check}, device_id=device, lang=lang,
                approval_id=approval_id, origin="held" if held else "proposal", asked=True,
            )  # fmt: skip
            if turn is not None:
                turn.approvals.append(view["id"])
            decisions.note_if_collecting("propose", "apply_option", said, ref=view["id"], rail=why)
            return view["id"], (
                "Nothing changed yet: the option (Pisa's plan and the calendar together) waits for one yes: "
                f"ask exactly «{question}»"
            )
        if needs:
            question = rules.with_notified(summary, notified, lang) if notified else summary
            view = await self._approvals.create(
                "calendar_move", question, details, payload, device_id=device, lang=lang,
                approval_id=approval_id, origin="held" if held else "proposal", asked=True,
            )  # fmt: skip
            if turn is not None:
                turn.approvals.append(view["id"])
            decisions.note_if_collecting("act", "apply_option", said, outcome="done")  # the plan part
            decisions.note_if_collecting("propose", "apply_option", said, ref=view["id"], rail=why)
            return view["id"], (
                f"Pisa's plan changed; the calendar event waits for one yes: ask exactly «{question}»"
            )
        view = await self._approvals.act(
            "calendar_move", summary, details, payload, device_id=device, lang=lang, approval_id=approval_id,
            origin="proposal", auth="tap" if auth == "tap" else "voice",
        )  # fmt: skip
        decisions.note_if_collecting("act", "apply_option", said, ref=view["id"], outcome=_outcome(view))
        if view["status"] != "done":  # a 412 or Google down: the plan moved, the calendar didn't (said)
            return view[
                "id"
            ], f"The calendar event was not moved ({view['result']}); only Pisa's plan changed."
        return view["id"], None

    async def undo_through(self, approval_id: str) -> str | None:
        """Undo the Google side of an option the user undid: done, waiting (its question is withdrawn),
        or why not; None when there was nothing to undo."""
        row = await self._approvals.get(approval_id)
        if row["status"] == "pending":
            await self._approvals.decide(approval_id, "reject", "tap")
            return None
        if row["status"] != "done" or not row["undo_until"]:
            return None
        try:
            view = await self._approvals.undo(approval_id)
        except PisaError as err:
            return err.message
        return view["result"]

    # --- the user's yes, heard by code, and their undo -----------------------------------------------------

    async def answer(
        self,
        device_id: uuid.UUID,
        words: str,
        lang: str,
        asked: list[str],
        acted: list[str] | None = None,
        proposed: list[str] | None = None,
        spoken: bool = True,
    ) -> str | None:
        """`spoken` False: a typed answer: the voice's 10 s and 120 s don't apply, the card's
        10 minutes do.
        The user's next message answers the approval Pisa asked in their previous turn. A short yes runs
        it (no model call: nothing Pisa read can approve anything), "sì, senza avvisarli" runs it notifying no
        one, a short no rejects it. A marked "no, alle 9" corrects the time Pisa echoed. What Pisa
        says; None when the words aren't an answer (the model goes on)."""
        if rules.is_tap(words):
            return None  # RAIL: a tap on Pisa's choice never answers an approval, whatever its label says
        if (fixed := rules.corrected_time(words)) is not None:
            return await self._corrected(device_id, words, lang, asked, acted or [], fixed)
        if not asked:
            return None
        quietly = rules.says_quietly(words)
        yes = is_short_yes(words) or (quietly and is_short_yes(words.split(",")[0]))
        no = not yes and rules.is_short_no(words)
        send = not yes and not no and rules.says_send(words)
        if not yes and not no and not send:
            return None
        pending = await self._approvals.pending_for(device_id, asked)
        if not pending:
            return None
        if send:
            # the read-back's "Lo mando?" was the one question; "sì
            # mandala" answers it, by code, never a second question. Only when that send is all Pisa asked.
            if len(pending) != 1 or pending[0]["kind"] != "email_send":
                return None
            yes = True
        # the questions Pisa asked together in one turn ("Aggiungo la spesa alle 10, la call alle 14…?")
        # are one question: one yes runs them all, one no keeps them all. A Face ID one stays on the phone.
        if no:
            for row in pending:
                await self._approvals.decide(str(row["id"]), "reject", "voice", in_conversation=True)
            return rules.word(lang, "kept")
        # seen and rejected on the wrist, approved on the phone
        if wrist.current() == "watch":
            return yescard.on_the_phone(lang)
        # a "sì" more than 10 s after Pisa read the card out runs nothing: Pisa reads it again (the
        # next yes is heard by code as this one); past the 120 s a spoken yes holds, the phone's card decides
        heard = {
            str(r["id"]): self._approvals.heard(r) for r in pending if spoken and not r["requires_biometric"]
        }
        if "late" in heard.values():  # the late cards read again, never half decided
            over = [yescard.on_the_phone(lang)] if "over" in heard.values() else []
            late = [r for r in pending if heard.get(str(r["id"])) == "late"]
            return " ".join([*over, await self._read_again(device_id, late, lang)])
        said: list[str] = []
        for row in pending:
            if row[
                "requires_biometric"
            ]:  # the card with Face ID is on the phone: a spoken yes can't stand in
                said.append(rules.word(lang, "face_id"))
                continue
            if heard.get(str(row["id"])) == "over":
                said.append(yescard.on_the_phone(lang))
                continue
            try:
                view = await self._approvals.decide(
                    str(row["id"]), "approve", "voice", in_conversation=True,
                    send_updates="none" if quietly else None, spoken=spoken,
                )  # fmt: skip
            except PisaError as err:
                said.append(err.message)
                continue
            sentence = outcome_sentence(view["status"], view["result"], row["lang"]) or ""
            if view["status"] == "done" and view["verified"] != "not_verified":  # read back
                sentence = self._said_done(row["payload"].get("said") or sentence, view, lang)
            said.append(sentence.strip())
        # a change Pisa held in that turn that its question didn't bind (no time said, a cancel with
        # no cancel verb) is named, exactly, as not done: their "sì" never reads as all of them
        left = [i for i in proposed or [] if i not in asked]
        if left and (rows := await self._approvals.pending_for(device_id, left)):
            items = ", ".join(f"«{rules.clean(str(r['payload'].get('title') or r['summary']))}»"
                              f"{' (' + str(r['payload']['when']) + ')' if r['payload'].get('when') else ''}"
                              for r in rows)  # fmt: skip
            said.append(rules.word(lang, "not_applied", items=items))
        return " ".join(dict.fromkeys(s for s in said if s))

    async def _read_again(self, device_id: uuid.UUID, rows: list[dict[str, Any]], lang: str) -> str:
        """Pisa reads the cards out again (the spoken renderer), and the user's next yes answers
        them: the same question, never a new one."""
        lines = []
        for n, row in enumerate(rows):
            action = await self._approvals.action(str(row["id"]))
            lines.append(yescard.spoken(action, row["summary"], row["lang"] or lang, again=n == 0))
        ids = [str(r["id"]) for r in rows]
        await self._approvals.read_out(ids, said=" ".join(lines), renew=False)  # never past the first 120 s
        if (turn := current_turn.get()) is not None:
            turn.approvals, turn.proposed = list(ids), list(ids)
        _log.info("approval_read_again", device_id=str(device_id), count=len(ids))
        return " ".join(lines)

    async def answer_proposal(self, approval: str, yes: bool, owner_said: str) -> str:
        """The user's answer to one of the proposals Pisa asked in their previous turn, in words code
        doesn't decide alone ("sì, solo il primo", "no al secondo"): the model names it, code checks that the
        user's words this turn say it (a yes or a no, no question) and that no outside text was read."""
        turn, device = current_turn.get(), turn_device()
        if turn is None or device is None:
            return results.error("answer_proposal: no conversation", "Only the user's words answer it.")
        if current().outside():  # RAIL: nothing Pisa read can approve anything
            return results.error(
                "answer_proposal: refused after outside text",
                "Ask the user to say it again, or to tap on the phone.",
            )
        ident = approval.replace("-", "")[:8]
        ids = [i for i in turn.asked_before if results.short(i) == ident]
        rows = await self._approvals.pending_for(device, ids) if ids else []
        if not rows:
            return results.error(
                f"answer_proposal: {ident} isn't waiting", "No such proposal waits from Pisa's last question."
            )
        if not rules.answers_proposal(owner_said, turn.text, yes):
            return results.error(
                f"answer_proposal: {ident} not answered",
                "owner_said must be the user's own words this turn saying yes (or no) to it: ask them.",
            )
        row = rows[0]
        order = [i for i in turn.asked_order if i in turn.asked_before] or sorted(turn.asked_before)
        asked_rows = await self._approvals.pending_for(device, order) if order else []
        by_id = {str(r["id"]): str(r["payload"].get("title") or r["summary"]) for r in asked_rows}
        titles = [by_id.get(i, "") for i in order]
        index = order.index(str(row["id"])) if str(row["id"]) in order else -1
        if index < 0 or not rules.means_proposal(turn.words, titles, index):
            return results.error(
                f"answer_proposal: {ident} isn't the one they mean",
                "Their words pick another proposal (by its order or its name), or none: ask them which.",
            )
        if row["kind"] == "email_send":  # a send has its own one yes (the read-back), never this
            return results.error(
                f"answer_proposal: {ident} is an email",
                "An email is sent only by the user's yes to its read-back.",
            )
        if not yes:
            await self._approvals.decide(str(row["id"]), "reject", "voice", in_conversation=True)
            return results.done(ident, f"{ident} declined: nothing changed")
        if row["requires_biometric"]:
            view = await self._approvals.get(str(row["id"]))
            return _waiting(view, view["summary"], "it needs Face ID: it's on the phone")
        if wrist.current() == "watch":  # approved on the phone, never from the wrist
            view = await self._approvals.get(str(row["id"]))
            return _waiting(view, yescard.on_the_phone(row["lang"] or self._owner.tag),
                            "from the watch an approval is approved on the phone")  # fmt: skip
        view = await self._approvals.decide(
            str(row["id"]), "approve", "voice", in_conversation=True, said_at=turn.heard_at,
            spoken=turn.spoken,
        )  # fmt: skip
        if view["status"] == "done" and view["verified"] != "not_verified":  # read back
            turn.acted.append(view["id"])
            turn.done.append(view["result"] or view["summary"])
        # the event as Google has it after the approved act, not the proposal's words
        stored = await self._stored_after(row["kind"], row["payload"]) if view["status"] == "done" else None
        return _done(view, row["payload"].get("said"), stored)

    async def _corrected(
        self,
        device_id: uuid.UUID,
        words: str,
        lang: str,
        asked: list[str],
        acted: list[str],
        at: tuple[int, int],
    ) -> str | None:
        """ "No, alle 9" after Pisa echoed a time (a question it asked, or a change it made): the same event,
        at that time on the same day. A pending question is withdrawn; a change made is redone at the new time
        (its Undo restores the first). What Pisa says; None when there is no time to correct."""
        rows = await self._approvals.pending_for(device_id, asked)
        if not rows and acted:  # a change Pisa just made: only within the minutes a question would last
            recent = self._clock() - EXPIRES_AFTER
            rows = [
                r
                for r in [await self._approvals.row(a) for a in acted[-1:]]
                if r["status"] == "done" and r["executed_at"] is not None and r["executed_at"] > recent
            ]
        rows = [r for r in rows if r["kind"] in ("calendar_move", "calendar_create")]
        if len(rows) != 1:
            return None
        row, tz = rows[0], self._owner.zone
        payload = row["payload"]
        if payload.get("after_outside"):  # the re-made proposal keeps the flag
            current().read_past([CORRECTED])
        proposed = (
            payload["start"]["dateTime"]
            if row["kind"] == "calendar_move"
            else payload["body"]["start"]["dateTime"]
        )
        day = datetime.fromisoformat(proposed).astimezone(tz).date()
        start = f"{day.isoformat()}T{at[0]:02d}:{at[1]:02d}"
        if row["status"] == "pending":
            await self._approvals.decide(str(row["id"]), "reject", "voice", in_conversation=True)
        turn = current_turn.get() or TurnState(uuid.uuid4(), words)
        # The item and its day are Pisa's echo, the time is theirs: as a command, for `owner_said`'s check.
        verb = "sposta" if row["kind"] == "calendar_move" or row["status"] == "done" else "metti"
        command = f"{verb} {payload['title']} {day.isoformat()} alle {at[0]:02d}:{at[1]:02d}"
        said_as, turn.text = turn.text, command
        try:
            if row["kind"] == "calendar_move":
                known = (payload["calendar_id"], payload["event_id"])
                result = await self.move_event(payload["title"], start, day.isoformat(), known=known)
            elif row["status"] == "done":  # the event Pisa just made moves: never a second one
                known = (payload["calendar_id"], payload["body"]["id"])
                result = await self.move_event(payload["title"], start, day.isoformat(), known=known)
            else:
                minutes = int((datetime.fromisoformat(payload["body"]["end"]["dateTime"])
                               - datetime.fromisoformat(proposed)).total_seconds() // 60)  # fmt: skip
                body = payload["body"]
                result = await self.create_event(
                    payload["title"], start, None, minutes, body.get("location"), None
                )
        except PisaError as err:
            return err.message
        finally:
            turn.text = said_as
        return _sentence(result, lang)

    async def undo_last_change(self) -> str:
        """The user said to undo Pisa's last change ("rimettilo com'era", "annulla"): within its 24 hours.
        A calendar change is put back; a task Pisa added by voice after it, minutes ago and untouched since
        ("Aggiungi un'attività…", then "Annulla"), is the last change, and goes as delete_task removes one
        (restorable for 24 h)."""
        turn = current_turn.get()
        if turn is None or not rules.says_undo(turn.text):
            return "Not undone: the user didn't ask to undo anything in this message."
        device = turn_device()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id::text, kind, payload, executed_at FROM approvals WHERE status = 'done' "
                    "AND undo_until > %s AND undone_at IS NULL "
                    "AND (%s::uuid IS NULL OR device_id = %s::uuid) "
                    "ORDER BY executed_at DESC LIMIT 1",
                    (self._clock(), device, device),
                )
            ).fetchone()
            task = await self._undo_added_task(conn, device, after=row[3] if row is not None else None)
        if task is not None:
            return task
        if row is None:
            return rules.word(await self._owner.lang(self._pool), "nothing_to_undo")
        try:
            view = await self._approvals.undo(row[0])
        except PisaError as err:
            return f"Not undone: {err.message}"
        # what the calendar holds after the undo (back at its time, or gone)
        stored = await self._stored_after(row[1], row[2])
        return results.done(
            results.short(row[0]), f"{results.short(row[0])} undone: {view['result']}", stored=stored
        )

    async def _undo_added_task(
        self, conn: AsyncConnection, device: uuid.UUID | None, *, after: datetime | None
    ) -> str | None:
        """The task Pisa added by voice on this device in the last 15 minutes, open and untouched, newer
        than the last undoable calendar change (`after`: its time), deleted as delete_task deletes one
        (restorable for 24 h); None when there is none (the calendar change, if any, is the last one)."""
        now = self._clock()
        since = max(now - UNDO_TASK_WITHIN, after) if after is not None else now - UNDO_TASK_WITHIN
        found = await (
            await conn.execute(
                "UPDATE tasks SET status = 'deleted', deleted_at = %s, updated_at = %s WHERE id = ("
                "SELECT id FROM tasks WHERE source_kind = 'voice' AND status = 'open' AND created_at > %s "
                "AND updated_at = created_at "
                "AND (%s::text IS NULL OR source_ref = %s::text) ORDER BY created_at DESC LIMIT 1) "
                "RETURNING id::text, title",
                (now, now, since, *([f"device:{device}"] * 2 if device else [None, None])),
            )
        ).fetchone()
        if found is None:
            return None
        _log.info("task_soft_deleted", task_id=found[0], by="undo_last_change")
        said = data_block("tasks", f"Deleted: {found[1]} (restore_task brings it back for 24 hours).",
                          what="the user's tasks; titles may come from email").render()  # fmt: skip
        return results.within(
            said, results.done(None, f"task deleted: «{found[1][:120]}» (restore_task for 24 h)")
        )

    # --- act on the user's words, or ask once --------------------------------------------------------------

    def _mode(
        self,
        kind: str,
        start: datetime | None,
        default_day: Any,
        *,
        attendees: bool,
        title: str | None = None,
        event_id: str | None = None,
        end: datetime | None = None,
        old_start: datetime | None = None,
        quote: str | None = None,
        earlier: str | None = None,
        place: str | None = None,
        end_said: bool = False,
    ) -> tuple[rules.Origin, str]:
        turn = current_turn.get()
        tz = self._owner.zone
        if turn is not None and event_id:
            turn.events.append(event_id)  # what Pisa's tool touched: named in this device's next turn
        if earlier is None and turn is not None and turn.corrects:
            earlier = turn.corrects  # their correction of Pisa's own change of the turn before
        outside = bool(current().outside())
        if (
            outside
            and turn is not None
            and not current().outside_now()
            and (past := current().outside_past())
            and CORRECTED not in past  # a re-made proposal held after outside text keeps its hold
            and rules.asks_it(turn.words)
            and title
            and (rules.names_item(title, turn.words) or any(rules.names_item(title, w) for w in turn.earlier))
            # a place, or an end they didn't say, from the earlier email keeps the hold
            and (not place or rules.names_item(place, turn.words))
            and (not end_said or end is None or self._end_in(end, turn.words))
        ):
            # for the calendar too. Outside text an EARLIER turn read never holds a change their own command
            # this turn carries (the verb and the values in their words, checked below as for any change),
            # when this turn read nothing outside and the item's name is theirs, this turn or earlier in the
            # talk. Outside text this turn, or a title only the email gave, keeps the hold.
            outside = False
        return rules.grounding(
            kind,
            turn.words if turn is not None else "",  # an unanswered turn's words count
            read_outside=outside,
            attendees=attendees,
            start=start.astimezone(tz).replace(tzinfo=None) if start is not None else None,
            default_day=default_day,
            today=self._owner.day(self._clock()),
            title=title,
            named=bool(turn is not None and event_id and event_id in turn.named_events),
            end=end.astimezone(tz).replace(tzinfo=None) if end is not None else None,
            old_start=old_start.astimezone(tz).replace(tzinfo=None) if old_start is not None else None,
            small_hours=self._small_hours(),
            quote=quote,
            earlier=earlier,
        )

    def _end_in(self, end: datetime, words: str) -> bool:
        """The end's time is in their words."""
        local = end.astimezone(self._owner.zone)
        return rules.says_time_in({"when": f"x {local:%H:%M}"}, words)

    async def _propose_or_act(
        self,
        mode: rules.Origin,
        kind: Any,
        summary: str,
        details: list[str],
        payload: dict[str, Any],
        lang: str,
        *,
        approval_id: uuid.UUID | None = None,
        notified: list[str] | None = None,
        ask: str | None = None,
        did: str | None = None,
        reason: str = "",
    ) -> str:
        """`ask`: the one question as Pisa says it (times as people say them); `did`: what Pisa says once it
        is done; `reason`: why it needs the user's yes (the `needs_yes` feedback). The approval's own summary
        and details keep the exact dates, for the card."""
        device, turn = turn_device(), current_turn.get()
        # a value only outside text gave asks, its source on the card
        if carried_now():
            mode, reason = "held", "a value in it comes from outside text: the user's yes decides"
        approval_id = approval_id or uuid.uuid4()
        payload = payload | {"approval_id": str(approval_id)} | ({"said": did} if did else {})
        if carried_now():  # the card's hash binds that it named the source; its read-out says it
            payload["outside"] = [list(c) for c in carried_now()]
        if turn is not None:  # the words a correction of this proposal may cite (`owner_said`)
            payload["owner_words"] = turn.words[:1000]
        if current().outside():  # a correction of it stays held
            payload["after_outside"] = True
            if MAIL_KINDS & set(current().outside()):  # an email's request may be behind it
                payload["after_mail"] = True
        if kind == "calendar_create" and (payload.get("after_outside") or carried_now()):
            # the event itself carries the mark (its Google tag, read back at every sync, and
            # its row once written): its title and place stay outside text whatever becomes of the approval
            payload["after_outside"] = True
            payload["body"] = _outside_body(payload["body"])
        # RAIL: provenance by code: outside text read first; then the user's words (an act, or one yes for
        # the people invited); "held" is Pisa's reading of words that didn't name it.
        said = decisions.provenance(said=mode != "held", outside=bool(current().outside()))
        # a change Pisa would hold for a yes acts at the user's own level for its kind (`tell`,
        # `silent`); outside text, people notified, money and a stop keep it at `ask` (the gate's rail).
        level = "ask"
        if mode == "held" and self.autonomy is not None and not carried_now():
            # a proposal made after outside text and re-made later (a correction) stays asked
            # Pisa's own lists carry invite and email titles too, and the day's context
            # names today's invites in every turn: after any of them a change Pisa proposes stays asked. A
            # list holding a stranger's title is read as outside text (`invite`, `foreign_titles`), so
            # read_outside holds it; Pisa's own agenda with only their titles doesn't hold
            # `tell`
            outside = (
                bool(current().outside()) or bool(payload.get("after_outside")) or await self._invites_near()
            )
            level = await self.autonomy(
                kind, outside=outside, others=bool(notified), early=self._early(payload)
            )
        if mode == "words" or level != "ask":
            view = await self._approvals.act(
                kind, summary, details, payload, device_id=device, lang=lang, approval_id=approval_id,
                origin="held" if level != "ask" else "words",
                auth=_AUTONOMY_AUTH.get(level, "words"),  # what decided it
                announce=level != "silent",  # at silent, no action_done toast
            )  # fmt: skip
            if level != "ask":
                decisions.note("act", _TOOLS[kind], "pisa_initiative", ref=view["id"], outcome=_outcome(view),
                               rail=f"autonomy {level}")  # fmt: skip
            else:
                decisions.note("act", _TOOLS[kind], said, ref=view["id"], outcome=_outcome(view))
            # their words' or the act counts as done only on a confirming read-back
            if turn is not None and view["status"] == "done" and view["verified"] != "not_verified":
                turn.done.append(view["result"] or summary)
                turn.acted.append(view["id"])
                if kind == "calendar_create":  # Pisa's new event: named for the user's "spostali" next turn
                    turn.events.append(payload["body"]["id"])
            stored = await self._stored(kind, payload) if view["status"] == "done" else None
            return _done(view, did, stored) + _AUTONOMY_SAY.get(level if view["status"] == "done" else "", "")
        view = await self._approvals.create(
            kind,
            summary,
            details,
            payload,
            device_id=device,
            lang=lang,
            approval_id=approval_id,
            origin=mode,
            asked=True,  # made in their turn, in reply to their request
        )
        decisions.note("propose", _TOOLS[kind], said, ref=view["id"], rail=_RAIL[mode])
        question = ask or summary
        question = rules.with_notified(question, notified, lang) if notified else question
        if carried := carried_lines(lang):  # the question they answer names the source too
            question = f"{question.rstrip(' ?')} ({'; '.join(carried)})?"
        if turn is not None:
            turn.approvals.append(view["id"])  # its yes is heard in the user's next turn
        return _waiting(view, question, reason)

    def _early(self, payload: dict[str, Any]) -> bool:
        """A start before 07:00 ("alle 5" may mean 17) is asked at any autonomy level."""
        start = (payload.get("start") or (payload.get("body") or {}).get("start") or {}).get("dateTime")
        if not start:
            return False
        return datetime.fromisoformat(start).astimezone(self._owner.zone).hour < rules.EARLY_HOUR

    async def waiting(self, device_id: uuid.UUID) -> list[dict[str, Any]]:
        """What Pisa asked this device in the last hours and nobody answered (the conversation lists it,
        so it is never treated as done, nor forgotten)."""
        return await self._approvals.unanswered(device_id, UNANSWERED_KEPT)

    async def changed(self, device_id: uuid.UUID, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
        """What Pisa changed for this device in this conversation (its ledger), oldest first."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT a.id, coalesce(a.result, a.summary) AS summary, a.executed_at, a.undone_at "
                "FROM approvals a JOIN conversations c ON c.id = %s WHERE a.device_id = %s "
                "AND a.status = 'done' AND a.executed_at >= c.started_at ORDER BY a.executed_at LIMIT 10",
                (conversation_id, device_id),
            )
            return await cur.fetchall()

    async def bundle(
        self, device_id: uuid.UUID, asked: list[str], reply: str, spoken: bool = True
    ) -> list[str]:
        """The approvals this reply asks as its one question (`_bound`), stamped as read out when the reply
        has been said aloud (their spoken yes counts within 10 s of that: the reply's
        length at 14 characters a second, on a spoken turn). A fresh question renews the 120 s."""
        bound = await self._bound(device_id, asked, reply)
        if spoken:  # a typed reply was never read out aloud
            await self._approvals.read_out(bound, said=reply)
        return bound

    async def read_out_now(self, ids: list[str]) -> None:
        """The talker said Pisa's question aloud just now: its cards are read out now."""
        await self._approvals.read_out(ids, at=self._clock())

    async def _bound(self, device_id: uuid.UUID, asked: list[str], reply: str) -> list[str]:
        """The approvals Pisa's reply asks as its one question: still waiting, and
        named in it (their title). An email's send is its own question: bundled only when it is the one asked.
        Their one yes runs exactly these; anything else goes to the model."""
        rows = await self._approvals.pending_for(device_id, asked)
        # a send is asked by voice only when this reply said its whole stored text
        # (the user heard what "sì" sends); else the card (a tap) sends it, never a yes
        heard = [r for r in rows if r["kind"] == "email_send" and await self._heard(r, reply)]
        # "… alle 16 e avviso Anna, e «Palestra» alle 17. Dimmi sì e lo faccio."
        # asks their yes in words as much as a "?" does; what it binds is still each change named with its
        # time
        if "?" not in reply and not claim_rules.asks_yes(reply):
            return []
        mail = [str(r["id"]) for r in heard]
        if any(r["kind"] == "email_send" for r in rows) and not mail:
            return []
        named = [
            str(r["id"]) for r in rows
            if r["kind"] != "email_send" and rules.names_item(str(r["payload"].get("title") or ""), reply)
            and rules.says_change(r["kind"], r["payload"], reply)
        ]  # fmt: skip
        if mail:
            return mail if len(mail) == 1 and len(rows) == 1 else []
        # one question over all of this turn's held changes ("Sposto tutto di un'ora: li
        # sposto entrambi e avviso Anna?") binds all of them: one question, one yes
        held_rows = [r for r in rows if r["kind"] != "email_send"]
        held = [str(r["id"]) for r in held_rows]
        # "tutti e due?" never binds a cancel it didn't say (nor a cancel verb a move)
        if rules.asks_them_all(reply, len(held)) and all(
            rules.verb_fits(r["kind"], reply) and rules.series_fits(r["payload"], reply) for r in held_rows
        ):  # "li aggiungo entrambi?" never binds a series it didn't say repeats
            # a card held after an email joins "tutti e due?" only when this reply
            # binds it as Pisa's own act with its time: a yes to another proposal ("Marco chiede di spostare
            # la palestra. Sposto la call alle 16: li sposto entrambi?") never applies the email's change. An
            # invite's title on their own calendar asks nothing: a yes after their agenda still binds both
            return [
                str(r["id"]) for r in held_rows if not r["payload"].get("after_mail") or str(r["id"]) in named
            ]
        return named

    async def _heard(self, row: dict[str, Any], reply: str) -> bool:
        """This reply says the draft's whole stored body (whitespace, case and quote
        marks aside); then the approval is marked heard, and only then does a voice yes send it."""
        # quoted outside text, or far past their words
        if row["payload"].get("voice") is False:
            return False
        draft = await self._draft(str(row["payload"].get("draft_id")))
        if isinstance(draft, str) or draft["version"] != row["payload"].get("version"):
            return False
        if not rules.said_whole(str(draft["body"] or ""), reply):
            return False
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE approvals SET payload = payload || '{\"heard\": true}'::jsonb WHERE id = %s",
                (row["id"],),
            )
        return True

    async def _invites_near(self) -> bool:
        """An event someone else made or controls (pisa/services/foreign.py) in the day's context window
        (today and the next
        two days): its title is in every turn's instructions, so Pisa's earned autonomy waits."""
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    # constant SQL fragments, no input
                    f"SELECT 1 FROM {foreign.EVENTS} WHERE {foreign.FOREIGN_EVENT} "  # noqa: S608
                    "AND e.status <> 'cancelled' AND e.ends_at > %s AND e.starts_at < %s LIMIT 1",
                    (now, now + timedelta(days=3)),
                )
            ).fetchone()
        return row is not None

    async def _replace_pending(
        self, kind: str, title: str, replaces: str | None = None
    ) -> tuple[str | None, bool]:
        """The device's proposal of `kind` for the same title, still waiting for a yes, is withdrawn: the tool
        being called re-makes it with the user's change; so is the one `replaces` names by its id
        (a correction under another title). The user's words of the turn that asked it (a
        correction may cite them), or None; and whether it was held after outside text."""
        device = turn_device()
        if device is None:
            return None, False
        earlier, outside = None, False
        # The same title only: "Call con Alessandro" never withdraws "Call con Marco", asked in the same
        # breath.
        target = (replaces or "").replace("-", "").lower()[:8]
        turn = current_turn.get()
        # a create after outside text (this turn's, or an earlier one's still in the talk)
        # never withdraws their own proposal (one made after none), by title or by id, unless their own words
        # this turn name the item: the stranger's text never takes their question away
        from_outside = bool(current().outside()) or bool(carried_now())
        his_words = turn is not None and rules.names_item(title, turn.words)
        for row in await self._approvals.pending_of(device, kind):
            if from_outside and not row["payload"].get("after_outside") and not his_words:
                continue
            named = bool(target) and results.short(row["id"]) == target
            if named or title_key(title) == title_key(str(row["payload"].get("title") or "")):
                await self._approvals.supersede(str(row["id"]))
                earlier = row["payload"].get("owner_words") or earlier
                outside = outside or bool(row["payload"].get("after_outside"))
        return earlier, outside

    async def _proposal_named(self, reference: str) -> dict[str, Any] | None:
        """The device's one new-event proposal still waiting that the user's words name (a "sposta"
        or "cancella" about something Pisa only proposed), or None."""
        device = turn_device()
        if device is None:
            return None
        rows = await self._approvals.pending_of(device, "calendar_create")
        named = [
            r for r in rows if match_score(reference, str(r["payload"].get("title") or "")) >= MATCH_THRESHOLD
        ]
        return named[0] if len(named) == 1 else None

    async def _remake(self, proposal: dict[str, Any], start: str, owner_said: str | None) -> str:
        """A proposal moved before it was done: the same event, re-made at the new start with its length (one
        yes if still needed; the old question is withdrawn)."""
        body = proposal["payload"]["body"]
        begin = datetime.fromisoformat(body["start"]["dateTime"])
        minutes = int((datetime.fromisoformat(body["end"]["dateTime"]) - begin).total_seconds() // 60)
        title = str(proposal["payload"].get("title") or body.get("summary") or "?")
        calendar = None  # the proposal's own calendar, never the primary instead
        if (calendar_id := proposal["payload"].get("calendar_id")) not in (None, "primary"):
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute("SELECT name FROM calendars WHERE id = %s", (calendar_id,))
                ).fetchone()
            calendar = row[0] if row else None
        return await self.create_event(
            title, start, None, minutes, body.get("location"), calendar, owner_said=owner_said
        )

    async def _stored_after(self, kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The calendar event an approval touched, read back now; None for another kind."""
        if kind not in ("calendar_create", "calendar_move", "calendar_cancel"):
            return None
        return await self._stored(kind, payload)

    async def _stored(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The event as Google has it now, read back after the write (what the model may say)."""
        event_id = payload["body"]["id"] if kind == "calendar_create" else payload["event_id"]
        try:
            live = await self._calendar().event(payload["calendar_id"], event_id)
        except ConnectorError:
            return {"read_back": "failed: say only what the receipt says"}
        if live is None or live.get("status") == "cancelled":
            return {"event": event_id[:12], "status": "cancelled"}
        tz = self._owner.zone
        when = {k: self._local(live[k]["dateTime"]).astimezone(tz).strftime("%a %Y-%m-%d %H:%M")
                for k in ("start", "end") if "dateTime" in (live.get(k) or {})}  # fmt: skip
        created = kind == "calendar_create"
        if payload.get("after_outside") or await self._is_foreign(event_id, created=created):
            # the live title of an event a stranger controls is never read back into Pisa's
            # own result (the model already names the event; Google's copy may say anything by now)
            return {"title": "(its title, written by others: not repeated)", "outside": True, **when}
        return {"title": rules.clean(str(live.get("summary") or "?")), **when}

    def _spoken(self, start: datetime, lang: str) -> str:
        return rules.spoken_when(
            start, self._owner.day(self._clock()), lang, self._owner.tz, small_hours=self._small_hours()
        )

    @staticmethod
    def _said_done(sentence: str, view: dict[str, Any], lang: str) -> str:
        """What Pisa says when a change is done: the sentence, and "Se vuoi lo annullo" when it can be
        undone."""
        return f"{sentence} {rules.word(lang, 'undo_hint')}" if view["undo_until"] else sentence

    async def _find_event(
        self, reference: str, day: str | None, *, since: datetime
    ) -> tuple[str, str, str] | str:
        """(calendar id, event id, calendar name) of the event the user means, or what to say instead: one
        ending after `since`, in the next 14 days, in a calendar the user owns."""
        now = self._clock()
        until = now + timedelta(days=FIND_EVENTS_DAYS)
        on: Any = None
        if day:
            when, _ = parse_when(day, self._owner.day(self._clock()), small_hours=self._small_hours())
            on = datetime.fromisoformat(when[:10]).date() if when else None
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT e.calendar_id, e.id, e.title, e.starts_at, c.name, c.access_role, "  # noqa: S608
                    f"coalesce({foreign.FOREIGN_EVENT}, true) FROM {foreign.EVENTS} "
                    "WHERE e.status <> 'cancelled' "
                    "AND e.ends_at > %s AND e.starts_at < %s ORDER BY e.starts_at",
                    (since, until),
                )
            ).fetchall()
            # the events Pisa itself created lately, before the calendar's next sync caches them ("no,
            # intendevo sabato" right after "metti tutto sul calendario").
            # at its latest start after Pisa's own move, gone after Pisa's cancel, and never once the
            # calendar holds it (the sync's row, whatever its status, is the truth).
            made = await (
                await conn.execute(
                    "SELECT a.payload->>'calendar_id', a.payload->'body'->>'id', a.payload->>'title', "
                    "coalesce((SELECT (m.payload->'start'->>'dateTime')::timestamptz FROM approvals m "
                    "WHERE m.kind = 'calendar_move' AND m.status = 'done' AND m.undone_at IS NULL "
                    "AND m.payload->>'event_id' = a.payload->'body'->>'id' "
                    "ORDER BY m.executed_at DESC LIMIT 1), "
                    "(a.payload->'body'->'start'->>'dateTime')::timestamptz), "
                    "coalesce(c.name, a.payload->>'calendar_id'), coalesce(c.access_role, 'owner'), "
                    "coalesce((a.payload->>'after_outside')::boolean, false) OR a.payload ? 'outside' "
                    "FROM approvals a LEFT JOIN calendars c ON c.id = a.payload->>'calendar_id' "
                    "WHERE a.kind = 'calendar_create' AND a.status = 'done' AND a.undone_at IS NULL "
                    "AND a.executed_at > %s AND NOT EXISTS (SELECT 1 FROM approvals x WHERE "
                    "x.kind = 'calendar_cancel' AND x.status = 'done' AND x.undone_at IS NULL "
                    "AND x.payload->>'event_id' = a.payload->'body'->>'id') AND NOT EXISTS (SELECT 1 "
                    "FROM calendar_events e WHERE e.id = a.payload->'body'->>'id')",
                    (now - timedelta(days=1),),
                )
            ).fetchall()
        known = {r[1] for r in rows}
        rows += [r for r in made if r[1] not in known and r[3] < until]
        tz = self._owner.zone
        if on is not None:
            # the day
            # an event is on, said as today's weekday, is today too (two that match ask which)
            days = (
                {on, self._owner.day(self._clock())}
                if on == self._owner.day(self._clock()) + timedelta(days=7)
                else {on}
            )
            rows = [r for r in rows if r[3].astimezone(tz).date() in days]
        scored = sorted(((match_score(reference, r[2]), r) for r in rows), key=lambda sr: -sr[0])
        plausible = [(s, r) for s, r in scored if s >= MATCH_THRESHOLD]
        if not plausible:
            return f"No event in the next {FIND_EVENTS_DAYS} days matches {reference!r}. Ask the user which."
        best = plausible[0][0]
        close = [r for s, r in plausible if best - s < CLEAR_MARGIN]
        if len(close) > 1:
            listed = "; ".join(
                f"{rules.clean(r[2])} ({r[3].astimezone(tz):%a %d %b %H:%M})" for r in close[:4]
            )
            if any(r[6] for r in close[:4]):  # a stranger's title listed: outside text
                current().read_foreign(listed)
            return f"Several events match: {listed}. Ask the user which one (a day narrows it); don't guess."
        calendar_id, event_id, _, _, calendar_name, role, _ = close[0]
        if role != "owner":
            return _not_owned(calendar_name)
        return calendar_id, event_id, calendar_name

    async def _known_event(self, calendar_id: str, event_id: str) -> tuple[str, str, str] | str:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT name FROM calendars WHERE id = %s", (calendar_id,))
            ).fetchone()
        if row is None and calendar_id == "primary":  # an event Pisa made in the primary calendar
            return calendar_id, event_id, await self._google.account_email() or "primary"
        return (calendar_id, event_id, row[0]) if row else "That calendar is no longer connected."

    async def _owned_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | str:
        try:
            live = await self._calendar().event(calendar_id, event_id)
        except ConnectorError as exc:
            raise PisaError("provider_error", exc.message, "Try again in a minute") from None
        if live is None or live.get("status") == "cancelled":
            return "That event is no longer in the user's calendar. Nothing was asked."
        if not rules.organised_by_owner(live):
            return (
                f"Pisa changes only events the user organises: this one was organised by "
                f"{rules.organiser_name(live)}. Nothing was asked; the user can ask them to change it."
            )
        return live

    # --- calendar: execute and undo -----------------------------------------------------------------------

    async def _unchanged(self, payload: dict[str, Any]) -> dict[str, Any]:
        live = await self._calendar().event(payload["calendar_id"], payload["event_id"])
        if live is None or live.get("status") == "cancelled":
            raise PisaError(
                "not_found",
                f"The event «{payload['title']}» is no longer in the calendar: nothing was done.",
                "",
            )
        if payload.get("updated") and live.get("updated") != payload["updated"]:
            raise PisaError(
                "plan_changed",
                f"The event «{payload['title']}» changed in the calendar after Pisa asked: nothing was done.",
                "Ask Pisa again",
            )
        return live

    async def _do_create(self, payload: dict[str, Any]) -> Outcome:
        lang = payload["lang"]
        try:
            event = await self._calendar().insert(
                payload["calendar_id"], payload["body"], payload["send_updates"]
            )
        except ConnectorError as exc:
            if exc.status != 409:  # 409: an event with this approval's id exists: it was created already
                raise _unclear(exc, lang, "calendar") from None
            event = await self._calendar().event(payload["calendar_id"], payload["body"]["id"]) or {}
        if payload.get("after_outside"):
            await self._mark_outside(payload["body"]["id"])
        undo = {"op": "delete", "calendar_id": payload["calendar_id"], "event_id": payload["body"]["id"],
                "title": payload["title"], "lang": lang, "send_updates": "none",
                "updated": event.get("updated"), "etag": event.get("etag")}  # fmt: skip
        when = payload["when"] + (f", {payload['repeat_said']}" if payload.get("repeat_said") else "")
        said = rules.word(lang, "created", title=payload["title"], when=when)
        return Outcome(said, undo, then=self._calendar_changed)

    async def _do_move(self, payload: dict[str, Any]) -> Outcome:
        lang = payload["lang"]
        try:
            await self._unchanged(payload)
            body = {"start": payload["start"], "end": payload["end"]}
            if payload.get("approval_id"):
                body["extendedProperties"] = {"private": _tag(payload["approval_id"])}
                if await self._is_outside(payload["event_id"]):
                    body["extendedProperties"]["private"][OUTSIDE_TAG] = "1"  # kept on a move
            moved = await self._calendar().patch(
                payload["calendar_id"],
                payload["event_id"],
                body,
                payload["send_updates"],
                payload.get("etag"),
            )
        except ConnectorError as exc:
            raise _changed_or_unclear(exc, lang, payload["title"]) from None
        undo = {"op": "patch", "calendar_id": payload["calendar_id"], "event_id": payload["event_id"],
                "body": payload["before"], "title": payload["title"], "lang": lang,
                "when": payload["old_when"], "send_updates": payload["send_updates"],
                "updated": moved.get("updated"), "etag": moved.get("etag")}  # fmt: skip
        if payload.get("plan"):  # an option of Pisa's: one Undo puts Pisa's plan back too
            undo["plan_approval"] = payload.get("approval_id")
        said = rules.word(lang, "moved", title=payload["title"], when=payload["when"])
        if (option := payload.get("option")) and self.apply_option is not None:
            try:  # the user's yes to the whole option: Pisa's plan follows the calendar now
                await self.apply_option(option["options_id"], option["option_id"], payload["approval_id"])
            except PisaError as err:  # options gone or the day changed: the sync re-plans from the calendar
                _log.warning("option_after_yes_failed", code=err.code)
        return Outcome(said, undo, then=self._calendar_changed)

    async def _do_cancel(self, payload: dict[str, Any]) -> Outcome:
        lang = payload["lang"]
        try:
            await self._unchanged(payload)
            writer = self._calendar()
            await writer.delete(
                payload["calendar_id"], payload["event_id"], payload["send_updates"], payload.get("etag")
            )
            gone = await writer.event(
                payload["calendar_id"], payload["event_id"]
            )  # Google keeps it, cancelled
        except ConnectorError as exc:
            raise _changed_or_unclear(exc, lang, payload["title"]) from None
        undo = {"op": "restore", "calendar_id": payload["calendar_id"], "event_id": payload["event_id"],
                "title": payload["title"], "lang": lang, "send_updates": payload["send_updates"],
                "updated": (gone or {}).get("updated"), "etag": (gone or {}).get("etag")}  # fmt: skip
        return Outcome(
            rules.word(lang, "cancelled", title=payload["title"]), undo, then=self._calendar_changed
        )

    # --- the read-back after acting -------------------------------------------------------------------------

    async def _read_calendar(self, payload: dict[str, Any], _: Outcome) -> bool:
        """The event as Google holds it now starts when the card said."""
        kind = "calendar_create" if "body" in payload else "calendar_move"
        event_id = payload["body"]["id"] if kind == "calendar_create" else payload["event_id"]
        live = await self._calendar().event(payload["calendar_id"], event_id)
        return yescard.calendar_confirms(kind, payload, live)

    async def _read_cancel(self, payload: dict[str, Any], _: Outcome) -> bool:
        live = await self._calendar().event(payload["calendar_id"], payload["event_id"])
        return yescard.calendar_confirms("calendar_cancel", payload, live)

    async def _read_sent(self, _: dict[str, Any], outcome: Outcome) -> bool:
        """Gmail holds the message it answered with as sent."""
        message_id = str((outcome.receipt or {}).get("message_id") or "")
        return bool(message_id) and await self._mail().sent(message_id)

    async def _undo(self, undo: dict[str, Any]) -> str:
        """Put it back as it was, if nobody changed it since Pisa's change: its etag (If-Match: a change made
        in Google meanwhile answers 412), and its `updated` for changes stored before etags."""
        writer, lang, title = self._calendar(), undo["lang"], undo["title"]
        calendar_id, event_id, updates = undo["calendar_id"], undo["event_id"], undo["send_updates"]
        etag = undo.get("etag")
        changed = PisaError(
            "plan_changed", rules.word(lang, "undo_changed", title=title), "Ask Pisa to change it"
        )
        try:
            live = await writer.event(calendar_id, event_id)
            if live is None or (undo.get("updated") and live.get("updated") != undo["updated"]):
                raise changed
            if undo["op"] == "delete":
                await writer.delete(calendar_id, event_id, updates, etag)
                said = rules.word(lang, "undo_created", title=title)
            elif undo["op"] == "patch":
                await writer.patch(calendar_id, event_id, undo["body"], updates, etag)
                said = rules.word(lang, "undo_moved", title=title, when=undo["when"])
            else:  # a cancelled event comes back as it was (Google keeps it, cancelled)
                await writer.patch(calendar_id, event_id, {"status": "confirmed"}, updates, etag)
                key = "undo_cancelled_invited" if updates == "all" else "undo_cancelled"
                said = rules.word(lang, key, title=title)
        except ConnectorError as exc:
            if exc.status == 412:
                raise changed from None
            raise _unclear(exc, lang, "calendar") from None
        if (approval := undo.get("plan_approval")) and self.undo_option is not None:
            await self.undo_option(str(approval))  # one Undo: Pisa's plan goes back with the calendar
        self._approvals.later(self._calendar_changed())
        return said

    async def _mark_outside(self, event_id: str) -> None:
        """Pisa's event made after outside text is marked on its row at write time (the sync that
        follows reads the same mark from its Google tag, and keeps it); a series' instances too."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE calendar_events SET outside = true WHERE id = %s OR recurring_event_id = %s",
                (event_id, event_id),
            )
            # the durable ledger (0110), even before the sync has a row: the event and, for a series, its id
            # as its instances' series
            await conn.execute(
                "INSERT INTO event_outside (event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,)
            )

    async def _is_foreign(self, event_id: str, *, created: bool) -> bool:
        """Pisa's copy of the event has a title a stranger controls. With no row: False only for
        Pisa's own create just made (no sync yet; the payload's `after_outside` decides it then); for a move,
        a cancel or an undo, unknown is foreign (the post-write sync may already have deleted the
        row of an invite moved out of the window)."""
        async with self._pool.connection() as conn:
            if not created:
                return await foreign.event_foreign(conn, [event_id])
            row = await (
                await conn.execute(
                    f"SELECT bool_or({foreign.FOREIGN_EVENT}) FROM {foreign.EVENTS} WHERE e.id = %s",  # noqa: S608
                    (event_id,),
                )
            ).fetchone()
        return bool(row and row[0])

    async def _is_outside(self, event_id: str) -> bool:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM calendar_events WHERE id = %s AND outside LIMIT 1", (event_id,)
                )
            ).fetchone()
        return row is not None

    async def _calendar_changed(self) -> None:
        """Sync the calendar and re-plan: Pisa's own view follows the change at once (a failure is logged; the
        next sync catches up)."""
        if self.after_calendar_change is None:
            return
        try:
            await self.after_calendar_change()
        except Exception:
            _log.exception("after_calendar_change_failed")

    # --- email --------------------------------------------------------------------------------------------

    async def draft_reply(self, email: str, text: str) -> str:
        await self._google.require(oauth.SCOPE_GMAIL_COMPOSE)
        body = text.strip()
        if not body:
            raise PisaError("validation_failed", "The reply needs a text.", "")
        found = await self._find_email(email)
        if isinstance(found, str):
            return found
        message_id, thread_id = found
        owner = (await self._google.account_email() or "").lower()
        mail = self._mail()
        try:
            headers = await mail.headers(message_id)
        except ConnectorError as exc:
            raise _failed(exc) from None
        to, cc = rules.reply_recipients(headers.from_, headers.reply_to, headers.to, headers.cc, {owner})
        if not to:
            return "Not drafted: Pisa found nobody to reply to in that email's thread."
        try:
            participants = await mail.participants(thread_id)
        except ConnectorError as exc:
            raise _failed(exc) from None
        # a Reply-To that isn't one of the thread's people is an outsider, like an added address
        # (Face ID, named in the approval).
        outsiders = [a for _, a in to + cc if a not in participants]
        subject = rules.reply_subject(headers.subject)
        raw = rules.reply_raw(sender=owner, to=to, cc=cc, subject=subject, body=body,
                              in_reply_to=headers.message_id, references=headers.references)  # fmt: skip
        try:
            gmail_draft_id = await mail.create(thread_id, raw)
        except ConnectorError as exc:
            raise _failed(exc) from None
        draft_id = uuid.uuid4()
        now = self._clock()
        async with self._pool.connection() as conn:
            older = await (
                await conn.execute(
                    "DELETE FROM email_drafts WHERE thread_id = %s AND sent_at IS NULL "
                    "RETURNING gmail_draft_id, approval_id",
                    (thread_id,),
                )
            ).fetchall()
            await conn.execute(
                "INSERT INTO email_drafts (id, gmail_draft_id, thread_id, reply_to_id, to_addresses, "
                "cc_addresses, outsiders, subject, body, created_at, updated_at, in_reply_to, "
                "references_header) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (draft_id, gmail_draft_id, thread_id, message_id, [_fmt(p) for p in to],
                 [_fmt(p) for p in cc], outsiders, subject, body, now, now, headers.message_id,
                 headers.references),
            )  # fmt: skip
        # Pisa's older drafts of the thread: Gmail would show them as people
        for old_id, old_approval in older:
            if old_approval is not None:
                await self._approvals.supersede(str(old_approval))
            try:
                await mail.delete(old_id)
            except ConnectorError as exc:
                _log.warning("email_old_draft_kept", status=exc.status)
        _log.info("email_draft_saved", draft_id=str(draft_id), outsiders=len(outsiders))
        await self._propose_send(draft_id)
        return await self._read_back(draft_id, "Draft saved in Gmail, in the thread: " + WAITING)

    async def draft_email(self, to: str, subject: str, text: str) -> str:
        """A new email, not a reply. `to`: an
        address the user said, or a name of someone in their mail (the latest address that name wrote from).
        Saved in Gmail, nothing sent; read back once, one yes sends it. An address the user didn't
        say and none of their correspondents uses is an outsider (Face ID, named in the approval)."""
        await self._google.require(oauth.SCOPE_GMAIL_COMPOSE)
        body, subject = text.strip(), " ".join(subject.split())[:200]
        if not body or not subject:
            raise PisaError("validation_failed", "A new email needs a subject and a text.", "")
        turn = current_turn.get()
        said = rules.addresses_said(turn.words) if turn is not None else set()
        lang = await self._owner.lang(self._pool)
        async with self._pool.connection() as conn:
            picked = await _new_recipient(conn, to, said, lang)
        if isinstance(picked, str):
            return picked
        name, address, outsider = picked
        owner = (await self._google.account_email() or "").lower()
        raw = rules.new_raw(sender=owner, to=[(name, address)], subject=subject, body=body)
        mail = self._mail()
        try:
            gmail_draft_id = await mail.create(None, raw)
        except ConnectorError as exc:
            raise _failed(exc) from None
        draft_id, now = uuid.uuid4(), self._clock()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO email_drafts (id, gmail_draft_id, thread_id, reply_to_id, to_addresses, "
                "outsiders, subject, body, created_at, updated_at, in_reply_to) "
                "VALUES (%s, %s, NULL, NULL, %s, %s, %s, %s, %s, %s, '')",
                (draft_id, gmail_draft_id, [_fmt((name, address))], [address] if outsider else [], subject,
                 body, now, now),
            )  # fmt: skip
        _log.info("email_new_draft_saved", draft_id=str(draft_id), outsiders=int(outsider))
        await self._propose_send(draft_id)
        said = await self._read_back(draft_id, "Draft saved in Gmail, a new email: " + WAITING)
        # they are away and it is night at home, where their people are: say their time, ask before sending
        if (
            theirs := zone.their_time(now, self._owner.tz, self._owner.home, self._owner.hour_cycle())
        ) is not None:
            said += (
                f" It is {theirs} at home now (the user is away): say their time and ask whether to send it "
                "now or wait for their morning (Pisa can't schedule a send: they say when)."
            )
        return said

    async def draft_package(
        self, to: str, subject: str, text: str, files: list[tuple[str, str, bytes]]
    ) -> str:
        """A new email with Pisa's attachments (the accountant package). Saved in Gmail as a
        draft, nothing sent; it goes only through the approval path, and always with Face ID on the card
        (documents leave with it). The draft stays a draft until then."""
        await self._google.require(oauth.SCOPE_GMAIL_COMPOSE)
        body, subject = text.strip(), " ".join(subject.split())[:200]
        if not body or not subject:
            raise PisaError("validation_failed", "A new email needs a subject and a text.", "")
        turn = current_turn.get()
        said = rules.addresses_said(turn.words) if turn is not None else set()
        lang = await self._owner.lang(self._pool)
        async with self._pool.connection() as conn:
            picked = await _new_recipient(conn, to, said, lang)
        if isinstance(picked, str):
            return picked
        name, address, outsider = picked
        # documents go only to an address they said this turn or has written to; a name matched
        # against who wrote to them ("Studio Commercialista <evil@x>") is never enough
        if outsider and address.lower() not in said:
            return (
                f"Not drafted: the user never wrote to {address}. Ask them for the accountant's address in "
                "their own words."
            )
        owner = (await self._google.account_email() or "").lower()
        raw = rules.package_raw(sender=owner, to=[(name, address)], subject=subject, body=body, files=files)
        try:
            gmail_draft_id = await self._mail().create(None, raw)
        except ConnectorError as exc:
            raise _failed(exc) from None
        attached = [
            {"filename": f, "size": len(data), "sign": rules.attachment_sign(f, data)} for f, _, data in files
        ]
        draft_id, now = uuid.uuid4(), self._clock()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO email_drafts (id, gmail_draft_id, thread_id, reply_to_id, to_addresses, "
                "outsiders, subject, body, created_at, updated_at, in_reply_to, attachments) "
                "VALUES (%s, %s, NULL, NULL, %s, %s, %s, %s, %s, %s, '', %s)",
                (draft_id, gmail_draft_id, [_fmt((name, address))], [address] if outsider else [], subject,
                 body, now, now, Jsonb(attached)),
            )  # fmt: skip
        _log.info("email_package_draft_saved", draft_id=str(draft_id), files=len(files))
        await self._propose_send(draft_id)
        return await self._read_back(
            draft_id, f"Draft saved in Gmail, a new email with {len(files)} attachments: {WAITING}"
        )

    async def _find_email(self, reference: str) -> tuple[str, str] | str:
        since = self._clock() - timedelta(days=FIND_EMAIL_DAYS)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, thread_id, coalesce(from_name, '') || ' ' || coalesce(from_address, '') || "
                    "' ' "
                    "|| subject, sent_at FROM email_messages WHERE sent_at > %s AND NOT (labels && %s) "
                    "ORDER BY sent_at DESC",
                    (since, list(NEVER_TO_MODEL_LABELS)),
                )
            ).fetchall()
        scored = sorted(((match_score(reference, r[2]), r) for r in rows), key=lambda sr: -sr[0])
        if not scored or scored[0][0] < MATCH_THRESHOLD:
            return f"No email of the last {FIND_EMAIL_DAYS} days matches {reference!r}. Ask the user which."
        best = scored[0][0]
        threads = {r[1] for s, r in scored if best - s < CLEAR_MARGIN}
        if len(threads) > 1:
            listed = "; ".join(dict.fromkeys(r[2][:80] for s, r in scored if best - s < CLEAR_MARGIN))
            return f"Several email threads match: {listed}. Ask the user which one; don't guess."
        top = scored[0][1]  # the newest message of that thread among the best (rows are newest first)
        return top[0], top[1]

    async def draft_view(self, draft_id: str) -> dict[str, Any]:
        """One of Pisa's drafts by id, for the phone's `draft:<id>` object: its
        recipients, subject and whole text, as the approval's details show them; sent ones too."""
        try:
            key = uuid.UUID(draft_id)
        except ValueError:
            raise PisaError("not_found", "There is no draft with that id.", "Check the id") from None
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT * FROM email_drafts WHERE id = %s", (key,))
            row = await cur.fetchone()
        if row is None:
            raise PisaError(
                "not_found", "There is no draft with that id (drafts are kept 90 days).", "Check the id"
            )
        tz = self._owner.zone
        return {
            "id": str(row["id"]), "subject": row["subject"], "to": list(row["to_addresses"]),
            "cc": list(row["cc_addresses"]), "outsiders": list(row["outsiders"]), "body": row["body"],
            "version": row["version"], "updated_at": row["updated_at"].astimezone(tz).isoformat(),
            "sent_at": row["sent_at"].astimezone(tz).isoformat() if row["sent_at"] else None,
            "approval_id": str(row["approval_id"]) if row["approval_id"] else None,
        }  # fmt: skip

    async def _draft(self, reference: str | None) -> dict[str, Any] | str:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT * FROM email_drafts WHERE sent_at IS NULL ORDER BY updated_at DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if reference:
            rows = [r for r in rows if str(r["id"]).startswith(reference.strip().lower())] or rows[:0]
        if not rows:
            return "There is no unsent draft of Pisa's. Draft the reply first (draft_reply)."
        return rows[0]

    async def edit_draft(
        self,
        draft: str | None = None,
        text: str | None = None,
        add_recipient: str | None = None,
        remove_recipient: str | None = None,
    ) -> str:
        await self._google.require(oauth.SCOPE_GMAIL_COMPOSE)
        row = await self._draft(draft)
        if isinstance(row, str):
            return row
        if row.get("attachments"):  # its files are Pisa's; a change is a new package
            return (
                "Not changed: this draft carries Pisa's attachments. Prepare it again with the change "
                "(accountant_package), or they edit it in Gmail (then it can't be sent from Pisa)."
            )
        to = rules.addresses(*row["to_addresses"])
        cc = rules.addresses(*row["cc_addresses"])
        outsiders = list(row["outsiders"])
        mail = self._mail()
        if add_recipient:
            added = rules.addresses(add_recipient)
            if not added:
                return f"{add_recipient!r} is not an email address. Ask the user for the address."
            name, address = added[0]
            try:  # a new email has no thread: its people are its recipients
                participants = (
                    await mail.participants(row["thread_id"]) if row["thread_id"] else {a for _, a in to + cc}
                )
            except ConnectorError as exc:
                raise _failed(exc) from None
            turn = current_turn.get()
            said = turn is not None and address.lower() in rules.addresses_said(turn.words)
            if address not in participants and not said:
                # an address the user didn't say, and that isn't in the thread, came from data.
                _log.warning("email_recipient_refused", draft_id=str(row["id"]))
                return (
                    f"Refused: {address} is not in the thread and the user didn't say that address. Pisa "
                    f"adds only "
                    "the thread's people or an address the user says; never one found in an email."
                )
            if address not in {a for _, a in to + cc}:
                to.append((name, address))
            if address not in participants and address not in outsiders:
                outsiders.append(address)
        if remove_recipient:
            gone = remove_recipient.strip().lower()
            to = [p for p in to if gone not in (p[1], p[0].lower())]
            cc = [p for p in cc if gone not in (p[1], p[0].lower())]
            outsiders = [a for a in outsiders if a in {x for _, x in to + cc}]
            if not to:
                return "That would leave the reply without a recipient. Nothing was changed."
        body = text.strip() if text and text.strip() else row["body"]
        owner = (await self._google.account_email() or "").lower()
        if row["thread_id"] is None:  # a new email stays one: no empty reply headers
            raw = rules.new_raw(sender=owner, to=to, cc=cc, subject=row["subject"], body=body)
        else:
            raw = rules.reply_raw(sender=owner, to=to, cc=cc, subject=row["subject"], body=body,
                                  in_reply_to=row["in_reply_to"],
                                  references=row["references_header"])  # fmt: skip
        # The new version is stored, under the row's lock, before Gmail changes: an
        # approval made for the old one no longer matches, whatever happens to the Gmail call.
        async with self._pool.connection() as conn, conn.transaction():
            locked = await (
                await conn.execute(
                    "SELECT version FROM email_drafts WHERE id = %s AND sent_at IS NULL FOR UPDATE",
                    (row["id"],),
                )
            ).fetchone()
            if locked is None or locked[0] != row["version"]:
                return "The draft changed or was sent meanwhile. Nothing was changed; read it again."
            await conn.execute(
                "UPDATE email_drafts SET to_addresses = %s, cc_addresses = %s, outsiders = %s, body = %s, "
                "updated_at = %s, version = version + 1 WHERE id = %s",
                ([_fmt(p) for p in to], [_fmt(p) for p in cc], outsiders, body, self._clock(), row["id"]),
            )
        try:
            await mail.update(row["gmail_draft_id"], row["thread_id"], raw)
        except ConnectorError as exc:
            raise _failed(exc) from None
        _log.info("email_draft_edited", draft_id=str(row["id"]))
        if row["approval_id"] is not None:  # the old version's approval: a later "sì" answers only this one
            await self._approvals.supersede(str(row["approval_id"]))
        await self._propose_send(row["id"])
        return await self._read_back(row["id"], "Draft changed: " + WAITING)

    async def _read_back(self, draft_id: Any, said: str) -> str:
        row = await self._draft(str(draft_id))
        assert not isinstance(row, str)
        to, cc = rules.addresses(*row["to_addresses"]), rules.addresses(*row["cc_addresses"])
        outside = (
            f" Outside the thread (Face ID to send): {', '.join(row['outsiders'])}."
            if row["outsiders"]
            else ""
        )
        # a new email's read-back says the address ("Claudio Rossi <claudio@x.it>")
        new_email = row["thread_id"] is None
        # a reply to several people, or to someone they never wrote to, says every
        # address (reply-all lets a thread's sender choose who "the thread" is)
        full = new_email or len(to) + len(cc) > 1 or not await self._wrote_to([a for _, a in to])
        shown = ", ".join(rules.shown_address(n, a) for n, a in to) if full else rules.names(to)
        shown_cc = ", ".join(rules.shown_address(n, a) for n, a in cc) if full else rules.names(cc)
        files = [str(a["filename"]) for a in row.get("attachments") or []]
        if files:
            outside += (
                f" Attachments ({len(files)}): {', '.join(files)}. It is sent only from the card on the "
                "phone, with Face ID (documents leave with it): say so after the read-back."
            )
        # read back from the store: what their yes will do, never "done" before it
        return (
            f"{said} Their yes sends it, as stored: draft {str(row['id'])[:8]}. To: {shown}"
            + (f"; Cc: {shown_cc}" if cc else "")
            + f". Subject: {row['subject']}. Text: {row['body']}{outside} Read it back to the user, "
            "the whole text word for word (a shortened read-back can't be sent by voice), and end with one "
            'question, "Lo mando?" ("Shall I send it?"): their yes sends exactly this version '
            "(the read-back is the one approval; never ask again after it)."
        )

    async def _wrote_to(self, addresses: list[str]) -> bool:
        """The user wrote to every one of them before (their correspondents)."""
        async with self._pool.connection() as conn:
            kept = await (await conn.execute("SELECT lower(address) FROM email_correspondents")).fetchall()
            sent = await (
                await conn.execute("SELECT to_addresses FROM email_messages WHERE 'SENT' = ANY(labels)")
            ).fetchall()
        written = {a for (a,) in kept} | {
            a for _, a in rules.addresses(*(x for (p,) in sent for x in p or []))
        }
        return bool(addresses) and all(a.lower() in written for a in addresses)

    async def send_draft(self, draft: str | None = None) -> str:
        """The user said to send it ("mandala", "send it"). After the read-back of its latest
        version to this device, those words are the yes: sent at once. Without that read-back, the proposal
        (one yes)."""
        await self._google.require(oauth.SCOPE_GMAIL_COMPOSE)
        row = await self._draft(draft)
        if isinstance(row, str):
            return row
        turn, device = current_turn.get(), turn_device()
        # The user's "mandala" after the read-back is the yes; never in a turn that read outside text.
        said_send = turn is not None and rules.says_send(turn.text) and not current().outside()
        if row["approval_id"] is not None:
            asked = await self._approvals.pending_version(str(row["approval_id"]), row["version"])
            # only a version Pisa asked about in an EARLIER turn (its read-back): never
            # one an edit made this turn, sent unheard in the same breath
            earlier = turn is not None and str(asked["id"] if asked else "") in turn.asked_before
            if (
                asked is not None
                and said_send
                and earlier
                and device is not None
                and asked["device_id"] == device
            ):
                if not asked["payload"].get("heard"):
                    return (
                        "Not sent yet: the user hasn't heard the whole text. Read the full text back, "
                        'word for word, and ask "Lo mando?"; or they send it from the card on the phone.'
                    )
                if asked["requires_biometric"]:
                    return (
                        "Not sent yet: it goes to someone outside the thread, so it needs Face ID on the "
                        "phone (the card is there). Say so in one sentence."
                    )
                if wrist.current() == "watch":  # approved on the phone, never from the wrist
                    view = await self._approvals.get(str(row["approval_id"]))
                    return _waiting(view, yescard.on_the_phone(asked["lang"] or self._owner.tag),
                                    "from the watch an approval is approved on the phone")  # fmt: skip
                spoken = turn is None or turn.spoken
                timing = self._approvals.heard(asked, turn.heard_at if turn else None) if spoken else "ok"
                if timing == "late":  # read the card again, as their yes by code would
                    return _waiting(
                        asked | {"id": str(asked["id"])},
                        await self._read_again(
                            device, [asked], asked["lang"] or await self._owner.lang(self._pool)
                        ),
                        "the yes came too late",
                    )
                if timing == "over":
                    view = await self._approvals.get(str(row["approval_id"]))
                    return _waiting(view, yescard.on_the_phone(asked["lang"] or self._owner.tag),
                                    "a spoken yes holds two minutes: the card on the phone")  # fmt: skip
                view = await self._approvals.decide(
                    str(row["approval_id"]), "approve", "voice", in_conversation=True,
                    said_at=turn.heard_at if turn else None, spoken=spoken,
                )  # fmt: skip
                return _done(view)
            if asked is not None:  # the read-back already asked it: the same one question, not a second card
                view = await self._approvals.get(str(row["approval_id"]))
                if turn is not None and view["id"] not in turn.approvals:
                    turn.approvals.append(view["id"])
                if earlier and current().outside():
                    # an earlier turn's read-back asked its one question; this
                    # turn read outside text, so their words never send it (the rail above). Told "ask the
                    # user once" again, a model may ask "La mando?" a second time: say it, ask nothing.
                    return results.error(
                        f"{results.short(view['id'])} NOT sent: this turn read outside text, so spoken words "
                        "don't send it; the card stays on their phone",
                        "Not sent. Say so in one short sentence: you read text from outside in this turn, so "
                        "their words here don't send it; they send it from the card on the phone. Ask "
                        "nothing: "
                        "the read-back already asked its one question, so no question at all.",
                    )
                return await self._ask_send(view)
        return await self._ask_send(await self._propose_send(row["id"]))

    async def _ask_send(self, view: dict[str, Any]) -> str:
        return _waiting(view)

    async def _propose_send(self, draft_id: Any) -> dict[str, Any]:
        """The read-back's approval: what "sì" sends, exactly this version."""
        row = await self._draft(str(draft_id))
        assert not isinstance(row, str)
        lang = await self._owner.lang(self._pool)
        to, cc = rules.addresses(*row["to_addresses"]), rules.addresses(*row["cc_addresses"])
        files = list(row.get("attachments") or [])
        details = rules.email_details(
            to, cc, row["subject"], row["body"], list(row["outsiders"]), lang,
            attachments=[str(a["filename"]) for a in files],
        )  # fmt: skip
        # the outside values the draft carries (this call's and every earlier one's)
        outside = [list(c) for c in row.get("outside") or []]
        outside += [list(c) for c in carried_now() if list(c) not in outside]
        if outside != list(row.get("outside") or []):
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE email_drafts SET outside = %s WHERE id = %s", (Jsonb(outside), row["id"])
                )
        details += [
            line
            for what, source in outside
            if (line := quarantine.source_line(what, source, lang)) not in details
        ]
        summary = rules.word(lang, "send", names=rules.names(to + cc))
        shown = rules.fingerprint(
            [a for _, a in to],
            [a for _, a in cc],
            row["subject"],
            row["body"],
            [str(a["sign"]) for a in files],
        )
        turn = current_turn.get()
        voice = (
            turn is not None
            and not outside
            and rules.voice_sendable(
                str(row["body"] or ""),
                turn.words,
                current().texts(),  # never a tap's label
            )
        )
        payload = {"draft_id": str(row["id"]), "version": row["version"], "lang": lang,
                   "names": rules.names(to + cc), "fingerprint": shown, "voice": voice}  # fmt: skip
        if outside:  # its read-out names the source
            payload["outside"] = outside
        view = await self._approvals.create(
            "email_send", summary, details, payload,
            requires_biometric=bool(row["outsiders"]) or bool(files),  # documents leave: Face ID
            device_id=turn_device(), lang=lang,
        )  # fmt: skip
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE email_drafts SET approval_id = %s WHERE id = %s", (view["id"], row["id"])
            )
        if (turn := current_turn.get()) is not None:
            turn.approvals.append(view["id"])
        said = decisions.provenance(said=True, outside=bool(current().outside()))
        decisions.note("propose", "send_draft", said, ref=view["id"], rail="R2 one yes")
        return view

    async def _do_send(self, payload: dict[str, Any]) -> Outcome:
        """Send exactly what the user approved: the draft's row stays locked from the
        version check to the send (an edit waits), and the draft Gmail holds is read back and compared with
        the recipients and text the approval showed. Any difference: nothing is sent."""
        changed = PisaError(
            "plan_changed", "The draft changed after Pisa asked: nothing was sent.", "Ask Pisa again"
        )
        mail = self._mail()
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT * FROM email_drafts WHERE id = %s FOR UPDATE", (uuid.UUID(payload["draft_id"]),)
            )
            row = await cur.fetchone()
            if row is None or row["sent_at"] is not None:
                raise PisaError("approval_decided", "That reply was already sent, or its draft is gone.", "")
            if row["version"] != payload["version"]:
                raise changed
            try:
                held = rules.parse_raw_with(await mail.get(row["gmail_draft_id"]))
            except ConnectorError as exc:
                raise _failed(exc) from None
            if held is None or rules.fingerprint(*held) != payload["fingerprint"]:
                _log.warning("email_draft_differs", draft_id=payload["draft_id"])
                raise changed
            try:
                sent_id = await mail.send(row["gmail_draft_id"])
            except ConnectorError as exc:
                raise _unclear(exc, payload["lang"], "email") from None
            await conn.execute(
                "UPDATE email_drafts SET sent_at = %s WHERE id = %s", (self._clock(), row["id"])
            )
            if sent_id:  # Pisa's sent mail is Pisa's; the readers skip exactly this id
                await conn.execute(
                    "INSERT INTO pisa_sent_mail (gmail_message_id, draft_id, sent_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (gmail_message_id) DO NOTHING",
                    (sent_id, row["gmail_draft_id"], self._clock()),
                )
        _log.info("email_sent", draft_id=payload["draft_id"])
        return Outcome(rules.word(payload["lang"], "sent", names=payload["names"]),
                       receipt={"message_id": sent_id})  # fmt: skip


_RAIL = {"held": "R1 one yes", "proposal": "R2 one yes", "": "R2 one yes"}  # why a yes was asked
_AUTONOMY_AUTH: dict[str, Any] = {"tell": "autonomy:tell", "silent": "autonomy:silent"}
# what the model is told when Pisa acted at the user's level for this kind (instead of asking)
_AUTONOMY_SAY = {
    "tell": " Done at the user's own level for this kind (do it and tell): say what you did in one line.",
    "silent": (
        " Done at the user's own level for this kind (silent): don't mention it unless asked; the ledger "
        "has it."
    ),
}
_TOOLS = TOOL_OF  # the tool that asks with each approval kind (the registry's)


def _tag(approval_id: Any) -> dict[str, str]:
    """What Pisa created or moved carries its approval's id in Google (extendedProperties.private)."""
    return {"pisa": "1", "pisa_action": str(approval_id)}


# the private property that marks an event Pisa wrote after outside text (read back at every sync
# by connectors/google/calendar.py `from_outside`)
OUTSIDE_TAG = "pisa_outside"


def _outside_body(body: dict[str, Any]) -> dict[str, Any]:
    """The create's body with the outside mark in its private properties (a copy; the caller's is kept)."""
    extended = dict(body.get("extendedProperties") or {})
    extended["private"] = dict(extended.get("private") or {}) | {OUTSIDE_TAG: "1"}
    return body | {"extendedProperties": extended}


def _changed_or_unclear(exc: ConnectorError, lang: str, title: str) -> PisaError:
    """412 on If-Match: the event changed in Google after Pisa read it: nothing done."""
    if exc.status == 412:
        return PisaError("plan_changed", rules.word(lang, "changed_meanwhile", title=title), "Ask Pisa again")
    return _unclear(exc, lang, "calendar")


def _fmt(person: tuple[str, str]) -> str:
    return formataddr(person)


def _failed(exc: ConnectorError) -> PisaError:
    if exc.auth:
        return PisaError("connector_auth_failed", exc.message, "Reconnect Google in Settings → Connectors")
    return PisaError("provider_error", exc.message, "Try again in a minute")


def _not_owned(calendar_name: str) -> str:
    """A shared calendar's name is its user's text, never repeated to the model
    in Pisa's own result (the user's words, or the Calendar app, name it)."""
    del calendar_name
    return (
        "Pisa changes only calendars the user owns: that event's calendar is shared with the user (or Pisa's "
        "own). Nothing was asked; the user can change it in the Calendar app."
    )


def _unclear(exc: ConnectorError, lang: str, what: str) -> PisaError:
    """A Google call whose outcome is unknown (no answer, or a server error) may have happened: the user is
    told to check, never "not done". A clear refusal (4xx) is "not done"."""
    if exc.auth or (exc.status is not None and exc.status < 500):
        return _failed(exc)
    return PisaError("provider_error", rules.word(lang, f"maybe_{what}"), "")


async def _new_recipient(
    conn: AsyncConnection, to: str, said: set[str], lang: str
) -> tuple[str, str, bool] | str:
    """Who a new email goes to, and whether they are an outsider (Face ID).
    Known: an address the user wrote to (their SENT mail's recipients) or said this turn. An address given
    that is neither never gets a draft: it came from outside text. A name is matched as whole words
    against the people they wrote to and those who wrote to them; several → ask; one they never wrote to is an
    outsider. Never an empty or one-letter name ("io" is no Claudio)."""
    rows = await (
        await conn.execute("SELECT to_addresses FROM email_messages WHERE 'SENT' = ANY(labels)")
    ).fetchall()
    wrote = rules.addresses(*(a for (people,) in rows for a in people or []))
    # the people they wrote to, durably (`email_correspondents` outlives the 90 days of messages)
    kept = await (await conn.execute("SELECT address FROM email_correspondents")).fetchall()
    written = {a.lower() for _, a in wrote} | {str(a).lower() for (a,) in kept}
    found = rules.addresses(to)
    if found:
        name, address = found[0]
        if address.lower() not in said and address.lower() not in written:
            return (
                f"Not drafted: the user never wrote to {address} and didn't say it. Ask them for the address "
                "in their own words (an address from an email or a page is never used)."
            )
        # an address they said but never wrote to is a new recipient: Face ID
        return name, address, address.lower() not in written
    words = rules.name_words(to)
    if not words:
        return "Not drafted: say who it is for (a name or an address). Ask the user."
    received = await (
        await conn.execute(
            "SELECT DISTINCT from_name, from_address FROM email_messages WHERE from_address IS NOT NULL "
            "AND from_name IS NOT NULL"
        )
    ).fetchall()
    people: dict[str, str] = {}
    for person_name, address in [*wrote, *((str(n), str(a)) for n, a in received)]:
        if person_name and words <= rules.name_words(person_name):
            people.setdefault(address.lower(), person_name)
    if not people:
        return f"Not drafted: Pisa doesn't know {to!r}'s address. Ask the user for it."
    if len(people) > 1:  # an ambiguous target is code's one question, never Pisa's guess
        # each one by the name HE gave them (their people), else by the address: a display name
        # from a mail header is outside text, never said back verbatim
        own = dict(await (await conn.execute(
            "SELECT e, p.name FROM people p, unnest(p.emails) AS e WHERE NOT p.hidden "
            "AND (p.source = 'owner' OR 'name' = ANY(p.owner_fields)) AND e = ANY(%s)",
            (list(people),),
        )).fetchall())  # fmt: skip
        shown = [f"{own[a]} <{a}>" if a in own else a for a in people]
        ask = yescard.which(to, shown, lang)
        return (
            f"Several people match {to!r}: {'; '.join(shown[:5])}. Nothing was drafted: ask the user "
            f"exactly this, and pick no one: «{ask}»"
        )
    ((address, person_name),) = people.items()
    return person_name, address, address not in written and address not in said
