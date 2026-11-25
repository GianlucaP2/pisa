"""Google API failures, each naming the real cause (never a token or a secret)."""

from typing import Any

import httpx

from pisa.failures import redact


class ConnectorError(Exception):
    """A Google call failed. `auth` is True when the saved authorization itself is no longer accepted."""

    def __init__(self, message: str, status: int | None = None, *, auth: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.auth = auth


def google_reason(response: httpx.Response) -> str:
    """Google's own short explanation from an error body (e.g. "Gmail API has not been used in project…"),
    anything key-like masked."""
    try:
        body: Any = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        text = error.get("message") or error.get("status") or ""
    elif isinstance(error, str):  # OAuth endpoints: {"error": "invalid_grant", "error_description": "…"}
        description = body.get("error_description")
        text = f"{error}: {description}" if isinstance(description, str) and description else error
    else:
        text = ""
    return redact(str(text)).strip().rstrip(".")[:200]
