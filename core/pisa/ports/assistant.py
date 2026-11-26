"""What the conversation agent's tools need from Pisa (implemented in pisa.services)."""

from datetime import date, datetime
from typing import Any, Protocol

from pisa.domain.mail import EmailHit
from pisa.domain.quarantine import Fields
from pisa.domain.tasks import Task


class AssistantData(Protocol):
    @property
    def timezone(self) -> str:
        """The user's zone now (the phone's)."""
        ...

    async def agenda_day(self, day: date) -> dict[str, Any]:
        """One day of: {"date", "events", "tasks_due"} (overdue tasks are listed under today)."""
        ...

    async def tasks(self, status: str) -> list[Task]: ...

    async def low_emails(self) -> list[dict[str, Any]]:
        """The emails that became no task (the Low priority list's email part), newest first."""
        ...

    async def add_task(
        self,
        title: str,
        due: str | None,
        place: str | None,
        priority: int | None,
        duration_min: int | None = None,
        start: str | None = None,
        *,
        from_outside: bool = False,
    ) -> Task:
        """Raises `AlreadyListed` when the same item is open on their list (nothing is added twice)."""
        ...

    async def set_duration(self, task_id: str, minutes: int | None) -> Task:
        """Contract L: how long the task takes (5-480 minutes), or None to forget it."""
        ...

    async def complete_task(self, task_id: str) -> Task: ...

    async def update_task(self, task_id: str, changes: dict[str, Any], *, label: bool = True) -> Task:
        """Any of title, notes, due_at + due_kind, priority, place_text, repeat, status. `label`:
        false when the change came from outside text the user then said yes to: not the user's own."""
        ...

    async def plan_task(self, task_id: str, title: str) -> dict[str, Any]:
        """The user asked Pisa to fit this task into today: Pisa picks the time (a `plan` adjustment)
        and the plan is made again. The new plan."""
        ...

    async def delete_task(self, task_id: str) -> Task:
        """Delete it now (the user said so), restorable for 24 h."""
        ...

    async def deleted_tasks(self) -> list[Task]:
        """The tasks deleted by voice in the last 24 h, newest first."""
        ...

    async def undelete_task(self, task_id: str) -> Task:
        """One of those back, open."""
        ...

    async def search_email(self, query: str, days: int, limit: int) -> list[EmailHit]:
        """Metadata and snippet search over the email cache; never promotions, social or spam."""
        ...

    async def email_fields(self, ref: str, lang: str) -> Fields | None:
        """One email (as `email_text` finds it) read into typed fields: its sender,
        when, the subject, the values by rule and a neutral summary by a model with no tools. Never its
        text."""
        ...

    async def email_gists(self, ids: list[str]) -> dict[str, tuple[str, str | None]]:
        """The intake gist's one line ("what") of each of these emails the intake read, with its sender's
        address (the line's values are registered with the mail as their source)."""
        ...

    async def storyline_progress(self, ref: str, kind: str, quote: str, words: str, turn: str) -> str:
        """The user's own word on a storyline ("ho già spedito"): records the event their words say, or says
        why not (`services/storyline_chat`). `ref`: a card's ref or the storyline's key; `words`: what the
        user said this turn; `turn`: the chat turn's reference."""
        ...

    async def email_text(self, ref: str) -> tuple[EmailHit, str] | None:
        """One email by its id (or the start of it, at least 6 characters), with its whole text
        (the cached body, else its preview); None when no one email has that id, or one never shown to a
        model."""
        ...

    def today(self) -> date: ...

    def now(self) -> datetime: ...

    async def set_quiet_until(self, until: datetime | None) -> str:
        """Set or clear quiet.until; returns a short sentence on the resulting state."""
        ...

    async def plan(self, *, fresh: bool = False) -> dict[str, Any]:
        """Today's plan; `fresh` makes a new one now."""
        ...

    async def briefing(self) -> str:
        """A freshly composed briefing (trigger `ask`); never counts against the repeat rule."""
        ...

    # --- following the day --------------------------------------------------------------

    async def plan_options(self, item: str | None, goal: str) -> dict[str, Any]:
        """Contract N options for an item of today's plan (a title, a meal word like "pranzo", or an id), or
        for the rest of today when `item` is None."""
        ...

    async def apply_option(self, options_id: str, option_id: str) -> dict[str, Any]:
        """The new plan after applying one option."""
        ...

    async def plan_stop(
        self, what: str | None, when: str, relative_to: str | None, place_id: str | None, minutes: int | None
    ) -> dict[str, Any]:
        """An open task (by title) or a found place before / after / instead of an event, or when it
        fits; the new plan, or options when something fixed is in the way."""
        ...

    async def mark_done(self, what: str, pick: int | None = None) -> dict[str, Any]:
        """Close the open task or today's event the user named (title or place): {"done": [items]},
        {"ask": [titles]} when several match (`pick` then chooses by number), or {"none": true}."""
        ...

    async def remember_to(
        self,
        what: str,
        condition: str,
        due_at: datetime | None,
        place_id: str | None,
        purpose: str | None,
        provenance: str,
    ) -> str:
        """Pisa's own follow-up, a candidate when its condition holds (a time, arriving at or
        leaving a place, the next talk). Returns its id."""
        ...

    async def promise(self, promise_id: str) -> dict[str, Any] | None:
        """A follow-up as stored: {what, condition, due_at, place}; None when it is gone."""
        ...

    async def reaches(self, promise_id: str) -> bool:
        """The timed reminder will reach the user at its time (a push path, or the phone's own notifications
        working with it in their schedule); False: they'll see it when they open the app."""
        ...

    async def live_promises(self) -> list[dict[str, Any]]:
        """The follow-ups still to come (not fired, not cancelled), newest first: {id, what, condition,
        due_at, place}."""
        ...

    async def cancel_promise(self, promise_id: str) -> bool:
        """Cancel a follow-up still to come; False when it already fired, was cancelled or is gone."""
        ...

    async def tasks_bulk_match(
        self,
        which: str,
        *,
        days: int | None = None,
        source: str | None = None,
        ids: list[str] | None = None,
        sender: str | None = None,
    ) -> list[Task]:
        """The open tasks a closed filter names."""
        ...

    async def tasks_bulk_apply(self, ids: list[str], action: str) -> dict[str, Any]:
        """Change them all at once; {bulk_id, count, undo_until}."""
        ...

    async def thing(self, ref: str) -> dict[str, Any] | None:
        """Things v1: an order, a booking or a trip whole (its emails' gists, its tasks, its events),
        by a number it carries or its words; None when Pisa has none."""
        ...

    async def tasks_bulk_undo(self) -> dict[str, Any]:
        """The latest bulk change back, within 24 h; {bulk_id, count}."""
        ...

    async def skip_item(self, item: str) -> dict[str, Any]:
        """Skip an item today (an adjustment); the new plan."""
        ...

    async def undo_adjustment(self, adjustment_id: str) -> str:
        """Undo one adjustment ("last" = the latest of today); returns its title."""
        ...

    async def answer_check_in(self, check_in_id: str, choice: str) -> dict[str, Any]:
        """Contract O: {"check_in", "plan" | None, "options" | None}."""
        ...

    async def day(self, day: date) -> dict[str, Any]:
        """Contract O: {"date", "stays", "items"}: where the user was and what happened to each item."""
        ...

    async def default_mode(self) -> str:
        """The user's travel mode (Settings → Places)."""
        ...

    async def context(self) -> str | None:
        """The open check-in and the latest options, as plain lines, or None: what "la seconda" or "saltalo"
        refers to."""
        ...

    async def open_check_in(self) -> str | None:
        """The text of the newest open check-in Pisa asked, or None."""
        ...

    async def latest_check_in(self) -> dict[str, Any] | None:
        """The newest open check-in Pisa asked ({id, kind, text, choices}), or None."""
        ...


class PlaceData(Protocol):
    """What the place tools need. Values are plain dicts shaped like; provider content in
    them (names, reviews, research) is untrusted text, and the tools hand it to the agent as data."""

    async def where(self) -> dict[str, Any] | None:
        """The latest location: {lat, lon, age_min, accuracy_m, address, next} (next = the next event or task
        with a place today: where the user is going and why), or None when no location is known."""
        ...

    async def map_search(
        self, query: str, *, near: str | None, radius_m: int | None, open_now: bool, provider: str | None
    ) -> list[dict[str, Any]]:
        """Places with the map's facts: id, name, category, address, distance_m, open_now, rating,
        rating_count, price_level, attributes, provider."""
        ...

    async def find(self, place: str) -> str:
        """A place id from an id or a name (the best search match near the user)."""
        ...

    async def card(self, place_id: str, *, reviews: bool = True, travel: bool = True) -> dict[str, Any]: ...

    async def open_at(self, place_id: str, at: datetime) -> dict[str, Any]:
        """{name, known, open, next_change} at `at`."""
        ...

    async def web_search(
        self,
        query: str,
        *,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        recent_days: int | None,
        language: str | None,
    ) -> list[dict[str, str]]:
        """[{title, url, snippet}]: the results, not an answer."""
        ...

    async def save_menu(self, place_id: str, url: str, highlights: list[str]) -> None:
        """Keep a menu the model read this turn."""
        ...

    async def read_page(self, url: str, question: str | None = None) -> dict[str, Any] | None:
        """{url, text, fields} of one page (trimmed; `fields`: what the reader made of it), or
        None when it couldn't be read. `question`: the user's request, which the reader reads
        as its own data block to pull the passages and candidates that answer it."""
        ...

    async def travel(
        self,
        destination: str,
        mode: str | None,
        depart_at: datetime | None,
        arrive_by: datetime | None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """{minutes, traffic_minutes, distance_m, mode, provider, place_id | None, name} from `origin` ("here"
        when None, a place, an event or task title, or an address); "from" names the origin when given."""
        ...

    async def travel_many(self, place_ids: list[str], mode: str | None) -> dict[str, Any]:
        """From where the user is to many places in one call: {mode, provider, times:
        [{place_id, minutes | None, distance_m | None}]} (a place with no route: minutes None)."""
        ...

    async def detour(self, origin: str, via: str, destination: str, mode: str | None) -> dict[str, Any]:
        """{extra_min, to_stop_min, onward_min, direct_min, mode, provider, from, via, via_place_id, to}."""
        ...

    async def resolve(self, text: str) -> tuple[tuple[float, float], str, str | None]:
        """(position, name, place id) of "here", a place, an event or task title, or an address."""
        ...

    async def weather(self, where: str | None, when: str | None) -> dict[str, Any]:
        """The forecast (the shape) for here, a place, today's event or task, or a city; when: today,
        tomorrow, a weekday or YYYY-MM-DD, up to 7 days ahead."""
        ...
