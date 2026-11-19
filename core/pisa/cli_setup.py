"""`pisa setup`: the connection guide on the server (the PISA plan, "The connection guide"), for `pisa.cli`.

Four steps, each saying what it asks, why, what a yes or a no changes and where to change it later
(`domain.guide`, English and Italian): your place in the world (time zone, language, country), the AI model
(a cloud key or your own model server, probed), notifications (relay, direct or none, and what the server
needs for each), and pairing (`pisa pair`). Every answer has a flag, so a script can run it with `--yes` and
no prompt; what a flag does not give is then kept as it is.
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from pisa.domain import guide, locale
from pisa.domain.errors import PisaError
from pisa.settings import Settings


class Prompt:
    """Questions on a terminal, or none (`--yes`, or no terminal): then the flag's value or the default."""

    def __init__(
        self, interactive: bool, *, ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass, out: TextIO | None = None,
    ) -> None:  # fmt: skip
        self.interactive = interactive
        self._ask, self._secret = ask, secret
        self.out = out or sys.stdout

    def say(self, line: str = "") -> None:
        print(line, file=self.out)

    def ask(self, question: str, given: str | None, default: str = "") -> str:
        if given is not None:
            return given
        if not self.interactive:
            return default
        answer = self._ask(f"{question} [{default}]: " if default else f"{question}: ").strip()
        return answer or default

    def secret(self, question: str) -> str:
        if self.interactive:
            return self._secret(f"{question}: ")
        return sys.stdin.readline().strip()


async def step_place(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, pool: Any, lang: str
) -> None:
    from pisa.infra import hard_limit
    from pisa.services.owner import provide
    from pisa.services.settings import SettingsService

    owner = provide(settings, lambda: datetime.now(UTC))
    service = SettingsService(pool, owner=owner, hard_limits=hard_limit.limits(settings))
    current = locale.short((await service.get())["language"])
    country = settings.country or "-"
    prompt.say(guide.text("place.now", lang, tz=settings.timezone, language=current, country=country))
    chosen = prompt.ask(guide.text("place.ask_lang", lang), args.language, current).strip().lower()
    if chosen in locale.LANGUAGES and chosen != current:
        await service.patch({"language": locale.TAGS[chosen]})
        prompt.say(guide.text("place.lang_set", lang, language=chosen))
    env: list[str] = []
    zone = prompt.ask(guide.text("place.ask_tz", lang), args.timezone, settings.timezone).strip()
    if zone != settings.timezone:
        try:
            ZoneInfo(zone)
            env.append(f"PISA_TIMEZONE={zone}")
        except (ZoneInfoNotFoundError, ValueError, OSError):
            prompt.say(guide.text("place.bad_tz", lang, value=zone))
    # The country is `pisa init`'s (PISA_COUNTRY in the .env): confirmed here, never asked; --country still
    # prints the .env line for a script.
    if args.country is not None:
        code = args.country.strip().upper()
        if code == country:
            pass
        elif code == "-":
            env.append("PISA_COUNTRY=")
        elif len(code) == 2 and code.isascii() and code.isalpha():
            env.append(f"PISA_COUNTRY={code}")
        else:
            prompt.say(guide.text("place.bad_country", lang, value=code))
    elif settings.country:
        prompt.say(guide.text("place.country_set", lang, country=settings.country))
    else:
        prompt.say(guide.text("place.country_unset", lang))
    if env:
        prompt.say(guide.text("place.env", lang))
        for line in env:
            prompt.say(f"  {line}")


def test_line(result: Any, lang: str) -> str:
    """A key test's outcome in the guide's language (`AiService.test`'s message is English: kept as the
    detail of a failure, and as the line itself in English)."""
    if result.ok and result.models_found:
        return guide.text("ai.test_models", lang, n=result.models_found)
    key = "ai.test_ok" if result.ok else "ai.test_unverified" if result.ok is None else "ai.test_failed"
    return guide.text(key, lang, detail=result.message)


async def step_ai(prompt: Prompt, args: argparse.Namespace, ai: Any, lang: str) -> None:
    slots = await ai.slots()
    conversation = next((s for s in slots if s["id"] == "chat.conversation"), {})
    model = conversation.get("model")
    prompt.say(guide.text("ai.now", lang, model=model) if model else guide.text("ai.unset", lang))
    kind = prompt.ask(guide.text("ai.ask_kind", lang), args.ai, "skip").strip().lower()
    server_id: str | None = None
    if kind == "cloud":
        provider = prompt.ask(guide.text("ai.ask_provider", lang), args.provider, "").strip().lower()
        if not provider:
            prompt.say(guide.text("skipped", lang))
            return
        key = prompt.secret(guide.text("ai.ask_key", lang)) if args.key_stdin or prompt.interactive else ""
        if key:
            await ai.set_key(provider, key)
            result = await ai.test(provider)
            prompt.say(guide.text("ai.key_set", lang, result=test_line(result, lang)))
    elif kind == "own":
        url = prompt.ask(guide.text("ai.ask_server_url", lang), args.server_url, "").strip()
        server_id = prompt.ask(guide.text("ai.ask_server_id", lang), args.server_id, "").strip().lower()
        if not url or not server_id:
            prompt.say(guide.text("skipped", lang))
            return
        await ai.create_server(server_id, None, url, None)
        prompt.say(guide.text("ai.server_added", lang, id=server_id))
    else:
        prompt.say(guide.text("skipped", lang))
        return
    ref = prompt.ask(guide.text("ai.ask_model", lang), args.model, "").strip()
    if not ref:
        return
    provider_id, _, model_id = ref.partition(":")
    if kind == "own" and provider_id == server_id:
        prompt.say(guide.text("ai.probe", lang, model=ref))
        caps = await ai.probe(provider_id, model_id or None)
        names = ("tools", "json_schema", "json_object", "tool_choice", "vision", "audio", "embeddings")
        can = ", ".join(n for n in names if caps.get(n)) or "-"
        cannot = ", ".join(n for n in names if not caps.get(n)) or "-"
        prompt.say(guide.text("ai.caps", lang, can=can, cannot=cannot))
        if not caps.get("tools") and caps.get("features_needing_tools"):
            features = ", ".join(caps["features_needing_tools"])
            prompt.say(guide.text("ai.needs_tools", lang, features=features))
    await ai.update_slot("chat.conversation", ref, None)
    prompt.say(guide.text("ai.model_set", lang, model=ref))


async def step_push(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, conn: Any, lang: str
) -> None:
    from pisa.services.push import APNS_SECRET, push_configured

    relay = bool(settings.push_relay_url)
    stored = await (
        await conn.execute("SELECT 1 FROM server_secrets WHERE name = %s", (APNS_SECRET,))
    ).fetchone()
    direct = push_configured(settings.apns_team_id, settings.apns_topic) and stored is not None
    state = {True: guide.text("push.on", lang), False: guide.text("push.off", lang)}
    prompt.say(guide.text("push.state", lang, relay=state[relay], direct=state[direct]))
    choice = prompt.ask(guide.text("push.ask", lang), args.push, "relay" if relay else "none").strip().lower()
    if choice == "relay":
        prompt.say(guide.text("push.ready" if relay else "push.relay_missing", lang))
    elif choice == "direct":
        prompt.say(guide.text("push.ready" if direct else "push.direct_missing", lang))
    else:
        prompt.say(guide.text("push.none", lang))


async def step_pair(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, conn: Any, lang: str
) -> int:
    from pisa.cli_pair import pair_flow

    name = prompt.ask(guide.text("pair.ask", lang), args.pair, "").strip()
    if not name:
        prompt.say(guide.text("skipped", lang))
        return 0
    return await pair_flow(
        conn, settings, name, lang=lang, signer=args.signer, yes_sas=args.yes_sas, out=prompt.out,
        ask=prompt._ask if prompt.interactive and not args.yes_sas else None,
    )  # fmt: skip


async def run(args: argparse.Namespace, settings: Settings, prompt: Prompt, *, http: Any = None) -> int:
    from pisa.connectors.speech import speech_providers
    from pisa.db.pool import open_pool
    from pisa.infra import hard_limit
    from pisa.infra.keys import KeyStore
    from pisa.services.ai import AiService
    from pisa.services.owner import provide

    skip = {s.strip() for part in args.skip or [] for s in part.split(",")}
    pool = await open_pool(settings.database_url, max_size=2)
    status = 0
    try:
        await pool.wait(timeout=10)
        lang = args.lang or locale.short((await _settings_language(pool, settings)) or settings.language)
        prompt.say(guide.text("intro", lang))
        for step in guide.STEPS:
            prompt.say()
            for line in guide.explain(step, lang):
                prompt.say(line)
            if step in skip:
                prompt.say(guide.text("skipped", lang))
                continue
            if step == "place":
                await step_place(prompt, args, settings, pool, lang)
            elif step == "ai":
                import httpx

                async with http or httpx.AsyncClient() as client:
                    ai = AiService(pool, KeyStore(settings.secrets_key), client,
                                   speech=speech_providers(client),
                                   owner=provide(settings, lambda: datetime.now(UTC)),
                                   ollama_base_url=settings.ollama_base_url,
                                   hard_limits=hard_limit.limits(settings))  # fmt: skip
                    try:
                        await step_ai(prompt, args, ai, lang)
                    except PisaError as err:
                        prompt.say(f"{err.message} {err.action}".strip())
            else:
                async with pool.connection() as conn:
                    if step == "push":
                        await step_push(prompt, args, settings, conn, lang)
                    else:
                        status = await step_pair(prompt, args, settings, conn, lang)
        prompt.say()
        prompt.say(guide.text("done", lang))
    finally:
        await pool.close()
    return status


async def _settings_language(pool: Any, settings: Settings) -> str | None:
    from pisa.services.owner import provide

    return await provide(settings, lambda: datetime.now(UTC)).lang(pool)


def cmd_setup(args: argparse.Namespace, settings: Settings) -> int:
    prompt = Prompt(sys.stdin.isatty() and not args.yes)
    try:
        return asyncio.run(run(args, settings, prompt))
    except PisaError as err:
        print(f"error: {err.message}" + (f" {err.action}." if err.action else ""), file=sys.stderr)
        return 1
    except (psycopg.OperationalError, TimeoutError) as exc:
        print(f"error: the database is unreachable ({exc.__class__.__name__}).", file=sys.stderr)
        return 1


def add_setup_parser(sub: Any, func: Callable[[argparse.Namespace], int]) -> None:
    setup = sub.add_parser("setup", help="the connection guide on the server: place, AI model, push, pairing")
    setup.add_argument("--yes", action="store_true", help="ask nothing: use the flags, keep everything else")
    setup.add_argument("--lang", choices=locale.LANGUAGES, help="the guide's language (default: Pisa's)")
    setup.add_argument(
        "--skip", action="append", help="steps to skip: place, ai, push, pair (comma-separated)"
    )
    setup.add_argument("--language", choices=locale.LANGUAGES, help="the language Pisa speaks")
    setup.add_argument("--timezone", help="the home time zone (printed as a .env line to set)")
    setup.add_argument("--country", help="the country, two letters, or - for none (printed as a .env line)")
    setup.add_argument(
        "--ai", choices=("cloud", "own", "skip"), help="the conversation's model: cloud or own"
    )
    setup.add_argument("--provider", help="cloud: the provider id, e.g. openai")
    setup.add_argument("--key-stdin", action="store_true", help="cloud: read the API key from stdin")
    setup.add_argument("--server-url", help="own: the model server's address, ending in /v1")
    setup.add_argument("--server-id", help="own: a short id for the server")
    setup.add_argument("--model", help="the conversation's model, <provider>:<model>")
    setup.add_argument("--push", choices=("relay", "direct", "none"), help="the phone's notification mode")
    setup.add_argument("--pair", metavar="NAME", help="pair a device with this name at the end")
    setup.add_argument(
        "--signer", action="store_true", help="pairing: the device's key also signs device ops"
    )
    setup.add_argument("--yes-sas", metavar="DIGITS", help="pairing: confirm only if the phone shows these")
    setup.set_defaults(func=func)
