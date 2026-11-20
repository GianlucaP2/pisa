"""`pisa` command line: the entry point for every process (api, worker, migrate) and admin commands."""

import argparse
import asyncio
import getpass
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from pydantic import ValidationError

from pisa.domain.errors import PisaError
from pisa.logging import configure_logging, get_logger
from pisa.settings import Settings, get_settings


def _cmd_doctor(args: argparse.Namespace) -> int:
    """`pisa doctor`: what in this server's configuration weakens it, each with what to do. Offline: it reads
    the settings only. 0 when nothing is found, 1 with a warning per finding. `--live`: also opens a real
    session with natural mode's talker (`cli_doctor`), which spends a little."""
    from pisa.domain import guide
    from pisa.infra.keys import KeyStore

    settings = get_settings()
    findings = []
    if not KeyStore(settings.secrets_key).can_store:  # pairing tokens are then kept in plaintext
        findings.append("plaintext_pairing_tokens")
    if settings.insecure_loopback_pairing:  # plain http, this machine only
        findings.append("insecure_loopback_pairing")
    elif not _pairing_possible(settings):  # nothing can pair, so a new server can't be used
        findings.append("pairing_off")
    for key in findings:
        print(f"warning: {guide.doctor_text(key, settings.language)}", file=sys.stderr)
    live_ok = True
    if getattr(args, "live", False):
        from pisa.cli_doctor import live_lines

        try:
            live_ok, line = asyncio.run(live_lines(settings, settings.language))
        except psycopg.OperationalError as exc:
            return _fail(_unreachable(exc))
        print(line if live_ok else f"warning: {line}", file=sys.stdout if live_ok else sys.stderr)
    if not findings and live_ok:
        print(guide.doctor_text("ok", settings.language))
    return 1 if findings or not live_ok else 0


def _pairing_possible(settings: Settings) -> bool:
    from pisa.services import endpoints

    try:
        return bool(endpoints.pins(settings) and settings.address_list)
    except PisaError:
        return False


def _cmd_migrate(_: argparse.Namespace) -> int:
    from pisa.db.migrate import MigrationError, migrate

    settings = get_settings()
    url = settings.migrate_database_url
    if not url:
        print("PISA_MIGRATE_DATABASE_URL is not set (see .env.example)", file=sys.stderr)
        return 2
    import psycopg

    try:
        applied = migrate(url)
    except psycopg.OperationalError as exc:
        return _fail(_unreachable(exc))
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    get_logger("pisa.migrate").info("migrations_applied", count=len(applied), names=applied)
    return 0


def api_tls(settings: Settings) -> tuple[str, str] | None:
    """The certificate and key files from PISA_API_TLS_CERT and PISA_API_TLS_KEY; None when both are empty."""
    cert, key = settings.api_tls_cert.strip(), settings.api_tls_key.strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise PisaError(
            "validation_failed",
            "PISA_API_TLS_CERT and PISA_API_TLS_KEY go together: one of them is empty.",
            "Set both (the certificate's and its key's files), or neither for plain http",
        )
    for name, path in (("PISA_API_TLS_CERT", cert), ("PISA_API_TLS_KEY", key)):
        if not Path(path).is_file():
            raise PisaError(
                "validation_failed",
                f"{name} names {path}, which is not a readable file.",
                "With docker compose's local mode, run `pisa init --mode local --force` to make the "
                "certificate and key in deploy/tls/",
            )
    return cert, key


def _cmd_serve_api(_: argparse.Namespace) -> int:
    import uvicorn

    from pisa.api.app import create_app

    settings = get_settings()
    try:
        tls = api_tls(settings)
    except PisaError as err:
        return _fail(err)
    uvicorn.run(
        create_app(settings), host=settings.api_host, port=settings.api_port, log_config=None,
        ssl_certfile=tls[0] if tls else None, ssl_keyfile=tls[1] if tls else None,
    )  # fmt: skip
    return 0


def _cmd_worker(_: argparse.Namespace) -> int:
    from pisa.workers.main import run_worker

    return run_worker(get_settings())


def _cmd_voice_relay(args: argparse.Namespace) -> int:
    from pisa.voice_relay import main as relay  # no settings: it reads only its two variables

    return relay(check=args.check)


def _with_db(fn: Callable[[Any, Settings], Awaitable[int]]) -> int:
    settings = get_settings()

    async def go() -> int:
        async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
            return await fn(conn, settings)

    try:
        return asyncio.run(go())
    except PisaError as err:
        return _fail(err)
    except psycopg.OperationalError as exc:
        return _fail(_unreachable(exc))


def _unreachable(exc: Exception) -> PisaError:
    """A failed database connection, said by its kind: a refused password is not a network that can't reach
    Postgres. The driver's own text names hosts and users, never anything the user said."""
    state = getattr(exc, "sqlstate", None) or ""
    text = str(exc).lower()
    if state.startswith("28") or "password authentication failed" in text or "no pg_hba.conf entry" in text:
        return PisaError(
            "internal",
            "The database refused the login (wrong password or user).",
            "Check that the database passwords in .env match the ones the database was created with",
        )
    if "does not exist" in text and "database" in text:
        return PisaError(
            "internal",
            "The database Pisa expects does not exist.",
            "Check PISA_DATABASE_URL, or that the postgres service finished its first start",
        )
    if any(
        m in text
        for m in (
            "connection refused",
            "could not translate host name",
            "name or service not known",
            "timeout",
            "timed out",
            "no route to host",
            "network is unreachable",
        )
    ):
        return PisaError(
            "internal",
            "The database is unreachable (no answer from it on the network).",
            "Check that the postgres service runs and that this service is on the same Docker network: "
            "`docker compose ... down` then `up -d` recreates the network (your data stays)",
        )
    return PisaError(
        "internal",
        f"The database is unreachable ({exc.__class__.__name__}).",
        "Check that Postgres runs and that PISA_DATABASE_URL is right (see .env.example)",
    )


def _fail(err: PisaError) -> int:
    print(f"error: {err.message}" + (f" {err.action}." if err.action else ""), file=sys.stderr)
    return 1


def _cmd_devices_list(_: argparse.Namespace) -> int:
    from pisa.services.devices import list_devices

    async def go(conn: Any, settings: Settings) -> int:
        devices = await list_devices(conn)
        if not devices:
            print("No paired devices.")
        for d in devices:
            state = f"revoked {d.revoked_at:%Y-%m-%d %H:%M}" if d.revoked_at else "active"
            seen = f"{d.last_seen_at:%Y-%m-%d %H:%M}" if d.last_seen_at else "never"
            print(f"{d.id}  {d.name}  {d.platform}  {state}  last seen {seen} UTC")
        return 0

    return _with_db(go)


def _relay(settings: Settings, http: Any) -> Any:
    """The push relay's client for `devices.end`, or None when PISA_PUSH_RELAY_URL is not set."""
    from pisa.connectors.push_relay import RelayClient

    return RelayClient(http, settings.push_relay_url) if settings.push_relay_url else None


def _cmd_devices_revoke(args: argparse.Namespace) -> int:
    from pisa.services.devices import end

    try:
        device_id = uuid.UUID(args.device_id)
    except ValueError:
        print(f"error: {args.device_id!r} is not a device id (see `pisa devices list`)", file=sys.stderr)
        return 2

    async def go(conn: Any, settings: Settings) -> int:
        from pisa.infra.keys import KeyStore

        async with _http_client() as http:  # revoked, then the push relay forgets its handle (best effort)
            await end(conn, [device_id], keys=KeyStore(settings.secrets_key), relay=_relay(settings, http))
        print(f"Device {device_id} revoked: it must pair again to connect.")
        return 0

    return _with_db(go)


def _cmd_phone_keys_reset(args: argparse.Namespace) -> int:
    """re-pairing the iPhone revokes the device its Face ID key lives on, so that key signs
    nothing and a new one can't join (a later key needs a live signer). On the host only (like pairing): the
    phone keys and their open challenges go; the iPhone registers its key again as the first one.

    `--keep <device_id>` is required and every other device is revoked, so the first key goes
    to the device the user named, never to whoever asks first (a copied token's device would). The push relay
    is then asked to forget each revoked device's handle, as `pisa devices revoke` does (best effort)."""
    try:
        keep = uuid.UUID(args.keep)
    except ValueError:
        print(f"error: {args.keep!r} is not a device id (see `pisa devices list`)", file=sys.stderr)
        return 2
    from pisa.services.devices import end

    async def go(conn: Any, settings: Settings) -> int:
        from pisa.infra.keys import KeyStore

        row = await (
            await conn.execute(
                "SELECT name, platform, paired_via, revoked_at FROM devices WHERE id = %s", (keep,)
            )
        ).fetchone()
        if row is None or row[3] is not None:
            print(f"error: {keep} is not a paired device (see `pisa devices list`)", file=sys.stderr)
            return 2
        if row[1] != "ios" or row[2] != "host":
            print(
                f"error: {keep} ({row[0]!r}, {row[1]}) is not an iPhone paired on this server: "
                "pair the iPhone with `pisa pair` first",
                file=sys.stderr,
            )
            return 2
        keys: list[Any] = []

        async def reset(conn: Any) -> list[uuid.UUID]:
            """In the revocation's own transaction: the kept iPhone is still paired, every other device is
            revoked, and the phone keys and their open challenges go."""
            kept = await (
                await conn.execute("SELECT revoked_at FROM devices WHERE id = %s", (keep,))
            ).fetchone()
            if kept is None or kept[0] is not None:
                raise PisaError("not_found", f"{keep} is not a paired device.", "See `pisa devices list`")
            others = await (
                await conn.execute("SELECT id FROM devices WHERE revoked_at IS NULL AND id <> %s", (keep,))
            ).fetchall()
            keys.extend(await (await conn.execute("DELETE FROM phone_keys RETURNING key_id")).fetchall())
            await conn.execute("DELETE FROM phone_challenges WHERE used_at IS NULL")
            return [other for (other,) in others]

        # committed first, then the relay forgets each handle
        async with _http_client() as http:
            others = await end(conn, reset, keys=KeyStore(settings.secrets_key), relay=_relay(settings, http))
        print(
            f"Phone keys removed: {len(keys)}; other devices revoked: {len(others)} (pair them again). "
            f"Open Pisa on the iPhone ({row[0]}): it registers its Face ID key again."
        )
        return 0

    return _with_db(go)


def _cmd_identity(args: argparse.Namespace) -> int:
    """Pisa's own Ed25519 key, on the host only (`pisa identity …`). `rotate` makes a new
    one (the first one too; the old stays published two hours), `revoke` retires every key now (Pisa signs
    nothing until the next rotate), `show` prints the public facts. Never the user's phone keys."""
    from pisa.db.pool import open_pool
    from pisa.infra.keys import KeyStore
    from pisa.services.identity import IdentityService

    async def go() -> int:
        settings = get_settings()
        pool = await open_pool(settings.database_url, max_size=2)
        try:
            identity = IdentityService(pool, KeyStore(settings.secrets_key), agent_url=settings.agent_url)
            if args.identity_command == "rotate":
                print(
                    f"Pisa's new key: {await identity.rotate()} (the previous one stays published two hours)."
                )
            elif args.identity_command == "revoke":
                retired = await identity.revoke()
                print(f"Keys retired: {retired}. Pisa signs nothing until `pisa identity rotate`.")
            status = await identity.status()
            print(f"agent url: {status['agent_url'] or 'unset (PISA_AGENT_URL): Pisa signs nothing'}")
            print(f"active key: {status['kid'] or 'none'}  published: {len(status['published'])}")
            print(f"signing: {'yes' if status['signing'] else 'no'}")
            return 0
        finally:
            await pool.close()

    try:
        return asyncio.run(go())
    except PisaError as err:
        return _fail(err)
    except psycopg.OperationalError as exc:
        return _fail(_unreachable(exc))


def _cmd_devices_purge(args: argparse.Namespace) -> int:
    from pisa.services.devices import purge_device

    try:
        device_id = uuid.UUID(args.device_id)
    except ValueError:
        print(f"error: {args.device_id!r} is not a device id (see `pisa devices list`)", file=sys.stderr)
        return 2

    async def go(conn: Any, settings: Settings) -> int:
        from pisa.infra.keys import KeyStore

        async with _http_client() as http:  # revoked, the relay forgets its handle, then everything it made
            counts = await purge_device(
                conn,
                device_id,
                datetime.now(UTC),
                keys=KeyStore(settings.secrets_key),
                relay=_relay(settings, http),
            )
        removed = ", ".join(f"{n} {what}" for what, n in counts.items())
        print(f"Device {device_id} removed with everything it made ({removed}).")
        return 0

    return _with_db(go)


def email_rules_prefix() -> str:
    from pisa.services.intake import RULE_REASON

    return RULE_REASON


def _cmd_intake_retriage(args: argparse.Namespace) -> int:
    from pisa.services.intake import IntakeService
    from pisa.services.runtime import Runtime

    def day(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC) if len(value) == 10 else None
        except ValueError:
            return None

    since = day(args.since)
    if since is None:
        print(f"error: --since {args.since!r} is not a date: use YYYY-MM-DD", file=sys.stderr)
        return 2
    until = None
    if args.until is not None:
        start = day(args.until)
        if start is None:
            print(f"error: --until {args.until!r} is not a date: use YYYY-MM-DD", file=sys.stderr)
            return 2
        until = start + timedelta(days=1)  # through that whole day (UTC)

    async def go(conn: Any, settings: Settings) -> int:
        runtime = await Runtime.start(settings)
        try:
            service = IntakeService(runtime.pool, runtime.gateway, owner=runtime.owner)
            checked = await service.retriage(since, apply=args.apply, until=until, to_low=args.to_low)
        finally:
            await runtime.close()
        moved = "moved to low" if args.to_low else "dismissed"
        verb = moved if args.apply else f"would be {moved}"
        for item in checked:
            if item.reason:
                ruled = args.to_low and item.reason.startswith(email_rules_prefix())
                # the id and Pisa's reason, never the title (this goes into Actions logs)
                print(f"{'dismissed' if ruled and args.apply else verb}: task {item.task_id} ({item.reason})")
        gone = sum(1 for item in checked if item.reason)
        if args.apply:
            print(f"{gone} {moved}: the dry run's list (those still open; the user's rules dismiss).")
        else:
            print(f"{len(checked)} tasks checked, {gone} {verb}, {len(checked) - gone} kept.")
        return 0

    return _with_db(go)


def _read_secret(provider: str) -> str:
    """From a hidden prompt on a terminal, else one line from stdin. Never from argv (it would land in
    shell history and in the process list)."""
    if sys.stdin.isatty():
        return getpass.getpass(f"{provider} API key (input hidden): ")
    return sys.stdin.readline().strip()


def _cmd_secrets_set(args: argparse.Namespace) -> int:
    from pisa.brain.models.providers import get_provider
    from pisa.infra.keys import KeyStore

    settings = get_settings()
    store = KeyStore(settings.secrets_key)
    if not store.can_store:
        print(
            "error: PISA_SECRETS_KEY is not set on the server, so keys can't be stored encrypted. "
            "Set PISA_SECRETS_KEY (see .env.example) and try again.",
            file=sys.stderr,
        )
        return 2

    async def go(conn: Any, settings: Settings) -> int:
        spec = get_provider(args.provider)
        key = _read_secret(spec.name)
        if not key:
            print("error: no key was given", file=sys.stderr)
            return 2
        await store.set(conn, spec.id, key)
        print(f"{spec.name} key saved (ending in {key[-4:]}). Test it in the app: Settings → AI providers.")
        return 0

    return _with_db(go)


def _cmd_secrets_set_apns(args: argparse.Namespace) -> int:
    from pisa.infra.keys import KeyStore
    from pisa.services.push import set_apns_key

    settings = get_settings()
    store = KeyStore(settings.secrets_key)
    if not store.can_store:
        print(
            "error: PISA_SECRETS_KEY is not set on the server, so the APNs key can't be stored encrypted. "
            "Set PISA_SECRETS_KEY (see .env.example) and try again.",
            file=sys.stderr,
        )
        return 2
    if sys.stdin.isatty():
        print(
            "error: pipe the .p8 file into this command (it is never read from an argument).", file=sys.stderr
        )
        return 2
    pem = sys.stdin.read()

    async def go(conn: Any, settings: Settings) -> int:
        await set_apns_key(conn, store, args.key_id, pem)
        print(f"APNs key {args.key_id.strip()} stored (encrypted). Delete the .p8 file from this server now.")
        return 0

    return _with_db(go)


def _cmd_secrets_set_maps(args: argparse.Namespace) -> int:
    from pisa.infra.keys import KeyStore
    from pisa.services.place_providers import set_maps_key

    settings = get_settings()
    store = KeyStore(settings.secrets_key)
    if not store.can_store:
        print(
            "error: PISA_SECRETS_KEY is not set on the server, so the Apple Maps key can't be stored "
            "encrypted. Set PISA_SECRETS_KEY (see .env.example) and try again.",
            file=sys.stderr,
        )
        return 2
    if sys.stdin.isatty():
        print(
            "error: pipe the .p8 file into this command (it is never read from an argument).", file=sys.stderr
        )
        return 2
    pem = sys.stdin.read()

    async def go(conn: Any, settings: Settings) -> int:
        await set_maps_key(conn, store, args.key_id, pem)
        key_id = args.key_id.strip()
        print(f"Apple Maps key {key_id} stored (encrypted). Delete the .p8 file from this server now.")
        return 0

    return _with_db(go)


def _cmd_catalog_refresh(_: argparse.Namespace) -> int:
    import httpx

    from pisa.brain.models.catalog import refresh_catalog
    from pisa.infra.fx import refresh_usd_to_eur
    from pisa.infra.keys import KeyStore

    async def go(conn: Any, settings: Settings) -> int:
        async with httpx.AsyncClient() as http:
            await refresh_usd_to_eur(conn, http)
            report = await refresh_catalog(
                conn, http, KeyStore(settings.secrets_key), ollama_base_url=settings.ollama_base_url
            )
        print(f"Catalog refreshed: {report.models} models.")
        for error in report.errors:
            print(f"  skipped: {error}")
        return 0

    return _with_db(go)


def _http_client() -> Any:
    """The HTTP client `pisa models` reaches providers and model servers with (tests give a fake server)."""
    import httpx

    return httpx.AsyncClient()


def _with_ai(fn: Callable[[Any], Awaitable[int]]) -> int:
    """Run `fn` with the AI settings service the app uses (its slots, its providers' models)."""
    from pisa.connectors.speech import speech_providers
    from pisa.db.pool import open_pool
    from pisa.infra import hard_limit
    from pisa.infra.keys import KeyStore
    from pisa.services.ai import AiService
    from pisa.services.owner import provide

    settings = get_settings()

    async def go() -> int:
        pool = await open_pool(settings.database_url, max_size=2)
        try:
            await pool.wait(timeout=10)  # the database answers, or a clear error
            async with _http_client() as http:
                ai = AiService(
                    pool,
                    KeyStore(settings.secrets_key),
                    http,
                    speech=speech_providers(http),
                    owner=provide(settings, lambda: datetime.now(UTC)),
                    ollama_base_url=settings.ollama_base_url,
                    hard_limits=hard_limit.limits(settings),
                )
                try:
                    return await fn(ai)
                finally:
                    await ai.drain()  # no refresh left running on the client closed here
        finally:
            await pool.close()

    try:
        return asyncio.run(go())
    except PisaError as err:
        return _fail(err)
    except (psycopg.OperationalError, TimeoutError) as exc:
        return _fail(_unreachable(exc))


def _cmd_models_list(_: argparse.Namespace) -> int:
    async def go(ai: Any) -> int:
        slots = await ai.slots()
        width = max(len(s["id"]) for s in slots)
        for s in slots:
            model = s["model"] or "(not set)"
            fallback = f"  (fallback {s['fallback']})" if s["fallback"] else ""
            print(f"{s['id']:<{width}}  {model}{fallback}  {s['name']}")
        if any(s["model"] is None for s in slots):
            print(
                "\nA feature whose slot is not set fails until you choose a model for it: "
                "pisa models set <slot> <provider:model> (pisa models available lists them)."
            )
        return 0

    return _with_ai(go)


def _cmd_models_set(args: argparse.Namespace) -> int:
    async def go(ai: Any) -> int:
        slot = await ai.update_slot(args.slot, args.model, args.fallback)
        fallback = f", fallback {slot['fallback']}" if slot["fallback"] else ""
        print(f"{slot['id']} now uses {slot['model']}{fallback}.")
        for probe in slot["probes"]:  # a never-probed model on the user's own server, probed now
            print(probe["message"])
        return 0

    return _with_ai(go)


def _cmd_models_probe(args: argparse.Namespace) -> int:
    from pisa.domain.ai import ModelRef

    async def go(ai: Any) -> int:
        ref = ModelRef.parse(args.model)
        found = await ai.probe(ref.provider, ref.model_id)
        on = [k for k, v in found.items() if v is True]
        print(f"Probed {ref}: " + (", ".join(on) if on else "no capability") + ".")
        return 0

    return _with_ai(go)


def _cmd_models_available(args: argparse.Namespace) -> int:
    async def go(ai: Any) -> int:
        found = await ai.models(args.slot)
        usable = [m for m in found["models"] if m["key_set"] and m["compatible"]]
        if not usable:
            print(
                "No model is available yet: add a provider's key (pisa secrets set <provider>, or the app's "
                "Settings → AI providers), then refresh the catalog (pisa catalog refresh)."
            )
            return 0
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for m in usable:
            by_provider.setdefault(m["provider"], []).append(m)
        for provider in sorted(by_provider):
            print(f"{provider}:")
            for m in sorted(by_provider[provider], key=lambda m: m["ref"]):
                needs = ", ".join(m["capabilities"]) or "-"
                print(f"  {m['ref']}  ({needs})")
        return 0

    return _with_ai(go)


# the commands a person runs and reads: their logs go to stderr, warnings and up (all of them at
# PISA_LOG_LEVEL=debug), never between their own lines
INTERACTIVE = frozenset({"setup", "pair", "secrets", "doctor", "models", "catalog", "consent"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pisa", description="Pisa Core")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending database migrations").set_defaults(func=_cmd_migrate)
    doctor = sub.add_parser("doctor", help="check this server's configuration for weak spots")
    doctor.add_argument(
        "--live",
        action="store_true",
        help="also open natural mode's talker for a few seconds (spends a little)",
    )
    doctor.set_defaults(func=_cmd_doctor)
    sub.add_parser("serve-api", help="run the HTTP API + device gateway").set_defaults(func=_cmd_serve_api)
    sub.add_parser("worker", help="run the background worker (DBOS schedules)").set_defaults(func=_cmd_worker)
    voice_relay = sub.add_parser("voice-relay", help="relay the talk's UDP media from the tailnet to the API")
    voice_relay.add_argument("--check", action="store_true", help="healthcheck: exit 0 while the relay runs")
    voice_relay.set_defaults(func=_cmd_voice_relay, no_settings=True)

    devices = sub.add_parser(
        "devices", help="list, revoke and purge paired devices (`pisa pair` pairs one)"
    ).add_subparsers(dest="devices_command", required=True)
    devices.add_parser("list", help="list paired devices").set_defaults(func=_cmd_devices_list)
    revoke = devices.add_parser("revoke", help="cut a device off")
    revoke.add_argument("device_id")
    revoke.set_defaults(func=_cmd_devices_revoke)
    purge = devices.add_parser("purge", help="remove a throwaway device and everything it made (smoke test)")
    purge.add_argument("device_id")
    purge.set_defaults(func=_cmd_devices_purge)

    from pisa.cli_consent import add_consent_parser, cmd_consent
    from pisa.cli_init import add_init_parser
    from pisa.cli_pair import add_client_parser, add_pair_parser, cmd_pair
    from pisa.cli_setup import add_setup_parser, cmd_setup

    add_pair_parser(sub, lambda a: cmd_pair(a, get_settings()))
    add_client_parser(sub)
    add_setup_parser(sub, lambda a: cmd_setup(a, get_settings()))
    add_init_parser(sub)
    add_consent_parser(sub, lambda a: cmd_consent(a, get_settings()))

    phone_keys = sub.add_parser("phone-keys", help="the iPhone's Face ID keys").add_subparsers(
        dest="phone_keys_command", required=True
    )
    reset = phone_keys.add_parser(
        "reset", help="remove every phone key and revoke every device but one: the iPhone registers again"
    )
    reset.add_argument("--keep", required=True, help="the iPhone's device id (see `pisa devices list`)")
    reset.set_defaults(func=_cmd_phone_keys_reset)

    identity = sub.add_parser("identity", help="Pisa's own signing key (Web Bot Auth)").add_subparsers(
        dest="identity_command", required=True
    )
    for name, text in (("rotate", "make a new key (the first one too); the old one stays published 2 hours"),
                       ("revoke", "retire every key now: Pisa signs nothing until the next rotate"),
                       ("show", "the key's public id and whether Pisa signs")):  # fmt: skip
        identity.add_parser(name, help=text).set_defaults(func=_cmd_identity)

    intake = sub.add_parser("intake", help="email intake tools").add_subparsers(
        dest="intake_command", required=True
    )
    retriage = intake.add_parser(
        "retriage", help="dismiss the first sync's email tasks that the rules would not have made"
    )
    retriage.add_argument("--since", required=True, help="YYYY-MM-DD (UTC): tasks created from this day")
    retriage.add_argument(
        "--until",
        help="YYYY-MM-DD (UTC, inclusive): the sweep of every open email task to that day under today's "
        "rules (default: only the first sync's, before). The dry run and --apply take the same dates",
    )
    retriage.add_argument(
        "--apply",
        action="store_true",
        help="dismiss the last dry run's list (default: a dry run, which stores that list)",
    )
    retriage.add_argument(
        "--to-low",
        action="store_true",
        help="Move the list to low priority instead of dismissing it (the user's never rules still dismiss)",
    )
    retriage.set_defaults(func=_cmd_intake_retriage)

    secrets = sub.add_parser("secrets", help="provider API keys").add_subparsers(
        dest="secrets_command", required=True
    )
    set_key = secrets.add_parser("set", help="store a provider key (read from a hidden prompt or stdin)")
    set_key.add_argument("provider", help="provider id, e.g. openai")
    set_key.set_defaults(func=_cmd_secrets_set)
    set_apns = secrets.add_parser("set-apns", help="store the APNs .p8 key, read from stdin")
    set_apns.add_argument("--key-id", required=True, help="the APNs key id, e.g. ABCDE12345 (not secret)")
    set_apns.set_defaults(func=_cmd_secrets_set_apns)
    set_maps = secrets.add_parser("set-maps", help="store the Apple Maps .p8 key, read from stdin")
    set_maps.add_argument("--key-id", required=True, help="the Maps key id (not secret)")
    set_maps.set_defaults(func=_cmd_secrets_set_maps)

    catalog = sub.add_parser("catalog", help="model catalog").add_subparsers(
        dest="catalog_command", required=True
    )
    catalog.add_parser("refresh", help="refresh models, prices and the USD→EUR rate now").set_defaults(
        func=_cmd_catalog_refresh
    )

    models = sub.add_parser("models", help="the model each feature slot uses (none is chosen by default)")
    models.set_defaults(func=_cmd_models_list)
    model_commands = models.add_subparsers(dest="models_command")
    model_commands.add_parser("list", help="every slot and its model").set_defaults(func=_cmd_models_list)
    set_model = model_commands.add_parser("set", help="choose the model of a slot")
    set_model.add_argument("slot", help="a slot id, as `pisa models list` shows it (e.g. chat.conversation)")
    set_model.add_argument("model", help="<provider>:<model>, as `pisa models available` shows it")
    set_model.add_argument("--fallback", help="a second model, tried once when the first fails")
    set_model.set_defaults(func=_cmd_models_set)
    probe = model_commands.add_parser(
        "probe", help="probe what a model on an OpenAI-compatible server can do (a minute or more)"
    )
    probe.add_argument("model", help="<server id>:<model>, as `pisa models available` shows it")
    probe.set_defaults(func=_cmd_models_probe)
    available = model_commands.add_parser(
        "available", help="the models of every provider with a key, from the catalog"
    )
    available.add_argument("--slot", help="only the models that can serve this slot")
    available.set_defaults(func=_cmd_models_available)

    args = parser.parse_args(argv)
    if getattr(args, "no_settings", False):  # the headless client: no server settings on this machine
        return args.func(args)
    try:
        settings = get_settings()
    except ValidationError as exc:
        return _bad_settings(exc)
    from pisa.domain import model_catalog

    model_catalog.set_override(settings.model_catalog)  # the operator's catalog and talker models, as the API
    model_catalog.set_talker_models(settings.talker_models)
    if args.command in INTERACTIVE:  # stdout is the person's: the logs go to stderr, warnings and up
        configure_logging("debug" if settings.log_level == "debug" else "warning", stream=sys.stderr)
    else:  # a `--json` command: stdout is the JSON alone (the logs write there too)
        configure_logging("warning" if getattr(args, "json", False) else settings.log_level)
    return args.func(args)


def _bad_settings(exc: ValidationError) -> int:
    """Each invalid or missing setting on its own line, named as its environment variable."""
    print("error: Pisa's settings are not valid (see .env.example):", file=sys.stderr)
    for error in exc.errors():
        name = "PISA_" + "_".join(str(part) for part in error["loc"]).upper()
        if error["type"] == "missing":
            print(f"  {name} is required", file=sys.stderr)
        else:
            print(f"  {name}: {error['msg'].removeprefix('Value error, ')}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
