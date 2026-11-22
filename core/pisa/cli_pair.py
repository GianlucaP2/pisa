"""`pisa pair` (the server side of QR pairing) and `pisa client` (the headless client), for `pisa.cli`.

`pisa pair <name>` makes a one-time token, shows the QR (and the typed code), waits for the phone's request
and asks the user to confirm it by comparing the 6 digits both sides show (CONTRACT-2 §4.3). For scripts:
`--no-wait` only shows the code, `--yes-sas <digits>` confirms the first request only if its digits match, and
`pisa pair --confirm <digits>` (or `--refuse`) answers a waiting request later.

`pisa pair --review` (App Store review mode only, PISA_REVIEW_MODE=1; docs-site/app-review.md) shows the
review code: valid 30 days, for up to 3 devices (`--devices`), each paired without the 6-digit confirmation.
`pisa pair --review --revoke` ends it and revokes the devices paired with it.
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import psycopg

from pisa.domain import erase as erase_rules
from pisa.domain import guide
from pisa.domain import pairing as rules
from pisa.domain.errors import PisaError
from pisa.settings import Settings


def _body(data: str | None) -> str | None:
    """`--data`: the body as given, or read from a file (`@path`) or stdin (`@-`). A key or password given on
    the command line is visible to every process on the machine; a file or stdin is not."""
    if data is None or not data.startswith("@"):
        return data
    source = data[1:]
    if source == "-":
        return sys.stdin.read().rstrip("\n")
    with open(source, encoding="utf-8") as f:
        return f.read().rstrip("\n")


def show_qr(uri: str, out: TextIO) -> None:
    import segno

    segno.make(uri, error="m").terminal(out=out, compact=True)


async def pair_flow(
    conn: Any,
    settings: Settings,
    name: str,
    *,
    lang: str | None = None,
    signer: bool = False,
    minutes: int = 10,
    wait: bool = True,
    yes_sas: str | None = None,
    as_json: bool = False,
    ask: Callable[[str], str] | None = None,
    out: TextIO | None = None,
    qr: bool = True,
) -> int:
    """Show a pairing code, then wait for the request and the user's answer. 0 when a device was paired (or
    the code was only shown), 1 when refused or expired."""
    from pisa.infra.keys import KeyStore
    from pisa.services import endpoints, pairing

    stream: TextIO = out or sys.stdout
    lang = lang or settings.language  # Pisa's language (PISA_LANGUAGE) unless asked for another

    def say(key: str, **values: object) -> None:
        print(guide.pair_text(key, lang, **values), file=stream)

    pins = endpoints.pins(settings)
    addresses = settings.address_list
    if not pins or not addresses:
        say("no_pin")
        return 2
    if settings.insecure_loopback_pairing:
        print(guide.pair_text("insecure", lang), file=sys.stderr)
    keys = KeyStore(settings.secrets_key)
    made = await pairing.create_token(
        conn, keys, name, signer=signer, ttl=timedelta(minutes=max(1, min(minutes, 10)))
    )
    exp = int(made.expires_at.timestamp())
    uri = rules.qr_uri(addresses, pins[0], made.token, exp)
    if as_json:
        print(json.dumps({"uri": uri, "token": made.token, "expires_at": made.expires_at.isoformat(),
                          "qr": rules.parse_qr(uri)}), file=stream)  # fmt: skip
    else:
        until = made.expires_at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
        say("scan", until=until)
        if qr:
            show_qr(uri, stream)
        print(uri, file=stream)
        say("typed", code=guide.token_shown(made.token))
    if not wait:
        return 0
    if not as_json:
        say("waiting")
    while True:
        found = await pairing.waiting(conn, token_id=made.id)
        if found:
            request = found[0]
            break
        if await pairing.token_state(conn, made.id) == "expired":
            say("expired")
            return 1
        await asyncio.sleep(rules.POLL_S)
    say("request", name=request.name, platform=request.platform)
    if yes_sas is not None:
        confirm = "".join(c for c in yes_sas if c.isdigit()) == request.sas
        if not confirm:
            say("mismatch")
    elif ask is not None:
        answer = ask(guide.pair_text("compare", lang, sas=guide.sas_shown(request.sas)) + " [y/N] ")
        confirm = answer.strip().lower() in ("y", "yes", "s", "si", "sì")
    else:
        say("compare", sas=guide.sas_shown(request.sas))
        say("later")
        return 0
    device_id = await pairing.decide(conn, request.pairing_id, confirm)
    if device_id is None:
        if yes_sas is None:
            say("refused")
        return 1
    say("confirmed", name=request.name, device_id=device_id)
    if await pairing.signer_key_skipped(conn, device_id):
        say("signer_not_added", name=request.name, device_id=device_id)
    return 0


async def review_flow(
    conn: Any, settings: Settings, *, devices: int = 3, revoke: bool = False, as_json: bool = False,
    out: TextIO | None = None, qr: bool = True,
) -> int:  # fmt: skip
    """`pisa pair --review`: the review code (refused unless PISA_REVIEW_MODE=1), shown as `pisa pair` shows a
    code, and no wait: no one confirms its pairings. `--revoke`: the open review code ends and the devices it
    paired are revoked (with or without the flag, so a server leaving review mode can clean up)."""
    from pisa.infra.keys import KeyStore
    from pisa.services import devices as devices_service
    from pisa.services import endpoints, pairing

    stream: TextIO = out or sys.stdout
    lang = settings.language

    def say(key: str, **values: object) -> None:
        print(guide.pair_text(key, lang, **values), file=stream)

    keys = KeyStore(settings.secrets_key)
    if revoke:
        codes, paired = await pairing.revoke_review(conn)
        # nothing of review mode registered at the push relay (it is off there): no relay to tell
        ended = await devices_service.end(conn, paired, keys=keys, relay=None)
        say("review_revoked", codes=codes, devices=len(ended))
        return 0
    if not settings.review_mode:
        print(guide.pair_text("review_off", lang), file=sys.stderr)
        return 2
    pins = endpoints.pins(settings)
    addresses = settings.address_list
    if not pins or not addresses:
        say("no_pin")
        return 2
    made = await pairing.create_review_token(conn, keys, devices=devices)
    uri = rules.qr_uri(addresses, pins[0], made.token, int(made.expires_at.timestamp()))
    if as_json:
        print(json.dumps({"uri": uri, "token": made.token, "expires_at": made.expires_at.isoformat(),
                          "devices": max(1, min(devices, pairing.REVIEW_MAX_USES)),
                          "qr": rules.parse_qr(uri)}), file=stream)  # fmt: skip
        return 0
    until = made.expires_at.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")
    say("review_scan", until=until, devices=max(1, min(devices, pairing.REVIEW_MAX_USES)))
    if qr:
        show_qr(uri, stream)
    print(uri, file=stream)
    say("typed", code=guide.token_shown(made.token))
    say("review_notes", server=addresses[0])
    return 0


async def answer_waiting(conn: Any, sas: str, confirm: bool, *, lang: str) -> int:
    """`pisa pair --confirm <digits>` / `--refuse <digits>`: the waiting request whose SAS these are."""
    from pisa.services import pairing

    digits = "".join(c for c in sas if c.isdigit())
    for request in await pairing.waiting(conn):
        if request.sas == digits:
            device_id = await pairing.decide(conn, request.pairing_id, confirm)
            if device_id is None:
                print(guide.pair_text("refused", lang))
                return 0
            print(guide.pair_text("confirmed", lang, name=request.name, device_id=device_id))
            if await pairing.signer_key_skipped(conn, device_id):
                print(guide.pair_text("signer_not_added", lang, name=request.name, device_id=device_id))
            return 0
    print(guide.pair_text("none_waiting", lang), file=sys.stderr)
    return 1


def cmd_pair(args: argparse.Namespace, settings: Settings) -> int:
    async def go() -> int:
        async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
            if args.review or args.revoke:
                if not args.review:
                    print(guide.pair_text("revoke_needs_review", settings.language), file=sys.stderr)
                    return 2
                return await review_flow(
                    conn, settings, devices=args.devices, revoke=args.revoke, as_json=args.json
                )
            if args.confirm or args.refuse:
                return await answer_waiting(
                    conn, args.confirm or args.refuse, bool(args.confirm), lang=settings.language
                )
            if not args.name:
                print(guide.pair_text("need_name", settings.language), file=sys.stderr)
                return 2
            interactive = sys.stdin.isatty() and not args.yes_sas
            return await pair_flow(
                conn, settings, args.name, signer=args.signer, minutes=args.minutes, wait=not args.no_wait,
                yes_sas=args.yes_sas, as_json=args.json, ask=input if interactive else None,
            )  # fmt: skip

    try:
        return asyncio.run(go())
    except PisaError as err:
        print(f"error: {err.message}" + (f" {err.action}." if err.action else ""), file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(f"error: the database is unreachable ({exc.__class__.__name__}).", file=sys.stderr)
        return 1


def add_pair_parser(sub: Any, func: Callable[[argparse.Namespace], int]) -> None:
    pair = sub.add_parser(
        "pair", help="pair a phone or the CLI: a QR code (or a typed code), confirmed here by its 6 digits"
    )
    pair.add_argument("name", nargs="?", help='a label for the device, e.g. "iPhone"')
    pair.add_argument(
        "--signer",
        action="store_true",
        help="no longer needed: every paired client signs device ops with its own device key",
    )
    pair.add_argument("--minutes", type=int, default=10, help="how long the code is valid (at most 10)")
    pair.add_argument("--no-wait", action="store_true", help="show the code and exit")
    pair.add_argument("--yes-sas", metavar="DIGITS", help="confirm the request only if it shows these digits")
    pair.add_argument("--json", action="store_true", help="print the code as JSON (for scripts)")
    pair.add_argument("--confirm", metavar="DIGITS", help="confirm the waiting request showing these digits")
    pair.add_argument("--refuse", metavar="DIGITS", help="refuse the waiting request showing these digits")
    pair.add_argument(
        "--review",
        action="store_true",
        help="App Store review mode only (PISA_REVIEW_MODE=1): a code valid 30 days that pairs without the "
        "6-digit confirmation (docs-site/app-review.md)",
    )
    pair.add_argument(
        "--devices", type=int, default=3, help="with --review: how many devices the code pairs (1 to 3)"
    )
    pair.add_argument(
        "--revoke", action="store_true", help="with --review: end the review code and its devices"
    )
    pair.set_defaults(func=func)


# --- the headless client ------------------------------------------------------------------------------------
def cmd_client(args: argparse.Namespace) -> int:
    from pisa.client import Client, ClientError, client_file, observe_pin

    path = client_file(args.file)

    async def go() -> int:
        if args.client_command == "pair":
            client = Client.new(args.server)
            try:
                qr = rules.parse_qr(args.code)
            except rules.PairingInputError as err:
                raise ClientError(str(err)) from None
            # the QR's pin; a typed code has none: the key the server shows now, checked by the two MACs
            pin = qr.get("spki") or observe_pin(args.server)
            client.data["spki"] = [pin] if pin else []
            async with client.http() as http:
                started = await client.pair(http, args.code, args.name)
                print(f"Pairing {started['pairingId']}: check that the server shows "
                      f"{guide.sas_shown(started['sas'])}, and confirm it there.")  # fmt: skip
                state = await client.wait_confirmed(http, started["pairingId"], args.wait)
            if state != "confirmed":
                print(f"Not paired: the pairing was {state}.", file=sys.stderr)
                return 1
            client.save(path)
            print(f"Paired as device {client.data['deviceId']}; the key is in {path}.")
            return 0
        client = Client.load(path)
        async with client.http() as http:
            if args.client_command == "token":
                print(await client.session(http))
                return 0
            if args.client_command in ("approve", "reject"):
                response = await client.decide(http, args.approval_id, args.client_command)
                print(response.text)
                return 0 if response.status_code < 400 else 1
            if args.client_command == "erase":
                before = date.fromisoformat(args.before) if args.before else erase_default(args.family)
                failed = 0
                for family in erase_rules.FAMILY_NAMES if args.family == "all" else (args.family,):
                    response = await client.erase(http, family, before)
                    print(f"{family}: {response.text}")
                    failed += response.status_code >= 400
                return 1 if failed else 0
            sign = (args.sign[0], args.sign[1]) if args.sign else None
            response = await client.call(http, args.method, args.path, body=_body(args.data), sign=sign)
        print(response.text)
        return 0 if response.status_code < 400 else 1

    started = time.monotonic()
    try:
        return asyncio.run(go())
    except ClientError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc.__class__.__name__} after {time.monotonic() - started:.0f} s", file=sys.stderr)
        return 1


def erase_default(family: str) -> date:
    """`--before` left out: for `all`, tomorrow (the server reads it as every row up to the moment they asked,
    today's too); for one family, today."""
    today = date.today()
    return today + timedelta(days=1) if family == "all" else today


def add_client_parser(sub: Any) -> None:
    client = sub.add_parser(
        "client", help="the headless client: this machine as a paired device with its own key"
    ).add_subparsers(dest="client_command", required=True)
    pair = client.add_parser(
        "pair", help="pair with a code from `pisa pair` (the QR's text or the typed code)"
    )
    pair.add_argument("code", help="the pisa://pair... text, or the 26-character code")
    pair.add_argument("--server", required=True, help="the server's address, e.g. https://pisa.example:8443")
    pair.add_argument("--name", default="CLI", help="this device's name on the server")
    pair.add_argument("--wait", type=float, default=300, help="seconds to wait for the confirmation")
    token = client.add_parser("token", help="print a fresh 1 h session token")
    approve = client.add_parser(
        "approve", help="approve an approval, signed with this client's key if needed"
    )
    reject = client.add_parser("reject", help="reject an approval")
    for p in (approve, reject):
        p.add_argument("approval_id", help="the approval's id (GET /v1/approvals)")
    erase = client.add_parser("erase", help="delete a family of their data (signed with this client's key)")
    erase.add_argument("family", choices=("all", *erase_rules.FAMILY_NAMES), help="what to delete, or all")
    erase.add_argument(
        "--before",
        help="rows older than this day, YYYY-MM-DD (default: all of it, today's too, for `all`; else today)",
    )
    call = client.add_parser("call", help="one REST request with a fresh session token")
    call.add_argument("method", help="GET, POST, PUT, PATCH or DELETE")
    call.add_argument("path", help="e.g. /v1/ai/slots")
    call.add_argument(
        "--data",
        help=(
            "the JSON body, sent exactly as given (a signed op hashes it); @FILE reads it from a file "
            "and @- from stdin, so a secret never sits in the command line"
        ),
    )
    call.add_argument("--sign", nargs=2, metavar=("OP", "TARGET"), help="sign a device op, e.g. model_server")
    for p in (pair, token, call, approve, reject, erase):
        p.add_argument("--file", help="the client file (default ~/.config/pisa/client.json)")
        p.set_defaults(func=cmd_client, no_settings=True)
