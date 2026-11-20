"""`pisa consent`: the user's AI consent given on the server, for a paired device (CONTRACT-2 §4.4), when the
app can't show it. It prints the AI disclosure as it is now (`services.disclosure.current`): each provider,
model and what it receives, in Pisa's language; asks y/N (or takes `--yes`); then records it for the device
(`services.disclosure.record`), the same as the app's `POST /v1/ai/consent`.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

import psycopg

from pisa.domain import guide
from pisa.domain.errors import PisaError
from pisa.settings import Settings


async def active_devices(conn: Any) -> list[tuple[uuid.UUID, str, str]]:
    rows = await (
        await conn.execute(
            "SELECT id, name, platform FROM devices WHERE revoked_at IS NULL ORDER BY created_at, id"
        )
    ).fetchall()
    return [(r[0], str(r[1]), str(r[2])) for r in rows]


def shown(doc: dict[str, Any], lang: str) -> list[str]:
    """The disclosure, one line per provider and model, with what it receives and what for."""
    if not doc["processors"]:
        return [guide.consent_text("none", lang)]
    lines: list[str] = []
    for p in doc["processors"]:
        provider = p["provider"]
        kind = guide.consent_text(f"kind.{provider['kind']}", lang)
        data = ", ".join(guide.consent_category(c, lang) for c in p["dataCategories"])
        lines.append(
            guide.consent_text(
                "processor", lang, provider=provider["name"], kind=kind, model=p["model"], slot=p["slot"],
                data=data,
            )
        )  # fmt: skip
    return lines


async def consent_flow(
    conn: Any, settings: Settings, *, device: str | None, health: bool, yes: bool, lang: str,
    ask: Callable[[str], str] | None, out: TextIO,
) -> int:  # fmt: skip
    from pisa.services import disclosure

    def say(line: str = "") -> None:
        print(line, file=out)

    devices = await active_devices(conn)
    if not devices:
        say(guide.consent_text("no_device", lang))
        return 2
    chosen: tuple[uuid.UUID, str, str] | None = None
    if device is not None:
        chosen = next((d for d in devices if str(d[0]) == device.strip().lower()), None)
        if chosen is None:
            say(guide.consent_text("unknown_device", lang, device=device))
            return 2
    elif len(devices) == 1:
        chosen = devices[0]
    else:
        say(guide.consent_text("which", lang))
        for n, (device_id, name, platform) in enumerate(devices, 1):
            say(f"  {n}. {name} ({platform}) {device_id}")
        if ask is None:
            say(guide.consent_text("give_device", lang))
            return 2
        answer = ask(guide.consent_text("ask_which", lang) + " ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(devices):
            chosen = devices[int(answer) - 1]
        else:
            chosen = next((d for d in devices if str(d[0]) == answer.lower()), None)
        if chosen is None:
            say(guide.consent_text("unknown_device", lang, device=answer))
            return 2
    device_id, name, _ = chosen
    doc = await disclosure.current(conn, settings.ollama_base_url)
    say(guide.consent_text("intro", lang, name=name))
    for line in shown(doc, lang):
        say(f"  - {line}")
    say(guide.consent_text("health_yes" if health else "health_no", lang))
    if not yes:
        if ask is None:
            say(guide.consent_text("give_yes", lang))
            return 2
        if ask(guide.consent_text("ask", lang) + " [y/N] ").strip().lower() not in (
            "y",
            "yes",
            "s",
            "si",
            "sì",
        ):
            say(guide.consent_text("not_given", lang))
            return 1
    await disclosure.record(
        conn, device_id, doc["revision"], health, settings.ollama_base_url, datetime.now(UTC)
    )
    say(guide.consent_text("given", lang, name=name))
    return 0


def cmd_consent(args: argparse.Namespace, settings: Settings) -> int:
    interactive = sys.stdin.isatty() and not args.yes

    async def go() -> int:
        async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
            return await consent_flow(
                conn, settings, device=args.device, health=args.health, yes=args.yes,
                lang=settings.language, ask=input if interactive else None, out=sys.stdout,
            )  # fmt: skip

    try:
        return asyncio.run(go())
    except PisaError as err:
        print(f"error: {err.message}" + (f" {err.action}." if err.action else ""), file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(f"error: the database is unreachable ({exc.__class__.__name__}).", file=sys.stderr)
        return 1


def add_consent_parser(sub: Any, func: Callable[[argparse.Namespace], int]) -> None:
    consent = sub.add_parser(
        "consent", help="allow Pisa to send your words to the AI models it uses, for a paired device"
    )
    consent.add_argument("--device", help="the device's id (`pisa devices list`); asked when several")
    consent.add_argument("--health", action="store_true", help="also allow health to go to AI")
    consent.add_argument("--yes", action="store_true", help="allow without asking (you read the list)")
    consent.set_defaults(func=func)
