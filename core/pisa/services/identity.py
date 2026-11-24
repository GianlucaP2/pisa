# ruff: noqa: E501
"""Pisa's own Ed25519 identity. The private key is sealed with Pisa's KeyStore (PISA_SECRETS_KEY)
and unsealed only to sign one request, in this process; the model never sees it and has no tool that signs.
It is never the user's phone key (those authorise; this one only says "this is Pisa").

- `headers(method, url, purpose)`: Pisa's honest User-Agent, and when Pisa has a key and a public URL (PISA_AGENT_URL)
  the Web Bot Auth headers (`Signature-Agent`, `Signature-Input`, `Signature`). RAIL: `may_sign` first: a
  read, a catalog search, a cart's creation; never a checkout's completion, a payment or an order.
- `rotate()` (the host's `pisa identity rotate`): a new key; the old one stays published for two directory
  cache periods, then goes. `revoke()` (`pisa identity revoke`): every key erased now; Pisa signs nothing until
  the next rotation (the kill switch).
- `directory(authority)`, `card()`, `ucp_profile()`: what Pisa publishes at its URL (no owner data).

Never logged: a key, a signature, a URL Pisa read.
"""

import base64
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg_pool import AsyncConnectionPool

from pisa.domain import identity as rules
from pisa.domain.errors import PisaError
from pisa.domain.identity import NotSignable
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger

_log = get_logger("pisa.identity")


class IdentityService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        *,
        agent_url: str | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._keys = keys
        self.agent_url = rules.agent_url_ok(agent_url)
        if agent_url and self.agent_url is None:
            _log.warning(
                "identity_agent_url_invalid"
            )  # PISA_AGENT_URL must be https://host, nothing after it
        self._clock = clock

    # --- keys ------------------------------------------------------------------------------------------------

    async def rotate(self) -> str:
        """A new active key; the previous one is retired and stays published RETIRE_AFTER_S. Returns the kid."""
        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        x = rules.b64url(public)
        kid = rules.thumbprint(x)
        sealed = self._keys.seal(base64.b64encode(raw).decode())
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM pisa_identity_keys WHERE published_until <= %s", (now,))
            await conn.execute(
                "UPDATE pisa_identity_keys SET retired_at = %s, published_until = %s WHERE retired_at IS NULL",
                (now, now + timedelta(seconds=rules.RETIRE_AFTER_S)),
            )
            await conn.execute(
                "INSERT INTO pisa_identity_keys (kid, public_x, sealed, created_at) VALUES (%s, %s, %s, %s)",
                (kid, x, sealed, now),
            )
        _log.info("identity_rotated", kid=kid)
        return kid

    async def revoke(self) -> int:
        """The kill switch: every key unpublished and erased now (the sealed private keys too); Pisa signs
        nothing until `rotate()`."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM pisa_identity_keys")
            count = cur.rowcount
        _log.info("identity_revoked", keys=count)
        return count

    async def _published(self) -> list[tuple[str, str, bytes, datetime, datetime | None]]:
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT kid, public_x, sealed, created_at, retired_at FROM pisa_identity_keys "
                    "WHERE retired_at IS NULL OR published_until > %s ORDER BY retired_at IS NULL DESC, created_at DESC",
                    (now,),
                )
            ).fetchall()
        return [(r[0], r[1], bytes(r[2]), r[3], r[4]) for r in rows]

    async def _current(self) -> tuple[str, bytes] | None:
        """The active key, read at each signature (a revoke from the host stops the next one)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT kid, sealed FROM pisa_identity_keys WHERE retired_at IS NULL")
            ).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def _private(self, sealed: bytes) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(self._keys.unseal(sealed)))

    # --- signing ---------------------------------------------------------------------------------------------

    async def headers(self, method: str, url: str, purpose: rules.Purpose = "read") -> dict[str, str]:
        """The headers of one of Pisa's own requests. RAIL: a request Pisa never signs raises NotSignable, signed
        or not (the caller sends nothing)."""
        if not rules.may_sign(method, url, purpose):
            raise NotSignable(f"Pisa doesn't sign a {method.upper()} for {purpose}")
        out = {"User-Agent": rules.user_agent(self.agent_url)}
        if self.agent_url is None or not self._keys.can_store:
            return out
        current = await self._current()
        if current is None:
            return out
        kid, sealed = current
        created = int(self._clock().timestamp())
        nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        signed = rules.target(method, url)
        text, sp = rules.request_base(signed, self.agent_url, created=created, keyid=kid, nonce=nonce)
        signature = base64.b64encode(self._private(sealed).sign(text.encode())).decode()
        out |= {
            "Signature-Agent": rules.agent_header(self.agent_url),
            "Signature-Input": f"{rules.LABEL}={sp}",
            "Signature": f"{rules.LABEL}=:{signature}:",
        }
        return out

    # --- what Pisa publishes ------------------------------------------------------------------------------------

    async def directory(self, authority: str) -> tuple[dict[str, Any], dict[str, str]]:
        """The key directory (JWKS) and its response headers, signed by each key it lists."""
        rows = await self._published()
        keys = [rules.jwk(base64.urlsafe_b64decode(x + "=")) for _, x, _, _, _ in rows]
        headers = {"Cache-Control": f"public, max-age={rules.DIRECTORY_MAX_AGE_S}"}
        if rows and self._keys.can_store:
            created = int(self._clock().timestamp())
            inputs, sigs = [], []
            for i, (kid, _, sealed, _, _) in enumerate(rows):
                label = f"sig{i + 1}"
                nonce = base64.b64encode(secrets.token_bytes(32)).decode()
                text, sp = rules.directory_base(authority, created=created, keyid=kid, nonce=nonce)
                sig = base64.b64encode(self._private(sealed).sign(text.encode())).decode()
                inputs.append(f"{label}={sp}")
                sigs.append(f"{label}=:{sig}:")
            headers |= {"Signature-Input": ", ".join(inputs), "Signature": ", ".join(sigs)}
        return {"keys": keys}, headers

    async def card(self) -> dict[str, Any]:
        if self.agent_url is None:
            raise _NOT_PUBLISHED
        return rules.agent_card(self.agent_url, [r[0] for r in await self._published()])

    async def ucp_profile(self) -> dict[str, Any]:
        if self.agent_url is None:
            raise _NOT_PUBLISHED
        keys = [rules.jwk(base64.urlsafe_b64decode(r[1] + "=")) for r in await self._published()]
        return rules.ucp_profile(self.agent_url, keys)

    async def status(self) -> dict[str, Any]:
        """`GET /v1/world/identity`: what the user sees (public facts only, never the private key)."""
        rows = await self._published()
        active = next((r for r in rows if r[4] is None), None)
        return {
            "agent_url": self.agent_url,
            "signing": bool(active) and self.agent_url is not None and self._keys.can_store,
            "kid": active[0] if active else None,
            "created_at": active[3].isoformat() if active else None,
            "published": [r[0] for r in rows],
            "card_url": self.agent_url + rules.CARD_PATH if self.agent_url else None,
            "directory_url": self.agent_url + rules.DIRECTORY_PATH if self.agent_url else None,
        }


_NOT_PUBLISHED = PisaError(
    "not_found",
    "Pisa has no public URL (PISA_AGENT_URL), so it publishes no card.",
    "Set PISA_AGENT_URL (.env.example)",
)
