"""Pisa prepares, the user taps. A table, a ride, a message, a call, a shop: Pisa finds the page, the
number, the words and the list, and puts them on the phone as a sheet of big buttons (`handoff` blocks). Their
tap in the official app (book, request, send, call, order) is the yes; Pisa never sends, books, calls or
pays.

RAILS (code, never the model's):
- every URL from pisa/domain/handoffs.py's templates, its parameters encoded, checked by `safe_url` once more;
- a phone number the model gives is used only when it is in the user's own words, is the place's own (the
  places provider), or the user confirmed it after Pisa read it back (a number from an email or a page);
- nothing is written before their word: `handoff_result` needs their words this turn saying how it went, and
  only then a booking goes in the calendar (their "sì, fatto" is the one yes);
- Pisa asks how it went once (pisa/services/handoffs.py), never more.

- after outside text in the turn, Pisa prepares only what their own words ask for, with no link, site
  or code their words don't hold; a table's page is then the platform's search, never a site;
- a place's own website shows its host under the button, so they see where it goes.

Results that carry the map's facts (a name, a number, a site) are `place` blocks: outside text.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic_ai import Agent

from pisa.brain.present_tools import Presenter
from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.purpose import purposeful
from pisa.brain.registry import offer
from pisa.brain.untrusted import carried_lines, current, data_block
from pisa.degrade import degrades
from pisa.domain import decisions
from pisa.domain import handoffs as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.sheet import SheetData
from pisa.domain.tasks import is_short_yes
from pisa.logging import get_logger
from pisa.ports.actions import ActionsData
from pisa.ports.assistant import AssistantData, PlaceData
from pisa.ports.handoffs import HandoffsData
from pisa.ports.world import WorldData

_log = get_logger("pisa.handoff_tools")

# `show`, for the world tools' sheets (the same rails: the hold, `safe_url`, the store)
Shower = Callable[..., Awaitable[str]]
# a preparation's category on the ladder, and each button's rung (the ladder orders the buttons)
LADDER_CATEGORY = {"booking": "restaurants"}
RUNG_OF = {"open_url": "deep_link", "call": "call", "message": "deep_link", "map": "deep_link"}
BOOKING_MIN = 120  # a table's time in the calendar
SLOTS_SHOWN = 3  # the free slots a call's script offers
SLOT_MIN = 60
DAY_FROM, DAY_TO = 9, 19  # the hours a slot may fall in
SLOT_DAYS = 6


def _block(text: str, *, provider: bool = False) -> str:
    """Pisa's own result (`action`), or one carrying the map's facts (`place`: outside text)."""
    if provider:
        block = data_block(
            "place", text, what="what Pisa prepared, with the map's facts (a name, a number, a site)"
        )
    else:
        block = data_block("action", text, what="what Pisa prepared")
    return block.render()


def outside_facts(lang: str) -> list[tuple[str, str]]:
    """A value only outside text gave, named on the sheet they tap (the act running now
    carries it: `untrusted.carried_now`)."""
    return [("Da fuori" if lang == "it-IT" else "From outside", line) for line in carried_lines(lang)]


def _plain(text: str, limit: int) -> str:
    return " ".join(rules.clean(text).split())[:limit]


def _said(owner_said: str, words: str) -> bool:
    """RAIL: the words the model cites are the user's own, this turn."""
    quote = _plain(owner_said, 1000).casefold().strip(" .,!?")
    return bool(quote) and quote in _plain(words, 4000).casefold()


def add_handoff_tools(
    agent: Agent[None, str],
    handoffs: HandoffsData,
    screen: Callable[[], Presenter | None],
    turn: ProfileTurn,
    *,
    places: PlaceData | None,
    data: AssistantData | None,
    actions: ActionsData | None,
    owner: Owner,
    clock: Callable[[], datetime],
    world: WorldData | None = None,
) -> Shower:
    timezone = owner.tz  # their zone as the turn starts

    def tz() -> ZoneInfo:
        return ZoneInfo(timezone)

    def lang() -> str:
        return turn.lang or owner.tag

    def italian() -> bool:
        return not lang().startswith("en")

    async def place_card(name: str, *, near_here: bool = False) -> dict[str, Any] | None:
        """The map's card for a place; `near_here`: the best match around them, never anywhere."""
        if places is None or not name.strip():
            return None
        try:
            if near_here:
                found = await places.map_search(
                    name, near="here", radius_m=None, open_now=False, provider=None
                )
                if not found or not found[0].get("id"):
                    return None
                return await places.card(str(found[0]["id"]), reviews=False, travel=False)
            return await places.card(await places.find(name), reviews=False, travel=False)
        except PisaError:
            return None

    def map_phone(card: dict[str, Any] | None) -> str | None:
        number = rules.normalize_phone((card or {}).get("phone"), owner.region())
        return None if number is None or rules.premium(number) else number

    def phone_ok(phone: str | None, confirmed: bool) -> tuple[str | None, str | None]:
        """(the number to use, or why not). RAIL: their words, or their yes to Pisa's read-back."""
        if not phone:
            return None, None
        number = rules.normalize_phone(phone, owner.region())
        if number is None:
            return None, f"{_plain(phone, 32)!r} is not a phone number."
        if rules.premium(number):
            return None, "Not prepared: that is a premium-rate number; Pisa never puts one on a button."
        if rules.phone_in(turn.words, number, owner.region()):
            return number, None
        # their short yes in a LATER message than Pisa's read-back, never this turn's own words
        if handoffs.phone_confirmed(
            turn.device, number, confirmed and is_short_yes(turn.words), turn.turn_id
        ):
            return number, None
        handoffs.asked_phone(turn.device, number, turn.turn_id)
        return None, (
            f"Not prepared: the number {number} isn't in the user's words (it may come from an email or a "
            "page). Read the number back to them and ask if it's right, then stop: don't call again "
            "this turn. Only after their yes in their next message, call again with confirmed=true."
        )

    def held(tool: str, purpose: str, buttons: list[dict[str, Any]]) -> str | None:
        """RAIL: after outside text in the turn, Pisa prepares only what their own words ask for,
        and no prepared words carry a link, a site or a code their words don't hold."""
        if not current().outside():
            return None
        why = None
        if not rules.asks_for(purpose, turn.words):
            why = "their words don't ask for it"
        else:
            bits = [x for b in buttons for x in rules.foreign(str(b.get("text") or ""), turn.words)]
            if bits:
                why = f"the words carry {bits[0][:40]!r}, which isn't in their words"
        if why is None:
            return None
        decisions.note("show", tool, "outside_text", rail="R1 outside text shows nothing", outcome="held")
        return (
            f"Not prepared: this turn read outside text (an email, a page) and {why}. Say it in words; "
            "prepare it when they ask, in their own words."
        )

    async def show(
        tool: str,
        purpose: str,
        target: str,
        title: str,
        details: dict[str, Any],
        facts: list[tuple[str, str]],
        buttons: list[dict[str, Any]],
        words: str,
        *,
        provider: bool = False,
        words_provider: bool = False,
        always: str = "",
        site: str | None = None,
    ) -> str:
        """The sheet with its buttons on the phone, and the preparation kept; what to tell the model.
        `target`: the place or person as the user said it (never the map's name). `site`: the place's or
        the store's own site its buttons may open (never kept with the preparation)."""
        p = screen()
        if (refused := held(tool, purpose, buttons)) is not None:
            return refused
        if world is not None and purpose in LADDER_CATEGORY:  # the ladder's best route first
            ranked = await world.order(
                LADDER_CATEGORY[purpose], owner.region(), [RUNG_OF[b["kind"]] for b in buttons]
            )
            buttons = sorted(buttons, key=lambda b: ranked.index(RUNG_OF[b["kind"]]))
        for b in buttons:  # RAIL: the last check, whatever built it
            if b.get("url") is not None and not rules.safe_url(b["url"], site=site, phone=b.get("phone")):
                return f"Not prepared: a link Pisa built isn't one Pisa may open ({b['kind']})."
        facts = [*facts, *outside_facts(lang())]  # the source on their sheet
        blocks: list[dict[str, Any]] = [
            {"type": "fact", "label": _plain(k, 40), "value": _plain(v, 120)} for k, v in facts if v
        ]
        for b in buttons:
            block = {"type": "handoff", "handoff_id": b["id"], "kind": b["kind"], "title": b["title"]}
            block |= {k: b[k] for k in ("subtitle", "url", "phone", "text", "notes") if b.get(k)}
            blocks.append(block)
        data = {"title": _plain(title, 120), "blocks": blocks[:24]}
        try:
            SheetData.model_validate(data)
        except ValueError as err:
            return f"Not prepared: {str(err)[:300]}"
        object_id = f"sheet:{uuid.uuid4()}"
        await handoffs.prepare(
            device_id=turn.device,
            purpose=purpose,
            target=target,
            lang=lang(),
            details=details,
            buttons=buttons,
            object_id=object_id,
        )
        status = "unavailable"
        if p is not None:
            ack = await p.show(tool, "sheet", object_id, data["title"], "take", data=data)
            status = ack.status
        on_phone = status in ("shown", "queued", "deferred", "sent_unconfirmed")
        # on their screen, the result names only Pisa's buttons (the map's facts stay out of the brain);
        # off it, Pisa says the essentials in words, and the map's facts come as a `place` block (outside
        # text)
        provider = provider and not on_phone
        where = "on the phone as a sheet of buttons" if on_phone else (
            "but NOT on a screen (no phone showing it)"
        )  # fmt: skip
        listed = "; ".join(
            f"«{b['title']}»" + (f" {b['phone']}" if b.get("phone") and not on_phone else "") for b in buttons
        )
        said = (words if not (on_phone and words_provider) else "") + always
        if not on_phone:  # no button is on their screen: never "tap Send"; they copy it or do it themselves
            return _block(
                f"Prepared {where} ({status}): {listed}. It is ready, but this app can't show it: nothing is "
                f"booked, sent, called or ordered, and there is no button to tap. {said} Give the user the "
                'text, the number or the link to copy and do it themselves; never say "tap" or "Send", '
                "and don't say it's done.",
                provider=provider,
            )
        return _block(
            f"Prepared {where} ({status}): {listed}. Nothing is booked, sent, called or ordered: the user "
            f"does it with a tap in the app. {said} Don't say it's done; say what they tap.",
            provider=provider,
        )

    def start_of(text: str) -> datetime | None:
        try:
            at = datetime.fromisoformat(text.strip().replace(" ", "T", 1))
        except ValueError:
            return None
        return at.replace(tzinfo=tz()) if at.tzinfo is None else at.astimezone(tz())

    @offer(agent)
    @purposeful("propose")
    async def prepare_table(
        place: str,
        start: str,
        people: int,
        name: str | None = None,
        platform: Literal["thefork", "opentable"] | None = None,
    ) -> str:
        """Prepare a table booking the user makes themselves ("prenotami un tavolo da Gabrielli venerdì alle
        21 per 2"): the place's booking page and its phone with what to say, as buttons on their phone. Pisa
        never books.

        Args:
            place: the restaurant, as the user said it (or its id from map_search).
            start: when, as a local date and time "2026-11-06T21:00" (work out the day from the current time).
            people: how many.
            name: the name to book under, when the user gave one or Pisa knows it.
            platform: thefork or opentable for the booking page when the place has no website; default by
                country (thefork in Italy).
        """
        at = start_of(start)
        if at is None:
            return "Not prepared: give the start as a local date and time like 2026-11-06T21:00."
        if at <= clock():
            return "Not prepared: that time is in the past."
        if not 1 <= people <= 30:
            return "Not prepared: people must be between 1 and 30."
        card = await place_card(place)
        said = _plain(place, 120)
        target = _plain((card or {}).get("name") or place, 120)
        address = (card or {}).get("address")
        # after outside text, never a site (the platform's search); a site shows its host
        site = None if current().outside() else rules.place_site((card or {}).get("website"))
        if site is None:
            chosen = platform or ("opentable" if (owner.region() or "").upper() == "US" else "thefork")
            city = str(address).rsplit(",", 1)[-1].strip() if address and "," in str(address) else None
            url, page = (
                rules.booking_search(chosen, target, city),
                ("TheFork" if chosen == "thefork" else "OpenTable"),
            )
        else:
            host = rules.site_host(site)
            url, page = (
                site,
                (f"{host} · sito indicato sulla mappa" if italian() else f"{host} · the map's site"),
            )
        script = rules.booking_script(people, at, name, lang())
        buttons: list[dict[str, Any]] = [
            {
                "id": str(uuid.uuid4()),
                "kind": "open_url",
                "title": "Prenota online" if italian() else "Book online",
                "subtitle": _plain(page, 120),
                "url": url,
            }
        ]
        phone = map_phone(card)
        if phone:
            buttons.append(
                {
                    "id": str(uuid.uuid4()),
                    "kind": "call",
                    "title": "Chiama" if italian() else "Call",
                    "subtitle": target,
                    "url": rules.tel(phone),
                    "phone": phone,
                    "text": script,
                    "notes": [
                        "Il numero è quello del posto sulla mappa."
                        if italian()
                        else "The number is the place's own, from the map."
                    ],
                }
            )
        details = {
            "at": at.isoformat(),
            "people": people,
            "place": target,
            "address": address,
            "place_id": (card or {}).get("id"),
        }
        when = rules.day_time(at, lang())
        facts = [
            ("Quando" if italian() else "When", f"{when}, {people}"),
            ("Dove" if italian() else "Where", str(address or "")),
        ]
        words = f"The script for the call: «{script}»." if phone else "Pisa has no phone number for it."
        return await show(
            "prepare_table",
            "booking",
            said,
            f"Tavolo da {target}" if italian() else f"Table at {target}",
            details,
            facts,
            buttons,
            words,
            site=site,
            provider=card is not None,
        )

    @offer(agent)
    @purposeful("propose")
    async def prepare_ride(
        destination: str, app: Literal["uber", "lyft", "freenow", "taxi"] = "uber", name: str | None = None
    ) -> str:
        """Prepare a ride the user requests themselves ("chiamami un Uber per l'aeroporto"): the app opens
        with the pickup (where they are) and the drop-off filled in; they tap Request. A taxi: the city's
        radio taxi to call with what to say. Pisa never requests it.

        Args:
            destination: where to, as the user said it (a place, an address).
            app: uber, lyft, freenow or taxi.
            name: the name for a taxi, when known.
        """
        if places is None:
            return "Not prepared: Pisa has no map to find the destination."
        here = await places.where()
        pickup = (float(here["lat"]), float(here["lon"])) if here and here.get("lat") is not None else None
        card = await place_card(destination)
        if card is None or card.get("lat") is None:
            return "Not prepared: Pisa couldn't find the destination on the map; ask them where exactly."
        target = _plain(card.get("name") or destination, 120)
        dropoff = (float(card["lat"]), float(card["lon"]))
        address = here.get("address") if here else None
        buttons: list[dict[str, Any]] = []
        words = ""
        if app == "uber":
            buttons.append(
                {
                    "id": str(uuid.uuid4()),
                    "kind": "open_url",
                    "title": "Apri Uber" if italian() else "Open Uber",
                    "subtitle": target,
                    "url": rules.uber(pickup, dropoff, target),
                }
            )
        elif app == "lyft":
            buttons.append(
                {
                    "id": str(uuid.uuid4()),
                    "kind": "open_url",
                    "title": "Apri Lyft" if italian() else "Open Lyft",
                    "subtitle": target,
                    "url": rules.lyft(pickup, dropoff),
                }
            )
        elif app == "freenow":
            buttons.append(
                {
                    "id": str(uuid.uuid4()),
                    "kind": "open_url",
                    "title": "Apri FreeNow" if italian() else "Open FreeNow",
                    "subtitle": target,
                    "url": rules.FREENOW_URL,
                    "notes": [f"Destinazione: {target}" if italian() else f"Destination: {target}"],
                }
            )
        else:
            script = rules.taxi_script(address, target, name, lang())
            found = rules.dispatcher(address, owner.country)
            if found is not None:
                buttons.append(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "call",
                        "title": "Chiama il taxi" if italian() else "Call a taxi",
                        "subtitle": found[0],
                        "url": rules.tel(found[1]),
                        "phone": found[1],
                        "text": script,
                    }
                )
            if owner.in_country(rules.TAXI_COUNTRY):  # itTaxi is Italy's
                buttons.append(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "open_url",
                        "title": "Apri itTaxi" if italian() else "Open itTaxi",
                        "url": rules.ITTAXI_URL,
                        "text": script,
                    }
                )
            words = f"The script for the taxi: «{script}»."
        always = (
            " Pisa doesn't know where they are now: the app asks for the pickup." if pickup is None else ""
        )
        facts = [
            ("Da" if italian() else "From", str(address or ("posizione attuale" if italian() else "here"))),
            ("A" if italian() else "To", target),
        ]
        return await show(
            "prepare_ride",
            "ride",
            _plain(destination, 120),
            f"Corsa per {target}" if italian() else f"Ride to {target}",
            {"app": app, "to": target},
            facts,
            buttons,
            words,
            provider=True,
            words_provider=True,
            always=always,
        )

    @offer(agent)
    @purposeful("propose")
    async def prepare_message(
        to: str,
        text: str,
        app: Literal["whatsapp", "imessage"] = "whatsapp",
        phone: str | None = None,
        confirmed: bool = False,
    ) -> str:
        """Prepare a message the user sends themselves ("scrivi a Marco su WhatsApp che arrivo alle 9"):
        WhatsApp or iMessage opens with the words written; they pick or checks the contact and taps Send. Pisa
        never sends.

        Args:
            to: the person, as the user said it.
            text: the whole message, in the user's voice and language.
            app: whatsapp or imessage.
            phone: the number, only when the user said it, or after they confirmed one Pisa read back.
            confirmed: true only after Pisa read a number back and the user said yes in a later message.
        """
        body = rules.clean(text).strip()
        # the text they dictated this turn is the message, word for word (a model wrote
        # "Ciao Marco, arrivo alle 9." for "scrivi a Marco … che arrivo alle 9")
        if (his := rules.dictated(turn.words)) is not None and rules.rewords(body, his):
            turn.reworded.append((body, rules.clean(his).strip()))  # the reply quotes their words too
            body = rules.clean(his).strip()
        if not body or len(body) > rules.TEXT_MAX or not _plain(to, 80):
            return "Not prepared: a message needs a person and a text of at most 1,000 characters."
        number, why = phone_ok(phone, confirmed)
        if why is not None:
            return why
        person = _plain(to, 80)
        url = rules.whatsapp(number, body) if app == "whatsapp" else rules.sms(number, body)
        label = "WhatsApp" if app == "whatsapp" else "iMessage"
        pick = (
            None if number else (f"Scegli {person} in {label}" if italian() else f"Pick {person} in {label}")
        )
        buttons = [
            {
                "id": str(uuid.uuid4()),
                "kind": "message",
                "title": f"{label} a {person}" if italian() else f"{label} to {person}",
                "url": url,
                "text": body,
                **({"subtitle": pick} if pick else {}),
                **({"phone": number} if number else {}),
            }
        ]
        return await show(
            "prepare_message",
            "message",
            person,
            f"Messaggio a {person}" if italian() else f"Message to {person}",
            {"app": app},
            [],
            buttons,
            "They send it with a tap; it is NOT sent.",
        )

    @offer(agent)
    @purposeful("propose")
    async def prepare_call(
        who: str,
        script: list[str],
        phone: str | None = None,
        confirmed: bool = False,
        slots: bool = True,
    ) -> str:
        """Prepare a call the user places themselves ("chiama il dentista per spostare l'appuntamento"): the
        number, and what to say in at most 3 lines; with `slots`, the free times from their calendar that
        work. Pisa never
        calls. Afterwards Pisa asks once how it went; a change they then say is made with their words.

        Args:
            who: the place or person, as the user said it.
            script: 1-3 short lines: who they are and what to ask.
            phone: the number, only when the user said it or confirmed one Pisa read back; else Pisa looks the
                place up on the map.
            confirmed: true only after Pisa read a number back and the user said yes in a later message.
            slots: add the free times that work (for moving an appointment).
        """
        lines = [_plain(s, 200) for s in script if str(s).strip()][:3]
        if not lines:
            return "Not prepared: give 1-3 lines of what to say."
        number, why = phone_ok(phone, confirmed)
        if why is not None:
            return why
        said = target = _plain(who, 120)
        note = None
        card = None
        if number is None:
            card = await place_card(who, near_here=True)  # around them, never anywhere
            number = map_phone(card)
            if number is None:
                return "Not prepared: Pisa has no number for them. Ask the user for it."
            target = _plain(card.get("name") or who, 120) if card else target
            note = (
                f"Il numero è quello di {target} sulla mappa: controlla che sia giusto."
                if italian()
                else f"The number is {target}'s on the map: check it's the right one."
            )
        free = await free_slots() if slots and data is not None else []
        if free:
            lines.append(
                ("Mi andrebbero bene: " if italian() else "Times that work: ") + ", ".join(free) + "."
            )
        text = "\n".join(lines)
        buttons = [
            {
                "id": str(uuid.uuid4()),
                "kind": "call",
                "title": "Chiama" if italian() else "Call",
                "subtitle": target,
                "url": rules.tel(number),
                "phone": number,
                "text": text,
                **({"notes": [note]} if note else {}),
            }
        ]
        return await show(
            "prepare_call",
            "call",
            said,
            f"Chiamata a {target}" if italian() else f"Call to {target}",
            {"script": lines},
            [],
            buttons,
            f"The script: «{' / '.join(lines)}».",
            provider=card is not None,
        )

    async def free_slots() -> list[str]:
        """RAIL: only their own calendar's gaps (DAY_FROM-DAY_TO, at least SLOT_MIN), from tomorrow."""
        assert data is not None
        found: list[str] = []
        day = data.today() + timedelta(days=1)
        for _ in range(SLOT_DAYS):
            if day.weekday() < 5:
                agenda = await data.agenda_day(day)
                busy = sorted(
                    (
                        datetime.fromisoformat(e["start"]).astimezone(tz()),
                        datetime.fromisoformat(e["end"]).astimezone(tz()),
                    )
                    for e in agenda.get("events", [])
                    if not e.get("all_day") and e.get("start") and e.get("end")
                )
                at = datetime(day.year, day.month, day.day, DAY_FROM, tzinfo=tz())
                end = at.replace(hour=DAY_TO)
                for begin, finish in [*busy, (end, end)]:
                    if (min(begin, end) - at) >= timedelta(minutes=SLOT_MIN):
                        found.append(
                            f"{rules.day_time(at, lang()).split(' alle ')[0].split(',')[0]} "
                            f"{at:%H:%M}-{min(begin, end):%H:%M}"
                        )
                        break
                    at = max(at, finish)
            if len(found) >= SLOTS_SHOWN:
                break
            day += timedelta(days=1)
        return found

    @offer(agent)
    @purposeful("propose")
    async def prepare_shopping(
        store: Literal["glovo", "deliveroo", "esselunga", "instacart", "justeat"],
        items: list[str],
        with_tasks: bool = True,
    ) -> str:
        """Prepare a delivery or a shopping order the user places themselves ("fammi la spesa su Esselunga"):
        the store's page, and the list Pisa wrote (their words, plus the things to buy in their tasks) to
        paste. Pisa never
        orders or pays.

        Args:
            store: glovo, deliveroo, esselunga, instacart or justeat.
            items: what they asked for, one thing each.
            with_tasks: add the open tasks that are things to buy ("Comprare il latte").
        """
        pool = (
            [t for t in (rules.shopping_task(task.title) for task in await data.tasks("open")) if t]
            if data is not None
            else []
        )
        # what they said, or a thing to buy in their tasks: never a list item from anywhere else
        wanted = [
            i for i in (_plain(x, 120) for x in items if str(x).strip())
            if rules.in_words(i, turn.words) or any(i.casefold() == t.casefold() for t in pool)
        ]  # fmt: skip
        if with_tasks:
            wanted += pool
        listed = rules.shopping_list(wanted)
        if not listed:
            return "Not prepared: the list is empty; ask them what to buy."
        shop = rules.STORE_NAMES[store]
        buttons = [
            {
                "id": str(uuid.uuid4()),
                "kind": "open_url",
                "title": f"Apri {shop}" if italian() else f"Open {shop}",
                "url": rules.store_page(store),
                "text": listed,
                "notes": [
                    "Copia la lista e incollala nella ricerca."
                    if italian()
                    else "Copy the list and paste it into the search."
                ],
            }
        ]
        return await show(
            "prepare_shopping",
            "shopping",
            shop,
            f"Spesa su {shop}" if italian() else f"Shopping on {shop}",
            {"store": store, "items": listed.count("\n") + 1},
            [],
            buttons,
            f"The list: {listed.replace(chr(10), ' ')}",
        )

    async def prep_id(prepared: str) -> str:
        """The preparation's id, or the one open preparation its words name ("Gabrielli")."""
        text = prepared.strip().removeprefix("h:")
        try:
            return str(uuid.UUID(text))
        except ValueError:
            low = text.casefold()
            named = [
                p["id"] for p in await handoffs.open_preps()
                if low and (low in p["target"].casefold() or p["target"].casefold() in low)
            ]  # fmt: skip
            return named[0] if len(named) == 1 else text

    @offer(agent)
    @purposeful(None)
    async def handoff_result(prepared: str, worked: bool, owner_said: str) -> str:
        """The user's word on something Pisa prepared, after Pisa asked how it went ("sì, fatto", "no, era
        pieno"): Pisa keeps it; a booking that worked goes in their calendar. Only on their words this turn,
        never on a guess.

        Args:
            prepared: the preparation's id ("h:…", from the open preparations), or its place or person.
            worked: true when they say it worked (booked, done), false when it didn't.
            owner_said: their own words this turn that say it, copied exactly.
        """
        if not _said(owner_said, turn.words) or current().outside():  # the talk's taint
            return "Not kept: cite the user's own words this turn that say how it went."
        heard = rules.outcome(owner_said)
        # their whole words decide (a negation anywhere wins), not only the part the model cites
        if heard is None or (heard == "worked") != worked or rules.outcome(turn.words) != heard:
            return "Not kept: their words don't say whether it worked; ask them."
        key = await prep_id(prepared)
        open_one = next((p for p in await handoffs.open_preps() if p["id"] == key), None)
        if open_one is None:
            return "Not kept: there's no open preparation by that id."
        # a booking worked only after they opened it or Pisa asked; a bare "sì" only to Pisa's own
        # follow-up
        if worked and open_one["purpose"] == "booking" and not (open_one["opened"] or open_one["asked"]):
            return "Not kept: they haven't opened the booking Pisa prepared; ask them how it went."
        if is_short_yes(turn.words) and not open_one["asked"]:
            return "Not kept: a bare yes may answer something else; ask them how it went."
        prep = await handoffs.settle(key, heard)
        if prep is None:
            return "Not kept: there's no open preparation by that id."
        details = prep["details"] or {}
        if not worked:
            return _block(f"Kept: the {prep['purpose']} with {prep['target']} didn't work. Nothing written.")
        if prep["purpose"] != "booking" or actions is None or not details.get("at"):
            more = (
                " If they said a new time, make the change with their words (move_event)."
                if prep["purpose"] == "call"
                else ""
            )
            return _block(f"Kept: the {prep['purpose']} with {prep['target']} worked.{more}")
        at = datetime.fromisoformat(details["at"]).astimezone(tz())
        meal = (
            ("Cena" if at.hour >= 17 else "Pranzo") if italian() else ("Dinner" if at.hour >= 17 else "Lunch")
        )
        title = f"{meal} da {prep['target']}" if italian() else f"{meal} at {prep['target']}"
        try:
            done = await actions.create_event(
                title,
                at.replace(tzinfo=None).isoformat(timespec="minutes"),
                None,
                BOOKING_MIN,
                details.get("address") or prep["target"],
                None,
                owner_said,
                grounded_by=owner_said,
            )
        except PisaError as err:
            return f"Not in the calendar: Pisa kept that it worked, but the entry failed: {err.message}"
        return _block(f"Kept: booked. {done}")

    @agent.instructions(name="handoffs")  # kept as first said (a new tag on every call)
    async def open_handoffs() -> str:
        """What Pisa prepared in the last day and the user hasn't said how it went (none: nothing)."""
        preps = []  # the turn goes on without the line
        with degrades(_log, "handoffs_line"):
            preps = await handoffs.open_preps()
        if not preps:
            return ""
        lines = []
        # the target as they said it (never the map's name), the id, the purpose, the time
        for p in preps:
            d = p.get("details") or {}
            when = f" {d['at'][:16].replace('T', ' ')}" if d.get("at") else ""
            state = "Pisa asked how it went" if p["asked"] else "opened" if p["opened"] else "not opened yet"
            lines.append(f"- [h:{p['id']}] {p['purpose']} «{_plain(p['target'], 80)}»{when} ({state})")
        listed = data_block(
            "action", "\n".join(lines), what="Pisa's preparations (the names as the user said them)"
        )
        return (
            "What Pisa prepared for the user to do themselves, without their word on how it went yet:\n"
            f"{listed.render()}\n"
            "When they say how it went, call handoff_result with their words; nothing is written before."
        )

    return show  # the cart tools put their sheet on the phone the same way
