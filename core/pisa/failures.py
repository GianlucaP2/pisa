"""Every external call's failure is logged, so what did not work is known at any time.

One line per failure, `external_failure`, at warning: the provider (`openai`, `xai`, `google_calendar`,
`gmail`, `google_maps`, `apple_maps`, `tavily`, `elevenlabs`, `gemini_live`, `grok_voice`, `apns`,
`open_meteo`, …), the operation (an email address in its path written as `{email}`), the status (HTTP or the
provider's own code), the kind of failure (an exception's class, or `response`) and the provider's request id
when it sent one. Never the provider's own words: an error body can echo the user's query, a place or a name,
and no log line holds user data (`pisa.logging`). An operator who needs the words reproduces the call with
the CLI (`pisa doctor`, which shows them redacted with `redact`). The `failures` report part reads these
lines from the journal (24 h, by provider).
"""

import re

from pisa.logging import get_logger

REASON_CHARS = 400
_log = get_logger("pisa.failures")
_SECRETS = re.compile(
    r"(?i)\b[\w-]*(?:api[_-]?key|key|token|secret|password|authorization)\b[\"']?(\s*[=:]\s*[\"']?)"
    r"(?:bearer\s+)?[^\s,;&\"']+"
    r"|(?i:bearer)\s+[\w\-.~+/=]{8,}"
    r"|\bAIza[\w\-]{20,}"  # Google API keys
    r"|\b(?:sk|xai|tvly|pk)-[\w\-]{10,}"  # OpenAI, xAI, Tavily keys
    r"|\bya29\.[\w\-.]+"  # Google OAuth access tokens
    r"|\b1//[\w\-]{20,}"  # Google refresh tokens
    r"|\bGOCSPX-[\w\-]{10,}"  # Google OAuth client secrets
    r"|\beyJ[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]*"  # JWTs (APNs provider tokens)
)
_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s\"']*")
_EMAIL = re.compile(r"[\w.+-]+(?:@|%40)[\w-]+(?:\.[\w-]+)+")  # a calendar id in a path is often their address


def _mask(match: re.Match[str]) -> str:
    """`key=abc…` keeps its name (`key=****`); a bare key or token is `****`."""
    if match.group(1) is None:
        return "****"
    return match.group(0)[: match.end(1) - match.start(0)] + "****"


def _without(text: str, secrets: tuple[str | None, ...]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "****")
    return text


def redact(text: str, *secrets: str | None) -> str:
    """`text` with the known secrets (a provider may echo a key back) and anything key-like masked: the one
    redaction, for a log line, an error message or a provider's reason."""
    return _SECRETS.sub(_mask, _without(text, secrets))


def reason_of(text: object, *secrets: str | None) -> str | None:
    """The provider's own words, safe to log: the known secrets and anything key-like masked, URLs without
    their query string, whitespace folded, cut to 400 characters."""
    if text is None:
        return None
    clean = redact(_QUERY.sub(r"\1?…", _without(str(text), secrets)))
    clean = " ".join(clean.split())
    return clean[:REASON_CHARS] or None


_REQUEST_ID = re.compile(
    r"(?i)\b(?:x-)?request[_ -]?id[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9._-]{6,80})|\b(req_[A-Za-z0-9]{8,80})\b"
)


def request_id(reason: object) -> str | None:
    """The provider's request id, when its error carries one (an attribute, or `request id: …` / `req_…` in
    its text): an id, never its words."""
    got = getattr(reason, "request_id", None)
    if isinstance(got, str) and 6 <= len(got) <= 80 and re.fullmatch(r"[A-Za-z0-9._-]+", got):
        return got
    if reason is None:
        return None
    found = _REQUEST_ID.search(str(reason))
    return (found.group(1) or found.group(2)) if found else None


def external_failure(
    provider: str, op: str, status: int | str | None = None, reason: object = None, *secrets: str | None
) -> None:
    """Log one external failure, status only (never raises: logging a failure must not become one). `reason`
    gives its kind and request id, never its words; `secrets` are kept in the signature for the callers that
    pass them and are not needed, since nothing the provider said is written."""
    del secrets
    kind = type(reason).__name__ if isinstance(reason, BaseException) else ("response" if reason else None)
    try:
        rid = request_id(reason)
    except Exception:  # a bug in the reading: the line goes out without it
        rid = None
    try:
        _log.warning(
            "external_failure", provider=provider, op=_EMAIL.sub("{email}", op), status=status, kind=kind,
            request_id=rid,
        )  # fmt: skip
    except (OSError, ValueError):  # pragma: no cover - the log stream is closed or broken: nowhere to say it
        pass
