"""Provider API keys, encrypted at rest with Fernet (PISA_SECRETS_KEY), write-only.

A key is decrypted only to build the client for one call or one key test. It is never returned, logged or put
in an error message: `failures.redact` scrubs any text that could echo it back.
"""

from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from psycopg import AsyncConnection
from pydantic import SecretStr

from pisa.domain.errors import PisaError

_NO_SECRETS_KEY = PisaError(
    "internal",
    "PISA_SECRETS_KEY is not set on the server, so keys can't be stored encrypted.",
    "Set PISA_SECRETS_KEY (see .env.example) and restart Pisa",
)


@dataclass(frozen=True)
class KeyStatus:
    provider: str
    last4: str
    set_at: datetime
    last_test_ok: bool | None
    last_test_at: datetime | None
    last_test_message: str | None


class KeyStore:
    def __init__(self, secrets_key: SecretStr | None) -> None:
        self._fernet = Fernet(secrets_key.get_secret_value().encode()) if secrets_key else None

    @property
    def can_store(self) -> bool:
        return self._fernet is not None

    def _require(self) -> Fernet:
        if self._fernet is None:
            raise _NO_SECRETS_KEY
        return self._fernet

    def seal(self, secret: str) -> bytes:
        """Encrypt any secret (OAuth client secret, refresh token, PKCE verifier) for storage."""
        return self._require().encrypt(secret.encode())

    def unseal(self, data: bytes) -> str:
        try:
            return self._require().decrypt(bytes(data)).decode()
        except InvalidToken:
            raise PisaError(
                "internal",
                "A stored secret can't be decrypted: PISA_SECRETS_KEY changed since it was saved.",
                "Enter the secret again (for Google: Settings → Connectors)",
            ) from None

    def seal_bytes(self, data: bytes) -> bytes:
        """Encrypt bytes for storage (his enrolment audio and his voiceprints)."""
        return self._require().encrypt(data)

    def unseal_bytes(self, data: bytes) -> bytes:
        try:
            return self._require().decrypt(bytes(data))
        except InvalidToken:
            raise PisaError(
                "internal",
                "His stored voice can't be decrypted: PISA_SECRETS_KEY changed since it was saved.",
                "Record the voice again (Settings → Voice)",
            ) from None

    async def set(self, conn: AsyncConnection, provider: str, key: str) -> None:
        key = key.strip()
        if len(key) < 8 or any(c.isspace() for c in key):
            raise PisaError(
                "validation_failed",
                "That doesn't look like an API key: it is too short or contains spaces.",
                "Paste the whole key as shown by the provider",
            )
        ciphertext = self._require().encrypt(key.encode())
        await conn.execute(
            "INSERT INTO provider_keys (provider, ciphertext, last4, set_at) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (provider) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, last4 = EXCLUDED.last4, "
            "set_at = now(), last_test_ok = NULL, last_test_at = NULL, last_test_message = NULL",
            (provider, ciphertext, key[-4:]),
        )

    async def delete(self, conn: AsyncConnection, provider: str) -> None:
        await conn.execute("DELETE FROM provider_keys WHERE provider = %s", (provider,))

    async def get(self, conn: AsyncConnection, provider: str) -> str | None:
        """The plaintext key, for one call only. None when no key is stored."""
        row = await (
            await conn.execute("SELECT ciphertext FROM provider_keys WHERE provider = %s", (provider,))
        ).fetchone()
        if row is None:
            return None
        try:
            return self._require().decrypt(bytes(row[0])).decode()
        except InvalidToken:
            raise PisaError(
                "provider_key_missing",
                f"The stored {provider} key can't be decrypted: PISA_SECRETS_KEY changed since it was saved.",
                f"Add the {provider} key again in Settings → AI providers",
            ) from None

    async def statuses(self, conn: AsyncConnection) -> dict[str, KeyStatus]:
        rows = await (
            await conn.execute(
                "SELECT provider, last4, set_at, last_test_ok, last_test_at, last_test_message FROM "
                "provider_keys"
            )
        ).fetchall()
        return {r[0]: KeyStatus(*r) for r in rows}

    async def record_test(self, conn: AsyncConnection, provider: str, ok: bool | None, message: str) -> None:
        await conn.execute(
            "UPDATE provider_keys SET last_test_ok = %s, last_test_at = now(), last_test_message = %s "
            "WHERE provider = %s",
            (ok, message, provider),
        )
