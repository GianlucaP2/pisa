"""A Google that keeps state for Pisa's actions: calendar events (read, insert, patch, delete) and Gmail
(message headers, threads, drafts, send). Everything else is the recorded-shape FakeGoogle."""

import base64
import json
from datetime import datetime, timedelta
from email import message_from_bytes
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import psycopg

from pisa.connectors.google import oauth
from pisa.infra.keys import KeyStore
from tests.google_fake import FakeGoogle
from tests.native import native

OWNER = "owner@example.org"
CALENDAR = "owner@example.org"  # the primary calendar's id is the account's address
ACCESS_TOKEN = "ya29.test-access-actions"


class ActionsGoogle(FakeGoogle):
    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str, dict[str, list[str]], dict[str, Any] | None]] = []
        self.headers: dict[str, dict[str, str]] = {}  # gmail message id → headers
        self.threads: dict[str, list[str]] = {}  # thread id → message ids
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []  # draft ids sent
        self.sent_ids: set[str] = set()  # The messages Gmail holds as sent (the read-back)
        self.sent_lost = False  # a send that Gmail answered but never holds (the read-back can't confirm it)
        self.deleted: list[str] = []  # draft ids deleted
        self.put_answer_lost = False  # a draft update is stored, but answered 503
        self.insert_answer_lost = False  # an event insert is stored, but answered 503
        self.if_match: list[str | None] = []  # Each calendar PATCH's or DELETE's If-Match
        # Google took the write, but reads still answer the event as it was before it
        # (the read-back can't confirm it)
        self.stale_reads = False
        # A calendar that keeps starts on a grid of this many minutes (rounded up on a write)
        self.normalise: int | None = None
        self._before: dict[str, dict[str, Any] | None] = {}
        # a sync of the primary calendar lists the fake's own events too, as Google does. Opt-in: the recorded
        # sync lists only its fixtures, and a test that caches its own rows would race a sync inserting them.
        self.listed = False
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def event(
        self, event_id: str, title: str, start: datetime, end: datetime, **extra: Any
    ) -> dict[str, Any]:
        """An event the user organises (unless `organizer` says otherwise)."""
        body = {"id": event_id, "status": "confirmed", "summary": title,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                "organizer": {"email": OWNER, "self": True}, "creator": {"email": OWNER, "self": True},
                "iCalUID": f"{event_id}@google.com", "eventType": "default",
                "updated": "2026-10-20T08:00:00.000Z", "etag": f'"{event_id}-0"'} | extra  # fmt: skip
        self.events[event_id] = body
        return body

    def message(self, message_id: str, thread_id: str, **headers: str) -> None:
        self.headers[message_id] = {"Message-ID": f"<{message_id}@mail.gmail.com>", **headers}
        self.threads.setdefault(thread_id, []).append(message_id)

    def raw(self, draft_id: str) -> Any:
        """The draft's MIME message, parsed."""
        return message_from_bytes(base64.urlsafe_b64decode(self.drafts[draft_id]["raw"]))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, unquote(request.url.path), request.method
        params = parse_qs(request.url.query.decode())
        body = json.loads(request.content) if request.content else None
        if (
            host == "www.googleapis.com"
            and "/calendar/v3/calendars/" in path
            and ("/events/" in path or method != "GET")
        ):
            self.calls.append((host, path, params))
            if method != "GET":
                self.writes.append((method, path, params, body))
            if method in ("PATCH", "DELETE"):
                self.if_match.append(request.headers.get("If-Match"))
            return self._calendar(method, path, body, request.headers.get("If-Match"))
        if host == "gmail.googleapis.com" and (
            "/drafts" in path or "/threads/" in path or params.get("format") == ["metadata"]
        ):
            self.calls.append((host, path, params))
            if method != "GET":
                self.writes.append((method, path, params, body))
            return self._gmail(method, path, body)
        answer = super().__call__(request)
        if self.listed and path == f"/calendar/v3/calendars/{CALENDAR}/events" and answer.status_code == 200:
            page = answer.json()
            known = {item.get("id") for item in page.get("items", [])}
            mine = [_listed(e) for k, e in self.events.items() if k not in known]
            return httpx.Response(200, json={**page, "items": [*page.get("items", []), *mine]})
        return answer

    def edit_in_google(self, event_id: str, **changes: Any) -> None:
        """The user changes the event in Google meanwhile: a new etag."""
        self.events[event_id].update(changes)
        self.events[event_id]["etag"] = f'"{self._next("g")}"'

    def _calendar(
        self, method: str, path: str, body: dict[str, Any] | None, if_match: str | None = None
    ) -> httpx.Response:
        event_id = path.rsplit("/events/", 1)[1] if "/events/" in path else None
        if self.stale_reads and method != "GET":
            key = event_id or (body or {}).get("id") or ""
            if key not in self._before:
                self._before[key] = dict(self.events[key]) if key in self.events else None
        if self.stale_reads and method == "GET" and (event_id or "") in self._before:
            before = self._before[event_id or ""]
            if before is None:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            return httpx.Response(200, json=before)
        if method in ("POST", "PATCH") and self.normalise and body and "start" in body:
            body = _rounded(body, self.normalise)
        if method == "POST":
            new_id = (body or {}).get("id") or self._next("new-")
            if new_id in self.events:  # Google: an event with this id already exists
                exists = {"code": 409, "message": "The requested identifier already exists."}
                return httpx.Response(409, json={"error": exists})
            new = dict(body or {}) | {"id": new_id, "status": "confirmed",
                                      "organizer": {"email": OWNER, "self": True},
                                      "updated": self._next("u"), "etag": f'"{new_id}-0"'}  # fmt: skip
            self.events[new_id] = new
            if self.insert_answer_lost:
                return httpx.Response(503, json={"error": {"code": 503, "message": "Backend Error"}})
            return httpx.Response(200, json=new)
        event = self.events.get(event_id or "")
        if event is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        if method == "GET":
            return httpx.Response(200, json=event)
        if if_match is not None and if_match != event.get("etag"):  # changed since Pisa read it
            failed = {"code": 412, "message": "Precondition Failed"}
            return httpx.Response(412, json={"error": failed})
        if method == "PATCH":
            event.update(body or {})
            event["updated"] = self._next("u")
            event["etag"] = f'"{self._next("e")}"'
            return httpx.Response(200, json=event)
        if method == "DELETE":
            event["status"] = "cancelled"
            event["updated"] = self._next("u")
            event["etag"] = f'"{self._next("e")}"'
            return httpx.Response(204)
        raise AssertionError(method)

    def _gmail(self, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        base = "/gmail/v1/users/me"
        if path.startswith(f"{base}/messages/"):
            message_id = path.rsplit("/", 1)[1]
            if message_id in self.sent_ids:  # The read-back of a sent message
                return httpx.Response(200, json={"id": message_id, "labelIds": ["SENT"], "payload": {}})
            headers = self.headers.get(message_id)
            if headers is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": message_id, "payload": {
                "headers": [{"name": k, "value": v} for k, v in headers.items()]}})  # fmt: skip
        if path.startswith(f"{base}/threads/"):
            thread_id = path.rsplit("/", 1)[1]
            messages = [
                {
                    "id": m,
                    "payload": {"headers": [{"name": k, "value": v} for k, v in self.headers[m].items()]},
                }
                for m in self.threads.get(thread_id, [])
            ]
            for draft_id, draft in self.drafts.items():  # as Gmail: a thread lists its drafts too
                if draft["threadId"] == thread_id:
                    held = self.raw(draft_id)
                    headers = [{"name": k, "value": str(held[k])} for k in ("From", "To", "Cc") if held[k]]
                    messages.append(
                        {"id": f"msg-{draft_id}", "labelIds": ["DRAFT"], "payload": {"headers": headers}}
                    )
            return httpx.Response(200, json={"id": thread_id, "messages": messages})
        if path == f"{base}/drafts" and method == "POST":
            draft_id = self._next("draft-")
            message = body["message"]  # type: ignore[index]
            self.drafts[draft_id] = {"raw": message["raw"], "threadId": message.get("threadId")}  # type: ignore[index,union-attr]
            return httpx.Response(200, json={"id": draft_id, "message": {"id": "m-draft"}})
        if path.startswith(f"{base}/drafts/") and method == "GET":
            draft_id = path.rsplit("/", 1)[1]
            return httpx.Response(
                200, json={"id": draft_id, "message": {"raw": self.drafts[draft_id]["raw"]}}
            )
        if path.startswith(f"{base}/drafts/") and method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            self.drafts.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if path.startswith(f"{base}/drafts/") and method == "PUT" and self.put_answer_lost:
            # Gmail stores the change, but its answer never arrives (a crash or a timeout after the write).
            draft_id = path.rsplit("/", 1)[1]
            message = body["message"]  # type: ignore[index]
            self.drafts[draft_id] = {"raw": message["raw"], "threadId": message.get("threadId")}  # type: ignore[index,union-attr]
            return httpx.Response(503, json={"error": {"code": 503, "message": "Backend Error"}})
        if path.startswith(f"{base}/drafts/") and method == "PUT":
            draft_id = path.rsplit("/", 1)[1]
            message = body["message"]  # type: ignore[index]
            self.drafts[draft_id] = {"raw": message["raw"], "threadId": message.get("threadId")}  # type: ignore[index,union-attr]
            return httpx.Response(200, json={"id": draft_id})
        if path == f"{base}/drafts/send":
            self.sent.append(body["id"])  # type: ignore[index]
            if not self.sent_lost:
                self.sent_ids.add("m-sent")
            return httpx.Response(200, json={"id": "m-sent", "threadId": self.drafts[body["id"]]["threadId"]})  # type: ignore[index]
        raise AssertionError(f"{method} {path}")


def _rounded(body: dict[str, Any], grid: int) -> dict[str, Any]:
    """The event's start rounded up to the grid, its end moved with it (its length kept)."""
    start = datetime.fromisoformat(body["start"]["dateTime"])
    late = (-start.minute) % grid
    shift = timedelta(minutes=late) - timedelta(seconds=start.second, microseconds=start.microsecond)
    moved = dict(body)
    for key in ("start", "end"):
        if "dateTime" in (body.get(key) or {}):
            at = datetime.fromisoformat(body[key]["dateTime"]) + shift
            moved[key] = body[key] | {"dateTime": at.isoformat()}
    return moved


def connect(db: psycopg.Connection, secrets_key: str, scopes: tuple[str, ...] = oauth.SCOPES) -> None:
    """A connected Google account with a valid access token (no exchange), its mail read up to now (so an
    event Google last changed before now is past the mail sync: `foreign.OWN_EVENT`)."""
    from pydantic import SecretStr

    keys = KeyStore(SecretStr(secrets_key))
    db.execute(
        "INSERT INTO connector_accounts (connector, account, scopes, refresh_token_enc, access_token_enc, "
        "access_expires_at, status, needs_reconnect, connected_at, gmail_synced_at) "
        "VALUES ('google', %s, %s, %s, %s, now() + interval '10 years', 'connected', false, now(), now()) "
        "ON CONFLICT (connector) DO UPDATE SET account = EXCLUDED.account, scopes = EXCLUDED.scopes, "
        "refresh_token_enc = EXCLUDED.refresh_token_enc, access_token_enc = EXCLUDED.access_token_enc, "
        "access_expires_at = EXCLUDED.access_expires_at, "
        "gmail_synced_at = EXCLUDED.gmail_synced_at",  # after `cached_email`'s bare row
        (OWNER, list(scopes), keys.seal("1//refresh-fake"), keys.seal(ACCESS_TOKEN)),
    )
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES (%s, 'Personale', 'owner') "
        "ON CONFLICT (id) DO NOTHING",
        (CALENDAR,),
    )


def cached_event(db: psycopg.Connection, event: dict[str, Any], calendar: str = CALENDAR) -> None:
    """Pisa's copy of the event (calendar_events), as a sync would have stored it."""
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES (%s, %s, %s, %s, %s, false, 'confirmed')",
        (calendar, event["id"], event["summary"], event["start"]["dateTime"], event["end"]["dateTime"]),
    )
    # An event they organise is one they made in their own Google Calendar (the fake's default)
    if (event.get("organizer") or {}).get("self") and not event.get("attendees"):
        native(db, event["id"])


def cached_email(
    db: psycopg.Connection, message_id: str, thread_id: str, from_name: str, from_address: str, subject: str,
    sent_at: datetime, body: str = "",
) -> None:  # fmt: skip
    # mail in Pisa's cache was synced from a connected Google (M8: with none, its tools say "not connected")
    db.execute(
        "INSERT INTO connector_accounts (connector, account, status) VALUES ('google', %s, 'connected') "
        "ON CONFLICT (connector) DO NOTHING",
        (OWNER,),
    )
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, body) VALUES (%s, %s, %s, %s, %s, %s, '{INBOX}', %s, %s)",
        (message_id, thread_id, from_name, from_address, subject, sent_at, body[:100], body),
    )


def _listed(event: dict[str, Any]) -> dict[str, Any]:
    """An event as a sync lists it: Google's `updated` is an RFC 3339 time (a write here stamps a counter)."""
    try:
        datetime.fromisoformat(str(event.get("updated")))
    except ValueError:
        return {**event, "updated": "2026-10-20T08:00:00.000Z"}
    return event
