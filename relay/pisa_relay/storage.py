# SPDX-License-Identifier: AGPL-3.0-only
"""One SQLite file: the registrations, plus the short-lived replay and rate bookkeeping.

SQLite because the relay is one small process on one small host: no database server to run, patch or back up
separately, and every query is a primary-key lookup. The file holds no personal text; the HMAC keys in it are
sealed (AES-256-GCM under a key derived from the storage key, the handle as associated data), so a copied file
alone cannot sign a push. The calls are synchronous and short (local disk, indexed): the event loop waits
microseconds.
"""

import base64
import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE = 12
CANARY = b"pisa-relay storage key canary"
ACTIVITY_TOKENS_PER_KIND = 4  # per handle: past this, the oldest token of that kind is forgotten
ENDED_KEEP_S = 60 * 86_400  # past its expiry and any billing grace, an ended period needs no memory


class StorageKeyError(RuntimeError):
    """RELAY_STORAGE_KEY does not open what is stored: an operator error, never a sign a handle is gone."""


class TooManyDevices(Exception):
    """The subscription has its cap of handles, all used recently."""


@dataclass(frozen=True)
class Registration:
    handle: str
    apns_token: str
    environment: str
    key: bytes  # sha256(secret): the HMAC key the user's server signs with
    expires_at: int  # the paid period's end
    grace_expires_at: int | None  # Apple's billing grace, when in it
    language: str
    app_key: bytes | None  # sha256(app_secret): the HMAC key only the app holds
    ref: bytes  # the subscription reference
    created_at: str  # the hour
    account_bound: bool  # the registration proved the transaction's appAccountToken

    def ends_at(self) -> int:
        return max(self.expires_at, self.grace_expires_at or 0)


@dataclass(frozen=True)
class Device:
    """One of a subscription's registrations, as its own app may see it."""

    id: str  # opaque: never the handle
    handle: str
    created_at: str  # the hour
    last_used_at: str | None
    environment: str


def hour(now: float) -> str:
    """Times of use are kept to the hour: enough to order devices, not enough to log a day."""
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H")


def _app_aad(handle: str) -> bytes:
    return handle.encode() + b"\napp"


def _derive(storage_key: bytes, info: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt=None, info=info).derive(storage_key)


class Storage:
    def __init__(self, path: str, storage_key: bytes) -> None:
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._aead = AESGCM(_derive(storage_key, b"pisa-relay/v1 seal"))
        self._ref_key = _derive(storage_key, b"pisa-relay/v1 subscription ref")
        self._device_key = _derive(storage_key, b"pisa-relay/v1 device id")
        self._migrate()
        self._check_key()

    def _migrate(self) -> None:
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        folder = resources.files("pisa_relay") / "migrations"
        for script in sorted((f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name):
            number = int(script.name.split("_", 1)[0])
            if number > version:
                self._db.executescript(
                    f"BEGIN; {script.read_text()}; PRAGMA user_version = {number}; COMMIT;"
                )

    def _seal(self, value: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE)
        return nonce + self._aead.encrypt(nonce, value, aad)

    def _open(self, sealed: bytes, aad: bytes) -> bytes:
        try:
            return self._aead.decrypt(sealed[:NONCE], sealed[NONCE:], aad)
        except InvalidTag:
            raise StorageKeyError("RELAY_STORAGE_KEY does not open the stored keys.") from None

    def _check_key(self) -> None:
        """Refuses a storage key that does not open this database (a typo, a rotation, a restore beside
        another key): starting would answer every push with an error. A new database gets the canary."""
        row = self._db.execute("SELECT value FROM meta WHERE name = 'canary'").fetchone()
        if row is not None:
            if self._open(bytes(row[0]), b"canary") != CANARY:
                raise StorageKeyError("RELAY_STORAGE_KEY does not open the canary.")
            return
        sample = self._db.execute("SELECT handle, secret_hash FROM registration LIMIT 1").fetchone()
        if sample is not None:
            self._open(bytes(sample[1]), str(sample[0]).encode())
        self._db.execute(
            "INSERT INTO meta (name, value) VALUES ('canary', ?)", (self._seal(CANARY, b"canary"),)
        )

    def close(self) -> None:
        self._db.close()

    def subscription_ref(self, environment: str, original_transaction_id: str) -> bytes:
        message = f"{environment}\n{original_transaction_id}".encode()
        return hmac.new(self._ref_key, message, hashlib.sha256).digest()

    # --- registrations ------------------------------------------------------------------------------------
    def add(
        self, handle: str, apns_token: str, environment: str, key: bytes, ref: bytes,
        storekit_environment: str,
        expires_at: int, grace_expires_at: int | None, language: str, now: float, *, cap: int, idle_s: int,
        app_key: bytes, account_bound: bool,
    ) -> None:  # fmt: skip
        """A new handle. The same device re-registering under the same subscription replaces its old handle.
        At `cap` handles for one subscription, the least recently used one that has been idle for `idle_s`
        ends; when every one was used more recently than that, `TooManyDevices`."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for (old,) in self._db.execute(
                "SELECT handle FROM registration WHERE apns_token = ? AND subscription_ref = ?",
                (apns_token, ref),
            ).fetchall():
                self._forget(old)
            (count,) = self._db.execute(
                "SELECT count(*) FROM registration WHERE subscription_ref = ?", (ref,)
            ).fetchone()
            if count >= cap:
                idle = self._db.execute(
                    "SELECT handle FROM registration WHERE subscription_ref = ? AND "
                    "coalesce(last_used_at, created_at) < ? "
                    "ORDER BY coalesce(last_used_at, created_at), rowid "
                    "LIMIT 1",
                    (ref, hour(now - idle_s)),
                ).fetchone()
                if idle is None:
                    raise TooManyDevices
                self._forget(idle[0])
            self._db.execute(
                "INSERT INTO registration (handle, apns_token, environment, secret_hash, subscription_ref, "
                "storekit_environment, subscription_expires_at, grace_expires_at, language, created_at, "
                "app_key, account_bound) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (handle, apns_token, environment, self._seal(key, handle.encode()), ref, storekit_environment,
                 expires_at, grace_expires_at, language, hour(now), self._seal(app_key, _app_aad(handle)),
                 int(account_bound)),
            )  # fmt: skip
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def get(self, handle: str) -> Registration | None:
        """None for an unknown handle; `StorageKeyError` when the key does not open its row."""
        row = self._db.execute(
            "SELECT apns_token, environment, secret_hash, subscription_expires_at, grace_expires_at, "
            "language, app_key, subscription_ref, created_at, account_bound "
            "FROM registration WHERE handle = ?",
            (handle,),
        ).fetchone()
        if row is None:
            return None
        key = self._open(bytes(row[2]), handle.encode())
        app_key = self._open(bytes(row[6]), _app_aad(handle)) if row[6] is not None else None
        return Registration(handle, row[0], row[1], key, row[3], row[4], row[5], app_key, bytes(row[7]),
                            row[8], bool(row[9]))  # fmt: skip

    def device_id(self, handle: str) -> str:
        digest = hmac.new(self._device_key, handle.encode(), hashlib.sha256).digest()[:12]
        return base64.urlsafe_b64encode(digest).decode()  # 16 characters, no padding

    def devices(self, ref: bytes) -> list[Device]:
        rows = self._db.execute(
            "SELECT handle, created_at, last_used_at, environment FROM registration "
            "WHERE subscription_ref = ? "
            "ORDER BY created_at, rowid",
            (ref,),
        ).fetchall()
        return [Device(self.device_id(r[0]), r[0], r[1], r[2], r[3]) for r in rows]

    def _forget(self, handle: str) -> None:
        for table in ("registration", "usage", "activity_token"):
            self._db.execute(f"DELETE FROM {table} WHERE handle = ?", (handle,))  # noqa: S608 (fixed names)

    def delete(self, handle: str) -> None:
        self._forget(handle)

    def used(self, handle: str, now: float) -> None:
        self._db.execute("UPDATE registration SET last_used_at = ? WHERE handle = ?", (hour(now), handle))

    def renew(self, ref: bytes, expires_at: int) -> int:
        return self._db.execute(
            "UPDATE registration SET subscription_expires_at = max(subscription_expires_at, ?) "
            "WHERE subscription_ref = ?",
            (expires_at, ref),
        ).rowcount

    def grace(self, ref: bytes, grace_expires_at: int) -> int:
        return self._db.execute(
            "UPDATE registration SET grace_expires_at = max(coalesce(grace_expires_at, 0), ?) "
            "WHERE subscription_ref = ?",
            (grace_expires_at, ref),
        ).rowcount

    def end_subscription(self, ref: bytes, expires_at: int, *, remember: bool) -> int:
        """Ends the handles of a subscription whose paid period is not past `expires_at` (a stale notice about
        an older period leaves a renewed subscription alone). `remember`: a refund or a revocation, so that a
        transaction signed before it cannot register again."""
        if remember:
            self._db.execute(
                "INSERT OR IGNORE INTO ended_subscription (subscription_ref, expires_at) VALUES (?, ?)",
                (ref, expires_at),
            )
        handles = self._db.execute(
            "SELECT handle FROM registration WHERE subscription_ref = ? AND subscription_expires_at <= ?",
            (ref, expires_at),
        ).fetchall()
        for (handle,) in handles:
            self._forget(handle)
        return len(handles)

    def ended(self, ref: bytes, expires_at: int) -> bool:
        """A refund or a revocation covers this period (a renewal after it has a later expiry)."""
        return (
            self._db.execute(
                "SELECT 1 FROM ended_subscription WHERE subscription_ref = ? AND expires_at >= ?",
                (ref, expires_at),
            ).fetchone()
            is not None
        )

    # --- Live Activity tokens -----------------------------------------------------------------------------
    def add_activity_token(self, handle: str, token: str, kind: str, now: float) -> None:
        digest = hashlib.sha256(token.lower().encode()).digest()
        self._db.execute(
            "INSERT OR REPLACE INTO activity_token (handle, token_hash, kind, added_at) VALUES (?, ?, ?, ?)",
            (handle, digest, kind, int(now)),
        )
        self._db.execute(
            "DELETE FROM activity_token WHERE rowid IN (SELECT rowid FROM activity_token "
            "WHERE handle = ? AND "
            "kind = ? ORDER BY added_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (handle, kind, ACTIVITY_TOKENS_PER_KIND),
        )

    def has_activity_token(self, handle: str, token: str, kind: str) -> bool:
        digest = hashlib.sha256(token.lower().encode()).digest()
        return (
            self._db.execute(
                "SELECT 1 FROM activity_token WHERE handle = ? AND token_hash = ? AND kind = ?",
                (handle, digest, kind),
            ).fetchone()
            is not None
        )

    def drop_activity_token(self, handle: str, token: str) -> None:
        digest = hashlib.sha256(token.lower().encode()).digest()
        self._db.execute("DELETE FROM activity_token WHERE handle = ? AND token_hash = ?", (handle, digest))

    # --- replay and rate bookkeeping ----------------------------------------------------------------------
    def accept_signature(self, signature: bytes, expires_at: int) -> bool:
        """False when this signature was already accepted (the same request again)."""
        try:
            self._db.execute(
                "INSERT INTO accepted_signature (signature, expires_at) VALUES (?, ?)",
                (signature, expires_at),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def count(self, key: str, bucket: str, expires_at: int) -> int:
        """Counts one use of `key` (a handle, or `@...` for registration limits) in `bucket`; returns the
        count so far."""
        (count,) = self._db.execute(
            "INSERT INTO usage (handle, bucket, count, expires_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT (handle, bucket) DO UPDATE SET count = count + 1 RETURNING count",
            (key, bucket, expires_at),
        ).fetchone()
        return int(count)

    def sweep(self, now: float, grace_s: int) -> None:
        """Forgets what is past: accepted signatures, rate counters, old refunds, and subscriptions over
        their grace."""
        stamp = int(now)
        self._db.execute("DELETE FROM accepted_signature WHERE expires_at < ?", (stamp,))
        self._db.execute("DELETE FROM usage WHERE expires_at < ?", (stamp,))
        self._db.execute("DELETE FROM ended_subscription WHERE expires_at + ? < ?", (ENDED_KEEP_S, stamp))
        for (handle,) in self._db.execute(
            "SELECT handle FROM registration WHERE "
            "max(subscription_expires_at, coalesce(grace_expires_at, 0)) "
            "+ ? < ?",
            (grace_s, stamp),
        ).fetchall():
            self._forget(handle)
