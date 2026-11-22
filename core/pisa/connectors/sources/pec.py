"""Their PEC inbox over IMAP, **read-only**: the folder is opened with EXAMINE (read-only) and messages are
read with BODY.PEEK, so nothing is marked read, moved or deleted. Only the sender, the subject, the date and
the start of the text reach the radar's classifier; nothing of a message is stored but the obligation it
makes."""

import asyncio
import email
import email.message
import email.policy
import imaplib
import re
import ssl
from collections.abc import Callable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from pisa.connectors.sources.errors import SourceError
from pisa.failures import redact
from pisa.logging import get_logger

_log = get_logger("pisa.pec")

TEXT_MAX = 4000
MAX_MESSAGES = 200
Connect = Callable[[str, int], Any]  # host, port -> an imaplib.IMAP4-like client (tests pass a fake)


def _connect(host: str, port: int) -> Any:
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)


def _text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")[:TEXT_MAX]
        return ""
    payload = message.get_payload(decode=True)
    return (
        payload.decode(message.get_content_charset() or "utf-8", "replace")[:TEXT_MAX]
        if isinstance(payload, bytes)
        else ""
    )


_ON_BEHALF = re.compile(r"per\s+conto\s+di\s*:?\s*([^\s\"<>]+@[^\s\"<>]+)", re.IGNORECASE)


def _original(message: email.message.Message) -> email.message.Message | None:
    """The certified message inside a PEC envelope: the attached `postacert.eml` (message/rfc822)."""
    for part in message.walk():
        name = (part.get_filename() or "").lower()
        if part.get_content_type() == "message/rfc822" or name == "postacert.eml":
            payload = part.get_payload()
            if isinstance(payload, list) and payload and isinstance(payload[0], email.message.Message):
                return payload[0]
            raw = part.get_payload(decode=True)
            if isinstance(raw, bytes):
                return email.message_from_bytes(raw, policy=email.policy.compat32)
    return None


def certified(message: email.message.Message) -> bool:
    """The provider's own certified envelope (`X-Trasporto: posta-certificata`). An anomaly envelope
    (`X-Trasporto: errore`, "ANOMALIA MESSAGGIO") wraps ordinary mail whose sender nobody vouches for."""
    return str(message.get("X-Trasporto") or "").strip().lower() == "posta-certificata"


def unwrap(message: email.message.Message) -> tuple[str, str, str]:
    """A PEC notice is an envelope from their provider ("Per conto di: x@pec.ente.it"
    <posta-certificata@gestore.it>) with the original as `postacert.eml`. The radar reads the original's
    subject and text, and the sender the provider certified ("Per conto di:").

    Only a certified envelope is read through. Anything else (an anomaly
    envelope, ordinary mail writing "Per conto di: avvisi@pec.agenziaentrate.it" in its own display name) is
    read as it is: its sender is its real address, so a forged office stays flagged."""
    sender = str(message.get("From") or "")
    subject = str(message.get("Subject") or "")
    if not certified(message):
        return sender, subject, _text(message)
    behalf = _ON_BEHALF.search(sender)
    inner = _original(message)
    if inner is not None:
        return (
            (behalf.group(1) if behalf else str(inner.get("From") or "")),
            str(inner.get("Subject") or ""),
            _text(inner),
        )
    return (behalf.group(1) if behalf else sender), subject, _text(message)


def read(
    connect: Connect, secret: dict[str, str], settings: dict[str, Any], day: date
) -> list[dict[str, Any]]:
    host = settings.get("imap_host")
    if not host:
        raise SourceError("The PEC's IMAP server is missing: enter it in Settings → Connectors → PEC")
    try:
        client = connect(str(host), int(settings.get("imap_port") or 993))
    except (OSError, imaplib.IMAP4.error) as err:
        raise SourceError(f"The PEC server {host} is unreachable ({type(err).__name__})") from None
    try:
        try:
            client.login(secret["username"], secret["password"])
        except imaplib.IMAP4.error:
            raise SourceError(
                "The PEC server refused the login: check the PEC address and its app password in Settings → "
                "Connectors → PEC",
                auth=True,
            ) from None
        status, _ = client.select("INBOX", readonly=True)  # EXAMINE: read-only
        if status != "OK":
            raise SourceError("The PEC inbox can't be opened read-only")
        status, found = client.search(None, "SINCE", day.strftime("%d-%b-%Y"))
        ids = (found[0].split() if status == "OK" and found and found[0] else [])[-MAX_MESSAGES:]
        out: list[dict[str, Any]] = []
        for num in ids:
            status, data = client.fetch(num, "(BODY.PEEK[])")  # PEEK: never marked read
            raw = next((part[1] for part in data or [] if isinstance(part, tuple)), None)
            if status != "OK" or not isinstance(raw, bytes):
                continue
            message = email.message_from_bytes(raw, policy=email.policy.compat32)
            try:
                sent = parsedate_to_datetime(str(message.get("Date")))
            except (TypeError, ValueError):
                sent = datetime.now(UTC)
            ref = str(message.get("Message-ID") or f"pec:{num.decode() if isinstance(num, bytes) else num}")
            sender, subject, text = unwrap(message)
            out.append(
                {
                    "ref": ref.strip()[:200],
                    "from_address": sender[:200],
                    "subject": subject[:300],
                    "text": text,
                    "sent_at": sent if sent.tzinfo else sent.replace(tzinfo=UTC),
                }
            )
        return out
    except imaplib.IMAP4.error as err:
        raise SourceError(
            redact(f"The PEC server answered an error: {str(err)[:120]}", *secret.values())
        ) from None
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):  # a closed connection: nothing to give back
            pass
        except Exception as exc:  # a bug: said, never raised over the read's own outcome
            _log.warning("pec_logout_failed", cause=exc.__class__.__name__)


class PecImap:
    def __init__(self, connect: Connect = _connect) -> None:
        self._connect = connect

    async def since(
        self, secret: dict[str, str], settings: dict[str, Any], day: date
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(read, self._connect, secret, settings, day)
