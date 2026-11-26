"""Gmail as a MailSource (Gmail API v1, scope gmail.readonly).

Calls (base https://gmail.googleapis.com/gmail/v1/users/me):
- GET /profile → emailAddress, historyId
- GET /messages?q=newer_than:{days}d -in:trash&includeSpamTrash=true&maxResults&pageToken → messages[].id
- GET /messages/{id}?format=full → id, threadId, labelIds, snippet, internalDate, payload (headers, parts)
- GET /messages/{id}/attachments/{attachmentId} → data (base64url), size (a bill's PDF, read
  locally; gmail.readonly covers it)
- GET /threads/{id}?format=minimal → messages[] (id, labelIds): which of a thread's messages Pisa lacks
- GET /history?startHistoryId&historyTypes=messageAdded|messageDeleted|labelAdded|labelRemoved&pageToken
  → history[].messagesAdded/messagesDeleted/labelsAdded/labelsRemoved[].message{id, labelIds}, historyId;
  404 = the start point is too old (HistoryExpired)
"""

import asyncio
import base64
from datetime import UTC, datetime
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote

from pisa.connectors.google.api import GMAIL_BASE, GoogleApi
from pisa.connectors.google.errors import ConnectorError
from pisa.connectors.google.text import html_to_text
from pisa.domain.bill_pdf import MAX_BYTES, is_pdf
from pisa.domain.mail import (
    BODY_MAX_CHARS,
    Address,
    Attachment,
    LabelChange,
    MailChanges,
    MailMessage,
    ThreadFetch,
    ical_uids,
)
from pisa.domain.quote_cut import Cut, cut_html, cut_text
from pisa.domain.visible import normalize, plain
from pisa.ports.sources import HistoryExpired

BASE = GMAIL_BASE
_FETCH_CONCURRENCY = 4
_FETCH_PER_SECOND = 20  # messages.get costs 5 quota units; Gmail allows 250 units/s per user: stay at 100


def _header(headers: list[dict[str, Any]], name: str) -> str:
    for h in headers:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _addresses(value: str) -> tuple[Address, ...]:
    return tuple(Address(name or None, addr) for name, addr in getaddresses([value]) if addr)


def _decode(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str):
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset = "utf-8"
    content_type = _header(part.get("headers") or [], "Content-Type")
    for param in content_type.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip('"')
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            parts += _walk(child)
    return parts


def _flat(text: str) -> str:
    return " ".join(text.casefold().split())


def plain_body(payload: dict[str, Any]) -> tuple[str | None, int]:
    """The message text: text/plain if present (unless the HTML they see hides text), else text/html converted
    to text. Attachments skipped. With it, the hidden runs left out (zero-width characters, hidden HTML), a
    count only."""
    plain_parts, html = [], []
    for part in _walk(payload):
        if part.get("filename"):
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/plain":
            plain_parts.append(_decode(part))
        elif mime == "text/html":
            html.append(_decode(part))
    shown = html_to_text("\n".join(html)) if html else None
    if plain_parts:
        text, hidden = plain(normalize("\n\n".join(plain_parts)))
        # they see the HTML. What it hides that the plain part also
        # carries would reach the model: then the HTML's visible text is what Pisa reads, and those runs are
        # counted. A hidden preheader the plain part doesn't carry is ordinary: neither read nor counted.
        carried = [t for t in shown[2] if t and _flat(t) in _flat(text)] if shown is not None else []
        if shown is not None and carried:
            text, hidden = shown[0], hidden + len(carried)
    elif shown is not None:
        text, hidden = shown[0], shown[1]
    else:
        return None, 0
    return text[:BODY_MAX_CHARS], hidden


def quote_cut(payload: dict[str, Any], text: str | None) -> Cut | None:
    """The message without its quoted history, and the history apart. The HTML is cut before it becomes text
    (a quote block is easy to see there and hard to see after); a header the HTML leaves outside its quote
    block ("On ... wrote:") and a message with no HTML go through the plain-text cutter. `text`: the body as
    `plain_body` made it. None when the message has no text."""
    html = "\n".join(
        _decode(part)
        for part in _walk(payload)
        if not part.get("filename") and str(part.get("mimeType", "")).lower() == "text/html"
    )
    cut: Cut | None = None
    if html:
        new_markup, quoted_markup = cut_html(html)
        if quoted_markup:
            head = cut_text(html_to_text(new_markup)[0])
            quoted = html_to_text(quoted_markup)[0]
            cut = Cut(head.new_text, "\n".join(t for t in (head.quoted, quoted) if t))
    if cut is None and text is not None:
        cut = cut_text(text)
    if cut is None:
        return None
    return Cut(cut.new_text[:BODY_MAX_CHARS], cut.quoted[:BODY_MAX_CHARS], cut.cut_version)


def pdf_parts(payload: dict[str, Any]) -> tuple[Attachment, ...]:
    """The message's PDF attachments (metadata only), at most 10."""
    found: list[Attachment] = []
    for part in _walk(payload):
        name, mime = str(part.get("filename") or ""), str(part.get("mimeType") or "")
        part_id = str(part.get("partId") or "")
        if not name or not part_id or not is_pdf(name, mime):
            continue
        size = int((part.get("body") or {}).get("size") or 0)
        found.append(
            Attachment(part_id[:40], " ".join(name.split())[:200], mime[:100] or "application/pdf", size)
        )
    return tuple(found[:10])


_CALENDAR_MIMES = frozenset({"text/calendar", "application/ics", "application/calendar"})


def calendar_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The message's invitation parts: text/calendar (or an .ics file), inline or attached."""
    return [
        part
        for part in _walk(payload)
        if str(part.get("mimeType", "")).lower() in _CALENDAR_MIMES
        or str(part.get("filename") or "").lower().endswith(".ics")
    ]


def invite_uids(payload: dict[str, Any], fetched: dict[str, str] | None = None) -> tuple[str, ...]:
    """The UIDs of the invitations the message carries, from the parts Gmail sent inline and, by part id,
    those read apart (`fetched`)."""
    texts = [
        _decode(p) or (fetched or {}).get(str(p.get("partId") or ""), "") for p in calendar_parts(payload)
    ]
    return ical_uids("\n".join(texts))


def parse_message(body: dict[str, Any], fetched: dict[str, str] | None = None) -> MailMessage:
    payload = body.get("payload") or {}
    headers = payload.get("headers") or []
    senders = _addresses(_header(headers, "From"))
    millis = int(body.get("internalDate") or 0)
    text, hidden = plain_body(payload)
    cut = quote_cut(payload, text)
    subject, in_subject = plain(_header(headers, "Subject"))  # a subject's hidden characters
    return MailMessage(
        id=str(body["id"]),
        thread_id=str(body.get("threadId", "")),
        sender=senders[0] if senders else None,
        to=_addresses(_header(headers, "To")),
        subject=" ".join(subject.split()),
        sent_at=datetime.fromtimestamp(millis / 1000, tz=UTC),
        labels=tuple(str(label) for label in body.get("labelIds") or []),
        snippet=str(body.get("snippet", "")),
        body=text,
        hidden=hidden + in_subject,
        attachments=pdf_parts(payload),
        invites=invite_uids(payload, fetched),
        new_text=cut.new_text if cut else None,
        quoted=cut.quoted if cut else None,
        cut_version=cut.cut_version if cut else 0,
    )


class GmailSource:
    def __init__(self, api: GoogleApi) -> None:
        self._api = api

    async def profile(self) -> tuple[str, str]:
        """(email address, current historyId)."""
        body = await self._api.json(f"{BASE}/profile")
        return str(body.get("emailAddress", "")), str(body.get("historyId", ""))

    async def history_point(self) -> str:
        return (await self.profile())[1]

    async def recent(
        self, *, days: int, limit: int, skip: frozenset[str] = frozenset(), fetch: int | None = None
    ) -> list[MailMessage]:
        ids: list[str] = []
        page: str | None = None
        while len(ids) < limit:
            params: dict[str, Any] = {
                "q": f"newer_than:{days}d -in:trash -in:drafts -in:chats",
                "includeSpamTrash": "true",
                "maxResults": min(500, limit - len(ids)),
            }
            if page:
                params["pageToken"] = page
            body = await self._api.json(f"{BASE}/messages", params)
            ids += [str(m["id"]) for m in body.get("messages") or [] if isinstance(m, dict) and m.get("id")]
            page = body.get("nextPageToken")
            if not page:
                break
        wanted = [i for i in ids[:limit] if i not in skip]
        return await self._fetch(wanted if fetch is None else wanted[:fetch])

    async def attachment(self, message_id: str, part_id: str) -> bytes | None:
        """The bytes of one attachment, by its MIME part's id (Gmail's attachmentId changes between reads,
        so the message is read again first). None when it is gone or bigger than a bill's cap."""
        response = await self._api.get(f"{BASE}/messages/{quote(message_id, safe='')}", {"format": "full"})
        if response.status_code == 404:
            return None
        part = next(
            (p for p in _walk(response.json().get("payload") or {}) if str(p.get("partId") or "") == part_id),
            None,
        )
        if part is None:
            return None
        body = part.get("body") or {}
        if int(body.get("size") or 0) > MAX_BYTES:
            return None
        data = body.get("data")
        if not isinstance(data, str) and body.get("attachmentId"):
            got = await self._api.get(
                f"{BASE}/messages/{quote(message_id, safe='')}/attachments/"
                f"{quote(str(body['attachmentId']), safe='')}"
            )
            if got.status_code == 404:
                return None
            data = got.json().get("data")
        if not isinstance(data, str):
            return None
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw if len(raw) <= MAX_BYTES else None

    async def changes_since(self, history_point: str) -> MailChanges:
        added: dict[str, None] = {}
        labels: dict[str, tuple[str, ...]] = {}
        deleted: set[str] = set()
        page: str | None = None
        latest = history_point
        while True:
            params: list[tuple[str, str]] = [("startHistoryId", history_point)]
            params += [
                ("historyTypes", t) for t in ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")
            ]
            if page:
                params.append(("pageToken", page))
            response = await self._api.get(f"{BASE}/history", params)
            if response.status_code == 404:
                raise HistoryExpired(history_point)
            body = response.json()
            latest = str(body.get("historyId") or latest)
            for record in body.get("history") or []:
                for item in record.get("messagesAdded") or []:
                    added[str(item["message"]["id"])] = None
                for item in record.get("messagesDeleted") or []:
                    deleted.add(str(item["message"]["id"]))
                for key in ("labelsAdded", "labelsRemoved"):
                    for item in record.get(key) or []:
                        message = item["message"]
                        labels[str(message["id"])] = tuple(message.get("labelIds") or [])
            page = body.get("nextPageToken")
            if not page:
                break
        new = [m for m in added if m not in deleted]
        fetched = await self._fetch(new)
        fetched_ids = {m.id for m in fetched}
        return MailChanges(
            history_id=latest,
            added=tuple(fetched),
            relabelled=tuple(
                LabelChange(mid, labs)
                for mid, labs in labels.items()
                if mid not in deleted and mid not in fetched_ids
            ),
            deleted=tuple(sorted(deleted)),
        )

    async def thread(self, thread_id: str, *, skip: frozenset[str], limit: int) -> ThreadFetch:
        """The messages of the thread that Pisa does not hold: only their ids are listed first (format
        minimal), then the newest `limit` of the unknown ones are read in full. Drafts and chats are not."""
        response = await self._api.get(f"{BASE}/threads/{quote(thread_id, safe='')}", {"format": "minimal"})
        if response.status_code == 404:  # gone in the meantime
            return ThreadFetch(0)
        listed = [m for m in response.json().get("messages") or [] if isinstance(m, dict) and m.get("id")]
        real = [m for m in listed if not {"DRAFT", "CHAT"} & set(m.get("labelIds") or [])]
        wanted = [str(m["id"]) for m in real if str(m["id"]) not in skip]
        return ThreadFetch(len(real), tuple(await self._fetch(wanted[-limit:] if limit > 0 else [])))

    async def _calendar_attachments(self, message_id: str, body: dict[str, Any]) -> dict[str, str]:
        """The invitation parts Gmail did not send inline, read apart (their UIDs mark events as mailed)."""
        texts: dict[str, str] = {}
        for part in calendar_parts(body.get("payload") or {}):
            attachment = (part.get("body") or {}).get("attachmentId")
            if (part.get("body") or {}).get("data") or not attachment:
                continue
            got = await self._api.get(
                f"{BASE}/messages/{quote(message_id, safe='')}/attachments/{quote(str(attachment), safe='')}"
            )
            data = got.json().get("data") if got.status_code == 200 else None
            if isinstance(data, str):
                raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                texts[str(part.get("partId") or "")] = raw.decode("utf-8", errors="replace")
        return texts

    async def _fetch(self, ids: list[str]) -> list[MailMessage]:
        gate = asyncio.Semaphore(_FETCH_CONCURRENCY)
        pace = asyncio.Lock()  # starts at most _FETCH_PER_SECOND requests a second

        async def one(message_id: str) -> MailMessage | None:
            async with gate:
                async with pace:
                    await asyncio.sleep(1 / _FETCH_PER_SECOND)
                response = await self._api.get(f"{BASE}/messages/{message_id}", {"format": "full"})
            if response.status_code == 404:  # deleted in the meantime
                return None
            try:
                body = response.json()
                return parse_message(body, await self._calendar_attachments(message_id, body))
            except (KeyError, ValueError, TypeError):
                raise ConnectorError(
                    f"Gmail returned a message Pisa could not read ({message_id})."
                ) from None

        found = await asyncio.gather(*(one(i) for i in ids))
        return [m for m in found if m is not None]
