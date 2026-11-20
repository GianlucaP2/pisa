"""Pisa's actions outside Pisa: what the user reads before saying yes, who an email reply goes to, and the
reply itself. Pure rules, no I/O.

- **Calendar**: an approval names the calendar, the times, the place, and who Google will notify (the other
  attendees; `sendUpdates` = all when there are any). Only events the user organises change.
- **Email**: a reply goes to the thread's people only (an email's content never chooses recipients):
  the answered message's sender (or Reply-To) and its To, with its Cc, without the user. Anyone else is an
  outsider, named in the approval, and needs Face ID.
- **Pisa acts** (`decide_mode`): a change whose values are in the user's own words this turn (the time,
  the title, the cancel verb: "grounded"), on the user's own event with nobody else to notify, in a turn that
  read no outside text, is done at once (`words`, with Undo for 24 h). Other attendees: one yes that names
  them (`proposal`). Not grounded, or after outside text: one yes, worded as Pisa's proposal (`held`).
"""

import base64
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pisa.domain import tools
from pisa.domain.claims import points_back
from pisa.domain.errors import PisaError
from pisa.domain.lexicon.months import EN_MONTHS_TITLE, IT_MONTHS, MONTHS_SHORT
from pisa.domain.lexicon.weekdays import EN_WEEKDAYS_TITLE, IT_WEEKDAYS, WEEKDAYS_SHORT
from pisa.domain.tasks import MATCH_THRESHOLD, clock_times, day_named, match_score, parse_when
from pisa.domain.text import fold_apostrophe
from pisa.domain.tools import Grounding

DETAIL_LINE = 200  # a detail line's limit (the protocol's)

WORDS = {
    "it-IT": {
        "calendar": "Calendario",
        "when": "Quando",
        "where": "Dove",
        "notified": "Avvisati",
        "nobody": "nessuno",
        "same": "stessa durata",
        "to": "A",
        "cc": "Cc",
        "subject": "Oggetto",
        "text": "Testo",
        "no_undo": "Un'email inviata non si può annullare.",
        "more": "(+{n} caratteri)",
        "maybe_email": "Potrebbe essere stata inviata: Google non ha risposto. Controlla la posta inviata.",
        "maybe_calendar": "Potrebbe essere stato fatto: Google non ha risposto. Controlla il calendario.",
        "outside": "Fuori dal thread (serve Face ID)",
        "attached": "Allegati",
        "create": "Aggiungo «{title}» {when}?",
        "repeats": "Si ripete",
        "move": "Sposto «{title}» a {when}?",
        "cancel": "Cancello «{title}» di {when}?",
        "send": "Invio la risposta a {names}?",
        "created": "Aggiunto «{title}» {when}.",
        "moved": "«{title}» spostato a {when}.",
        "cancelled": "«{title}» cancellato.",
        "sent": "Risposta inviata a {names}.",
        "undo_created": "Annullato: «{title}» tolto dal calendario.",
        "undo_moved": "Annullato: «{title}» è di nuovo {when}.",
        "undo_cancelled_invited": "Annullato: «{title}» è tornato; gli invitati ricevono un nuovo invito.",
        "undo_changed": "«{title}» è cambiato nel calendario dopo la modifica di Pisa: non "
        "ho annullato niente.",
        "undo_cancelled": "Annullato: «{title}» è tornato.",
        "changed_meanwhile": "«{title}» è cambiato nel calendario nel frattempo: non ho fatto niente.",
        "notify": "e avviso {names}",
        "undo_hint": "Se vuoi lo annullo.",
        "kept": "Ok, lascio com'è.",
        "face_id": "Serve Face ID: è sul telefono.",
        "not_applied": "Non ho fatto {items}: aspetta ancora il tuo sì sulla scheda.",
        "which": "Quale? {options}",
        "nothing_to_undo": "Non c'è una modifica mia da annullare.",
        "did_create": "Ho aggiunto «{title}» {when}.",
        "did_move": "Ho spostato «{title}» a {when}.",
        "did_cancel": "Ho cancellato «{title}».",
        "ask_cancel": "Cancello «{title}» di {when}?",
        "today": "oggi",
        "tomorrow": "domani",
    },
    "en-US": {
        "calendar": "Calendar",
        "when": "When",
        "where": "Where",
        "notified": "Notified",
        "nobody": "nobody",
        "same": "same length",
        "to": "To",
        "cc": "Cc",
        "subject": "Subject",
        "text": "Text",
        "no_undo": "A sent email can't be undone.",
        "more": "(+{n} characters)",
        "maybe_email": "It may have been sent: Google didn't answer. Check the sent mail.",
        "maybe_calendar": "It may have been done: Google didn't answer. Check the calendar.",
        "outside": "Outside the thread (needs Face ID)",
        "attached": "Attachments",
        "create": "Add «{title}» {when}?",
        "repeats": "Repeats",
        "move": "Move «{title}» to {when}?",
        "cancel": "Cancel «{title}» on {when}?",
        "send": "Send the reply to {names}?",
        "created": "Added «{title}» {when}.",
        "moved": "«{title}» moved to {when}.",
        "cancelled": "«{title}» cancelled.",
        "sent": "Reply sent to {names}.",
        "undo_created": "Undone: «{title}» is off the calendar.",
        "undo_moved": "Undone: «{title}» is back {when}.",
        "undo_cancelled_invited": "Undone: «{title}» is back; the attendees get a new invitation.",
        "undo_changed": "«{title}» changed in the calendar after Pisa's change: nothing was undone.",
        "undo_cancelled": "Undone: «{title}» is back.",
        "changed_meanwhile": "«{title}» changed in the calendar meanwhile: I didn't do anything.",
        "notify": "and notify {names}",
        "undo_hint": "Say undo to put it back.",
        "kept": "OK, I'll leave it as it is.",
        "face_id": "It needs Face ID: it's on your phone.",
        "not_applied": "I haven't done {items}: it still waits for your yes on the card.",
        "which": "Which one? {options}",
        "nothing_to_undo": "There's no change of mine to undo.",
        "did_create": "I added «{title}» {when}.",
        "did_move": "I moved «{title}» to {when}.",
        "did_cancel": "I cancelled «{title}».",
        "ask_cancel": "Cancel «{title}», {when}?",
        "today": "today",
        "tomorrow": "tomorrow",
    },
}


_BARE_HOUR = re.compile(r"\b(alle|ore|at|for)\s+([01]?\d|2[0-3])\b(?![:.]\d)", re.IGNORECASE)


_SPOKEN_MINUTES = re.compile(
    r"\b(alle|ore)\s+([01]?\d|2[0-3])\s+e\s+(mezza|mezzo|un quarto|tre quarti|[0-5]?\d)\b", re.IGNORECASE
)
_QUARTERS = {"mezza": 30, "mezzo": 30, "un quarto": 15, "tre quarti": 45}
# hours said as words, at noon or midnight, and 12-hour times ground a time too.
_HOUR_WORDS = {"una": 1, "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
               "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13,
               "quattordici": 14, "quindici": 15, "sedici": 16, "diciassette": 17, "diciotto": 18,
               "diciannove": 19, "venti": 20, "ventuno": 21, "ventidue": 22, "ventitre": 23, "ventitré": 23,
               "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
               "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}  # fmt: skip
_HOUR_WORD = "|".join(sorted(_HOUR_WORDS, key=len, reverse=True))
_WORD_HOUR = re.compile(rf"\b(alle|ore|at)\s+({_HOUR_WORD})\b", re.IGNORECASE)
_NOON = re.compile(r"\b(?:a |at )?(mezzogiorno|mezzanotte|noon|midnight)\b", re.IGNORECASE)
_TWELVE_HOUR = re.compile(r"\b(?:alle |at )?(1[0-2]|0?[1-9])(?:[:.]([0-5]\d))?\s*(am|pm|a\.m\.|p\.m\.)",
                          re.IGNORECASE)  # fmt: skip
# "dopo le 21", "prima delle 9", "after 9", "by 10": a bound, not the time to move to.
_BOUND_TIME = re.compile(
    r"\b(dopo|prima|entro|after|before|by|until|fino)\s+(?:le |delle |alle |a |at )?"
    r"([01]?\d|2[0-3])(?:[:.][0-5]\d)?\b",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def with_minutes(text: str) -> str:
    """ "oggi alle 21" → "oggi alle 21:00", "alle 9 e mezza" → "alle 09:30", "alle 9 e un quarto" → "alle
    09:15": the user says the time in words (parse_when wants HH:MM)."""

    def spoken(m: re.Match[str]) -> str:
        said = m.group(3).lower()
        minutes = _QUARTERS[said] if said in _QUARTERS else int(said)
        return f"{m.group(1)} {int(m.group(2)):02d}:{minutes:02d}"

    def twelve(m: re.Match[str]) -> str:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower().startswith("p") else 0)
        return f"at {hour:02d}:{int(m.group(2) or 0):02d}"

    noon = ("mezzogiorno", "noon")
    text = _NOON.sub(lambda m: "alle 12:00" if m.group(1).lower() in noon else "alle 00:00", text)
    text = _TWELVE_HOUR.sub(twelve, text)
    text = _WORD_HOUR.sub(lambda m: f"{m.group(1)} {_HOUR_WORDS[m.group(2).lower()]}", text)
    text = _SPOKEN_MINUTES.sub(spoken, text)
    return _BARE_HOUR.sub(lambda m: f"{m.group(1)} {int(m.group(2)):02d}:00", text)


def addresses_said(text: str) -> set[str]:
    """The whole email addresses in the user's words ("bob@x.it" is not said by
    "jacob@x.it" or "bob@x.it.evil.com")."""
    return {a.lower().rstrip(".") for a in _ADDRESS.findall(text)}


def word(lang: str, key: str, **values: str) -> str:
    return WORDS[lang][key].format(**values)


def when_text(start: datetime, end: datetime | None, lang: str, timezone: str) -> str:
    """ "ven 23 ott, 10:00-11:00", with an en dash (the end's day too when it differs)."""
    tz = ZoneInfo(timezone)
    a = start.astimezone(tz)
    day = f"{WEEKDAYS_SHORT[lang][a.weekday()]} {a.day} {MONTHS_SHORT[lang][a.month - 1]}"
    if end is None:
        return f"{day}, {a:%H:%M}"
    b = end.astimezone(tz)
    if b.date() != a.date():
        other = f"{WEEKDAYS_SHORT[lang][b.weekday()]} {b.day} {MONTHS_SHORT[lang][b.month - 1]}"
        return f"{day}, {a:%H:%M} \u2013 {other}, {b:%H:%M}"
    return f"{day}, {a:%H:%M}\u2013{b:%H:%M}"


def at_text(start: datetime, lang: str, timezone: str) -> str:
    """ "ven 23 ott alle 10:00" / "Fri 23 Oct at 10:00"."""
    a = start.astimezone(ZoneInfo(timezone))
    joint = "alle" if lang == "it-IT" else "at"
    return f"{WEEKDAYS_SHORT[lang][a.weekday()]} {a.day} {MONTHS_SHORT[lang][a.month - 1]} {joint} {a:%H:%M}"


def spoken_clock(hour: int, minute: int, lang: str) -> str:
    """A time as people say it: "alle 21", "alle 9 e mezza", "all'una", "alle 18 e 15";
    "9 pm", "9:30 am", "noon"."""
    if lang == "it-IT":
        joint = "all'" if hour == 1 else "alle "
        rest = "" if minute == 0 else " e mezza" if minute == 30 else f" e {minute}"
        return f"{joint}{'una' if hour == 1 else hour}{rest}"
    if (hour, minute) == (12, 0):
        return "at noon"
    if (hour, minute) == (0, 0):
        return "at midnight"
    h12 = hour % 12 or 12
    clock = f"{h12}" if minute == 0 else f"{h12}:{minute:02d}"
    return f"at {clock} {'am' if hour < 12 else 'pm'}"


def spoken_when(start: datetime, today: date, lang: str, timezone: str, *, small_hours: bool = False) -> str:
    """ "oggi, giovedì 30, alle 21", "domani, venerdì 31, alle 10 e mezza", "sabato 1 alle 15", "lunedì 10
    novembre alle 9"; "today, Thursday 30, at 9 pm", "Saturday 1 at 3 pm", "Monday 10 November at 9 am": the
    day as people name it, always with its weekday and date ("giovedì 30" leaves no doubt), then the
    time. `small_hours`: before 05:00 "oggi" and "domani" are ambiguous (the user hasn't slept): the weekday
    and date alone."""
    a = start.astimezone(ZoneInfo(timezone))
    days = (a.date() - today).days
    it = lang == "it-IT"
    weekday = f"{(IT_WEEKDAYS if it else EN_WEEKDAYS_TITLE)[a.weekday()]} {a.day}"
    if 0 <= days < 2 and not small_hours:
        day = f"{word(lang, 'today' if days == 0 else 'tomorrow')}, {weekday},"
    elif 0 <= days < 7:
        day = weekday
    else:
        day = f"{weekday} {IT_MONTHS[a.month - 1]}" if it else f"{weekday} {EN_MONTHS_TITLE[a.month - 1]}"
    return f"{day} {spoken_clock(a.hour, a.minute, lang)}"


def clean(text: str) -> str:
    """A title or a name from someone else, as the user reads it in an approval: no control or format
    characters (bidi overrides, zero-width), no «» (they frame titles in Pisa's sentences), one-spaced."""
    kept = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf") and c not in "«»")
    return " ".join(kept.split())


def attendees_to_notify(event: dict[str, Any]) -> list[str]:
    """The people Google will tell about a change: the attendees other than the user and rooms."""
    out = []
    for a in event.get("attendees") or []:
        if a.get("self") or a.get("resource"):
            continue
        out.append(clean(str(a.get("displayName") or a.get("email") or "?")))
    return out


def organised_by_owner(event: dict[str, Any]) -> bool:
    """The user organises it: Google says so (`organizer.self`). An event without an organizer is not
    taken as the user's."""
    return bool((event.get("organizer") or {}).get("self"))


def organiser_name(event: dict[str, Any]) -> str:
    organizer = event.get("organizer") or {}
    return clean(str(organizer.get("displayName") or organizer.get("email") or "someone else"))


def notified_line(names: list[str], lang: str) -> str:
    return f"{word(lang, 'notified')}: {', '.join(names) if names else word(lang, 'nobody')}"


# --- email ------------------------------------------------------------------------------------------------


def addresses(*headers: str) -> list[tuple[str, str]]:
    """(name, address) pairs from header values, lower-cased addresses, first occurrence kept."""
    seen: set[str] = set()
    out = []
    for name, address in getaddresses([h for h in headers if h]):
        address = address.strip().lower()
        if "@" in address and address not in seen:
            seen.add(address)
            out.append((name.strip(), address))
    return out


def reply_recipients(
    from_: str, reply_to: str, to: str, cc: str, owner: set[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(To, Cc) of a reply-all to one message, without the user. The user's own message: to its To."""
    sender = addresses(reply_to or from_)
    if any(address in owner for _, address in addresses(from_)):
        sender = []  # the user wrote it: the reply goes on to the same people
    to_list = [p for p in sender + addresses(to) if p[1] not in owner]
    to_seen = {p[1] for p in to_list}
    cc_list = [p for p in addresses(cc) if p[1] not in owner and p[1] not in to_seen]
    return _unique(to_list), cc_list


def _unique(people: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out = []
    for p in people:
        if p[1] not in seen:
            seen.add(p[1])
            out.append(p)
    return out


def name_words(text: str) -> set[str]:
    """A person's name as whole words, without accents ("Claudio Rossi" → claudio, rossi);
    one-letter words dropped."""
    return {w for w in re.findall(r"[a-z]+", fold_apostrophe(text)) if len(w) > 1}


def names(people: list[tuple[str, str]]) -> str:
    """ "Claudio, Paola"; a first name two recipients share is replaced by their addresses:
    the user must see who exactly gets it."""
    firsts = [clean(name).split()[0] if clean(name) else "" for name, _ in people]
    shown = [
        first if first and firsts.count(first) == 1 else address
        for first, (_, address) in zip(firsts, people, strict=True)
    ]
    return ", ".join(shown) or "?"


def attachment_sign(filename: str, data: bytes) -> str:
    """What an approval shows of an attachment Pisa put in a draft (name, SHA-256)."""
    return f"{' '.join(filename.split())}|{hashlib.sha256(data).hexdigest()}"


def parse_raw_with(raw: str) -> tuple[set[str], set[str], str, str, list[str]] | None:
    """(To, Cc, Subject, text, attachments) of a draft. A draft may also be a
    multipart/mixed of exactly one text/plain part and attachments, each named (its `attachment_sign`); any
    other shape (an HTML part, a nested multipart, an inline part, a Bcc, a second To) is None: nothing the
    fingerprint can't see goes out (8b N1)."""
    message = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
    if (
        not isinstance(message, EmailMessage)
        or message.get_all("Bcc")
        or len(message.get_all("To") or []) != 1
        or len(message.get_all("Cc") or []) > 1
        or len(message.get_all("Subject") or []) > 1
    ):
        return None
    signs: list[str] = []
    if message.is_multipart():
        if message.get_content_type() != "multipart/mixed":
            return None
        parts = list(message.iter_parts())
        if not parts:
            return None
        text_part, *files = parts
        if (
            not isinstance(text_part, EmailMessage)
            or text_part.is_multipart()
            or text_part.get_content_type() != "text/plain"
            or text_part.get_filename()
        ):
            return None
        for part in files:
            if not isinstance(part, EmailMessage) or part.is_multipart() or not part.get_filename():
                return None
            if part.get_content_disposition() != "attachment":
                return None
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                return None
            signs.append(attachment_sign(str(part.get_filename()), data))
        body = str(text_part.get_content())
    elif message.get_content_type() != "text/plain":
        return None
    else:
        body = str(message.get_content())
    to = {a for _, a in addresses(str(message.get("To") or ""))}
    cc = {a for _, a in addresses(str(message.get("Cc") or ""))}
    return to, cc, str(message.get("Subject") or ""), body, signs


def fingerprint(
    to: list[str] | set[str],
    cc: list[str] | set[str],
    subject: str,
    body: str,
    attachments: list[str] | tuple[str, ...] = (),
) -> str:
    """What an email approval showed (recipients, subject, text and, each attachment's name
    and SHA-256), to compare with the draft before sending."""
    norm = "|".join(sorted(a.lower() for a in to)) + "#" + "|".join(sorted(a.lower() for a in cc))
    text = f"{norm}#{' '.join(subject.split())}#{body.strip()}"
    if attachments:
        text += "#" + "#".join(sorted(attachments))
    return hashlib.sha256(text.encode()).hexdigest()


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith(("re:", "r:")) else f"Re: {subject}".strip()


def reply_raw(
    *,
    sender: str,
    to: list[tuple[str, str]],
    cc: list[tuple[str, str]],
    subject: str,
    body: str,
    in_reply_to: str,
    references: str,
) -> str:
    """The reply as base64url RFC 5322, threaded (In-Reply-To, References), as Gmail's drafts API wants."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(formataddr(p) for p in to)
    if cc:
        message["Cc"] = ", ".join(formataddr(p) for p in cc)
    message["Subject"] = subject
    message["In-Reply-To"] = in_reply_to
    message["References"] = f"{references} {in_reply_to}".strip()
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def new_raw(
    *,
    sender: str,
    to: list[tuple[str, str]],
    subject: str,
    body: str,
    cc: list[tuple[str, str]] | None = None,
) -> str:
    """A new email (no thread) as base64url RFC 5322, as Gmail's drafts API wants: no reply
    headers, ever (an edited one keeps none either)."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(formataddr(p) for p in to)
    if cc:
        message["Cc"] = ", ".join(formataddr(p) for p in cc)
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def package_raw(
    *, sender: str, to: list[tuple[str, str]], subject: str, body: str, files: list[tuple[str, str, bytes]]
) -> str:
    """A new email with attachments (filename, MIME type, bytes): multipart/mixed, one
    text/plain part first, each file an attachment. No reply headers, no Cc, no Bcc."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(formataddr(p) for p in to)
    message["Subject"] = subject
    message.set_content(body)
    for filename, mime, data in files:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                               filename=filename)  # fmt: skip
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def text_line(body: str, lang: str) -> str:
    """ "Testo: <the start>" within the detail's 200 characters, "(+N caratteri)" when the text goes on: the
    whole text is the approval's `preview`."""
    text = " ".join(body.split())
    head = f"{word(lang, 'text')}: "
    if len(head) + len(text) <= DETAIL_LINE:
        return head + text
    room = DETAIL_LINE - len(head) - len(word(lang, "more", n="99999")) - 1
    shown = text[:room].rsplit(" ", 1)[0] or text[:room]
    return f"{head}{shown}… {word(lang, 'more', n=str(len(text) - len(shown)))}"


_QUOTES = str.maketrans({c: " " for c in "\"'«»\u201c\u201d\u2018\u2019`*_"})


def _flat(text: str) -> str:
    return " ".join(text.translate(_QUOTES).casefold().split())


def said_whole(body: str, reply: str) -> bool:
    """The reply says the email's whole text (whitespace, case, quote marks and
    markdown emphasis aside), so the user's yes is to words they heard. An empty body is never heard."""
    flat = _flat(body)
    return bool(flat) and flat in _flat(reply)


VOICE_MARGIN_WORDS = 25  # a body this many words past the user's dictated words needs the card
QUOTE_WORDS = 8  # … or one that repeats this many words in a row of outside text the turn read


def voice_sendable(body: str, owner_words: str, outside: list[str]) -> bool:
    """The stronger fix: a spoken yes may send only a body close to what the user
    dictated: never much longer than their words, never carrying a run of outside text (an email, a page) the
    turn read. Otherwise the card (a tap, the whole text in front of them) sends it."""
    if len(_flat(body).split()) > len(_flat(owner_words).split()) + VOICE_MARGIN_WORDS:
        return False
    return not quotes_outside(body, outside)


def quotes_outside(body: str, outside: list[str]) -> bool:
    """The text repeats QUOTE_WORDS words in a row of outside text the turn read."""
    words = _flat(body).split()
    seen = [f" {_flat(text)} " for text in outside]
    for i in range(len(words) - QUOTE_WORDS + 1):
        run = " " + " ".join(words[i : i + QUOTE_WORDS]) + " "
        if any(run in text for text in seen):
            return True
    return False


def shown_address(name: str, address: str) -> str:
    """ "Niccolò Rossi <n@x.it>" for people to read (formataddr is for headers: it MIME-encodes accents and
    quotes commas); commas dropped from the name so a list of them stays readable."""
    name = " ".join(clean(name).replace(",", " ").split())
    return f"{name} <{address}>" if name else address


DETAIL_MAX = 200  # an approval's `details` item (the WebSocket schema's maxLength)
DETAIL_LINES = 8  # and at most this many items


def _fitted(label: str, items: list[str]) -> list[str]:
    """`label: a, b, c` as detail lines of at most DETAIL_MAX characters each, every item named (a
    package's "Allegati" line past 200 characters made the approval unsendable on the socket); one item longer
    than a line is cut with "…"."""
    lines: list[str] = []
    line = f"{label}:"
    for item in items:
        piece = item if len(item) <= DETAIL_MAX - 4 else item[: DETAIL_MAX - 5] + "…"
        candidate = f"{line} {piece}" if line.endswith(":") else f"{line}, {piece}"
        if len(candidate) > DETAIL_MAX:
            lines.append(line + ",")
            line = f"  {piece}"
        else:
            line = candidate
    lines.append(line)
    return lines


def email_details(
    to: list[tuple[str, str]],
    cc: list[tuple[str, str]],
    subject: str,
    body: str,
    outsiders: list[str],
    lang: str,
    attachments: list[str] | None = None,
) -> list[str]:
    lines = _fitted(word(lang, "to"), [shown_address(n, a) for n, a in to])
    if cc:
        lines += _fitted(word(lang, "cc"), [shown_address(n, a) for n, a in cc])
    lines.append(f"{word(lang, 'subject')}: {clean(subject)}")
    lines.append(text_line(body, lang))
    if attachments:  # every file named on the card (as far as the card's lines allow)
        room = DETAIL_LINES - len(lines) - (1 if outsiders else 0) - 1  # the outsiders' and no-undo lines
        shown = _fitted(f"{word(lang, 'attached')} ({len(attachments)})", attachments)
        if len(shown) > max(room, 1):
            shown = shown[: max(room, 1)]
            named = sum(1 for a in attachments if any(a[:20] in line for line in shown))
            shown[-1] = (shown[-1].rstrip(",") + f" … (+{len(attachments) - named})")[:DETAIL_MAX]
        lines += shown
    if outsiders:
        lines.append(f"{word(lang, 'outside')}: {', '.join(outsiders)}")
    lines.append(word(lang, "no_undo"))
    return lines


# --- Pisa acts on the user's words
# ----------------------------------------------------------------------------

Origin = Literal["words", "proposal", "held"]
EARLY_HOUR = 7  # a move or a new event before 07:00 stays a proposal: "alle 5" may mean 17
# whole words ("dropbox" is no drop), with no negation just before ("non cancellare").
_CANCEL = re.compile(
    r"(cancell|annull|elimin|disdic|disdir|togli)\w*|cancel(s|led|ed)?|delete[sd]?|remove[sd]?|drop(ped)?"
)
_NEGATION = frozenset({"non", "not", "don't", "dont", "never", "mai", "nessun", "senza"})
# The verbs that move or put an event or a task somewhere: the time that counts is the one in their clause.
_MOVE_VERB = re.compile(
    r"(spost|anticip|posticip|rimand|mett|port|fiss|segn|aggiung|crea|prenot|cambi)\w*|"
    r"move[sd]?|push(ed)?|put|reschedule[sd]?|schedule[sd]?|add(ed)?|book(ed)?|set|make"
)
_CLAUSE = re.compile(r"[,;.!?]|\b(?:ma|pero|poi|perche|but|then|because|so)\b")
_UNDO = re.compile(
    r"\b(com'era|come prima|annulla (l'ultima|la modifica|quello che hai fatto)|rimettilo|rimettila|"
    r"undo|put it back|change it back|revert)\b|"
    # the whole message, after a take-back at most ("anzi no, annulla"); "annulla la cena"
    # is a cancel, never an undo
    r"^\W*(?:(?:anzi|no|ok|allora)\W+)*(annulla|annullalo|annullala)\W*$",
    re.IGNORECASE,
)
_QUIETLY = re.compile(
    r"\b(senza avvisar\w*|non avvisar\w*|senza dirlo|without (telling|notifying)|don'?t (tell|notify))\b",
    re.IGNORECASE,
)
_NO_WORDS = frozenset({"no", "nope", "nah", "non", "farlo", "lascia", "stare", "perdere", "grazie", "thanks",
                       "don't", "dont", "do", "it", "not", "meglio", "di", "never", "mind",
                       "lasciamo"})  # fmt: skip


def _verbs(words: list[str], verb: re.Pattern[str]) -> list[int]:
    """Where a whole word of `verb` stands with no negation in the two words before it."""
    return [
        i
        for i, w in enumerate(words)
        if verb.fullmatch(w) and not any(n in _NEGATION for n in words[max(0, i - 2) : i])
    ]


def says_cancel(owner_words: str) -> bool:
    """A cancel verb in the user's words (it: cancella, annulla, elimina, togli, disdici; en: cancel…), as a
    whole word and not negated ("non cancellare la cena" cancels nothing)."""
    words = re.findall(r"[a-z']+", fold_apostrophe(owner_words))
    if any(w in _NEGATION for w in words):  # "non cancellare la cena, cancella il pranzo": one yes
        return False
    return bool(_verbs(words, _CANCEL))


_VERBS = {
    "calendar_create": re.compile(
        r"\b(?:aggiung|mett|cre[aoi]|ricre[aoi]|rimett|riaggiung|segn|fiss|prenot|inseris|add|put|schedul|book|"
        r"set)\w*",
        re.IGNORECASE,
    ),
    "calendar_move": re.compile(
        r"\b(?:spost|anticip|posticip|rimand|cambi|mov|resched|push|shift)\w*", re.IGNORECASE
    ),
    # "annullabile per 24 ore" (Pisa's Undo note) is no cancel
    "calendar_cancel": re.compile(
        r"\b(?:annull(?!abil)|cancell|elimin|togli|disdic|cancel|delet|remov|drop)\w*", re.IGNORECASE
    ),
}


def asked_sentence(reply: str) -> str:
    """The sentence of Pisa's reply that asks (its last "?"), the one their yes answers; the whole
    reply when none asks. "Palestra è fatta, annullabile per 24 ore. Aggiungo il Macro a mezzanotte?" asks the
    Macro, and never a cancel."""
    return _asked(reply)[0]


# A last question that only asks whether Pisa should tell someone
# ("… alle 16 e la palestra alle 17: va bene? Avviso Anna anche io?", "Ti avviso Anna?", "Shall I let Anna
# know?") names no change of its own: their yes still answers the question before it. Pisa never tells anyone
# by itself: that part stays a draft or a question.
_TELL_ONLY = re.compile(
    r"^\s*(?:e\s+)?(?:(?:ti\s+|lo\s+|la\s+|li\s+|le\s+|gli\s+)?(?:avvis\w*|aviso)\b|(?:lo|la|glielo|gliela)\s+dic\w*|"
    r"(?:scrivo|mando\s+un\s+messaggio)\s+(?:a|ad)\b|(?:shall|should)\s+i\s+(?:tell|let|notify|text|message)\b|"
    r"(?:do\s+you\s+want\s+me\s+to\s+)(?:tell|let|notify|text|message)\b)[^?\d]*\?\s*$",
    re.IGNORECASE,
)


def _asked(reply: str, colon: bool = True) -> tuple[str, bool]:
    """(the asked span, whether it was widened back past the question's own sentence). `colon`: also
    widened when the question points back after its own colon."""
    end = reply.rfind("?")
    if end < 0:
        return reply, False
    # a sentence ends at ".", "!" or "?" before a space, or a newline ("alle 16.30?" is one)
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)|\n", reply[:end])]
    asked = reply[(ends[-1] if ends else 0) : end + 1]
    cut = ends[-1] if ends else 0
    if _TELL_ONLY.match(asked) and "?" in reply[:cut] and not any(v.search(asked) for v in _OWN_ACT.values()):
        span, widened = _asked(reply[:cut].rstrip(), colon)
        return span + " " + asked.strip(), widened
    # a question pointing back after the colon of its own sentence
    if points_back(asked) or (colon and ": " in asked and points_back(asked.rpartition(": ")[2])):
        # "Propongo call alle 16 e palestra alle 17. Confermi?", "… alle 16 e
        # la Palestra alle 17. Ti sposto così e avviso Anna?": a question to go on, or one pointing back
        # ("così"), asks their yes for what Pisa said just before it, since its last question; what that binds
        # is still each change named there with its time, and a cancel only with a cancel verb
        start = reply.rfind("?", 0, end) + 1
        return reply[start : end + 1], True
    return asked, False


# in a widened span, a sentence binds a change only when it says Pisa will make it: never
# negated ("La cena con Paola non la cancello", "Non sposto la call alle 16"), never left as it is ("la lascio
# com'è"), never the past ("Ieri alle 16 avevi la call con Anna"). "Non l'ho ancora messo" is pending: no
# refusal
_PAST = re.compile(
    r"\b(?:ieri|l'altro\s+ieri|yesterday|avevi|avevo|aveva|era|eri|ero|c'era|had|was|were)\b", re.IGNORECASE
)
_REFUSED = re.compile(r"\b(?:non|not|never|mai|no|won't|don't|can't|cannot)\b|n't\b", re.IGNORECASE)
_KEPT = re.compile(
    r"\b(?:l[aoie]\s+lascio|lasciamo|la\s+tengo|lo\s+tengo|leave\s+it|keep\s+it|resta\s+com'?\s?è|"
    r"rimane\s+com'?\s?è|com'?\s?è|as\s+it\s+is)\b",
    re.IGNORECASE,
)


def _refused(verbs: re.Pattern[str], sentence: str) -> bool:
    """A verb of the kind in the sentence with a negation in the few words before it, unless "ancora"/"yet"
    makes it pending ("non l'ho ancora messo")."""
    for match in verbs.finditer(sentence):
        before = " ".join(sentence[: match.start()].split()[-4:])
        if _REFUSED.search(before) and not re.search(r"\b(?:ancora|yet)\b", before, re.IGNORECASE):
            return True
    return False


# A widened question binds only Pisa's own act said as Pisa's (a first-person
# present or "I'll …"), or Pisa's proposal, never an infinitive ("Potrei cancellare…", "Non è il caso di
# cancellare…", "Avrei voluto spostare…", "Mi hai chiesto di spostare…"), and never in a sentence that sets
# the change aside ("Invece di spostare la call…, sposto la palestra").
_OWN_ACT = {
    "calendar_create": re.compile(
        r"\b(?:aggiungo|metto|creo|segno|fisso|prenoto|inserisco|ricreo|rimetto|riaggiungo|rifaccio|adding|booking|"
        r"(?:i'?ll|i\s+will|let\s+me)\s+(?:add|put|create|schedule|book|set))\b",
        re.IGNORECASE,
    ),
    "calendar_move": re.compile(
        r"\b(?:sposto|anticipo|posticipo|rimando|cambio|moving|"
        r"(?:i'?ll|i\s+will|let\s+me)\s+(?:move|reschedule|push|shift))\b",
        re.IGNORECASE,
    ),
    "calendar_cancel": re.compile(
        r"\b(?:cancello|annullo|tolgo|elimino|cancelling|canceling|"
        r"(?:i'?ll|i\s+will|let\s+me)\s+(?:cancel|remove|delete|drop))\b",
        re.IGNORECASE,
    ),
}
_OWN_PROPOSAL = re.compile(
    r"\b(?:propongo|ti\s+propongo|suggerisco|ho\s+preparato|ti\s+preparo|preparo|i\s+suggest|i\s+propose|"
    r"i'?ve\s+prepared)\b",
    re.IGNORECASE,
)
_SET_ASIDE = re.compile(
    r"\b(?:invece\s+di|anzich[eé]|piuttosto\s+che|senza|instead\s+of|rather\s+than|without)\b", re.IGNORECASE
)


# a sentence's clauses ("Cancello X e lo ricreo fino alle 12, aggiungo pranzo alle 12 e partenza
# per Y alle 13:30"): cut at a comma, a semicolon, a colon or an "e"/"poi" between clauses, never inside a
# time ("13:30", "alle 9 e mezza", "alle 9 e 30")
_CLAUSE_CUT = re.compile(
    r"\s*[,;]\s+|:\s+|\s+(?:e\s+poi|e|ed|poi|and\s+then|and|then)\s+"
    r"(?!\d|mezz|un\s+quarto|tre\s+quarti|quindici|trenta|quaranta|cinque\b|dieci\b|venti\b)",
    re.IGNORECASE,
)
# … its object said by a pronoun before Pisa's verb ("lo ricreo", "la sposto", "l'aggiungo", "I'll move it")
_PRONOUN = re.compile(r"^\W*(?:(?:lo|la|li|le|gli)\s+(\w+)|l'(\w+))|\b\w+\s+(?:it|them)\b", re.IGNORECASE)


def _pronoun_act(clause: str) -> bool:
    """The clause's object is a pronoun right before a verb of change ("lo ricreo", "l'aggiungo", "move it"),
    never an article before a noun ("la cena alle 20")."""
    found = _PRONOUN.search(clause)
    if found is None:
        return False
    verb = found.group(1) or found.group(2) or found.group(0).split()[0]
    return any(v.match(verb) for v in _VERBS.values()) or any(v.match(verb) for v in _OWN_ACT.values())


def _clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE_CUT.split(sentence) if c and c.strip()]


def _binds_in(
    kind: str, payload: dict[str, Any], sentence: str, question: str, carried: str | None = None
) -> bool:
    """One sentence of a widened span names the change's title, says Pisa will make it
    (the kind's verb, or a proposal; or the question's own verb: "Lo aggiungo così?"), not negated, not kept
    as it is, not in the past, and says its time (a cancel: its cancel verb).

    read clause by clause. A clause binds the change when
    it names it (its title, or a pronoun for the title named just before: "Cancello X e lo ricreo fino alle
    12"), Pisa's own verb of its kind is in it, or, a noun phrase with no verb ("e partenza per Y alle
    13:30"), in the clause before it, and it says the change's time (its start, or its end: "fino alle 12"). A
    cancel still binds only by its own cancel verb in its own clause, and a clause with a cancel verb binds
    nothing else."""
    title = str(payload.get("title") or "")
    if title and not names_item(title, sentence):
        return False
    if _PAST.search(sentence) or _KEPT.search(sentence) or _SET_ASIDE.search(sentence):
        return False
    cancel = _VERBS["calendar_cancel"]
    own = _OWN_ACT.get(kind)
    verbs = _VERBS.get(kind)
    if own is None or verbs is None:
        return _says_time(payload, sentence)
    proposal = kind != "calendar_cancel" and (
        _OWN_PROPOSAL.search(sentence) is not None or own.search(question) is not None
    )
    # `carried`: the kind of Pisa's own verb in the clause before (a noun phrase takes it), from the sentence
    # before too ("Aggiungo il pranzo alle 12. Poi la partenza per Y alle 13:30.")
    named_before = cancelled_before = False
    clauses = _clauses(sentence)
    for n, clause in enumerate(clauses):
        acts = {k for k, v in _OWN_ACT.items() if v.search(clause)}
        any_verb = bool(acts) or any(v.search(clause) for v in _VERBS.values())
        named = (bool(title) and names_item(title, clause)) or (named_before and _pronoun_act(clause))
        if not title:
            named = True
        if kind == "calendar_cancel":
            ours = kind in acts and not _refused(cancel, clause)
        else:
            ours = (
                cancel.search(clause) is None
                and not _refused(verbs, clause)
                and (
                    kind in acts
                    or (not any_verb and (carried == kind or proposal))
                    or (not acts and proposal)
                )
            )
        if not any_verb and _negated(clause):
            ours = False  # "…e nemmeno la call alle 18", "né la call…" bind nothing
        # the end alone binds only a create that recreates what this sentence cancels just
        # before ("Cancello X e lo ricreo fino alle 12"); never a move, never a new thing ("prima della call
        # alle 13")
        recreated = kind == "calendar_create" and cancelled_before and _says_end(payload, clause)
        timed = kind == "calendar_cancel" or _says_time(payload, clause) or recreated
        if named and ours and timed and _says_series(payload, _with_tail(clauses, n)):
            return True
        named_before = named_before or (bool(title) and names_item(title, clause))
        cancelled_before = cancelled_before or (
            bool(title)
            and names_item(title, clause)
            and "calendar_cancel" in acts
            and not _refused(cancel, clause)
        )
        carried = _carried(clause, acts, any_verb, carried)
    return False


# a negation in a clause ("Non sposto…", "nemmeno", "né"): its verb is carried to no noun phrase
_NEGATED_CLAUSE = re.compile(
    r"\b(?:non|no|n[eé]|nemmeno|neanche|neppure|niente|nulla|mai|not|never|nor|neither|nothing)\b|n't\b",
    re.IGNORECASE,
)


def _with_tail(clauses: list[str], n: int) -> str:
    """A clause with the verbless, timeless clauses right after it ("Aggiungo il lavoro alle 13,
    tutti i giorni lavorativi"): what it says about itself; never the next change's words."""
    out = [clauses[n]]
    for clause in clauses[n + 1 :]:
        if re.search(r"\d", clause) or any(v.search(clause) for v in (*_VERBS.values(), *_OWN_ACT.values())):
            break
        out.append(clause)
    return " ".join(out)


def _negated(clause: str) -> bool:
    """The clause says no; "non l'ho ancora messo" (pending) is no refusal, as for `_refused`."""
    if _NEGATED_CLAUSE.search(clause) is None:
        return False
    return re.search(r"\b(?:non|not)\b[^.!?]{0,24}\b(?:ancora|yet)\b", clause, re.IGNORECASE) is None


def _carried(clause: str, acts: set[str], any_verb: bool, before: str | None) -> str | None:
    """The kind of Pisa's own verb a following noun phrase takes from this clause: its one act, never a
    negated one; a negated clause with no verb carries nothing either; a clause with nothing of either keeps
    it."""
    if _negated(clause):
        return None
    if acts:
        return next(iter(acts)) if len(acts) == 1 else None
    return None if any_verb else before


def last_act(sentence: str) -> str | None:
    """The kind of Pisa's own verb in a sentence's last clause with a verb (None: none, or two)."""
    carried: str | None = None
    for clause in _clauses(sentence):
        acts = {k for k, v in _OWN_ACT.items() if v.search(clause)}
        carried = _carried(
            clause, acts, bool(acts) or any(v.search(clause) for v in _VERBS.values()), carried
        )
    return carried  # never from a negated clause


def _says_end(payload: dict[str, Any], text: str) -> bool:
    """The change's end said as its time, right after "fino alle"/"until" ("lo ricreo fino alle
    12"); never a bare "alle", never another thing's time."""
    end = str(((payload.get("body") or {}).get("end") or {}).get("dateTime") or "")
    found = re.search(r"T(\d{2}):(\d{2})", end)
    if found is None:
        return False
    hour, minute = int(found.group(1)), int(found.group(2))
    clocks = {f"{hour}:{minute:02d}", f"{hour:02d}:{minute:02d}", f"{hour}.{minute:02d}"}
    for h in {hour, hour % 12 or 12}:
        clocks |= {spoken_clock(h, minute, lang).casefold() for lang in ("it-IT", "en-US")}
        if minute == 0:
            clocks.add(str(h))
    low = text.casefold()
    return any(
        re.search(
            rf"\b(?:fino\s+(?:alle|al|a)|until|till)\s+(?:le\s+|ore\s+)?{re.escape(c)}(?![\d]|[:.]\d)", low
        )
        for c in clocks
    )


def verb_fits(kind: str, reply: str) -> bool:
    """A cancel is asked with a cancel verb, and nothing else is (one question over all too)."""
    return (_VERBS["calendar_cancel"].search(reply) is not None) == (kind == "calendar_cancel")


def says_change(kind: str, payload: dict[str, Any], reply: str) -> bool:
    """Pisa's spoken question says what the card does, not only its title: a cancel
    says a cancel verb, nothing else says one ("Confermo la riunione con Anna?" is never the yes to cancelling
    it), and a move or a new event says its time ("alle 4", "alle 16:00", "a mezzanotte"), in the question's
    own sentence (i1). A question pointing back ("Va bene?", "Ti sposto così?") is answered
    by the one sentence before it that says this change (`_binds_in`)."""
    span, widened = _asked(reply)
    if widened:
        parts = [s.strip() for s in re.findall(r"[^.!?\n]+(?:[.!?]+|\n|$)", span) if s.strip()]
        question = parts[-1] if parts else span
        # the changes said before the colon of the asking sentence itself are what
        # its pointing-back question asks; each still binds only with Pisa's verb and its time
        head, colon, tail = question.rpartition(": ")
        if colon and re.search(r"\w", head) and points_back(tail):
            parts = [*parts[:-1], head + ".", tail]
            #  1: what proposes is Pisa's own act said by a pronoun before the colon ("Le
            # sposto solo con il tuo sì: va bene?"), never negated; else only the question after it: no
            # sentence binds by the whole asking sentence's verb ("Sposto la palestra alle 17: va bene?" never
            # binds the call another sentence reports)
            question = head if _pronoun_act(head) and not _negated(head) else tail
        carried: str | None = None
        for sentence in parts[:-1]:
            if _binds_in(kind, payload, sentence, question, carried):
                return True
            carried = last_act(sentence)
        span, widened = _asked(reply, colon=False)
        if widened:
            return False
        # widened only at its colon ("Il Macro a mezzanotte, stanotte: confermi?"): what the rule before it
        # binds still binds (i1); the widening only adds what the sentences before it bind
    reply = span
    cancel = _VERBS["calendar_cancel"].search(reply) is not None
    if kind == "calendar_cancel" or cancel:
        return kind == "calendar_cancel" and cancel
    return _says_time(payload, reply) and _says_series(payload, reply)


# a series is bound only by words that say it repeats ("ogni lunedì", "tutti i giorni lavorativi")
_REPEATS_SAID = re.compile(
    r"\bogni\s+\w+|\btutti\s+i\s+(?:giorni|luned|marted|mercoled|gioved|venerd|sabati|domeniche)\w*|"
    r"\bgiorni\s+(?:lavorativi|feriali)\b|\bsettimanal\w*|\bevery\s+\w+|\bweekdays?\b|\bweekly\b",
    re.IGNORECASE,
)


def series_fits(payload: dict[str, Any], reply: str) -> bool:
    """For one question over all: a series in it is said to repeat in the asking sentence."""
    return _says_series(payload, asked_sentence(reply))


def _says_series(payload: dict[str, Any], text: str) -> bool:
    """A change that is a series (`repeat_said`) is said as one: its repeat words are in the text."""
    return not payload.get("repeat_said") or _REPEATS_SAID.search(text) is not None


def says_time_in(payload: dict[str, Any], text: str) -> bool:
    """The change's time (`payload["when"]`) is said in the text."""
    return _says_time(payload, text)


def _says_time(payload: dict[str, Any], reply: str) -> bool:
    """The change's time, said in the text (a change with no time says it by its title)."""
    when = str(payload.get("when") or "")
    if not re.search(r"\d{2}:\d{2}$", when):
        return True
    hour, minute = int(when[-5:-3]), int(when[-2:])
    low = reply.casefold()
    clocks = {f"{hour}:{minute:02d}", f"{hour:02d}:{minute:02d}", f"{hour}.{minute:02d}"}
    for h in {hour, hour % 12 or 12}:
        clocks |= {spoken_clock(h, minute, lang).casefold() for lang in ("it-IT", "en-US")}
        if minute == 0:
            clocks |= {f"alle {h}", f"ore {h}", f"at {h}"}
    if (hour, minute) == (0, 0):
        clocks |= {"mezzanotte", "midnight"}
    if (hour, minute) == (12, 0):
        clocks |= {"mezzogiorno", "noon"}
    return any(re.search(rf"(?<![\d:.]){re.escape(c)}(?![\d])", low) for c in clocks)


def names_item(title: str, owner_words: str) -> bool:
    """The item's title is in the user's words: at least half of its meaningful words, so
    "cancella il promemoria del latte" is no word about the dinner."""
    return bool(title.strip()) and match_score(title, owner_words) >= MATCH_THRESHOLD


# Pisa's one question about all of a turn's changes, not naming each ("li sposto entrambi?"). Never a
# bare "tutto"/"everything" ("Ok tutto fatto, cancello anche la palestra?" names one).
_THEM_ALL = re.compile(
    r"\b(?:entramb[ie]|tutt[ie]\s+e\s+(?:due|tre|quattro|cinque)|(?:li|le)\s+(?:sposto|metto|aggiungo|segno|"
    r"creo|cancello|annullo|tolgo|faccio)|both|all\s+of\s+them|all\s+(?:two|three|four|five)|"
    r"(?:move|add|put|cancel|do)\s+them)\b",
    re.IGNORECASE,
)
_COUNT_SAID = {
    "entrambi": 2, "entrambe": 2, "both": 2, "due": 2, "two": 2, "tre": 3, "three": 3, "quattro": 4,
    "four": 4, "cinque": 5, "five": 5,
}  # fmt: skip
_COUNT_WORD = re.compile(r"\b(?:" + "|".join(_COUNT_SAID) + r")\b", re.IGNORECASE)


def asks_them_all(reply: str, count: int) -> bool:
    """Pisa's reply ends with one question about all `count` held changes at once, not one by name: its own
    sentence says them all, and a number it says ("entrambi", "tutti e tre") is that count."""
    text = reply.strip()
    if not text.endswith("?") or text.count("?") != 1:
        return False
    last = re.split(r"(?<=[.!…:;])\s+", text)[-1]
    if _THEM_ALL.search(last) is None:
        return False
    said = {_COUNT_SAID[m.group(0).lower()] for m in _COUNT_WORD.finditer(last)}
    return not said or said == {count}


_YES_WORDS = frozenset(
    {"si", "ok", "okay", "yes", "certo", "fallo", "falla", "confermo", "procedi", "vai", "sure"}
)
_NO_ANSWER = frozenset({"no", "non", "nope", "lascia", "lasciamo", "don't", "dont", "not"})
# A refusal or a take-back anywhere in the turn: no yes by code ("sì la corsa, il dentista toglilo", "sì,
# aspetta", "yes, hold on", "forget it"). The model asks again or reads it.
_REFUSES = re.compile(
    r"\b(?:togli\w*|salt[ao]\w*|lascial\w*|cancella\w*|annulla\w*|elimina\w*|anzi|aspett\w*|neanche|nemmeno|"
    r"meglio|invece|ripensandoci|skip|drop|forget|scratch|wait|hold|instead|actually|never)\b"
)


_PROPOSAL_ORDINALS = {
    "primo": 1, "prima": 1, "first": 1, "1": 1, "secondo": 2, "seconda": 2, "second": 2, "2": 2,
    "terzo": 3, "terza": 3, "third": 3, "3": 3, "quarto": 4, "quarta": 4, "fourth": 4, "4": 4,
    "quinto": 5, "quinta": 5, "fifth": 5, "5": 5, "ultimo": -1, "ultima": -1, "last": -1,
}  # fmt: skip
_ALL = frozenset({"tutti", "tutte", "entrambi", "entrambe", "both", "all", "tutto"})


def means_proposal(owner_words: str, titles: list[str], index: int) -> bool:
    """Which of the proposals Pisa asked (`titles`, in the order Pisa said them) the
    user's words answer: the ordinal they say ("solo il primo"), else the title they name, else the only one
    asked, or all of them ("sì, tutti e due"). The model's pick alone answers nothing."""
    words = _WORD.findall(fold_apostrophe(owner_words.replace("'", " ").replace("\u2019", " ")))
    said = [_PROPOSAL_ORDINALS[w] for w in words if w in _PROPOSAL_ORDINALS and not w.isdigit()]
    if said:
        return any(n == index + 1 or (n == -1 and index == len(titles) - 1) for n in said)
    if len(titles) == 1 or _ALL & set(words):
        return True
    return names_item(titles[index], owner_words)


def answers_proposal(quote: str, owner_words: str, yes: bool) -> bool:
    """The user's own words answer one proposal ("sì, solo il primo", "no al secondo").
    the quote only finds its clause; the whole clause is judged, and the whole turn
    for a question or a negation: "no, ok, lascia stare la corsa" quoted as "ok" is no yes, nor is "non va
    bene", "ok, ma la mandi davvero?" or "Marco ha detto sì, ma io non voglio"."""
    plain = fold_apostrophe(owner_words)
    found = _quoted(owner_words, quote)
    if not found or "?" in owner_words:
        return False
    spans = _spans(plain)
    clause = set(_WORD.findall(" ".join(plain[a:b] for n, (a, b) in enumerate(spans) if n in found)))
    turn = set(_WORD.findall(plain))
    if clause & (THEIRS | {"detto", "dice", "said", "says"}):
        return False  # someone else's yes
    if yes:
        first = set(_WORD.findall(plain[spans[0][0] : spans[0][1]])) if spans else set()
        # the yes in the quote's clause, or opening the turn ("sì, solo la corsa")
        said_yes = bool(_YES_WORDS & (clause | first)) or "va bene" in " ".join(plain[a:b] for a, b in spans)
        return said_yes and not (_NO_ANSWER | _NEGATION) & turn and not _REFUSES.search(plain)
    return bool(_NO_ANSWER & clause)


def says_undo(owner_words: str) -> bool:
    """The user asks to undo Pisa's last change ("rimettilo com'era", "annulla", "undo", "put it back")."""
    return bool(_UNDO.search(fold_apostrophe(owner_words)))


def says_quietly(owner_words: str) -> bool:
    """ "Sì, ma senza avvisarli" / "yes, without telling them": the change notifies nobody."""
    return bool(_QUIETLY.search(fold_apostrophe(owner_words)))


def is_short_no(text: str) -> bool:
    """ "No", "no grazie", "lascia stare", "no, non farlo": the whole message declines. "No, alle 9" is a
    correction, not a no."""
    words = re.findall(r"[a-z']+", fold_apostrophe(text))
    if not words or len(words) > 4 or any(w not in _NO_WORDS for w in words):
        return False
    return words[0] in ("no", "nope", "nah", "lascia", "lasciamo", "non", "never", "meglio", "don't", "dont")


# --- one command, several events, each on its own clause and range ------------------------------------------

_SEGMENT = re.compile(r"[,;.!?\u2026]|\b(?:poi|ma|pero|then|but)\b")
_SLOT_HOUR = rf"([01]?\d|2[0-3]|{_HOUR_WORD})(?:[:.]([0-5]\d))?"
# A time and the word before it, which says what it is: a range's start, its end, or a plain "at".
_SLOT_MARK = re.compile(
    rf"\b(fino alle|fino all'|fino a|entro le|tra le|fra le|dalle|dall'|e le|alle|all'|ore|between|from|"
    rf"until|"
    rf"till|to|and|at)\s*{_SLOT_HOUR}\b"
)
_RANGE_FROM = frozenset({"tra le", "fra le", "dalle", "dall'", "between", "from"})
_RANGE_TO = frozenset({"e le", "alle", "all'", "to", "and", "until", "till"})
_UNTIL = frozenset({"fino alle", "fino all'", "fino a", "entro le", "until", "till"})


@dataclass(frozen=True)
class Slot:
    """One clause of the user's words: its text, the start times said in it, and the end said, if any."""

    text: str
    starts: tuple[tuple[int, int], ...]
    end: tuple[int, int] | None


def _hour(said: str, minutes: str | None) -> tuple[int, int]:
    hour = _HOUR_WORDS[said] if said in _HOUR_WORDS else int(said)
    return hour, int(minutes or 0)


def said_slots(owner_words: str, *, events: bool = False) -> list[Slot]:
    """The user's words, clause by clause, with the times each says: "tra le 10 e le 14" (10 to 14), "dalle
    quattro" (from 4), "fino alle quattro" (until 4), "alle 14" (at 14). `events`: "pranzo alle 13 e dentista
    alle 17" is two clauses."""
    slots = []
    noon = ("mezzogiorno", "noon")
    said = fold_apostrophe(
        _NOON.sub(lambda m: "alle 12" if m.group(1).lower() in noon else "alle 0", owner_words)
    )
    for a, b in _pieces(said, events):
        text = said[a:b]
        starts: list[tuple[int, int]] = []
        end: tuple[int, int] | None = None
        ranging = False
        for mark in _SLOT_MARK.finditer(text):
            word, at = mark.group(1), _hour(mark.group(2), mark.group(3))
            if word in _UNTIL:
                end, ranging = at, False
            elif ranging and word in _RANGE_TO:
                end, ranging = at, False
            elif word in _RANGE_FROM:
                starts.append(at)
                ranging = True
            elif word not in ("e le", "and", "to"):
                starts.append(at)
        slots.append(Slot(text.strip(), tuple(starts), end))
    return slots


def _same_time(said: tuple[int, int], at: datetime) -> bool:
    """ "Alle quattro" is 4 or 16: people say the afternoon in 12-hour form. Only for 1 to 7: "alle 10" is
    10, and 22 is the model's guess."""
    hour, minute = said
    return minute == at.minute and (at.hour == hour or (1 <= hour <= EARLY_HOUR and at.hour == hour + 12))


# Someone else's plans ("Marco ha una call alle 4", "Nino has the dentist at 10") are no command for the
# user's calendar, even with a "metti" elsewhere in the sentence.
THEIRS = frozenset({"ha", "hanno", "has", "have", "c'ha"})
CLAUSE_NO = _NEGATION | {"no"}
_WORD = re.compile(r"[a-z0-9']+")


def _spans(plain: str) -> list[tuple[int, int]]:
    """Where each clause of `said_slots` stands in the text (the same split, the same empty ones left out)."""
    spans, at = [], 0
    for sep in [*_SEGMENT.finditer(plain), None]:
        stop = sep.start() if sep is not None else len(plain)
        if plain[at:stop].strip():
            spans.append((at, stop))
        at = sep.end() if sep is not None else stop
    return spans


# "e" between two events, each with its time ("pranzo alle 13 e dentista alle 17"); never inside a time ("tra
# le 10 e le 14", "alle 9 e mezza").
_AND = re.compile(r"\b(?:e|and)\b(?!\s+(?:le|l'|mezza|mezzo|un quarto|tre quarti|\d))")


def _pieces(plain: str, events: bool) -> list[tuple[int, int]]:
    """The clauses' spans; with `events`, a clause naming two events joined by "e", each with its own time,
    is two."""
    if not events:
        return _spans(plain)
    out = []
    for a, b in _spans(plain):
        at = a
        for joint in _AND.finditer(plain, a, b):
            half = set(_WORD.findall(plain[at : joint.start()]))
            if half & (CLAUSE_NO | THEIRS):
                break  # the half after "e" keeps the negation and the subject before it
            if _SLOT_MARK.search(plain[at : joint.start()]) and _SLOT_MARK.search(plain[joint.end() : b]):
                out.append((at, joint.start()))
                at = joint.end()
        out.append((at, b))
    return [(x, y) for x, y in out if plain[x:y].strip()]


def _quote_list(quote: str) -> list[str] | None:
    """A JSON list of two or more quotes ('["a", "b"]'), else None (one quote, brackets or not)."""
    text = quote.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    try:
        items = json.loads(text)
    except ValueError:
        return None
    if (
        not isinstance(items, list)
        or len(items) < 2
        or not all(isinstance(i, str) and i.strip() for i in items)
    ):
        return None
    return [str(i) for i in items]


def _quoted(owner_words: str, quote: str, events: bool = False) -> set[int] | None:
    """The clauses of the user's words that `quote` covers (its words in order, punctuation aside), or None
    when the user didn't say it."""
    if (several := _quote_list(quote)) is not None:
        # a list of their quotes, in any order: each one theirs, the clauses of all of them
        found = [_quoted(owner_words, q, events) for q in several]
        return None if any(f is None for f in found) else set().union(*(f for f in found if f is not None))
    plain = fold_apostrophe(owner_words)
    said = [(m.group(), m.start()) for m in _WORD.finditer(plain)]
    words = _WORD.findall(fold_apostrophe(quote))
    if not words:
        return None
    for i in range(len(said) - len(words) + 1):
        if [w for w, _ in said[i : i + len(words)]] == words:
            first, last = said[i][1], said[i + len(words) - 1][1]
            return {n for n, (a, b) in enumerate(_pieces(plain, events)) if a <= last and first < b}
    return None


_DAY_RANGE = re.compile(r"\b(?:da|dal|from)\s+(\S+)\s+(?:a|al|to|till|until)\s+(\S+)")
RANGE_DAYS = 14  # "da martedì a giovedì": a range of days, at most two weeks


def _range(text: str, today: date, small_hours: bool) -> set[date] | None:
    """The days of "da martedì a giovedì" / "from Tuesday to Thursday" (one event a day), or None."""
    found = _DAY_RANGE.search(text)
    if found is None:
        return None
    first = day_named(found.group(1), today, small_hours=small_hours)
    last = day_named(found.group(2), today, small_hours=small_hours)
    if first is None or last is None:
        return None
    if last < first:
        last += timedelta(days=7)
    if (last - first).days > RANGE_DAYS:
        return None
    return {first + timedelta(days=n) for n in range((last - first).days + 1)}


def _days(clauses: list[Slot], n: int, default_day: date, today: date, small_hours: bool) -> set[date]:
    """The day of the n-th clause: the one it names (or the days of its range), else the nearest one named
    before it, else the one day the whole sentence names, else `default_day` ("oggi alle 10 il dentista,
    domani alle 15 la call": the call is tomorrow)."""
    for slot in reversed(clauses[: n + 1]):
        if others(slot):
            continue  # never a day or a range from someone else's clause
        if (days := _range(slot.text, today, small_hours)) is not None:
            return days
        if (day := day_named(slot.text, today, small_hours=small_hours)) is not None:
            return {day}
    named = {
        d
        for s in clauses
        if not others(s) and (d := day_named(s.text, today, small_hours=small_hours)) is not None
    }
    return named if len(named) == 1 else {default_day}


# A clause that says only a time ("…, dalle 9 alle 18"): the time of the event named just before it.
_TIME_ONLY_WORDS = frozenset(
    {"dalle", "alle", "all'", "dall'", "tra", "fra", "le", "e", "fino", "a", "al", "ore", "entro", "mezza",
     "mezzo", "un", "quarto", "tre", "quarti", "from", "to", "until", "till", "at", "and", "between"}
)  # fmt: skip


def _only_time(slot: Slot) -> bool:
    words = _WORD.findall(slot.text)
    return bool(slot.starts) and all(w.isdigit() or w in _TIME_ONLY_WORDS or w in _HOUR_WORDS for w in words)


@dataclass(frozen=True)
class Change:
    """`owner_said`: the change the model wants to make, and the user's words it cites."""

    kind: str  # calendar_create | calendar_move | calendar_cancel
    title: str
    start: datetime | None = None  # local, naive
    end: datetime | None = None
    quote: str | None = None  # owner_said: the piece of the user's words the change comes from
    named: bool = False  # the event Pisa's own tool touched in this device's previous turn


def owner_said(
    change: Change,
    owner_words: str,
    *,
    default_day: date | None = None,
    today: date | None = None,
    earlier: str | None = None,
    small_hours: bool = False,
) -> str | None:
    """RAIL (the condition): the change is in the user's own words. The quote
    existing is not enough; code checks, on the clause it stands in:
    - the quote is from the current turn (or from `earlier`, the turn whose proposal this corrects);
    - its clause commands: a put/move/cancel verb in it, in a clause before it, or in a command clause of its
      own ("…, metti tutto sul calendario");
    - no "?", no negation ("no" included), no third-party subject ("Marco ha una call alle 4");
    - it names the event (or Pisa's tool touched it in the previous turn), and the time is the clause's own,
      with +12 only for 1-7 ("alle quattro" is 4 or 16, "alle 10" never 22);
    - the day is the clause's own, else the nearest one before it, else the sentence's one day.
    Without a quote, every clause of the current turn is tried. None when grounded; else why not (the
    `needs_yes` feedback)."""  # fmt: skip
    # the words of the proposal this corrects count only when this turn's own words carry the
    # change (a time or a day): "ok grazie, e che tempo fa?" corrects nothing.
    earlier = earlier if earlier and _carries_change(owner_words, today, small_hours) else None
    for words in (owner_words, earlier) if change.quote else (owner_words,):
        if not words:
            continue
        events = change.kind != "calendar_move"
        clauses = None if change.quote is None else _quoted(words, change.quote, events)
        if change.quote is not None and clauses is None:
            continue
        why = _grounded(change, words, clauses, default_day, today, small_hours)
        if (
            why is not None
            and earlier
            and words == owner_words
            and _day_corrected(change, owner_words, earlier, default_day, today, small_hours)
        ):
            return None  # "no aspetta, intendevo sabato": the day theirs, the rest from before
        return why
    return "owner_said isn't in the user's words: cite them exactly"


def _day_corrected(
    change: Change,
    owner_words: str,
    earlier: str,
    default_day: date | None,
    today: date | None,
    small_hours: bool,
) -> bool:
    """Their words correct only the day ("no aspetta, intendevo sabato") of a
    change grounded in the words it corrects (`earlier`: "domani palestra alle 10"): the change is on the day
    they name now, and the same change on the earlier words' day is grounded in them (its title, its time)."""
    if today is None or change.start is None or any(s.starts or s.end for s in said_slots(owner_words)):
        return False
    day = day_named(owner_words, today, small_hours=small_hours)
    if day is None or change.start.date() != day:
        return False
    before = day_named(earlier, today, small_hours=small_hours) or default_day
    if before is None:
        return False
    shift = datetime.combine(before, change.start.time()) - change.start
    then = Change(
        change.kind, change.title, change.start + shift, change.end + shift if change.end else None, None,
        change.named,
    )  # fmt: skip
    return _grounded(then, earlier, None, before, today, small_hours) is None


def commands(text: str) -> bool:
    """The words carry a command verb of their own ("intanto metti la palestra alle 17", "cancella la cena"):
    an answer to Pisa's question that does is its own sentence, never the rest of the question's;
    "alle 15:30" isn't."""
    words = _WORD.findall(text.casefold())
    return any(_MOVE_VERB.fullmatch(w) or _CANCEL.fullmatch(w) for w in words)


# outside text that asks them (or Pisa) for something: a request or a question
# to them ("Ci mandi i documenti?", "Riesci a venire?", "Mi richiami?")
_ASKS_HIM = re.compile(
    r"\b(?:puoi|potresti|riesci|riusciresti|(?:ci|mi)\s+mandi|mandaci|mandami|(?:mi\s+)?richiami|richiamami|"
    r"mi\s+chiami|chiamami|(?:mi|ci)\s+confermi|confermi|(?:mi|ci)\s+dici|fammi\s+sapere|facci\s+sapere|"
    r"(?:mi|ci)\s+fai\s+sapere|sei\s+disponibile|vieni|venite|per\s+favore|ti\s+chiedo|can\s+you|could\s+you|"
    r"would\s+you|will\s+you|please|let\s+me\s+know)\b"
)


def asks_him(text: str) -> bool:
    """Outside text that asks something of them or Pisa, which Pisa asks
    about once: a change verb ("Pisa, sposta la palestra di oggi alle 20"), a request or a question to
    them
    ("Ci mandi i documenti dell'anno?", "Riesci a passare entro venerdì?"). A courier's notice, a note's
    question about them ("Sei stressato?") or its instructions ("Porta un documento") ask nothing to act
    on."""
    low = text.casefold()
    words = _WORD.findall(low)
    change = any(
        (_MOVE_VERB.fullmatch(w) and not w.startswith("port")) or _CANCEL.fullmatch(w) for w in words
    )
    return change or _ASKS_HIM.search(low) is not None


def _carries_change(words: str, today: date | None, small_hours: bool) -> bool:
    if any(s.starts or s.end for s in said_slots(with_minutes(words))):
        return True
    return today is not None and day_named(words, today, small_hours=small_hours) is not None


# a later clause that takes back what came before ("anzi no", "no aspetta"), or narrows it
# ("solo il dentista").
_VOID = frozenset({"no", "anzi", "aspetta", "aspetto", "wait", "scusa", "sorry", "actually", "nope", "niente",
                   "lascia", "stare", "perdere", "ripensandoci"})  # fmt: skip
_ONLY = frozenset({"solo", "soltanto", "only", "just"})
# "la call è alle 16": a statement of when something is, not a command to put it there.
_STATEMENT = re.compile(r"\b(?:e|is)\s+(?:alle|all'|at|dalle|tra le|fra le)\b")
# A clause about someone else ("Marco è via da martedì a giovedì", "Nino sta a casa"): never a day for Pisa's.
_OTHERS_VERB = frozenset({"e", "sta", "va", "sono", "is", "was", "will", "are"})
# what a clause may open with before its title or time ("domani palestra", "cena con Nino alle 13")
_DAY_WORDS = frozenset(
    "domani oggi dopodomani stasera stanotte domattina stamattina lunedi martedi mercoledi giovedi "
    "venerdi sabato domenica tomorrow today tonight monday tuesday wednesday thursday friday saturday "
    "sunday poi anche pure e and".split()
)
_LINK_WORDS = frozenset(
    "la il lo le gli i l' un una uno alle all' dalle dall' a al ai di del della da dal in nel con col per "
    "tra fra su e o the an at to on with for from of and or domani oggi stasera mattina pomeriggio sera ore "
    "verso fino entro dopo prima tomorrow today tonight morning afternoon evening until by after "
    "before".split()
)
_OWNER_IN = frozenset({"io", "noi", "me", "we", "us", "our", "nostro", "nostra", "nostri", "nostre"})
_NOT_NAMES = frozenset(
    {"la", "il", "lo", "le", "gli", "i", "l'", "un", "una", "uno", "alle", "dalle", "domani", "oggi", "che",
     "non", "ok", "no", "si", "io", "tu", "ci", "it", "the", "this", "that"}
)  # fmt: skip


# A clause that opens by taking back what came before: "anzi alle 5", "no aspetta", "meglio alle 5", "never
# mind", "forget it", "scratch that", "non importa", "ho cambiato idea", "lascia perdere"; or says "invece" /
# "instead" ("metti la call alle 4, anzi alle 5" acted at 4).
_TAKE_BACK = re.compile(
    # "aspetto un attimo / un secondo / un momento" still takes back; "aspetto la spesa" doesn't
    r"^(?:(?:no|anzi|aspetta|wait|scusa|sorry|actually|nope|meglio|ripensandoci|niente)\b"
    r"|aspetto un (?:attimo|secondo|momento)\b"
    r"|never ?mind\b|forget (?:it|that)\b|scratch that\b|non importa\b|(?:ho )?cambiato idea\b"
    r"|cambio idea\b|lascia(?:mo)? (?:stare|perdere)\b|hold on\b|make it\b)|\b(?:invece|instead)\b"
)


def _lead_off(words: list[str]) -> list[str]:
    """The clause's words after its opening take-back words ("anzi solo la call" → "solo la call")."""
    n = 0
    while n < len(words) and words[n] in _VOID:
        n += 1
    return words[n:]


def voids(slot: Slot) -> bool:
    words = _WORD.findall(slot.text)
    return bool(words) and (all(w in _VOID for w in words) or bool(_TAKE_BACK.search(slot.text.strip())))


def others(slot: Slot) -> bool:
    words = _WORD.findall(slot.text)
    # the user is in it ("Marco e io andiamo a Milano domani", "Nino viene con me"): the user's day
    if set(words) & _OWNER_IN or any(w.endswith("iamo") for w in words):
        return False
    if set(words) & THEIRS:
        return True
    if len(words) > 2 and words[1] in _OTHERS_VERB and words[0] not in _NOT_NAMES and words[0].isalpha():
        return True
    # someone else's plan with any verb ("Marco parte domani", "Giulia
    # viene alle 19", "Marco vuole la call alle 4"): a name or a noun, then a word that is no article,
    # preposition or number, in a clause with no verb that puts or moves. A false match only holds the change.
    return (
        len(words) > 2
        and not _verbs(words, _MOVE_VERB)
        and words[0].isalpha()
        and words[0] not in _NOT_NAMES
        and words[0] not in _DAY_WORDS
        and words[1] not in _LINK_WORDS
        and not words[1].isdigit()
    )


def _stated(slot: Slot) -> bool:
    found = _STATEMENT.search(slot.text)
    return found is not None and not _SLOT_MARK.search(slot.text[: found.start()])


def _taken_back(clauses: list[Slot], n: int, title: str) -> bool:
    """A later clause takes clause n back: a "solo …" that doesn't name this event, or an "anzi no"."""
    narrows = [c for c in clauses[n + 1 :] if (rest := _lead_off(_WORD.findall(c.text))) and rest[0] in _ONLY]
    if narrows:
        return not any(names_item(title, c.text) for c in narrows)
    return any(voids(c) for c in clauses[n + 1 :])


def _grounded(
    change: Change, words: str, quoted: set[int] | None, default_day: date | None, today: date | None,
    small_hours: bool,
) -> str | None:  # fmt: skip
    if "?" in words:
        return "the user asked a question, not a command"
    if change.kind == "calendar_cancel":
        if not says_cancel(words):
            return "the user's words don't say to cancel it"
        if not (change.named or names_item(change.title, words)):
            return "the user's words don't name this event"
        return None
    assert change.start is not None and default_day is not None and today is not None
    move = change.kind == "calendar_move"
    # The times as the user said them ("alle 9 e mezza", "9pm"); for a move, a bound ("dopo le 21") is none.
    norm = with_minutes(words)
    norm = _BOUND_TIME.sub(" ", norm) if move else norm
    if move and any(w in _NEGATION for w in _WORD.findall(fold_apostrophe(norm))):
        return "a negation in the user's words"  # "sposta la cena alle 21 non alle 22"
    clauses = said_slots(norm, events=not move)
    if quoted is not None and len(clauses) != len(_pieces(fold_apostrophe(words), not move)):
        quoted = None  # writing the times out changed the clauses ("9 a.m."): every clause is tried
    put = [bool(_verbs(_WORD.findall(c.text), _MOVE_VERB)) for c in clauses]
    commands_all = any(p and not c.starts and c.end is None for p, c in zip(put, clauses, strict=True))
    named = change.named or (move and names_item(change.title, words))
    why = "the user's words don't say this event at this time (no command with its title and time)"
    for n, slot in enumerate(clauses):
        # its quote was the hours at home alone ("resto a casa dalle 12 alle 16"):
        # the wait said just before them ("aspetto la spesa, …") is the same event's clause
        waits = (
            n + 1 < len(clauses) and not slot.starts and slot.end is None and bool(_WAIT.search(slot.text))
            and bool(_AT_HOME.search(clauses[n + 1].text))
        )  # fmt: skip
        if quoted is not None and n not in quoted and not (waits and n + 1 in quoted):
            continue
        if not (any(put[: n + 1]) or commands_all):
            continue
        if (
            any(w in CLAUSE_NO or w in THEIRS for w in _WORD.findall(slot.text))
            or _stated(slot)
            or others(slot)  # "…, Giulia viene alle 19" is Giulia's, under any "metti"
        ):
            why = "the clause is negated, a statement, or about someone else's plans"
            continue
        if _taken_back(clauses, n, change.title):
            why = 'the user took it back after ("anzi no", "solo …")'
            continue
        # The clause names the event, or has its own verb for one named elsewhere ("la cena, spostala alle
        # 21"): never a time from a clause about something else ("alle 9 ho il treno, sposta la cena").
        # in the wait ("aspetto la spesa, resto a casa dalle 12
        # alle 16") the two clauses say one event: both name its title ("A casa, in attesa della spesa")
        wait_pair = (
            n + 1 < len(clauses)
            and bool(_WAIT.search(slot.text))
            and bool(_AT_HOME.search(clauses[n + 1].text))
        )
        pair_text = f"{slot.text} {clauses[n + 1].text}" if wait_pair else slot.text
        if not (
            names_item(change.title, slot.text) or names_item(change.title, pair_text) or (named and put[n])
        ):
            continue
        timed = n
        if not slot.starts and slot.end is None and n + 1 < len(clauses) and _only_time(clauses[n + 1]):
            timed = n + 1  # "metti il convegno da martedì a giovedì, dalle 9 alle 18"
        elif (
            not slot.starts and slot.end is None and n + 1 < len(clauses) and _WAIT.search(slot.text)
            and _AT_HOME.search(clauses[n + 1].text)
        ):  # fmt: skip
            timed = n + 1  # "aspetto la spesa, resto a casa dalle 12 alle 16"
        if not _clause_time(clauses, timed, change.start, None if move else change.end):
            continue
        if change.start.date() not in _days(clauses, n, default_day, today, small_hours):
            why = "the user said another day"
            continue
        return None
    return why


# a wait at home said in two clauses, the thing waited for, then the hours they stay in
_WAIT = re.compile(r"\b(?:aspetto|attendo|aspettiamo|arriva|arrivano|consegn\w*|waiting\s+for|expecting)\b")
_AT_HOME = re.compile(
    r"\b(?:resto|rimango|sto|sono|restiamo|rimaniamo)\s+(?:a\s+casa|in\s+casa)\b|\b(?:stay(?:ing)?|i'?m|i\s+am)\s+"
    r"(?:at\s+)?home\b"
)


def _clause_time(clauses: list[Slot], n: int, start: datetime, end: datetime | None) -> bool:
    """The clause's own start ("alle 14 in ufficio", "dalle quattro"), or its end with the start said in the
    clause before ("alle 14 …, call con Marco fino alle quattro"); an end said must be the event's."""
    slot = clauses[n]
    if slot.end is not None and end is not None and not _same_time(slot.end, end):
        return False
    if len(slot.starts) == 1:
        return _same_time(slot.starts[0], start)
    if not slot.starts and slot.end is not None and end is not None and n > 0:
        before = clauses[n - 1]
        if _STATED & set(_WORD.findall(before.text)):
            return False  # "alle 9 ho il treno, metti la call fino alle 11": the 9 is the train's
        return any(_same_time(t, start) for t in (*before.starts, *([before.end] if before.end else [])))
    return False


# a clause that states a plan already there ("alle 9 ho il treno", "c'è la
# riunione") has an event of its own: an end-only clause after it never borrows its start. An item of the
# command's own list ("alle 14 in ufficio, call con Marco fino alle quattro") still gives it.
_STATED = frozenset({"ho", "abbiamo", "c'e", "ce", "c'ho", "ha", "hanno", "have", "has", "i've", "there's"})


def day_kept(start: datetime, old_start: datetime, owner_words: str, today: date, small_hours: bool) -> bool:
    """ "sono per oggi, il 30": a change of day only, at the same time, to the day the user named."""
    named = day_named(owner_words, today, small_hours=small_hours)
    return (start.hour, start.minute) == (old_start.hour, old_start.minute) and named == start.date()


# RAIL: act at once only on the user's own words for their own event; outside text or other
# people: one yes.
def grounding(
    kind: str,
    owner_words: str,
    *,
    read_outside: bool,
    attendees: bool,
    start: datetime | None = None,
    default_day: date | None = None,
    today: date | None = None,
    title: str | None = None,
    named: bool = False,
    end: datetime | None = None,
    old_start: datetime | None = None,
    small_hours: bool = False,
    quote: str | None = None,
    earlier: str | None = None,
) -> tuple[Origin, str]:
    """`words` (done at once), `proposal` (one yes, it names who is notified) or `held` (one yes,
    worded as Pisa's proposal: not in the user's words, or after outside text), with the reason a yes is
    needed (the tool tells the model, never a silent hold). `start` is local time. `named`: the event Pisa's
    own tool touched in this device's previous turn ("spostala alle 21" right after;, choice
    1), known from code. `quote`: owner_said, checked by `owner_said`; `earlier`: the user's words of the
    proposal this corrects. `old_start`: the time a change of day only keeps ("sono per oggi");
    `small_hours`: "domani" said before 05:00 is today's date."""  # fmt: skip
    if read_outside:
        return "held", "it comes after outside text (an email, a page, an invite): the user's yes decides"
    if OWNER_SAID_REQUIRED and not (quote or "").strip():
        # a write cites the user's words or it waits
        return "held", "no owner_said: cite the user's own words this change comes from, or ask them"
    change = Change(kind, title or "", start, end, quote, named)
    why = owner_said(change, owner_words, default_day=default_day, today=today, earlier=earlier,
                     small_hours=small_hours)  # fmt: skip
    if why is not None and kind == "calendar_move" and old_start is not None and start is not None:
        assert today is not None
        kept = day_kept(start, old_start, owner_words, today, small_hours)
        if kept and (named or names_item(title or "", owner_words)):
            why = None  # "sono per oggi, il 30": the day only, at the same time
    if why is not None:
        return "held", why
    if attendees:
        return "proposal", "it notifies other people: name them in the question"
    if start is not None and start.hour < EARLY_HOUR:
        return "proposal", f'it starts before {EARLY_HOUR:02d}:00 ("alle 5" may mean 17): say the hour'
    return "words", ""


# off: a write without `owner_said` is tried clause by clause; on, such a write waits for the user's yes.
OWNER_SAID_REQUIRED = False


def decide_mode(kind: str, owner_words: str, **given: Any) -> Origin:
    """The origin alone (see `grounding`)."""
    return grounding(kind, owner_words, **given)[0]


def with_notified(summary: str, notified: list[str], lang: str) -> str:
    """ "Sposto «Riunione» a oggi 15:00?" → "… e avviso Marco, Anna?": the one yes names who hears of it."""
    names = ", ".join(notified[:4]) + (f" (+{len(notified) - 4})" if len(notified) > 4 else "")
    return f"{summary.rstrip('?').rstrip()} {word(lang, 'notify', names=names)}?"


_MARKED_NO = re.compile(r"^\W*(no|ma no|nope|no no|anzi|actually)\b", re.IGNORECASE)


CORRECTION_WORDS = 4  # "no, alle 22": the words after "no" are only the correction
_TIME_ONLY = frozenset({"alle", "all'", "a", "ad", "le", "ore", "e", "mezza", "mezzo", "un", "quarto", "at",
                        "and", "half", "h"})  # fmt: skip


# a bare "no, alle 9" right after Pisa said a time; anything richer goes to the model.
def corrected_time(owner_words: str) -> tuple[int, int] | None:
    """A marked "no, alle 9" ("ma no, alle 21:30", "no, at 9") right after Pisa echoed a time is a
    correction of that slot: the one clock time it names, or None. Only when the rest is just the correction
    ("no, alle 23 c'è la partita" says something else)."""
    marked = _MARKED_NO.search(owner_words)
    if not marked:
        return None
    rest = owner_words[marked.end() :]
    if len(re.findall(r"[\w:.']+", rest)) > CORRECTION_WORDS:
        return None
    # Only the time itself: "no, alle 9 di sera" (a period word) or "no, la palestra alle 22" (another item)
    # go to the model, never a move by code.
    if any(w not in _TIME_ONLY for w in re.findall(r"[a-z']+", fold_apostrophe(rest))):
        return None
    times = clock_times(with_minutes(owner_words))
    return next(iter(times)) if len(times) == 1 else None


# "Mandala", "inviala pure", "send it": whole words that mean "send it" ("mandami il riassunto", "le mail
# inviate", "mandarini" don't), or a short message that starts with the verb ("manda pure", "invia").
_SEND_IT = frozenset({"mandala", "mandalo", "mandale", "mandali", "mandatela", "inviala", "invialo",
                      "inviale", "inviali", "spediscila", "spediscilo", "spediscile", "send it", "send that",
                      "send this", "send the email", "send the reply"})  # fmt: skip
_SEND_SHORT = frozenset({"manda", "invia", "spedisci", "send"})


# Words that may stand around "mandala" without changing it ("sì, mandala pure"); anything else ("mandala
# dopo", "mandala a Marco invece", "send it to my boss") is not a plain send.
_SEND_FILLERS = frozenset({"si", "ok", "okay", "pure", "dai", "allora", "va", "bene", "vai", "yes", "yeah",
                           "sure", "please", "go", "ahead", "perfetto", "grazie", "thanks"})  # fmt: skip


# RAIL: an email leaves only on the user's own "mandala" after the read-back.
def says_send(owner_words: str) -> bool:
    words = [w for w in re.findall(r"[a-z']+", fold_apostrophe(owner_words)) if w not in _SEND_FILLERS]
    if not words or any(n in _NEGATION for n in words):
        return False
    text = " ".join(words)
    return text in _SEND_IT or (len(words) == 1 and words[0] in _SEND_SHORT)


# --- a plan or task change grounded in the user's words acts even after outside text ------------

_ORDINALS = {
    1: ("prima", "primo", "uno", "first", "one", "1", "o1"),
    2: ("seconda", "secondo", "due", "second", "two", "2", "o2"),
    3: ("terza", "terzo", "tre", "third", "three", "3", "o3"),
}
# An ordinal names an option only as one: after an article or "opzione" ("la prima", "option 2"), or alone in
# a short answer ("la seconda", "2"); never "prima di", "pranzo prima del museo".
_OPTION_WORD = frozenset(
    {"la", "il", "l'", "opzione", "option", "numero", "number", "the", "quella", "quello"}
)
_NOT_ORDINAL_AFTER = frozenset({"di", "del", "della", "dello", "dei", "delle", "che", "of", "than", "a", "ad",
                                "o", "possibile"})  # fmt: skip
_CARDINALS = frozenset({"uno", "due", "tre", "one", "two", "three", "1", "2", "3"})
_SKIP = re.compile(r"salt(a|ala|alo|are|iamo|iamola|iamolo|ate)|skip(ped|ping)?")
# Whole words that say it's done ("presto", "è stato bello" don't): the participles and their phrases.
_DONE = re.compile(
    r"(fatt|finit|comprat|pres|ritirat|pagat|chius|consegnat|spedit|mandat)[oaie]|"
    r"done|did|finished|bought|paid|completed|collected|picked"
)
_BEEN = re.compile(r"\b(sono|ci sono|siamo|ci siamo) stat[oaie]\b|\b(i've|i have|i) (been|went)\b")
_DISMISS = re.compile(
    r"\b(lascia(mo)? perdere|non (mi )?(interessa|serve)|archivia\w*|ignora\w*|scarta\w*|togli\w*|"
    r"dismiss\w*|forget (it|about)|never ?mind|drop\w*|ignore\w*|not needed)\b",
    re.IGNORECASE,
)
_PRIORITY_WORDS = {
    1: ("urgente", "urgent", "alta", "importante", "prioritario", "prioritaria", "priorita", "subito", "high",
        "important", "asap"),
    2: ("media", "normale", "medium", "normal", "mid", "alza", "raise"),
    3: ("bassa", "poco importante", "low", "abbassa", "lower", "non urgente", "con calma"),
}  # fmt: skip
_NO_DUE = re.compile(
    r"\b(senza|nessuna|togli(\w*)?( la)?|leva(\w*)?( la)?|no) (data|scadenza)\b|"
    r"\b(no|remove the|without a?) ?(due )?date\b|\bnone\b"
)
_WHEN_STOP = ("prima", "dopo", "invece", "quando ci sta", "before", "after", "instead", "when it fits")


def _words_of(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", fold_apostrophe(text))


# a start they give themselves: a move verb, or a do-verb of theirs ("chiamo il notaio alle 18", "lo
# faccio alle 15", "mettilo alle 9"); never "alle/at" alone ("il notaio alle 18?")
_DO_VERB = re.compile(r"chiamo|faccio|fallo|falla|mettil[oa]|vado|call|do")
# someone else's time, said before it in the sentence: "ha detto che alle 18 chiude", "l'email dice alle 18"
_REPORTED = re.compile(
    r"\b(?:dice|dicono|diceva|ha detto|hanno detto|scrive|scrivono|ha scritto|says|said|writes|wrote)\b"
)
_SENTENCE = re.compile(r"[.;!?](?!\d)")


def _start_said(when: str, owner_words: str, today: date) -> bool:
    """set_task_start's time is their own command: in a sentence of theirs with a move or do verb not
    governed by a negation, never a question ("il notaio alle 18?", "alle 18? no, alle 15" never acts at
    18), never after someone else's word for it ("dice", "ha detto", "says", "scrive")."""
    try:
        due, kind = parse_when(when, today)
    except (PisaError, ValueError):
        return False
    moment = datetime.fromisoformat(due) if due is not None and kind == "datetime" else None
    text, start = with_minutes(owner_words), 0
    for end in [m.end() for m in _SENTENCE.finditer(text)] + [len(text)]:
        sentence, start = text[start:end], end
        words = _words_of(sentence)
        if not (_verbs(words, _MOVE_VERB) or _verbs(words, _DO_VERB) or _NO_DUE.search(" ".join(words))):
            continue
        if moment is None:
            return True
        at = re.search(rf"(?<!\d)0?{moment.hour}[:.]{moment.minute:02d}(?!\d)", sentence)
        if at is None or sentence.rstrip().endswith("?"):
            continue
        if not _REPORTED.search(fold_apostrophe(sentence[: at.start()])):
            return True
    return False


def _due_in_words(when: str, owner_words: str, today: date) -> bool:
    """reschedule_task's `when` is in the user's words: the day they named, and the time when it has one."""
    try:
        due, kind = parse_when(when, today)
    except (PisaError, ValueError):
        return False
    if due is None:  # "senza data", "togli la scadenza": never any "no"
        return bool(_NO_DUE.search(" ".join(_words_of(owner_words))))
    moment = datetime.fromisoformat(due)
    named = day_named(owner_words, today)
    if kind == "datetime":
        said = clock_times(with_minutes(owner_words))
        return (moment.hour, moment.minute) in said and (named or today) == moment.date()
    return named == moment.date()


# a tap on one of Pisa's choices comes back as the user's message "[tap <object>] o2: <label>":
# the object tapped is "options:<id>" when the choice offered Pisa's plan options, else the choice's own id.
_TAP = re.compile(r"^\[tap(?: ([a-z]+:\S+))?\] o([1-5]):")


def is_tap(owner_words: str) -> bool:
    return bool(_TAP.match(owner_words.strip()))


def without_label(owner_words: str) -> str:
    """A tap's label is the model's text, never the user's words: only "[tap
    <object>] o2" grounds anything (a number, a URL or a change in a label grounds nothing)."""
    tap = _TAP.match(owner_words.strip())
    return tap[0].rstrip(":") if tap is not None else owner_words


def says_option(n: int, owner_words: str, options_id: str | None = None) -> bool:
    """The user picks option `n` by its ordinal, used as one ("la seconda", "opzione 2", "2"), or taps it on
    the choice that offered these options (never a tap on another object's same ids)."""
    if (tap := _TAP.match(owner_words.strip())) is not None:
        # FACT: the user's own tap on Pisa's choice of these options (grounded as their words)
        return bool(options_id) and tap[1] == f"options:{options_id}" and int(tap[2]) == n
    words = _words_of(owner_words.replace("'", "' "))
    named = {k for k, forms in _ORDINALS.items() for w in words if w in forms}
    if any(w in _NEGATION for w in words) or "no" in words or len(named) > 1:
        return False  # "non la seconda", "la seconda no, la prima": one yes
    for i, w in enumerate(words):
        if w not in _ORDINALS.get(n, ()):
            continue
        after = words[i + 1] if i + 1 < len(words) else ""
        before = words[i - 1] if i > 0 else ""
        if after in _NOT_ORDINAL_AFTER:
            continue
        if w in _CARDINALS and len(words) > 1 and before not in _OPTION_WORD:
            continue  # "le due", "alle 2": a time, not option 2
        if before in _OPTION_WORD or len(words) <= 2:
            return True
    return False


def _says(words: list[str], plain: str, pattern: re.Pattern[str]) -> bool:
    return bool(_verbs(words, pattern))


# "don't ask me again" is a go-ahead, never a refusal ("aggiungilo, non chiedermelo più", "non
# serve chiedere", "senza chiedere", "non c'è bisogno di chiedere").
_GO_AHEAD = re.compile(
    r"\b(?:non (?:me |mi |ce |ci )?(?:serve |c'e bisogno di |devi |stare a )?|senza )"
    r"(?:chieder\w*|domandar\w*|ask(?:ing)?)(?: (?:piu|ancora|again|me))*\b"
    r"|\b(?:don'?t|no need to) ask(?: me)?(?: again)?\b"
)
_TAKE_BACK_WORDS = re.compile(
    r"\b(?:anzi|invece|instead|lascia\w* (?:stare|perdere)|forget it|never ?mind)\b"
)
_CHANGE_VERB = re.compile(f"{_MOVE_VERB.pattern}|{_CANCEL.pattern}")


# RAIL: a change's values come from the user's words, never from text Pisa read.
def refuses(owner_words: str) -> bool:
    """The words say no or take something back ("no, non aggiungerlo", "anzi", "lascia
    stare"): never a request, never a yes. A negation counts only when it governs the change
    verb (in its clause, before the verb: "non aggiungerlo", "non voglio che lo sposti"); "mettilo domani,
    non oggi", "non so perché chiedi, ti ho detto aggiungilo" and "non chiedermelo più" are their go-ahead.
    Without a change verb, any "no" or negation still refuses ("no grazie", "non farlo")."""
    plain = _GO_AHEAD.sub(" ", fold_apostrophe(owner_words))
    if _TAKE_BACK_WORDS.search(plain):
        return True
    clauses = [_WORD.findall(c) for c in _CLAUSE.split(plain) if c and c.strip()]
    clauses = [c for c in clauses if c]
    if not clauses:
        return False
    if clauses[0][0] in ("no", "nope", "nah") or any(_TAKE_BACK.search(" ".join(c)) for c in clauses):
        return True  # "no, …" first, or a clause that opens by taking back ("…, no aspetta")
    verb = False
    for words in clauses:
        for i, w in enumerate(words):
            if _CHANGE_VERB.fullmatch(w):
                verb = True
                if any(n in _NO_ANSWER | _NEGATION for n in words[:i]):
                    return True  # the negation governs the verb: "non cancellare il bagnoschiuma"
    return not verb and any(w in _NO_ANSWER | _NEGATION for c in clauses for w in c)


def asks_it(owner_words: str) -> bool:
    """The user's words this turn are a request of their own ("ti ho detto aggiungilo",
    "spostalo a domani"): a command verb, never a question. Never with a negation or a "no"
    ("no, non cancellarlo"), as owner_said; a yes alone ("ok", "sì") names nothing."""
    return "?" not in owner_words and not refuses(owner_words) and commands(owner_words)


def owner_names(tool: str, args: dict[str, Any], owner_words: str, *, title: str | None = None) -> bool:
    """After outside text an EARLIER turn read, the user's own words (this turn's command, or
    their words earlier in the talk) name what the change touches: the task or item's title (`title`, the one
    the tool found), the new task's title, the stop, the option. Pure."""
    if not owner_words.strip():
        return False
    if tool == "apply_option":
        n = str(args.get("option_id") or "").strip().lstrip("o")
        return n.isdigit() and says_option(int(n), owner_words, str(args.get("options_id") or "") or None)
    title = str(title or args.get("item") or args.get("what") or args.get("title") or "")
    return names_item(title, owner_words)


@dataclass(frozen=True)
class _Said:
    """The user's words this turn, as each grounding rule reads them, beside the tool's own arguments."""

    args: dict[str, Any]
    owner_words: str
    words: list[str]
    plain: str
    named: bool  # the item's title is in their words (this turn's, or their earlier words in the talk)
    today: date
    named_in: tuple[str, ...]


def _option(s: _Said) -> bool:  # the option's ordinal used as one
    n = str(s.args.get("option_id") or "").strip().lstrip("o")
    linked = str(s.args.get("options_id") or "").strip() or None
    return n.isdigit() and says_option(int(n), s.owner_words, linked)


def _reschedule(s: _Said) -> bool:  # a move verb of theirs, not negated, with the day or time they said
    when = str(s.args.get("when") or "")
    if not when and "due_at" in s.args:  # the tool's own value, as the hold sees it (local ISO, or cleared)
        when = str(s.args["due_at"] or "none")
    moved = bool(_verbs(s.words, _MOVE_VERB)) or bool(_NO_DUE.search(s.plain))
    return s.named and moved and _due_in_words(when, s.owner_words, s.today)


def _start(s: _Said) -> bool:  # their move verb and their time for the block
    when = str(s.args.get("when") or s.args.get("start_at") or "none")
    return (
        s.named and _due_in_words(when, s.owner_words, s.today) and _start_said(when, s.owner_words, s.today)
    )


def _priority(s: _Said) -> bool:
    level = s.args.get("priority")
    return (
        s.named
        and isinstance(level, int)
        and any(re.search(rf"\b{p}\b", s.plain) for p in _PRIORITY_WORDS.get(level, ()))
    )


def _add(s: _Said) -> bool:
    title = str(s.args.get("title") or "")
    if match_score(title, s.owner_words) >= MATCH_THRESHOLD:
        return True
    # an add verb of theirs this turn, the task's name from their earlier words ("aggiungilo")
    added = bool(_verbs(s.words, re.compile(r"(aggiung|crea|mett|segn)\w*|add(ed)?|put|create")))
    return added and any(match_score(title, w) >= MATCH_THRESHOLD for w in s.named_in)


def _stop(s: _Said) -> bool:  # the stop itself in their words; a place Pisa found, where they said to put it
    if s.args.get("what"):
        return match_score(str(s.args["what"]), s.owner_words) >= MATCH_THRESHOLD
    return bool(s.args.get("place_id")) and any(re.search(rf"\b{w}\b", s.plain) for w in _WHEN_STOP)


# Each grounding rule a tool declares in the registry (`pisa.domain.tools`, its `grounding`)
_GROUNDS: dict[Grounding, Callable[[_Said], bool]] = {
    "option": _option,
    "skip": lambda s: s.named and _says(s.words, s.plain, _SKIP),
    "done": lambda s: s.named and (_says(s.words, s.plain, _DONE) or bool(_BEEN.search(s.plain))),
    "dismiss": lambda s: s.named and bool(_DISMISS.search(s.plain)),
    "delete": lambda s: s.named and says_cancel(s.owner_words),
    "reschedule": _reschedule,
    "start": _start,
    "priority": _priority,
    "add": _add,
    "stop": _stop,
}


def grounded_change(
    tool: str,
    args: dict[str, Any],
    owner_words: str,
    today: date,
    *,
    title: str | None = None,
    named_in: tuple[str, ...] = (),
) -> bool:
    """The change a plan or task tool makes is in the user's own words this
    turn, so it acts even after the turn read outside text (an email, a page): whole words, tied to the item.
    The option's ordinal used as one; a skip, done, dismiss or delete verb, the time or the priority word,
    each with the item's title in their words (`title`, the task or item the tool found); the stop.
    Otherwise the change waits for one yes. Pure: `args` are the tool's own. The rule is the tool's own
    `grounding` in the registry; a tool without one is never grounded.
    `named_in`, the user's earlier words in the talk, may name the item ("spostalo a
    domani" after naming it); the verb and the values are always this turn's own."""
    declared = tools.entry(tool)
    rule = _GROUNDS.get(declared.grounding) if declared is not None and declared.grounding else None
    words = _words_of(owner_words)
    if rule is None or not words:
        return False
    title = str(title or args.get("item") or args.get("what") or "")
    named = names_item(title, owner_words) or any(names_item(title, w) for w in named_in)
    return rule(_Said(args, owner_words, words, " ".join(words), named, today, named_in))


# --- a recurring event (Google Calendar's RRULE) ------------------------------------------------------------

Repeat = Literal["weekly", "weekdays"]
REPEATS: tuple[Repeat, ...] = ("weekly", "weekdays")
_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def rrule(repeat: Repeat, start: datetime, until: datetime | None = None) -> str:
    """The series' RRULE: every week on the start's weekday, or every weekday (Monday to Friday); `until`: the
    last day it may happen (its local end of day, written in UTC as RFC 5545 asks for a zoned DTSTART)."""
    if repeat == "weekdays":
        if start.weekday() >= 5:
            raise ValueError("a weekday series starts on a weekday")
        rule = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    else:
        rule = f"RRULE:FREQ=WEEKLY;BYDAY={_BYDAY[start.weekday()]}"
    if until is not None:
        if until.date() < start.date():
            raise ValueError("the series ends before it starts")
        last = until.replace(hour=23, minute=59, second=59, microsecond=0)
        rule += f";UNTIL={last.astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}"
    return rule


def repeat_words(repeat: Repeat, start: datetime, lang: str, until: datetime | None = None) -> str:
    """How Pisa says the series: "ogni martedì" / "tutti i giorni lavorativi" (", fino al 18 dicembre")."""
    it = lang == "it-IT"
    if repeat == "weekdays":
        said = "tutti i giorni lavorativi" if it else "every weekday"
    else:
        day = (IT_WEEKDAYS if it else EN_WEEKDAYS_TITLE)[start.weekday()]
        said = f"ogni {day}" if it else f"every {day}"
    if until is not None:
        month = (IT_MONTHS if it else EN_MONTHS_TITLE)[until.month - 1]
        said += f", fino al {until.day} {month}" if it else f", until {month} {until.day}"
    return said
