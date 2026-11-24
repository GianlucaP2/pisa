"""`pisa setup`: the connection guide on the server (the PISA plan, "The connection guide"), for `pisa.cli`.

Six steps, each saying what it asks, why, what a yes or a no changes and where to change it later
(`domain.guide`, English and Italian): your place in the world (time zone, language, country), the AI models
(a cloud key, then every feature's model from its catalog, or your own model server, probed), voice talks
(natural with Gemini Live when a Google key is set, else economy), notifications (relay, direct or none, and
what the server needs for each), places and web search (Apple Maps, Google Maps, Tavily: what each is for,
where its key comes from, how it is set), and pairing (`pisa pair`). Every answer has a flag, so a script can
run it with `--yes` and no prompt; what a flag does not give is then kept as it is."""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from pisa.domain import experimental, guide, locale, setup_models
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


# the line that points at the chosen provider's own key page (`ProviderSpec.keys_url`)
KEY_PAGE = {
    "en": "Get a key on the provider's page: {url}",
    "it": "Crea una chiave sulla pagina del fornitore: {url}",
}


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
        from pisa.brain.models.providers import PROVIDERS_BY_ID

        if (spec := PROVIDERS_BY_ID.get(provider)) is not None and spec.needs_key:
            prompt.say(KEY_PAGE.get(lang, KEY_PAGE["en"]).format(url=spec.keys_url))
        key = prompt.secret(guide.text("ai.ask_key", lang)) if args.key_stdin or prompt.interactive else ""
        if key:
            await ai.set_key(provider, key)
            result = await ai.test(provider)
            prompt.say(guide.text("ai.key_set", lang, result=test_line(result, lang)))
            await ai.drain(within_s=60)  # the catalog's refresh with this key's models, before choosing
        await fill_slots(prompt, args, ai, provider, lang)
        return
    if kind == "own":
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


async def fill_slots(prompt: Prompt, args: argparse.Namespace, ai: Any, provider: str, lang: str) -> None:
    """Every text slot from the provider's catalog (`domain.setup_models`), the conversation's model as asked
    (or `--model`; Enter takes the provider's main one), memory search from any provider with an embeddings
    model; then one sentence naming what stayed off."""
    rows = (await ai.models(None))["models"]
    chosen, missing = setup_models.plan(rows, provider)
    ref = prompt.ask(
        guide.text("ai.ask_model", lang), args.model, chosen.get("chat.conversation", "")
    ).strip()
    if ref:
        chosen["chat.conversation"] = ref
        missing = [s for s in missing if s != "chat.conversation"]
    set_now: list[tuple[str, str]] = []
    for slot_id, model in chosen.items():
        try:
            await ai.update_slot(slot_id, model, None)
        except PisaError:  # not this model after all (the catalog changed): the slot stays as it was
            missing.append(slot_id)
            continue
        set_now.append((slot_id, model))
    if set_now:
        prompt.say(guide.text("ai.slots_set", lang))
        for slot_id, model in set_now:
            prompt.say(f"  {slot_id}: {model}")
    if dict(set_now).get("chat.conversation"):
        prompt.say(guide.text("ai.model_set", lang, model=dict(set_now)["chat.conversation"]))
    off = [s for s in setup_models.TEXT_SLOTS if s in missing]
    if setup_models.EMBED_SLOT in missing:
        off.append(guide.text("ai.embed_missing", lang))
    if off:  # every slot left unset, in one sentence
        prompt.say(guide.text("ai.slots_missing", lang, slots=", ".join(off)))


async def step_talk(prompt: Prompt, settings: Settings, pool: Any, lang: str) -> None:
    """Natural talks with Gemini Live when a Google key is set, else economy (the phone's own voice); never
    Grok Voice by default. Written through the settings service, so the change is told like the app's."""
    from pisa.brain.models.catalog import all_models
    from pisa.domain import model_catalog
    from pisa.infra import hard_limit
    from pisa.infra.keys import KeyStore
    from pisa.services.owner import provide
    from pisa.services.settings import SettingsService

    async with pool.connection() as conn:
        keys = await KeyStore(settings.secrets_key).statuses(conn)
        catalog = await all_models(conn)
    service = SettingsService(
        pool, owner=provide(settings, lambda: datetime.now(UTC)), hard_limits=hard_limit.limits(settings)
    )
    natural = "google" in keys
    if natural:
        # the talker hears everything said in a talk, health included: natural talks need the health consent
        prompt.say(guide.text("talk.health", lang))
        if prompt.interactive:
            answer = prompt.ask(
                guide.text("talk.ask_natural", lang), None, guide.text("talk.answer_yes", lang)
            )
            natural = answer.strip().lower()[:1] in ("y", "s")
            if not natural:
                prompt.say(guide.text("talk.kept_economy", lang))
    if natural:
        await service.patch({"conversation": {"mode": "natural", "talker": "gemini"}})
        prompt.say(guide.text("talk.natural", lang))
        current = model_catalog.default_model("talker", "gemini")
        live = sorted(
            {r.model_id for r in catalog if r.provider == "google" and "live" in r.model_id.lower()}
        )
        if live and live != [current]:  # the refreshed catalog's Live models: the current one preselected
            prompt.say(guide.text("talk.models", lang, models=", ".join(live)))
            chosen = prompt.ask(guide.text("talk.ask_model", lang), None, current).strip()
            if chosen and chosen != current:
                prompt.say(guide.text("place.env", lang))
                prompt.say(f"  PISA_TALKER_MODEL_GEMINI={chosen}")
    else:
        await service.patch({"conversation": {"mode": "economy"}})
        if "google" not in keys:  # the xAI talker is named only while that experimental feature is on
            xai = experimental.on(
                "talker_xai", experimental.enabled(settings.experimental, review_mode=settings.review_mode)
            )
            prompt.say(guide.text("talk.economy" if xai else "talk.economy.gemini", lang))


async def step_push(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, conn: Any, lang: str
) -> None:
    from pisa.services.push import APNS_SECRET, push_configured

    # the relay off (review mode keeps it off: `experimental.REVIEW_OFF`), it isn't offered
    offered = experimental.on(
        "push_relay", experimental.enabled(settings.experimental, review_mode=settings.review_mode)
    )
    relay = offered and bool(settings.push_relay_url)
    stored = await (
        await conn.execute("SELECT 1 FROM server_secrets WHERE name = %s", (APNS_SECRET,))
    ).fetchone()
    direct = push_configured(settings.apns_team_id, settings.apns_topic) and stored is not None
    state = {True: guide.text("push.on", lang), False: guide.text("push.off", lang)}
    if not offered:
        prompt.say(guide.text("push.state.direct", lang, direct=state[direct]))
        choice = prompt.ask(guide.text("push.ask.direct", lang), args.push, "none").strip().lower()
    else:
        prompt.say(guide.text("push.state", lang, relay=state[relay], direct=state[direct]))
        choice = prompt.ask(guide.text("push.ask", lang), args.push, "relay" if relay else "none")
        choice = choice.strip().lower()
    if choice == "relay" and not offered:
        prompt.say(guide.text("push.relay_off", lang))
    elif choice == "relay":
        prompt.say(guide.text("push.ready" if relay else "push.relay_missing", lang))
    elif choice == "direct":
        prompt.say(guide.text("push.ready" if direct else "push.direct_missing", lang))
    else:
        prompt.say(guide.text("push.none", lang))


# the providers a key can be typed in for here (Apple Maps' is a .p8 file: `pisa secrets set-maps`)
TYPED_PLACE_KEYS = ("google", "tavily")


async def step_places(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, pool: Any, lang: str
) -> None:
    """The places providers (`services.place_providers`): what each is for, where its key comes from, how it
    is set; a Google Maps or Tavily key can be stored here (encrypted, as `PUT /v1/places/providers/{id}/key`
    stores it). No provider call: the key is tested from a client."""
    import httpx

    from pisa.infra import hard_limit
    from pisa.infra.keys import KeyStore
    from pisa.services.owner import provide
    from pisa.services.place_providers import PROVIDERS, PlaceProviderService

    keys = KeyStore(settings.secrets_key)
    async with httpx.AsyncClient() as http:  # never used: statuses and set_key call no provider
        service = PlaceProviderService(
            pool, keys, http, team_id=settings.apns_team_id,
            owner=provide(settings, lambda: datetime.now(UTC)), hard_limits=hard_limit.limits(settings),
            disabled=settings.review_mode,
        )  # fmt: skip
        ready = {s["id"]: s["configured"] for s in await service.statuses()}
        state = {True: guide.text("push.on", lang), False: guide.text("push.off", lang)}
        prompt.say(guide.text("places.state", lang, **{p: state[ready.get(p, False)] for p in PROVIDERS}))
        for provider in ("apple", "google", "tavily"):
            prompt.say(f"  {guide.text(f'places.{provider}', lang)}")
        choice = prompt.ask(guide.text("places.ask", lang), args.places, "skip").strip().lower()
        if choice == "apple":
            prompt.say(guide.text("places.apple_cmd", lang))
            return
        if choice not in TYPED_PLACE_KEYS:
            prompt.say(guide.text("skipped", lang))
            return
        if not keys.can_store:
            prompt.say(guide.text("places.no_store", lang))
            return
        name = PROVIDERS[choice].name
        typed = args.places_key_stdin or prompt.interactive
        key = prompt.secret(guide.text("places.ask_key", lang, name=name)).strip() if typed else ""
        if not key:
            prompt.say(guide.text("skipped", lang))
            return
        try:
            await service.set_key(choice, key)
        except PisaError as err:  # not a key's shape, or review mode: said, nothing stored
            prompt.say(f"{err.message} {err.action}".strip())
            return
        prompt.say(guide.text("places.key_set", lang, name=name, id=choice))


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
            # the push step without the relay while that experimental feature is off
            relay_off = step == "push" and not experimental.on(
                "push_relay", experimental.enabled(settings.experimental, review_mode=settings.review_mode)
            )
            for line in guide.explain(step, lang, "direct" if relay_off else ""):
                prompt.say(line)
            if step in skip:
                prompt.say(guide.text("skipped", lang))
                continue
            if step == "place":
                await step_place(prompt, args, settings, pool, lang)
            elif step == "talk":
                await step_talk(prompt, settings, pool, lang)
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
                    finally:
                        await ai.drain()  # no refresh left running on the client closed here
            else:
                async with pool.connection() as conn:
                    if step == "push":
                        await step_push(prompt, args, settings, conn, lang)
                    elif step == "places":
                        await step_places(prompt, args, settings, pool, lang)
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
    setup = sub.add_parser(
        "setup", help="the connection guide on the server: place, AI model, push, places, pairing"
    )
    setup.add_argument("--yes", action="store_true", help="ask nothing: use the flags, keep everything else")
    setup.add_argument("--lang", choices=locale.LANGUAGES, help="the guide's language (default: Pisa's)")
    setup.add_argument(
        "--skip", action="append",
        help="steps to skip: place, ai, talk, push, places, pair (comma-separated)",
    )  # fmt: skip
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
    setup.add_argument(
        "--places", choices=("google", "tavily", "apple", "skip"),
        help="a places provider's key to store: google or tavily (apple: `pisa secrets set-maps`)",
    )  # fmt: skip
    setup.add_argument("--places-key-stdin", action="store_true", help="places: read the key from stdin")
    setup.add_argument("--pair", metavar="NAME", help="pair a device with this name at the end")
    setup.add_argument(
        "--signer", action="store_true", help="pairing: the device's key also signs device ops"
    )
    setup.add_argument("--yes-sas", metavar="DIGITS", help="pairing: confirm only if the phone shows these")
    setup.set_defaults(func=func)
