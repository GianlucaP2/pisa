"""The Google connection: the user's web + iOS client ids, the server auth code the
iPhone gets from Google Sign-In, tokens, status, disconnect.

Secrets (web client secret, refresh and access tokens) are Fernet-encrypted at rest and never returned, logged
or put in a message. When Google refuses the refresh token (in Testing mode it expires after 7 days), the
connector shows status error with a reconnect message and syncing pauses until the user reconnects: no retry
loop.
"""

import asyncio
import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.google import oauth
from pisa.connectors.google.api import GoogleApi
from pisa.connectors.google.calendar import GoogleCalendarSource
from pisa.connectors.google.errors import ConnectorError
from pisa.connectors.google.gmail import GmailSource
from pisa.domain.connectors import FAILURES_BEFORE_ERROR
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.infra import locks
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.services.people import forget_google

DISCONNECT_LOCKS = (locks.GMAIL_SYNC, locks.CALENDAR_SYNC, locks.INTAKE)

CONNECTOR = "google"
NAME = "Google (Gmail + Calendar)"
CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
BAD_CLIENT = (
    "Google rejected Pisa's client credentials (invalid_client): re-enter the client credentials in "
    "Settings → Connectors → Google, then reconnect."
)
EXPIRED = "Google access expired (the app is in Testing mode): reconnect Google in Settings → Connectors."
_TOKEN_MARGIN = timedelta(seconds=60)
_log = get_logger("pisa.google")

_SCOPE_NAMES = {
    oauth.SCOPE_GMAIL: "read Gmail",
    oauth.SCOPE_CALENDAR: "read Calendar",
    oauth.SCOPE_CALENDAR_APP: "manage Pisa's own calendar",
    oauth.SCOPE_CALENDAR_EVENTS: "change your calendar",
    oauth.SCOPE_GMAIL_COMPOSE: "send replies",
}


@dataclass(frozen=True)
class Account:
    account: str | None
    scopes: list[str]
    status: str
    message: str | None
    needs_reconnect: bool
    gmail_history_id: str | None
    gmail_synced_at: datetime | None
    calendar_synced_at: datetime | None


@dataclass(frozen=True)
class Clients:
    web_client_id: str
    web_client_secret_enc: bytes


def _not_configured() -> PisaError:
    return PisaError(
        "connector_not_configured",
        "Google is not configured: your own Google web client's ID and secret are missing.",
        "Create your Google client (Settings → Connectors → Google → Set up) and enter them",
    )


def switched_off() -> PisaError:
    return PisaError(
        "connector_off",
        "Google is switched off in Settings → Connectors: Pisa reads no mail and no calendar and changes "
        "nothing.",
        "Switch Google on in Settings → Connectors",
    )


async def is_off(conn: AsyncConnection) -> bool:
    """Their switch in the registry (a missing row is on: the registry writes it at start)."""
    row = await (await conn.execute("SELECT enabled FROM connectors WHERE id = %s", (CONNECTOR,))).fetchone()
    return row is not None and not row[0]


def _not_connected() -> PisaError:
    return PisaError(
        "connector_not_connected", "Google is not connected.", "Connect Google in Settings → Connectors"
    )


STATE_TTL = timedelta(minutes=10)  # a sign-in started on the phone, finished in the browser
PENDING_TTL = timedelta(minutes=15)  # a different account waits this long for the starting device's yes


def _nothing_pending() -> PisaError:
    return PisaError(
        "not_found",
        "No Google account is waiting for this device's confirmation (or its 15 minutes are over).",
        "Connect Google again",
    )


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


class GoogleService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        http: httpx.AsyncClient,
        sync_http: httpx.Client,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_connected: Callable[[], None] | None = None,
        redirect_uri: str | None = None,
        disabled: bool = False,
    ) -> None:
        self._pool = pool
        # App Store review mode: Google is never called (no mail read or sent, no calendar written), whatever
        # the database holds
        self.disabled = disabled
        self._keys = keys
        self._http = http
        self._sync_http = sync_http  # google-auth's refresh is synchronous (run in a thread)
        self._owner = owner
        self._clock = clock
        self._refresh_lock = asyncio.Lock()  # parallel fetches must not trigger parallel refreshes
        self.on_connected = on_connected
        self.redirect_uri = redirect_uri  # this server's own callback (None: no https address, no Google)

    # --- status -------------------------------------------------------------------------------------------

    async def account(self, conn: AsyncConnection) -> Account | None:
        row = await (
            await conn.execute(
                "SELECT account, scopes, status, message, needs_reconnect, gmail_history_id, "
                "gmail_synced_at, "
                "calendar_synced_at FROM connector_accounts WHERE connector = %s",
                (CONNECTOR,),
            )
        ).fetchone()
        return Account(*row) if row else None

    async def _clients(self, conn: AsyncConnection) -> Clients | None:
        row = await (
            await conn.execute(
                "SELECT web_client_id, web_client_secret_enc FROM connector_credentials WHERE connector = %s",
                (CONNECTOR,),
            )
        ).fetchone()
        return Clients(row[0], bytes(row[1])) if row else None

    async def connector(self, device_id: uuid.UUID | None = None) -> dict[str, Any]:
        """The connection's state, as `device_id` sees it: `pending_started_here` is true on the device that
        started the waiting sign-in only (false on any other, false when nothing waits or no device asks)."""
        async with self._pool.connection() as conn:
            clients = await self._clients(conn)
            account = await self.account(conn)
            pending = await self._pending(conn, None)
        gmail_at = account.gmail_synced_at if account else None
        calendar_at = account.calendar_synced_at if account else None
        return {
            "id": CONNECTOR,
            "name": NAME,
            "configured": clients is not None,
            "status": account.status if account else "not_connected",
            "account": account.account if account else None,
            "scopes": list(account.scopes) if account else [],
            "last_sync": {
                "gmail": gmail_at.isoformat() if gmail_at else None,
                "calendar": calendar_at.isoformat() if calendar_at else None,
            },
            "message": account.message if account and account.status == "error" else None,
            "client": {"web_client_id": clients.web_client_id if clients else None},
            "redirect_uri": self.redirect_uri,
            "pending_account": pending[2] if pending else None,
            "pending_started_here": bool(pending and device_id is not None and pending[1] == device_id),
            "requested_scopes": list(oauth.SCOPES),
            # the permissions Pisa asks for but doesn't have (the app offers to reconnect Google).
            "missing_scopes": [s for s in oauth.SCOPES if not account or s not in account.scopes],
        }

    async def require(self, scope: str) -> None:
        """Raises a PisaError naming what's missing when the connected account didn't grant `scope`."""
        async with self._pool.connection() as conn:
            account = await self.account(conn)
        if account is None or account.status == "not_connected":
            raise _not_connected()
        if scope not in account.scopes:
            raise PisaError(
                "google_permission_missing",
                f"Google hasn't given Pisa permission to {_SCOPE_NAMES[scope]}.",
                f"Reconnect Google in Settings → Connectors to let Pisa {_SCOPE_NAMES[scope]}",
            )

    async def account_email(self) -> str | None:
        async with self._pool.connection() as conn:
            account = await self.account(conn)
        return account.account if account else None

    async def connectors(self, device_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        return [await self.connector(device_id)]

    async def record_status(
        self, conn: AsyncConnection, source: Literal["gmail", "calendar"], *, error: str | None
    ) -> None:
        """A sync round's outcome. Non-auth failures turn the connector red only after
        FAILURES_BEFORE_ERROR in a row for that source, so a single Google 503 doesn't show as broken."""
        mine = sql.Identifier(f"{source}_failures")
        other = sql.Identifier(f"{'calendar' if source == 'gmail' else 'gmail'}_failures")
        if error:
            query = sql.SQL(
                "UPDATE connector_accounts SET {mine} = {mine} + 1, "
                "status = CASE WHEN {mine} + 1 >= %s THEN 'error' ELSE status END, "
                "message = CASE WHEN {mine} + 1 >= %s THEN %s ELSE message END WHERE connector = %s"
            ).format(mine=mine)
            await conn.execute(query, (FAILURES_BEFORE_ERROR, FAILURES_BEFORE_ERROR, error, CONNECTOR))
        else:
            query = sql.SQL(
                "UPDATE connector_accounts SET {mine} = 0, "
                "status = CASE WHEN {other} >= %s THEN 'error' ELSE 'connected' END, "
                "message = CASE WHEN {other} >= %s THEN message ELSE NULL END WHERE connector = %s"
            ).format(mine=mine, other=other)
            await conn.execute(query, (FAILURES_BEFORE_ERROR, FAILURES_BEFORE_ERROR, CONNECTOR))

    async def mark_reconnect(self, conn: AsyncConnection, message: str) -> None:
        """Google no longer accepts Pisa's authorization: show why, and pause syncing until a reconnect."""
        await conn.execute(
            "UPDATE connector_accounts SET status = 'error', message = %s, needs_reconnect = true, "
            "access_token_enc = NULL, access_expires_at = NULL WHERE connector = %s",
            (message, CONNECTOR),
        )
        _log.warning("google_needs_reconnect", cause=message)

    # --- credentials and connecting -----------------------------------------------------------------------

    async def set_credentials(self, web_client_id: str, web_client_secret: str) -> None:
        """The user's own Google web client (their project's): the secret sealed, never shown again."""
        web_client_id, web_client_secret = web_client_id.strip(), web_client_secret.strip()
        if not web_client_id.endswith(CLIENT_ID_SUFFIX):
            raise PisaError(
                "validation_failed",
                f"That is not a Google client ID: it should end with {CLIENT_ID_SUFFIX}.",
                "Copy it from Google Cloud → Google Auth Platform → Clients",
            )
        if len(web_client_secret) < 10 or any(c.isspace() for c in web_client_secret):
            raise PisaError(
                "validation_failed",
                "That doesn't look like a Google client secret: it is too short or contains spaces.",
                "Copy the Client secret of your web client from Google Cloud",
            )
        sealed = self._keys.seal(web_client_secret)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO connector_credentials (connector, web_client_id, web_client_secret_enc, set_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (connector) DO UPDATE SET "
                "web_client_id = EXCLUDED.web_client_id, web_client_secret_enc = "
                "EXCLUDED.web_client_secret_enc, set_at = EXCLUDED.set_at",
                (CONNECTOR, web_client_id, sealed, self._clock()),
            )
        _log.info("google_credentials_set")

    async def _secret(self, conn: AsyncConnection) -> tuple[str, str]:
        clients = await self._clients(conn)
        if clients is None:
            raise _not_configured()
        return clients.web_client_id, self._keys.unseal(clients.web_client_secret_enc)

    async def start(self, device_id: uuid.UUID, lang: str) -> dict[str, Any]:
        """`POST /v1/connectors/google/start`: Google's consent page for the user's own web client, with a
        state (single use, STATE_TTL, this device's) and a PKCE challenge; the verifier stays here, sealed."""
        if self.redirect_uri is None:
            raise PisaError(
                "connector_not_configured",
                "Google needs this server's https address to come back to, and it has none.",
                "Reach Pisa at an https address (a tailnet name or your own domain) in PISA_ADDRESSES",
            )
        now = self._clock()
        state = secrets.token_urlsafe(32)
        verifier, challenge = oauth.pkce_pair()
        async with self._pool.connection() as conn:
            if await is_off(conn):
                raise switched_off()
            clients = await self._clients(conn)
            if clients is None:
                raise _not_configured()
            await conn.execute(
                "DELETE FROM google_oauth_states WHERE expires_at < %s AND coalesce(pending_until, %s) <= %s",
                (now, now, now),
            )
            # one sign-in under way a device: a newer start replaces it, its state comes back refused
            await conn.execute(
                "DELETE FROM google_oauth_states WHERE device_id = %s AND used_at IS NULL", (device_id,)
            )
            await conn.execute(
                "INSERT INTO google_oauth_states (state_hash, device_id, verifier_enc, redirect_uri, lang, "
                "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (_hash(state), device_id, self._keys.seal(verifier), self.redirect_uri, lang[:5], now,
                 now + STATE_TTL),
            )  # fmt: skip
        _log.info("google_sign_in_started", device_id=str(device_id))
        url = oauth.authorize_url(
            client_id=clients.web_client_id, redirect_uri=self.redirect_uri, state=state, challenge=challenge
        )
        expires = (now + STATE_TTL).isoformat()
        return {"auth_url": url, "redirect_uri": self.redirect_uri, "expires_at": expires}

    async def finish(self, state: str, code: str | None, error: str | None) -> tuple[str, str | None, str]:
        """`GET /oauth/google/callback`: the state taken (once), the code exchanged with the stored secret.
        (status "connected" or "error", the error's reason, the page's language). Never logs the code, the
        state or a token."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE google_oauth_states SET used_at = %s WHERE state_hash = %s AND used_at IS NULL "
                    "AND expires_at > %s RETURNING verifier_enc, redirect_uri, lang, device_id",
                    (now, _hash(state), now),
                )
            ).fetchone()
        if row is None:
            _log.info("google_sign_in_refused", reason="state_expired")
            return "error", "state_expired", "en"
        verifier, redirect_uri, lang = self._keys.unseal(bytes(row[0])), str(row[1]), str(row[2])
        if error or not code:
            reason = "access_denied" if error == "access_denied" else "exchange_failed"
            _log.info("google_sign_in_refused", reason=reason)
            return "error", reason, lang
        try:
            connected = await self.connect(
                code, redirect_uri=redirect_uri, code_verifier=verifier, hold=_hash(state)
            )
        except PisaError as err:
            reason = "missing_scopes" if "part of the access" in err.message else (
                "not_configured" if err.code == "connector_not_configured" else "exchange_failed"
            )  # fmt: skip
            _log.info("google_sign_in_refused", reason=reason)
            return "error", reason, lang
        return ("connected" if connected is not None else "confirm"), None, lang

    async def _pending(self, conn: AsyncConnection, device_id: uuid.UUID | None) -> tuple[Any, ...] | None:
        """The account waiting for a device's yes (any device's when None), within PENDING_TTL."""
        return await (
            await conn.execute(
                "SELECT state_hash, device_id, pending_account, pending_scopes, pending_refresh_enc, "
                "pending_access_enc, pending_access_expires_at FROM google_oauth_states "
                "WHERE pending_account IS NOT NULL AND pending_until > %s "
                "AND (%s::uuid IS NULL OR device_id = %s) "
                "ORDER BY created_at DESC LIMIT 1",
                (self._clock(), device_id, device_id),
            )
        ).fetchone()

    async def pending_account(self, device_id: uuid.UUID) -> str | None:
        """The account waiting for this device's yes, if any."""
        async with self._pool.connection() as conn:
            row = await self._pending(conn, device_id)
        return str(row[2]) if row is not None else None

    async def confirm(self, device_id: uuid.UUID, account: str | None = None) -> dict[str, Any]:
        """The starting device's yes to the account Google came back with: connected now (another account's
        caches go first), the first sync starts. Only that device; only within PENDING_TTL."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._pending(conn, device_id)
            if row is None or (
                account is not None and row[2] != account
            ):  # the account the yes was signed for
                raise _nothing_pending()
            # this one, and any older sign-in of the device still waiting: one answer settles them
            await conn.execute(
                "DELETE FROM google_oauth_states WHERE device_id = %s AND pending_account IS NOT NULL",
                (device_id,),
            )
        tokens = oauth.Tokens(
            access_token=self._keys.unseal(bytes(row[5])), expires_in=max(
                60, int((row[6] - self._clock()).total_seconds())
            ), refresh_token=self._keys.unseal(bytes(row[4])), scopes=tuple(row[3] or ()), email=str(row[2]),
        )  # fmt: skip
        return await self._store(str(row[2]), tokens, self._clock())

    async def cancel(self, device_id: uuid.UUID) -> None:
        """The starting device's no: every authorization held for it is forgotten and revoked at Google (each
        one, best effort: a failure never stops the others); nothing changed."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._pending(conn, device_id)
            if row is None:
                raise _nothing_pending()
            held = await (
                await conn.execute(
                    "DELETE FROM google_oauth_states WHERE device_id = %s AND pending_account IS NOT NULL "
                    "RETURNING pending_refresh_enc",
                    (device_id,),
                )
            ).fetchall()
        for (sealed,) in held:
            if sealed is None:
                continue
            try:
                await oauth.revoke(self._http, self._keys.unseal(bytes(sealed)))
            except PisaError as err:  # best effort: forgotten here either way
                _log.warning("google_revoke_failed", during="cancel", code=err.code)
        _log.info("google_sign_in_cancelled", held=len(held))

    async def connect(
        self, code: str, *, redirect_uri: str = "", code_verifier: str | None = None, hold: str | None = None
    ) -> dict[str, Any] | None:
        """Exchange an authorization code for the user's own web client; a first sync starts at once. `hold`:
        the sign-in's state (its hash): an account other than the connected one waits there for the starting
        device's confirmation (None returned) instead of being connected."""
        now = self._clock()
        async with self._pool.connection() as conn:
            web_client_id, web_client_secret = await self._secret(conn)
        try:
            tokens = await oauth.exchange_server_auth_code(
                self._http,
                web_client_id=web_client_id,
                web_client_secret=web_client_secret,
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
            )
        except ConnectorError as exc:
            _log.warning("google_code_rejected", status=exc.status)
            if "invalid_client" in exc.message:
                raise PisaError(
                    "connector_auth_failed",
                    "Google rejected Pisa's client credentials (invalid_client).",
                    "Re-enter the client credentials in Settings → Connectors → Google",
                ) from None
            raise PisaError("connector_auth_failed", exc.message, "Connect Google again") from None
        missing = [s for s in oauth.READ_SCOPES if s not in tokens.scopes]  # the action scopes may be missing
        if missing or not tokens.refresh_token:
            # Refused below: don't leave a live grant at Google that Pisa will never use.
            try:
                await oauth.revoke(self._http, tokens.refresh_token or tokens.access_token)
            except PisaError as err:  # best effort: the refusal below stands either way
                _log.warning("google_revoke_failed", during="partial_grant", code=err.code)
        if missing:
            names = ", ".join(_SCOPE_NAMES[s] for s in missing)
            raise PisaError(
                "connector_auth_failed",
                f"Google granted only part of the access Pisa needs: {names} is missing.",
                "Connect again and allow every permission",
            )
        if not tokens.refresh_token:
            raise PisaError(
                "connector_auth_failed",
                "Google did not return a lasting authorization (no refresh token).",
                "Connect again",
            )
        email = tokens.email or await self._email_from_gmail(tokens.access_token)
        if hold is not None:
            async with self._pool.connection() as conn:
                previous = await self.account(conn)
            if previous is None or previous.account != email:
                # another account than the connected one (or the first): held for the starting device's yes
                await self._hold(hold, email, tokens, now)
                return None
        return await self._store(email, tokens, now)

    async def _hold(self, state_hash: str, email: str, tokens: oauth.Tokens, now: datetime) -> None:
        assert tokens.refresh_token is not None
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE google_oauth_states SET pending_account = %s, pending_scopes = %s, "
                "pending_refresh_enc = %s, pending_access_enc = %s, pending_access_expires_at = %s, "
                "pending_until = %s WHERE state_hash = %s",
                (email, list(tokens.scopes), self._keys.seal(tokens.refresh_token),
                 self._keys.seal(tokens.access_token), now + timedelta(seconds=tokens.expires_in),
                 now + PENDING_TTL, state_hash),
            )  # fmt: skip
        _log.info("google_sign_in_pending")

    async def _store(self, email: str, tokens: oauth.Tokens, now: datetime) -> dict[str, Any]:
        """The account connected: its tokens sealed, another account's caches gone first, first sync on."""
        assert tokens.refresh_token is not None
        async with self._pool.connection() as conn, conn.transaction():
            previous = await self.account(conn)
            if previous is not None and previous.account != email:
                await _delete_caches(conn)  # a different account: its predecessor's data must go
            await conn.execute(
                "INSERT INTO connector_accounts (connector, account, scopes, refresh_token_enc, "
                "access_token_enc, "
                "access_expires_at, status, message, needs_reconnect, connected_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'connected', NULL, false, %s) "
                "ON CONFLICT (connector) DO UPDATE SET account = EXCLUDED.account, scopes = EXCLUDED.scopes, "
                "refresh_token_enc = EXCLUDED.refresh_token_enc, access_token_enc = "
                "EXCLUDED.access_token_enc, "
                "access_expires_at = EXCLUDED.access_expires_at, status = 'connected', message = NULL, "
                "needs_reconnect = false, connected_at = EXCLUDED.connected_at, gmail_failures = 0, "
                "calendar_failures = 0",
                (
                    CONNECTOR,
                    email,
                    list(tokens.scopes),
                    self._keys.seal(tokens.refresh_token),
                    self._keys.seal(tokens.access_token),
                    now + timedelta(seconds=tokens.expires_in),
                    now,
                ),
            )
        _log.info("google_connected")
        if self.on_connected is not None:
            self.on_connected()
        return await self.connector()

    async def _email_from_gmail(self, access_token: str) -> str:
        async def token(_: bool) -> str:
            return access_token

        try:
            email, _ = await GmailSource(GoogleApi(self._http, token, service="Gmail")).profile()
        except ConnectorError as exc:
            raise PisaError("connector_auth_failed", exc.message, "Connect Google again") from None
        return email

    # --- tokens and sources -------------------------------------------------------------------------------

    async def access_token(self, force_refresh: bool = False) -> str:
        async with self._refresh_lock:
            return await self._access_token(force_refresh)

    async def _access_token(self, force_refresh: bool) -> str:
        now = self._clock()
        async with self._pool.connection() as conn:
            if self.disabled or await is_off(conn):  # off means off, no call to Google
                raise switched_off()
            row = await (
                await conn.execute(
                    "SELECT refresh_token_enc, access_token_enc, access_expires_at, needs_reconnect "
                    "FROM connector_accounts WHERE connector = %s",
                    (CONNECTOR,),
                )
            ).fetchone()
            if row is None or row[0] is None:
                raise _not_connected()
            refresh_enc, access_enc, expires_at, needs_reconnect = row
            if needs_reconnect:
                raise ConnectorError(EXPIRED, auth=True)
            if (
                not force_refresh
                and access_enc is not None
                and expires_at
                and expires_at - _TOKEN_MARGIN > now
            ):
                return self._keys.unseal(access_enc)
            web_client_id, web_client_secret = await self._secret(conn)
        try:
            tokens = await oauth.refresh_access_token(
                self._sync_http,
                web_client_id=web_client_id,
                web_client_secret=web_client_secret,
                refresh_token=self._keys.unseal(refresh_enc),
            )
        except ConnectorError as exc:
            if exc.auth and "invalid_grant" in exc.message:
                raise ConnectorError(EXPIRED, auth=True) from None
            if exc.auth and "invalid_client" in exc.message:
                raise ConnectorError(BAD_CLIENT, auth=True) from None
            if exc.auth:
                cause = exc.message.rstrip(".")
                raise ConnectorError(
                    f"{cause}: reconnect Google in Settings → Connectors.", auth=True
                ) from None
            raise
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE connector_accounts SET access_token_enc = %s, access_expires_at = %s WHERE "
                "connector = %s",
                (self._keys.seal(tokens.access_token), now + timedelta(seconds=tokens.expires_in), CONNECTOR),
            )
        return tokens.access_token

    def api(self, service: str) -> GoogleApi:
        return GoogleApi(self._http, self.access_token, service=service)

    def gmail(self) -> GmailSource:
        return GmailSource(self.api("Gmail"))

    def calendar(self) -> GoogleCalendarSource:
        return GoogleCalendarSource(
            GoogleApi(self._http, self.access_token, service="Google Calendar"), owner=self._owner
        )

    # --- disconnect ---------------------------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Revoke at Google (best effort), delete the tokens and the email and calendar caches. Tasks stay."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT refresh_token_enc FROM connector_accounts WHERE connector = %s", (CONNECTOR,)
                )
            ).fetchone()
        revoked = False
        if row is not None and row[0] is not None:
            try:
                revoked = await oauth.revoke(self._http, self._keys.unseal(bytes(row[0])))
            except PisaError as err:  # the stored token can't be read: disconnected all the same, not revoked
                _log.warning("google_revoke_failed", during="disconnect", code=err.code)
                revoked = False
        async with self._pool.connection() as conn, conn.transaction():
            # Wait (up to 60 s) for a running Gmail/Calendar sync or intake round to finish: after this
            # transaction they find no account and stop, so no email reaches a model after a disconnect.
            await conn.execute("SET LOCAL lock_timeout = '60s'")
            for key in DISCONNECT_LOCKS:
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            await conn.execute("DELETE FROM connector_accounts WHERE connector = %s", (CONNECTOR,))
            await _delete_caches(conn)
        _log.info("google_disconnected", revoked_at_google=revoked)


async def _delete_caches(conn: AsyncConnection) -> None:
    await conn.execute("DELETE FROM email_messages")
    await conn.execute("DELETE FROM calendars")  # events cascade
    await forget_google(conn)  # what Pisa learnt about people from them
