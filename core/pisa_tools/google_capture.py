"""One-off capture of real Google responses as anonymised test fixtures (replace the
hand-written fixtures). Run from a checkout's core/, with the server's settings:
`uv run python -m pisa_tools.google_capture --out <dir>`.

Each endpoint Pisa uses is called once with the user's connected account. Before anything is written, every
personal value is replaced: email addresses, names, subjects, snippets, bodies, event titles, locations
and descriptions; tokens are dropped. Ids, labels, timestamps and the JSON shape stay, which is what the
tests need. The output is for review before committing; nothing is sent anywhere.
"""

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pisa.connectors.google.calendar import BASE as CALENDAR
from pisa.connectors.google.errors import ConnectorError
from pisa.connectors.google.gmail import BASE as GMAIL
from pisa.services.google import GoogleService

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_FREE_TEXT = {
    "snippet", "summary", "summaryOverride", "description", "location", "displayName", "title", "filename",
}  # fmt: skip
# Personal and not used by Pisa: dropped, not redacted (meeting links, the event page, app-private data).
_DROP = {"conferenceData", "hangoutLink", "htmlLink", "extendedProperties"}
_SECRETS = {"access_token", "refresh_token", "id_token", "nextSyncToken", "syncToken", "etag"}
_HEADERS = {"from": "Sender <sender@example.com>", "to": "Owner <owner@example.com>", "cc": "", "bcc": "",
            "subject": "Subject redacted", "reply-to": "", "delivered-to": "owner@example.com",
            "return-path": "<sender@example.com>"}  # fmt: skip
_KEEP_HEADERS = {"content-type", "date", "mime-version", "content-transfer-encoding"}


def _text(value: str) -> str:
    return f"redacted ({len(value)} characters)"


def anonymize(value: Any, key: str = "") -> Any:
    """Replace personal values, keep the shape. Header lists keep only harmless headers."""
    if isinstance(value, dict):
        if "name" in value and "value" in value and len(value) == 2:  # a Gmail header
            name = str(value["name"]).lower()
            if name in _HEADERS:
                return {"name": value["name"], "value": _HEADERS[name]}
            return value if name in _KEEP_HEADERS else None
        out = {}
        for k, v in value.items():
            if k in _DROP:
                continue
            if k in _SECRETS:
                out[k] = "redacted"
            elif k == "data" and isinstance(v, str):  # a base64url message body
                out[k] = base64.urlsafe_b64encode(b"Body redacted.").decode().rstrip("=")
            else:
                out[k] = anonymize(v, k)
        return out
    if isinstance(value, list):
        return [x for x in (anonymize(v, key) for v in value) if x is not None]
    if isinstance(value, str):
        if key in _FREE_TEXT:
            return _text(value)
        return _EMAIL.sub("person@example.com", value)
    return value


async def capture(google: GoogleService, out: Path) -> list[str]:
    """Write anonymised fixtures to `out`. Returns the file names written."""
    api = google.api("Gmail")  # the authorized client Pisa itself uses
    written: list[str] = []

    async def save(name: str, url: str, params: Any = None) -> Any:
        """One call, saved even when Google refuses it (a 400 for a garbage syncToken is a fixture too), so
        one refused call never stops the capture or the copy that follows it."""
        try:
            response = await api.get(url, params)
            body = response.json() if response.content else {}
            if response.status_code >= 400:
                body = {"_status": response.status_code, **(body if isinstance(body, dict) else {})}
        except ConnectorError as err:
            body = {"_status": err.status, "_error": err.message}
        text = json.dumps(anonymize(body), indent=2, ensure_ascii=False) + "\n"
        await asyncio.to_thread((out / name).write_text, text)
        written.append(name)
        return body

    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    profile = await save("gmail_profile.json", f"{GMAIL}/profile")
    listing = await save(
        "gmail_list.json",
        f"{GMAIL}/messages",
        {"q": "newer_than:7d -in:trash -in:drafts -in:chats", "includeSpamTrash": "true", "maxResults": "5"},
    )
    for i, message in enumerate((listing.get("messages") or [])[:3]):
        await save(f"gmail_message_{i}.json", f"{GMAIL}/messages/{message['id']}", {"format": "full"})
    start = str(max(1, int(profile.get("historyId", "1")) - 200))
    await save(
        "gmail_history.json",
        f"{GMAIL}/history",
        [("startHistoryId", start), ("historyTypes", "messageAdded")],
    )
    await save("gmail_history_expired.json", f"{GMAIL}/history", [("startHistoryId", "1")])
    calendars = await save("calendar_list.json", f"{CALENDAR}/users/me/calendarList")
    primary = next((c for c in calendars.get("items") or [] if c.get("primary")), None)
    if primary is not None:
        events_url = f"{CALENDAR}/calendars/{quote(primary['id'], safe='')}/events"
        full = await save(
            "calendar_events_full.json", events_url, {"singleEvents": "true", "maxResults": "5"}
        )
        if full.get("nextSyncToken"):
            await save(
                "calendar_events_incremental.json",
                events_url,
                {"singleEvents": "true", "syncToken": full["nextSyncToken"]},
            )
        await save("calendar_gone.json", events_url, {"singleEvents": "true", "syncToken": "invalid-token"})
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    from pisa.services.runtime import Runtime
    from pisa.settings import get_settings

    parser = argparse.ArgumentParser(description="save anonymised real Google responses as test fixtures")
    parser.add_argument("--out", required=True, help="output directory (reviewed before committing)")
    args = parser.parse_args(argv)

    async def go() -> list[str]:
        runtime = await Runtime.start(get_settings())
        try:
            return await capture(runtime.google, Path(args.out))
        finally:
            await runtime.close()

    written = asyncio.run(go())
    print(f"Wrote {len(written)} anonymised fixtures to {args.out}: {', '.join(written)}")
    print("Review them before committing (tests/data/google/).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
