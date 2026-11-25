"""The connectors' registry and their syncs. Every source is a connector, parametric, switched in its
own Settings section (`domain/connectors.REGISTRY`); this service keeps the rows, decides each state, holds
the credentials (sealed with PISA_SECRETS_KEY like the provider keys: write-only, never in a response or a
log), runs the syncs of the connectors that are on, and answers the brain's and the night's three readers.

RAILS:
- **Off means off**: a connector switched off is never synced, never called, and feeds no tool (the brain's
  `tools_off`); a phone's health summary for an off connector is refused (409 `connector_off`), nothing kept.
- **Read-only**: the bank's provider has no payment call; the PEC is opened with EXAMINE and read with PEEK.
- **Outside text is data**: an obligation's text is code's own; a bank's counterparty reaches the
brain
  only inside a data block; links are never followed; a phishing-like message is flagged, never tracked.
- **Logs carry ids and counts only.**
"""

import json
import secrets as token_maker
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.sources.ade import AdeCassetto
from pisa.connectors.sources.enable_banking import EnableBanking
from pisa.connectors.sources.errors import SourceError
from pisa.connectors.sources.garmin import GarminPersonal
from pisa.connectors.sources.pec import PecImap
from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.domain import connectors as rules
from pisa.domain import experimental
from pisa.domain import health_days as health_rules
from pisa.domain import obligations as radar
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.tasks import end_of_day
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.connectors import BankProvider, Mailbox, SportProvider, TaxDocuments
from pisa.services.tasks import insert_task

_log = get_logger("pisa.connectors")
RADAR_DAYS = timedelta(days=60)  # the radar reads the last 60 days of their mail, each email once
CONSENT_STATE = "connector_consent:"  # + id: the bank consent's one-use `state`
HOURLY = timedelta(hours=1)
BANK_EVERY = timedelta(hours=6)  # PSD2 allows 4 unattended reads a day
MAIL_BATCH = 500
KEEP_ONCE = "ON CONFLICT (source, source_ref) DO NOTHING"
KEEP_UPDATE = (  # a vault document's dates follow the document
    "ON CONFLICT (source, source_ref) DO UPDATE SET due_on = EXCLUDED.due_on, what = EXCLUDED.what, "
    "updated_at = EXCLUDED.updated_at"
)
HEALTH_COLUMNS = (
    "sleep_min",
    "in_bed_min",
    "bedtime",
    "wake_time",
    "steps",
    "active_kcal",
    "exercise_min",
    "resting_hr",
    "hrv_sdnn_ms",
    "vo2max",
    "workouts",
    "doses_taken",
    "doses_missed",
)
# Each Apple Health area they can switch off, and the columns it fills
AREAS = {
    "sleep": ("sleep_min", "in_bed_min", "bedtime", "wake_time"),
    "activity": ("steps", "active_kcal", "exercise_min"),
    "workouts": ("workouts",),
    "heart": ("resting_hr", "hrv_sdnn_ms", "vo2max"),
    "medications": ("doses_taken", "doses_missed"),
}
_PART_OF = {r.key: r.part for r in radar.RULES} | {
    "condominium_assembly": "condominium",
    "missing_document": "car",  # "the car's date is missing" only while their list has a car
}


def _not_found(connector_id: str) -> PisaError:
    return PisaError(
        "not_found", f"There is no connector {connector_id!r}.", "Pick one from Settings → Connections"
    )


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Check the value and try again")


def _off(s: rules.Spec) -> PisaError:
    return PisaError(
        "connector_off",
        f"{s.name} is switched off in Settings → Connections: Pisa takes nothing from it.",
        f"Switch {s.name} on in Settings → Connections",
    )


class ConnectorsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        http: httpx.AsyncClient,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sport: dict[str, SportProvider] | None = None,
        bank: BankProvider | None = None,
        mailbox: Mailbox | None = None,
        tax: TaxDocuments | None = None,
        enabled: frozenset[str] = experimental.ALL,
    ) -> None:
        self._pool = pool
        # the connectors this install has: one whose experimental feature is off (`PISA_EXPERIMENTAL`) is
        # absent (never listed, its routes 404, no sync, no tool, no line of the radar); its row stays
        self.registry: tuple[rules.Spec, ...] = tuple(
            s for s in rules.REGISTRY if experimental.connector_on(s.id, enabled)
        )
        self._keys = keys
        self._owner = owner
        self._clock = clock
        self.sport: dict[str, SportProvider] = sport or {"garmin": GarminPersonal(http, clock=clock)}
        self.bank: BankProvider = bank or EnableBanking(http, clock=clock)
        self.mailbox: Mailbox = mailbox or PecImap()
        self.tax: TaxDocuments = tax or AdeCassetto(http)
        self.on_change: Callable[[], Awaitable[None]] | None = None

    # --- the registry ------------------------------------------------------------------------------------

    async def ensure(self) -> None:
        """One row a spec (code's list): a new connector arrives with its default switch and settings; one
        that serves another country than Pisa's (PISA_COUNTRY) arrives switched off."""
        async with self._pool.connection() as conn:
            for s in rules.REGISTRY:
                on = s.default_enabled and rules.in_reach(s, self._owner.country)
                await conn.execute(
                    "INSERT INTO connectors (id, family, provider, enabled, state, settings) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET family = "
                    "EXCLUDED.family, "
                    "provider = EXCLUDED.provider",
                    (
                        s.id,
                        s.family,
                        s.provider,
                        on,
                        "needs_owner" if on else "off",
                        Jsonb(rules.defaults(s)),
                    ),
                )
            for s in self.registry:
                await self._refresh(conn, s)

    async def _row(self, conn: AsyncConnection, connector_id: str) -> dict[str, Any]:
        cur = conn.cursor(row_factory=dict_row)
        row = await (await cur.execute("SELECT * FROM connectors WHERE id = %s", (connector_id,))).fetchone()
        if row is None:
            raise _not_found(connector_id)
        return row

    async def _facts(self, conn: AsyncConnection, s: rules.Spec, row: dict[str, Any]) -> rules.Facts:
        google_status = phone_seen = None
        google_enabled = True
        if s.id in ("google", "admin_mail"):
            got = await (
                await conn.execute("SELECT status FROM connector_accounts WHERE connector = 'google'")
            ).fetchone()
            google_status = str(got[0]) if got else "not_connected"
            switch = await (
                await conn.execute("SELECT enabled FROM connectors WHERE id = 'google'")
            ).fetchone()
            google_enabled = switch is None or bool(switch[0])
        if s.id == "apple_health":
            phone_seen = (
                await (
                    await conn.execute("SELECT 1 FROM health_days WHERE source = 'apple_health' LIMIT 1")
                ).fetchone()
            ) is not None
        return rules.Facts(
            enabled=bool(row["enabled"]),
            has_secret=row["secret_enc"] is not None,
            secret_expires_on=row["secret_expires_on"],
            consent_expires_at=row["consent_expires_at"],
            failures=int(row["failures"]),
            google_status=google_status,
            google_enabled=google_enabled,
            phone_seen=bool(phone_seen),
        )

    async def _refresh(self, conn: AsyncConnection, s: rules.Spec) -> tuple[dict[str, Any], rules.Facts]:
        """The row with its state decided now (a consent or a key may have expired since the last look). Off
        means off: a connector that is off (one that serves another country than Pisa's now) is switched off
        too."""
        row = await self._row(conn, s.id)
        facts = await self._facts(conn, s, row)
        state = rules.state_of(s, facts, self._clock(), self._owner.day(self._clock()), self._owner.country)
        if state != row["state"]:
            enabled = bool(row["enabled"]) and state != "off"
            await conn.execute(
                "UPDATE connectors SET state = %s, enabled = %s, updated_at = %s WHERE id = %s",
                (state, enabled, self._clock(), s.id),
            )
            _log.info("connector_state", connector=s.id, state=state)
            row["state"], row["enabled"] = state, enabled
        return row, facts

    async def states(self) -> dict[str, rules.State]:
        """Every connector's state decided now (never a stored state that may be stale)."""
        async with self._pool.connection() as conn:
            return await self._states(conn)

    async def _states(self, conn: AsyncConnection) -> dict[str, rules.State]:
        return {s.id: (await self._refresh(conn, s))[0]["state"] for s in self.registry}

    async def tools_unconnected(self) -> dict[str, str]:
        """The brain tools whose connector they haven't connected: tool -> its name."""
        return rules.tools_unconnected(await self.states())

    async def tools_off(self) -> frozenset[str]:
        """The brain tools no connector that is on feeds (never offered this turn)."""
        return rules.tools_off(await self.states())

    def _out(self, s: rules.Spec, row: dict[str, Any], facts: rules.Facts) -> dict[str, Any]:
        tz = self._owner.zone
        consent = row["consent_expires_at"]
        renew = rules.renew_from(consent)
        return {
            "id": s.id,
            "name": rules.say(s.name, self._owner.tag),
            "family": s.family,
            "provider": s.provider,
            "state": row["state"],
            "enabled": bool(row["enabled"]),
            "missing": rules.missing(s, facts, self._clock(), self._owner.day(self._clock())),
            "message": row["message"] if row["state"] == "error" else None,
            "settings": rules.defaults(s) | dict(row["settings"] or {}),
            "settings_schema": rules.schema(s, self._owner.tag),
            "credential": (
                {
                    "fields": [
                        {
                            "name": f.name,
                            "title": rules.say(f.title, self._owner.tag),
                            "secret": f.secret,
                            "kind": f.kind,
                        }
                        for f in s.credential
                    ],
                    "set": row["secret_enc"] is not None,
                    "set_at": row["secret_set_at"].astimezone(tz).isoformat()
                    if row["secret_set_at"]
                    else None,
                    "expires_on": row["secret_expires_on"].isoformat() if row["secret_expires_on"] else None,
                }
                if s.credential
                else None
            ),
            "needs_consent": s.needs_consent,
            "consent": (
                {
                    "expires_at": consent.astimezone(tz).isoformat(),
                    "renew_from": renew.astimezone(tz).isoformat() if renew else None,
                }
                if consent
                else None
            ),
            "data_out": {k: rules.say(v, self._owner.tag) for k, v in asdict(s.data_out).items()}
            if s.data_out
            else None,
            "data_out_seen": row["data_out_seen_at"] is not None,
            "last_sync": {
                "at": row["last_sync_at"].astimezone(tz).isoformat() if row["last_sync_at"] else None,
                "counts": {k: int(v) for k, v in dict(row["last_sync_counts"] or {}).items()},
            },
            "tools": sorted(s.tools),
            "built": s.built,
        }

    async def all(self) -> list[dict[str, Any]]:
        out = []
        async with self._pool.connection() as conn:
            for s in self.registry:
                row, facts = await self._refresh(conn, s)
                out.append(self._out(s, row, facts))
        return out

    def spec(self, connector_id: str) -> rules.Spec:
        s = rules.spec(connector_id)
        if s is None or s not in self.registry:  # an experimental connector that is off doesn't exist
            raise _not_found(connector_id)
        return s

    async def get(self, connector_id: str) -> dict[str, Any]:
        s = self.spec(connector_id)
        async with self._pool.connection() as conn:
            row, facts = await self._refresh(conn, s)
        return self._out(s, row, facts)

    async def patch(
        self,
        connector_id: str,
        *,
        enabled: bool | None,
        settings: dict[str, Any] | None,
        data_out_seen: bool,
    ) -> dict[str, Any]:
        """Their switch and their settings (the route checked the phone's signature). Switching on a connector
        that sends their data to a provider needs `data_out_seen`: the app showed the provider and what goes
        there."""
        s = self.spec(connector_id)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(conn, s.id)
            if enabled and not s.built:
                raise PisaError(
                    "validation_failed",
                    f"{s.name}'s provider isn't in Pisa yet: the row and its settings are ready, the "
                    "provider is the next one to add.",
                    "Leave it off for now",
                )
            if enabled and not rules.in_reach(s, self._owner.country):
                raise PisaError(
                    "validation_failed",
                    f"{s.name} serves a user in {s.country} only, and Pisa's country (PISA_COUNTRY) is "
                    f"{self._owner.country or 'not set'}.",
                    "Leave it off, or set PISA_COUNTRY on the server",
                )
            if (
                enabled
                and not row["enabled"]
                and s.data_out
                and not (data_out_seen or row["data_out_seen_at"])
            ):
                raise PisaError(
                    "validation_failed",
                    f"Switching {s.name} on sends data to {s.data_out.provider}: the app must show where it "
                    "goes first.",
                    "Show the provider and what goes there, then switch it on with data_out_seen",
                )
            new_settings = dict(row["settings"] or {})
            if settings is not None:
                try:
                    new_settings = rules.validate_settings(s, new_settings, settings)
                except ValueError as err:
                    raise _invalid(f"{s.name}: {err}") from None
            seen_at = row["data_out_seen_at"] or (now if data_out_seen and s.data_out else None)
            on = row["enabled"] if enabled is None else enabled
            await conn.execute(
                "UPDATE connectors SET enabled = %s, settings = %s, data_out_seen_at = %s, updated_at = %s, "
                "state = CASE WHEN %s THEN (CASE WHEN state = 'off' THEN 'needs_owner' ELSE state END) "
                "ELSE 'off' END, failures = CASE WHEN %s THEN failures ELSE 0 END WHERE id = %s",
                (on, Jsonb(new_settings), seen_at, now, on, on, s.id),
            )
            row, facts = await self._refresh(conn, s)
        _log.info("connector_changed", connector=s.id, enabled=bool(on), settings=settings is not None)
        return self._out(s, row, facts)

    # --- credentials (write-only) -------------------------------------------------------------------------

    async def set_credential(self, connector_id: str, fields: dict[str, str]) -> dict[str, Any]:
        s = self.spec(connector_id)
        try:
            sealed, expires = rules.validate_credential(s, fields)
        except ValueError as err:
            raise _invalid(f"{s.name}: {err}") from None
        blob = self._keys.seal(json.dumps(sealed))
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE connectors SET secret_enc = %s, secret_set_at = %s, secret_expires_on = %s, "
                "access_enc = "
                "NULL, access_expires_at = NULL, failures = 0, message = NULL, updated_at = %s WHERE id = %s",
                (blob, self._clock(), expires, self._clock(), s.id),
            )
            row, facts = await self._refresh(conn, s)
        _log.info("connector_credential_set", connector=s.id)
        return self._out(s, row, facts)

    async def delete_credential(self, connector_id: str) -> dict[str, Any]:
        s = self.spec(connector_id)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE connectors SET secret_enc = NULL, secret_set_at = NULL, secret_expires_on = NULL, "
                "access_enc = NULL, access_expires_at = NULL, consent_expires_at = NULL, updated_at = %s "
                "WHERE id = %s",
                (self._clock(), s.id),
            )
            row, facts = await self._refresh(conn, s)
        _log.info("connector_credential_removed", connector=s.id)
        return self._out(s, row, facts)

    def _secret(self, row: dict[str, Any]) -> dict[str, str]:
        if row["secret_enc"] is None:
            raise PisaError(
                "connector_not_configured",
                "The connector's credential is missing.",
                "Enter it in Settings → Connections",
            )
        return dict(json.loads(self._keys.unseal(bytes(row["secret_enc"]))))

    # --- the bank's consent (PSD2) -------------------------------------------------------------------------

    async def consent_start(self, connector_id: str) -> dict[str, Any]:
        s = self.spec(connector_id)
        if not s.needs_consent:
            raise _invalid(f"{s.name} takes no consent")
        async with self._pool.connection() as conn:
            row, _ = await self._refresh(conn, s)
            if not row["enabled"]:
                raise _off(s)
            secret = self._secret(row)
            settings = rules.defaults(s) | dict(row["settings"] or {})
            # the bank's country: theirs (PISA_COUNTRY) when Settings names none
            settings["aspsp_country"] = settings.get("aspsp_country") or self._owner.country
            state = token_maker.token_urlsafe(18)
            await set_setting(conn, CONSENT_STATE + s.id, Jsonb({"state": state}))
        valid_until = self._clock() + timedelta(days=int(settings["consent_days"]))
        try:
            started = await self.bank.start(secret, settings, valid_until, state)
        except SourceError as err:
            raise PisaError(
                "connector_auth_failed" if err.auth else "provider_error",
                err.message,
                "Check the bank's settings in Settings → Connections",
            ) from None
        _log.info("connector_consent_started", connector=s.id)
        return {"url": started["url"], "state": state}

    async def consent_code(self, connector_id: str, code: str, state: str) -> dict[str, Any]:
        s = self.spec(connector_id)
        async with self._pool.connection() as conn:
            row, _ = await self._refresh(conn, s)
            if not row["enabled"]:
                raise _off(s)
            expected = await get_setting(conn, CONSENT_STATE + s.id)
            if not isinstance(expected, dict) or expected.get("state") != state:
                raise _invalid(
                    "This consent wasn't started by Pisa (or was started again): start it once more"
                )
            secret = self._secret(row)
            settings = rules.defaults(s) | dict(row["settings"] or {})
        try:
            session = await self.bank.session(secret, code)
        except SourceError as err:
            raise PisaError(
                "connector_auth_failed" if err.auth else "provider_error",
                err.message,
                "Start the bank's consent again in Settings → Connections",
            ) from None
        now = self._clock()
        until = session.get("valid_until") or now + timedelta(days=int(settings["consent_days"]))
        async with self._pool.connection() as conn, conn.transaction():
            await delete_setting(conn, CONSENT_STATE + s.id)
            await conn.execute(
                "UPDATE connectors SET access_enc = %s, consent_expires_at = %s, failures = 0, message = "
                "NULL, "
                "updated_at = %s WHERE id = %s",
                (self._keys.seal(json.dumps({"session_id": session["session_id"]})), until, now, s.id),
            )
            for a in session["accounts"]:
                await conn.execute(
                    "INSERT INTO bank_accounts (connector_id, uid, name, iban_last4, currency, seen_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (connector_id, uid) DO UPDATE SET name = "
                    "EXCLUDED.name, iban_last4 = EXCLUDED.iban_last4, currency = EXCLUDED.currency, seen_at "
                    "= "
                    "EXCLUDED.seen_at",
                    (
                        s.id,
                        a["uid"],
                        (a.get("name") or None) and str(a["name"])[:120],
                        a.get("iban_last4"),
                        a.get("currency"),
                        now,
                    ),
                )
            row, facts = await self._refresh(conn, s)
        _log.info("connector_consent_given", connector=s.id, accounts=len(session["accounts"]))
        return self._out(s, row, facts)

    # --- the phone's health summary ------------------------------------------------------------------------

    async def health_summary_in(self, summary: health_rules.DaySummary) -> dict[str, Any]:
        """POST /v1/health/summary: one day of their derived numbers from the phone (Apple Health)."""
        s = self.spec("apple_health")
        today = self._owner.day(self._clock())
        if summary.day > today or summary.day < today - timedelta(days=rules.HEALTH_MAX_DAYS):
            raise _invalid(f"day must be within the last {rules.HEALTH_MAX_DAYS} days, not in the future")
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(conn, s.id)
            if not row["enabled"]:
                raise _off(s)
            settings = rules.defaults(s) | dict(row["settings"] or {})
            await self._store_day(conn, summary, "apple_health", settings)
            await conn.execute(
                "UPDATE connectors SET last_sync_at = %s, last_sync_counts = %s, failures = 0, message = "
                "NULL "
                "WHERE id = %s",
                (self._clock(), Jsonb({"days": 1}), s.id),
            )
            row, _ = await self._refresh(conn, s)
        _log.info("health_summary_received", connector=s.id)
        return {"day": summary.day.isoformat(), "state": row["state"]}

    async def _store_day(
        self, conn: AsyncConnection, summary: health_rules.DaySummary, source: str, areas: dict[str, Any]
    ) -> None:
        values: dict[str, Any] = {
            "sleep_min": summary.sleep.asleep_min if summary.sleep else None,
            "in_bed_min": summary.sleep.in_bed_min if summary.sleep else None,
            "bedtime": summary.sleep.bedtime if summary.sleep else None,
            "wake_time": summary.sleep.wake if summary.sleep else None,
            "steps": summary.steps,
            "active_kcal": summary.active_kcal,
            "exercise_min": summary.exercise_min,
            "resting_hr": summary.resting_hr,
            "hrv_sdnn_ms": summary.hrv_sdnn_ms,
            "vo2max": round(summary.vo2max, 1) if summary.vo2max is not None else None,
            "workouts": [w.model_dump(exclude_none=True) for w in summary.workouts],
            "doses_taken": summary.doses.taken if summary.doses else None,
            "doses_missed": summary.doses.missed if summary.doses else None,
        }
        for area, columns in AREAS.items():  # an area they switched off: nothing of it is kept
            if not areas.get(area, True):
                for c in columns:
                    values[c] = [] if c == "workouts" else None
        values["workouts"] = Jsonb(values["workouts"])
        query = sql.SQL(
            "INSERT INTO health_days (day, source, {cols}, received_at) VALUES (%s, %s, {marks}, %s) "
            "ON CONFLICT (day, source) DO UPDATE SET {updates}, received_at = EXCLUDED.received_at"
        ).format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in HEALTH_COLUMNS),
            marks=sql.SQL(", ").join(sql.Placeholder() for _ in HEALTH_COLUMNS),
            updates=sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in HEALTH_COLUMNS
            ),
        )
        await conn.execute(query, (summary.day, source, *(values[c] for c in HEALTH_COLUMNS), self._clock()))

    # --- the syncs (the minute's `connectors` step) --------------------------------------------------------

    async def tick(self) -> dict[str, int]:
        """Every minute (Pisa's initiative group): each connector that is on, when its sync is due; then the
        PSD2 consents' renewals and the radar's reminders. Off: nothing at all."""
        done: dict[str, int] = {}
        async with self._pool.connection() as conn:
            rows = {s.id: (await self._refresh(conn, s))[0] for s in self.registry}
        now = self._clock()
        due: list[tuple[str, Callable[[dict[str, Any]], Awaitable[dict[str, int]]], timedelta]] = [
            ("garmin", self._sync_sport, timedelta(hours=20)),
            *((b, self._sync_bank, BANK_EVERY) for b in rules.BANKS),
            ("admin_mail", self._sync_mail, timedelta(0)),
            ("admin_pec", self._sync_pec, HOURLY),
            ("admin_documents", self._sync_documents, timedelta(0)),
            ("admin_calendar", self._sync_calendar, HOURLY),
            ("ade_cassetto", self._sync_ade, timedelta(hours=20)),
        ]
        for connector_id, sync, every in due:
            if connector_id not in rows:  # absent (an experimental connector that is off)
                continue
            row = rows[connector_id]
            if row["state"] not in ("on", "error"):  # off, or something of theirs is missing: no call
                continue
            if row["last_sync_at"] is not None and now - row["last_sync_at"] < every:
                continue
            await self._run(connector_id, sync, row)
            done[connector_id] = 1
        for name, step in (("consents", self._consents), ("reminders", self._remind)):
            try:
                done[name] = await step()
            except Exception as err:  # the error's class only, never its text
                _log.warning("connector_step_failed", step=name, error=type(err).__name__)
        return done

    async def _run(
        self,
        connector_id: str,
        sync: Callable[[dict[str, Any]], Awaitable[dict[str, int]]],
        row: dict[str, Any],
    ) -> None:
        s = self.spec(connector_id)
        try:
            counts = await sync(row)
        except (SourceError, PisaError) as err:
            message = err.message[:300]
            auth = isinstance(err, SourceError) and err.auth
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE connectors SET failures = CASE WHEN %s THEN greatest(failures + 1, %s) "
                    "ELSE failures + 1 END, message = %s, last_sync_at = %s WHERE id = %s",
                    (auth, rules.FAILURES_BEFORE_ERROR, message, self._clock(), s.id),
                )
                await self._refresh(conn, s)
            _log.warning("connector_sync_failed", connector=s.id, auth=auth)
            return
        except Exception as err:  # one connector's bug never stops the others
            await self._failed(s, err)
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE connectors SET failures = 0, message = NULL, last_sync_at = %s, last_sync_counts = "
                "%s "
                "WHERE id = %s",
                (self._clock(), Jsonb(counts), s.id),
            )
            await self._refresh(conn, s)
        _log.info("connector_synced", connector=s.id, **counts)

    async def _failed(self, s: rules.Spec, err: Exception) -> None:
        """An unexpected failure: counted like a failing sync; the log and the message name its class only
        (a database error's text can carry a row of theirs)."""
        kind = type(err).__name__
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE connectors SET failures = failures + 1, message = %s, last_sync_at = %s "
                "WHERE id = %s",
                (f"{s.name}: the sync failed ({kind}); Pisa tries again later", self._clock(), s.id),
            )
            await self._refresh(conn, s)
        _log.warning("connector_sync_failed", connector=s.id, error=kind)

    async def _sync_sport(self, row: dict[str, Any]) -> dict[str, int]:
        provider = self.sport[row["id"]]
        secret = self._secret(row)
        now = self._clock()
        token: str | None = None
        if row["access_enc"] is not None and row["access_expires_at"] and row["access_expires_at"] > now:
            token = self._keys.unseal(bytes(row["access_enc"]))
        if token is None:  # the Personal API's token lasts 24 h: one a day
            token, until = await provider.token(secret)
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE connectors SET access_enc = %s, access_expires_at = %s WHERE id = %s",
                    (self._keys.seal(token), until, row["id"]),
                )
        today = self._owner.day(self._clock())
        days = await provider.days(token, today - timedelta(days=3), today + timedelta(days=1))
        kept = 0
        settings = dict(row["settings"] or {})
        areas = {
            "sleep": settings.get("sleep", True),
            "activity": settings.get("daily", True),
            "heart": settings.get("daily", True),
            "workouts": settings.get("activities", True),
            "medications": False,
        }
        async with self._pool.connection() as conn:
            for raw in days:
                try:
                    summary = health_rules.DaySummary.model_validate(raw)
                except ValueError:
                    continue  # a provider's day Pisa can't read as derived numbers: never kept half
                await self._store_day(conn, summary, "garmin", areas)
                kept += 1
        return {"days": kept}

    async def _sync_bank(self, row: dict[str, Any]) -> dict[str, int]:
        secret = self._secret(row)
        if row["access_enc"] is None:
            raise PisaError(
                "connector_not_configured",
                "The bank's consent is missing.",
                "Give the consent in Settings → Connections → Bank",
            )
        settings = dict(row["settings"] or {})
        today = self._owner.day(self._clock())
        async with self._pool.connection() as conn:
            accounts = await (
                await conn.execute("SELECT uid FROM bank_accounts WHERE connector_id = %s", (row["id"],))
            ).fetchall()
            latest = await (
                await conn.execute(
                    "SELECT max(booked_on), min(booked_on) FILTER (WHERE pending) FROM bank_transactions "
                    "WHERE connector_id = %s",
                    (row["id"],),
                )
            ).fetchone()
        oldest = today - timedelta(days=int(settings.get("history_days", 90)))
        since = oldest
        if latest and latest[0]:
            since = max(oldest, latest[0] - timedelta(days=3))
        if latest and latest[1]:
            # back to the oldest pending movement still kept (a hotel's or
            # a car rental's hold lasts days), so its booking under another id replaces it, never adds to it
            since = max(oldest, min(since, latest[1]))
        made = gone = 0
        for (uid,) in accounts:
            moves = await self.bank.transactions(secret, str(uid), since)
            async with self._pool.connection() as conn, conn.transaction():
                # a pending movement the bank no longer returns for the window read was
                # cancelled, or booked under another id: it goes (never counted twice)
                dropped = await conn.execute(
                    "DELETE FROM bank_transactions WHERE connector_id = %s AND account_uid = %s AND pending "
                    "AND booked_on >= %s AND NOT (provider_ref = ANY(%s))",
                    (row["id"], str(uid), since, [m["provider_ref"] for m in moves]),
                )
                gone += dropped.rowcount
                for m in moves:
                    got = await conn.execute(
                        "INSERT INTO bank_transactions (id, connector_id, account_uid, provider_ref, "
                        "booked_on, "
                        "amount, currency, direction, counterparty, description, pending, seen_at) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (connector_id, "
                        "provider_ref) "
                        "DO UPDATE SET pending = EXCLUDED.pending, booked_on = EXCLUDED.booked_on",
                        (
                            uuid.uuid4(),
                            row["id"],
                            str(uid),
                            m["provider_ref"],
                            m["booked_on"],
                            m["amount"],
                            m["currency"],
                            m["direction"],
                            m.get("counterparty"),
                            m.get("description"),
                            bool(m.get("pending")),
                            self._clock(),
                        ),
                    )
                    made += got.rowcount
        return {"accounts": len(accounts), "transactions": made, "dropped": gone}

    async def _keep(self, conn: AsyncConnection, o: radar.Obligation, *, update: bool = False) -> int:
        """One obligation, once per (source, ref). A vault document's dates follow the document."""
        now = self._clock()
        got = await conn.execute(
            "INSERT INTO obligations (id, kind, what, subject, due_on, source, source_ref, rule_source_url, "
            "penalty, channel, amount_text, state, flag_reason, remind_on, last_verified, created_at, "
            "updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            + (KEEP_UPDATE if update else KEEP_ONCE),
            (
                uuid.uuid4(),
                o.kind,
                o.what[:200],
                o.subject,
                o.due_on,
                o.source,
                o.source_ref[:200],
                o.rule_source_url,
                o.penalty,
                o.channel,
                o.amount_text,
                o.state,
                o.flag_reason,
                o.remind_on,
                o.last_verified,
                now,
                now,
            ),
        )
        return got.rowcount

    async def _classify(
        self, mails: list[radar.Mail], source: radar.Source, wanted: dict[str, Any]
    ) -> dict[str, int]:
        kept = flagged = failed = 0
        async with self._pool.connection() as conn:
            for mail in mails:
                try:
                    o = radar.classify(mail, source, wanted)
                    if o is None:
                        continue
                    async with conn.transaction():  # one message that can't be kept is skipped
                        if await self._keep(conn, o):
                            flagged += o.state == "flagged"
                            kept += o.state != "flagged"
                except Exception:
                    failed += 1
        if failed:
            _log.warning("radar_messages_skipped", count=failed)
        return {"read": len(mails), "obligations": kept, "flagged": flagged, "skipped": failed}

    async def _sync_mail(self, row: dict[str, Any]) -> dict[str, int]:
        """The existing mail (Google's copy, no new scope), classified for the radar. Each email
        is marked read by the radar (`radar_read_at`), never a cursor on its Date header: an office's mail
        that arrives late is still read. The last 60 days only (older mail was never the radar's)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, coalesce(from_address, ''), subject, coalesce(left(body, 4000), snippet), "
                    "sent_at FROM email_messages WHERE radar_read_at IS NULL AND sent_at > %s "
                    "ORDER BY sent_at, id LIMIT %s",
                    (self._clock() - RADAR_DAYS, MAIL_BATCH),
                )
            ).fetchall()
        mails = [radar.Mail(f"gmail:{r[0]}", r[1], r[2], r[3] or "", r[4]) for r in rows]
        counts = await self._classify(mails, "mail", dict(row["settings"] or {}))
        if rows:  # read, kept or skipped: never read again
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE email_messages SET radar_read_at = %s WHERE id = ANY(%s)",
                    (self._clock(), [r[0] for r in rows]),
                )
        return counts

    async def _sync_pec(self, row: dict[str, Any]) -> dict[str, int]:
        settings = rules.defaults(self.spec("admin_pec")) | dict(row["settings"] or {})
        since = self._owner.day(self._clock()) - timedelta(days=int(settings["days"]))
        got = await self.mailbox.since(self._secret(row), settings, since)
        mails = [
            radar.Mail(f"pec:{m['ref']}", m["from_address"], m["subject"], m["text"], m["sent_at"])
            for m in got
        ]
        return await self._classify(mails, "pec", {})

    async def _sync_documents(self, row: dict[str, Any]) -> dict[str, int]:
        async with self._pool.connection() as conn:
            docs = await (
                await conn.execute("SELECT id::text, kind, expires_on FROM vault_documents")
            ).fetchall()
            made = 0
            for doc_id, kind, expires_on in docs:
                made += await self._keep(conn, radar.from_document(doc_id, kind, expires_on), update=True)
            await conn.execute(  # a document they deleted: its line is closed, never deleted
                "UPDATE obligations SET state = 'done', updated_at = %s WHERE source = 'vault' AND state <> "
                "'done' "
                "AND NOT EXISTS (SELECT 1 FROM vault_documents v WHERE v.id::text = obligations.source_ref)",
                (self._clock(),),
            )
        return {"documents": len(docs), "obligations": made}

    async def _sync_calendar(self, row: dict[str, Any]) -> dict[str, int]:
        settings = rules.defaults(self.spec("admin_calendar")) | dict(row["settings"] or {})
        parts = {
            "income_tax": settings["income_tax"],
            "property": settings["property"],
            "isee": settings["isee"],
            "condominium": settings["condominium"],
        }
        seeds = radar.calendar(
            parts, self._owner.day(self._clock()), condominium_year_end=settings["condominium_year_end"]
        )
        made = 0
        async with self._pool.connection() as conn:
            held = {
                str(r[0]) for r in await (await conn.execute("SELECT kind FROM vault_documents")).fetchall()
            }
            if settings["car"]:
                seeds += radar.missing_car_documents(held)
            for o in seeds:
                made += await self._keep(conn, o)
                if o.kind == "missing_document":
                    # a line closed earlier (car off, or the document since
                    # removed from the vault) opens again while the date is missing
                    await conn.execute(
                        "UPDATE obligations SET state = %s, updated_at = %s WHERE source = %s "
                        "AND source_ref = %s AND kind = 'missing_document' AND state = 'done'",
                        (o.state, self._clock(), o.source, o.source_ref[:200]),
                    )
            # a "missing date" line closes once the vault has that kind, or their list has no car
            await conn.execute(
                "UPDATE obligations SET state = 'done', updated_at = %s WHERE source = 'calendar' "
                "AND kind = 'missing_document' AND state <> 'done' AND (NOT %s OR subject = ANY(%s))",
                (self._clock(), bool(settings["car"]), sorted(held)),
            )
        return {"obligations": made}

    async def _sync_ade(self, row: dict[str, Any]) -> dict[str, int]:
        settings = rules.defaults(self.spec("ade_cassetto")) | dict(row["settings"] or {})
        found = await self.tax.documents(self._secret(row), int(settings["years"]))
        return {"documents": len(found)}

    async def _consents(self) -> int:
        """PSD2: each bank's consent is an obligation; its reminder comes 7 days before it ends."""
        made = 0
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, consent_expires_at FROM connectors WHERE id = ANY(%s) AND enabled "
                    "AND consent_expires_at IS NOT NULL",
                    ([b for b in rules.BANKS if any(s.id == b for s in self.registry)],),
                )
            ).fetchall()
            for connector_id, until in rows:
                renew = rules.renew_from(until)
                if renew is None:
                    continue
                o = radar.psd2_consent(
                    connector_id, rules.say(self.spec(connector_id).name, "it"), until, renew
                )
                made += await self._keep(conn, o)
                await conn.execute(  # a renewed consent closes the older consent's line
                    "UPDATE obligations SET state = 'done', updated_at = %s WHERE source = 'bank' "
                    "AND source_ref LIKE %s AND source_ref <> %s AND state <> 'done'",
                    (self._clock(), f"consent:{connector_id}:%", o.source_ref),
                )
        return made

    async def _remind(self) -> int:
        """An obligation whose first nudge is due: one task in the pool, once (code's own title). Flagged
        lines, done ones and the vault's (its own reminders) never make one."""
        today = self._owner.day(self._clock())
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.obligations'))")
            cur = conn.cursor(row_factory=dict_row)
            rows = await (
                await cur.execute(
                    "SELECT o.* FROM obligations o WHERE o.remind_on IS NOT NULL AND o.remind_on <= %s "
                    "AND o.task_id IS NULL AND o.state IN ('upcoming', 'ready') AND o.source <> 'vault' "
                    "AND (o.due_on IS NULL OR o.due_on >= %s) ORDER BY o.remind_on LIMIT 20",
                    (today, today - timedelta(days=1)),
                )
            ).fetchall()
            states = await self._states(conn)
            lang = await self._owner.lang(conn)
            for r in rows:
                if not self._source_on(r, states):
                    continue
                o = radar.Obligation(r["kind"], r["what"], r["due_on"], r["source"], r["source_ref"])
                task = await insert_task(
                    conn,
                    title=radar.task_title(o, lang),
                    now=self._clock(),
                    source_kind="manual",
                    notes=r["channel"],
                    due_at=end_of_day(r["due_on"], self._owner.tz) if r["due_on"] else None,
                    due_kind="date" if r["due_on"] else "none",
                    priority=1 if r["kind"] in ("send_notice", "psd2_consent") else 2,
                    source_ref=f"obligation:{r['id']}",
                    source_label="radar",
                    deadline_source="owner",
                    priority_reason=None,
                )
                await conn.execute(
                    "UPDATE obligations SET task_id = %s, remind_on = CASE WHEN %s THEN remind_on ELSE NULL "
                    "END, "
                    "updated_at = %s WHERE id = %s",
                    (task.id if task else None, task is not None, self._clock(), r["id"]),
                )
                made += task is not None
        if made:
            _log.info("obligation_reminders", made=made)
        return made

    @staticmethod
    def _source_on(r: dict[str, Any], states: Mapping[str, str]) -> bool:
        owner = {
            "mail": "admin_mail",
            "pec": "admin_pec",
            "vault": "admin_documents",
            "calendar": "admin_calendar",
            "ade": "ade_cassetto",
        }.get(r["source"])
        if r["source"] == "bank":
            owner = str(r["source_ref"]).split(":")[1]
        return owner is not None and states.get(owner) in ("on", "error")

    # --- the brain's and the night's readers ----------------------------------------------------------------

    async def health_summary(self, days: int, lang: str) -> dict[str, Any]:
        days = max(1, min(int(days), rules.HEALTH_MAX_DAYS))
        today = self._owner.day(self._clock())
        start = today - timedelta(days=days - 1)
        async with self._pool.connection() as conn:
            states = await self._states(conn)
            on = [s for s in rules.HEALTH_SOURCES if states.get(s, "off") != "off"]
            cur = conn.cursor(row_factory=dict_row)
            rows = await (
                await cur.execute(
                    "SELECT * FROM health_days WHERE source = ANY(%s) AND day >= %s AND day <= %s "
                    "ORDER BY day DESC, source",
                    (on, start - timedelta(days=28), today),
                )
            ).fetchall()

        def day(r: dict[str, Any]) -> health_rules.Day:
            return health_rules.Day(
                r["day"],
                r["source"],
                r["sleep_min"],
                r["bedtime"],
                r["wake_time"],
                r["steps"],
                r["exercise_min"],
                r["resting_hr"],
                r["hrv_sdnn_ms"],
                float(r["vo2max"]) if r["vo2max"] is not None else None,
                list(r["workouts"] or []),
                r["doses_taken"],
                r["doses_missed"],
            )

        asked = [day(r) for r in rows if r["day"] >= start]
        earlier = [day(r) for r in rows if r["day"] < start]
        lines = health_rules.describe(asked, earlier, lang)
        return {"sources": on, "days": len({d.day for d in asked}), "lines": lines}

    async def money_recent(self, days: int, lang: str) -> dict[str, Any]:
        days = max(1, min(int(days), rules.MONEY_MAX_DAYS))
        since = self._owner.day(self._clock()) - timedelta(days=days - 1)
        async with self._pool.connection() as conn:
            states = await self._states(conn)
            on = [b for b in rules.BANKS if states.get(b, "off") != "off"]
            totals = await (
                await conn.execute(
                    "SELECT currency, direction, sum(amount), count(*) FROM bank_transactions WHERE "
                    "connector_id = "
                    "ANY(%s) AND booked_on >= %s GROUP BY currency, direction ORDER BY currency, direction",
                    (on, since),
                )
            ).fetchall()
            latest = await (
                await conn.execute(
                    "SELECT booked_on, direction, amount, currency, coalesce(counterparty, description, "
                    "'—'), pending "
                    "FROM bank_transactions WHERE connector_id = ANY(%s) AND booked_on >= %s "
                    "ORDER BY booked_on DESC, seen_at DESC LIMIT 12",
                    (on, since),
                )
            ).fetchall()
        return {
            "banks": on,
            "totals": [
                {"currency": c, "direction": d, "amount": f"{a:.2f}", "count": int(n)}
                for c, d, a, n in totals
            ],
            "moves": [
                {
                    "day": b.isoformat(),
                    "direction": d,
                    "amount": f"{a:.2f}",
                    "currency": c,
                    "party": str(p)[:80],
                    "pending": bool(pend),
                }
                for b, d, a, c, p, pend in latest
            ],
        }

    async def obligations(self, window_days: int, lang: str) -> dict[str, Any]:
        window = max(1, min(int(window_days), rules.OBLIGATIONS_MAX_DAYS))
        today = self._owner.day(self._clock())
        async with self._pool.connection() as conn:
            states = await self._states(conn)
            calendar = rules.defaults(rules.BY_ID["admin_calendar"]) | dict(
                (await self._row(conn, "admin_calendar"))["settings"] or {}
            )
            cur = conn.cursor(row_factory=dict_row)
            rows = await (
                await cur.execute(
                    "SELECT * FROM obligations WHERE state <> 'done' AND (due_on IS NULL OR "
                    "(due_on >= %s AND due_on <= %s)) ORDER BY due_on NULLS LAST, kind LIMIT 200",
                    (today - timedelta(days=60), today + timedelta(days=window)),
                )
            ).fetchall()
        items: list[dict[str, Any]] = []
        flagged = 0
        for r in rows:
            if not self._source_on(r, states):
                continue
            part = _PART_OF.get(r["kind"])
            if r["source"] == "calendar" and part is not None and not calendar.get(part, False):
                continue  # a part of the calendar they switched off in their list
            if r["state"] == "flagged":
                if r["created_at"] >= self._clock() - timedelta(days=window):
                    flagged += 1
                continue
            o = radar.Obligation(
                r["kind"], r["what"], r["due_on"], r["source"], r["source_ref"], state=r["state"]
            )
            items.append(
                {
                    "id": str(r["id"]),
                    "kind": r["kind"],
                    "what": r["what"],
                    "due_on": r["due_on"].isoformat() if r["due_on"] else None,
                    "state": radar.state_on(o, today),
                    "source": r["source"],
                    "penalty": r["penalty"],
                    "channel": r["channel"],
                    "amount_as_printed": r["amount_text"],
                    "rule_source_url": r["rule_source_url"],
                    "last_verified": r["last_verified"].isoformat() if r["last_verified"] else None,
                }
            )
        return {"window_days": window, "items": items, "flagged": flagged}

    async def flagged(self, limit: int = 20) -> list[dict[str, Any]]:
        """The phishing-like messages, newest first, of the sources that are on."""
        async with self._pool.connection() as conn:
            states = await self._states(conn)
            cur = conn.cursor(row_factory=dict_row)
            rows = await (
                await cur.execute(
                    "SELECT id, source, source_ref, flag_reason, created_at FROM obligations "
                    "WHERE state = 'flagged' ORDER BY created_at DESC LIMIT %s",
                    (limit * 4,),
                )
            ).fetchall()
        tz = self._owner.zone
        return [
            {
                "id": str(r["id"]),
                "source": r["source"],
                "why": r["flag_reason"],
                "at": r["created_at"].astimezone(tz).isoformat(),
            }
            for r in rows
            if self._source_on(r, states)
        ][:limit]

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        """The night reads the same three readers (shadow: counts into its own record, nothing to them)."""

        async def read(_night: date) -> int:
            lang = self._owner.tag
            got = 0
            if (await self.health_summary(1, lang))["lines"]:
                got += 1
            if (await self.money_recent(1, lang))["moves"]:
                got += 1
            if (await self.obligations(30, lang))["items"]:
                got += 1
            return got

        return {"n10_sources": read}
