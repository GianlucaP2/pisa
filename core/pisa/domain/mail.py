"""Email as Pisa sees it: data, never instructions."""

import re
from dataclasses import dataclass, field
from datetime import datetime

# Gmail labels whose messages are kept but never sent to an AI model.
# DRAFT: the user's own unsent text (every autosave is a new message id); CHAT: Hangouts history.
NEVER_TO_MODEL_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "DRAFT", "CHAT"})
BODY_MAX_CHARS = 8_000
# Mail providers shared by many people: an address there is a person, never a brand.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.it", "live.com", "live.it",
    "msn.com", "yahoo.com", "yahoo.it", "icloud.com", "me.com", "mac.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tim.it", "fastwebnet.it", "email.it", "inwind.it", "aol.com",
    "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "gmx.de", "web.de", "ziggo.nl", "kpnmail.nl", "zoho.com", "yandex.com", "mail.com",
})  # fmt: skip
# Senders that are not a person writing (the notices come from these).
AUTOMATED = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|news(letter)?|info|support|hello|team|billing|"
    r"accounts?|mailer-daemon|postmaster|updates?|alerts?|marketing|service|bookings?|reservations?)\b",
    re.IGNORECASE,
)


_NAME_WORD = re.compile(r"^[^\W\d_](?:[^\W\d_]|['\u2019])*$")  # letters, and an apostrophe inside


def person_sender(name: str | None, address: str | None, freemail: frozenset[str] = frozenset()) -> bool:
    """The sender reads like a person writing: an address that isn't a system's (no-reply, info, billing…)
    and a display name of letters only ("Marco Rossi"), two to four words, or one on a free-mail domain
    ("Marco <marco@gmail.com>"). "google-one", "Amazon.it" or "PayPal" are not. The code's guard on "a person
    is never low", never the model's reading of the email's own text."""
    if not address or "@" not in address or AUTOMATED.match(address.split("@")[0].strip()):
        return False
    words = " ".join((name or "").replace('"', " ").split()).split()
    if not words or len(words) > 4 or not all(_NAME_WORD.match(w) for w in words):
        return False
    return len(words) >= 2 or address.rsplit("@", 1)[1].lower() in freemail


@dataclass(frozen=True)
class Address:
    name: str | None
    address: str

    def __str__(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


@dataclass(frozen=True)
class Attachment:
    """An attachment's metadata (never its bytes): the MIME part's id, its name, type, size."""

    part_id: str
    filename: str
    mime: str
    size: int


@dataclass(frozen=True)
class MailMessage:
    id: str
    thread_id: str
    sender: Address | None
    to: tuple[Address, ...]
    subject: str
    sent_at: datetime
    labels: tuple[str, ...]
    snippet: str
    body: str | None  # plain text, at most BODY_MAX_CHARS
    attachments: tuple[Attachment, ...] = ()  # the PDFs only
    hidden: int = 0  # hidden runs left out of `body` (a count, never the text)
    # the iCalendar UIDs of the invitations it carries (text/calendar or .ics parts): an event with one of
    # these UIDs arrived by email (`services/foreign.py`)
    invites: tuple[str, ...] = ()
    # the text without its quoted history, the quoted history apart, and the cut rules' version
    # (`domain/quote_cut.py`); `body` stays as it was
    new_text: str | None = None
    quoted: str | None = None
    cut_version: int = 0


@dataclass(frozen=True)
class LabelChange:
    message_id: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ThreadFetch:
    """The messages of one thread that Pisa did not have (oldest first, at most the cap asked for), and how
    many messages the thread holds in all (more than Pisa holds plus these: the cap cut the fetch)."""

    total: int
    messages: tuple[MailMessage, ...] = ()


@dataclass(frozen=True)
class MailChanges:
    """New messages, label changes and deletions since a history point, plus the new history point."""

    history_id: str
    added: tuple[MailMessage, ...] = ()
    relabelled: tuple[LabelChange, ...] = ()
    deleted: tuple[str, ...] = field(default=())


ICAL_UID_MAX = 2048  # as long as a calendar event's stored `ical_uid` (connectors/google/calendar.py)
_UID_LINE = re.compile(r"^UID(?:;[^:\r\n]*)?:(.*)$", re.IGNORECASE | re.MULTILINE)


def ical_uids(text: str) -> tuple[str, ...]:
    """The UIDs an iCalendar text names (RFC 5545: folded lines unfolded first), at most 20."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    found = dict.fromkeys(m.strip()[:ICAL_UID_MAX] for m in _UID_LINE.findall(unfolded) if m.strip())
    return tuple(found)[:20]


def may_go_to_model(labels: tuple[str, ...] | list[str]) -> bool:
    return not NEVER_TO_MODEL_LABELS.intersection(labels)


EMAIL_REF_MIN = 6  # the shortest start of an email's id Pisa may read it by
EMAIL_REF_SHOWN = 10  # … and the start shown beside each search result


@dataclass(frozen=True)
class EmailHit:
    id: str
    sender: str
    subject: str
    sent_at: datetime
    snippet: str
