"""Enable Banking (a licensed PSD2 provider): their bank
accounts and transactions, **read-only**. Pisa never starts a payment: this client has no payment call.

Their own Enable Banking application (its id and its private key, write-only in Settings) signs a short JWT
for every call (RS256, `kid` the application id). The consent happens on the bank's page (SCA): `start`
returns its URL, the phone opens it, the bank sends the phone back with a `code`, `session` turns the code
into the consent's session and its accounts. API reference: https://enablebanking.com/docs/api/reference/
"""

import base64
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pisa.connectors.sources.errors import SourceError
from pisa.failures import redact

BASE = "https://api.enablebanking.com"
JWT_TTL = timedelta(minutes=10)
MAX_PAGES = 20


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(application_id: str, private_key_pem: str, now: datetime) -> str:
    """The call's JWT: RS256 over {iss, aud, iat, exp}, `kid` their application's id."""
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except (ValueError, TypeError):
        raise SourceError(
            "The Enable Banking private key can't be read: paste the application's PEM key again in "
            "Settings → Connections → Bank",
            auth=True,
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SourceError("The Enable Banking key must be the application's RSA key", auth=True)
    header = {"typ": "JWT", "alg": "RS256", "kid": application_id}
    iat = int(now.timestamp())
    claims = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": iat,
        "exp": iat + int(JWT_TTL.total_seconds()),
    }
    signing = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing}.{_b64(signature)}"


class EnableBanking:
    def __init__(self, http: httpx.AsyncClient, *, clock: Any = lambda: datetime.now(UTC)) -> None:
        self._http = http
        self._clock = clock

    async def _call(self, method: str, path: str, secret: dict[str, str], **kwargs: Any) -> Any:
        token = jwt(secret["application_id"], secret["private_key"], self._clock())
        try:
            response = await self._http.request(
                method, BASE + path, headers={"Authorization": f"Bearer {token}"}, timeout=30.0, **kwargs
            )
        except httpx.HTTPError as err:
            raise SourceError(
                f"Enable Banking is unreachable ({type(err).__name__}): Pisa tries again later"
            ) from None
        if response.status_code in (401, 403):
            raise SourceError(
                f"Enable Banking refused the application's key (HTTP {response.status_code}): check the "
                "application id and its key in Settings → Connections → Bank",
                status=response.status_code,
                auth=True,
            )
        if response.status_code >= 400:
            try:
                why = str(response.json().get("message") or response.json().get("error") or "")[:160]
            except ValueError:
                why = ""
            why = redact(why, *secret.values())
            raise SourceError(
                f"Enable Banking answered HTTP {response.status_code}" + (f": {why}" if why else ""),
                status=response.status_code,
            )
        return response.json()

    async def start(
        self, secret: dict[str, str], settings: dict[str, Any], valid_until: datetime, state: str
    ) -> dict[str, Any]:
        if not settings.get("aspsp_name") or not settings.get("redirect_url"):
            raise SourceError("Choose the bank and the return address in Settings → Connections → Bank first")
        country = settings.get("aspsp_country")  # the user's country when Settings names none (the service's)
        if not country:
            raise SourceError("Choose the bank's country in Settings → Connections → Bank first")
        body = {
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": settings["aspsp_name"], "country": country},
            "state": state,
            "redirect_url": settings["redirect_url"],
            "psu_type": "personal",
        }
        out = await self._call("POST", "/auth", secret, json=body)
        return {"url": str(out["url"]), "authorization_id": str(out.get("authorization_id") or "")}

    async def session(self, secret: dict[str, str], code: str) -> dict[str, Any]:
        out = await self._call("POST", "/sessions", secret, json={"code": code})
        accounts = []
        for a in out.get("accounts") or []:
            iban = str(((a.get("account_id") or {}).get("iban")) or "")
            accounts.append(
                {
                    "uid": str(a["uid"]),
                    "name": (a.get("name") or None),
                    "currency": a.get("currency"),
                    "iban_last4": iban[-4:] if len(iban) >= 4 else None,
                }
            )
        valid = (out.get("access") or {}).get("valid_until")
        return {
            "session_id": str(out["session_id"]),
            "accounts": accounts,
            "valid_until": datetime.fromisoformat(valid) if valid else None,
        }

    async def transactions(
        self, secret: dict[str, str], account_uid: str, since: date
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, str] = {"date_from": since.isoformat()}
        for _ in range(MAX_PAGES):
            page = await self._call("GET", f"/accounts/{account_uid}/transactions", secret, params=params)
            for t in page.get("transactions") or []:
                amount = t.get("transaction_amount") or {}
                debit = t.get("credit_debit_indicator") == "DBIT"
                party = (t.get("creditor") if debit else t.get("debtor")) or {}
                info = t.get("remittance_information") or []
                ref = t.get("entry_reference") or t.get("transaction_id")
                booked = t.get("booking_date") or t.get("value_date") or t.get("transaction_date")
                if not ref or not booked or amount.get("amount") is None:
                    continue
                out.append(
                    {
                        "provider_ref": str(ref)[:200],
                        "booked_on": date.fromisoformat(str(booked)[:10]),
                        "amount": abs(float(amount["amount"])),
                        "currency": str(amount.get("currency") or "EUR"),
                        "direction": "out" if debit else "in",
                        "counterparty": (str(party.get("name"))[:120] if party.get("name") else None),
                        "description": (" ".join(str(i) for i in info)[:200] if info else None),
                        "pending": t.get("status") == "PDNG",
                    }
                )
            key = page.get("continuation_key")
            if not key:
                break
            params = {"date_from": since.isoformat(), "continuation_key": str(key)}
        return out
