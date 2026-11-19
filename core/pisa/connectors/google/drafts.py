"""Gmail drafts and sending (scope gmail.compose): Pisa's replies, saved as drafts in their thread and
sent only by an approved action.

Calls (base https://gmail.googleapis.com/gmail/v1/users/me):
- GET /messages/{id}?format=metadata&metadataHeaders=Message-ID,References,Subject,From,To,Cc,Reply-To
- GET /threads/{id}?format=metadata&metadataHeaders=From,To,Cc → messages[].payload.headers
- POST /drafts {"message": {"raw", "threadId"}} → {"id"}
- PUT /drafts/{id} {"id", "message": {"raw", "threadId"}}
- POST /drafts/send {"id"} → {"id" (the message), "threadId"}
- GET /drafts/{id}?format=raw → {"message": {"raw"}} (what would be sent, checked before sending)
- DELETE /drafts/{id} → 204 (404: already gone)
"""

from email.utils import getaddresses
from typing import Any
from urllib.parse import quote

from pisa.connectors.google.api import GMAIL_BASE, GoogleApi
from pisa.connectors.google.errors import ConnectorError
from pisa.ports.actions import MessageHeaders

BASE = GMAIL_BASE
_HEADERS = ("Message-ID", "References", "Subject", "From", "To", "Cc", "Reply-To")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = str(h.get("name", ""))
        if name.lower() not in {k.lower() for k in out}:
            out[name] = str(h.get("value", ""))
    return {k.lower(): v for k, v in out.items()}


class GmailDrafts:
    def __init__(self, api: GoogleApi) -> None:
        self._api = api

    async def headers(self, message_id: str) -> MessageHeaders:
        params = [("format", "metadata"), *(("metadataHeaders", h) for h in _HEADERS)]
        body = await self._api.json(f"{BASE}/messages/{quote(message_id, safe='')}", params)
        h = _headers(body.get("payload") or {})
        if not h.get("message-id"):
            raise ConnectorError("Gmail gave no Message-ID for the email to answer.")
        return MessageHeaders(
            message_id=h["message-id"], references=h.get("references", ""), subject=h.get("subject", ""),
            from_=h.get("from", ""), to=h.get("to", ""), cc=h.get("cc", ""), reply_to=h.get("reply-to", ""),
        )  # fmt: skip

    async def participants(self, thread_id: str) -> set[str]:
        params = [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "To"),
                  ("metadataHeaders", "Cc")]  # fmt: skip
        body = await self._api.json(f"{BASE}/threads/{quote(thread_id, safe='')}", params)
        found: set[str] = set()
        for message in body.get("messages") or []:
            if "DRAFT" in (message.get("labelIds") or []):
                continue  # drafts aren't participants: a re-draft can't vouch for its own Reply-To
            h = _headers(message.get("payload") or {})
            values = [h.get("from", ""), h.get("to", ""), h.get("cc", "")]
            found |= {address.lower() for _, address in getaddresses(values) if "@" in address}
        return found

    async def create(self, thread_id: str | None, raw: str) -> str:
        message: dict[str, str] = {"raw": raw} | (
            {"threadId": thread_id} if thread_id else {}
        )  # a new email: none
        body = await self._api.send_json("POST", f"{BASE}/drafts", body={"message": message})
        if not body.get("id"):
            raise ConnectorError("Gmail saved the draft but gave no id.")
        return str(body["id"])

    async def update(self, draft_id: str, thread_id: str | None, raw: str) -> None:
        message: dict[str, str] = {"raw": raw} | ({"threadId": thread_id} if thread_id else {})
        await self._api.send_json(
            "PUT", f"{BASE}/drafts/{quote(draft_id, safe='')}", body={"id": draft_id, "message": message}
        )

    async def send(self, draft_id: str) -> str:
        body = await self._api.send_json("POST", f"{BASE}/drafts/send", body={"id": draft_id})
        return str(body.get("id") or "")

    async def sent(self, message_id: str) -> bool:
        """The read-back: Gmail holds this message as sent (label SENT)."""
        params = [("format", "metadata"), ("metadataHeaders", "Message-ID")]
        body = await self._api.json(f"{BASE}/messages/{quote(message_id, safe='')}", params)
        return "SENT" in (body.get("labelIds") or [])

    async def get(self, draft_id: str) -> str:
        body = await self._api.json(f"{BASE}/drafts/{quote(draft_id, safe='')}", {"format": "raw"})
        raw = (body.get("message") or {}).get("raw")
        if not raw:
            raise ConnectorError("Gmail returned the draft without its message.")
        return str(raw)

    async def delete(self, draft_id: str) -> None:
        response = await self._api.request("DELETE", f"{BASE}/drafts/{quote(draft_id, safe='')}")
        if response.status_code >= 400 and response.status_code not in (404, 410):
            raise ConnectorError(f"Gmail refused to delete the draft (HTTP {response.status_code}).")
