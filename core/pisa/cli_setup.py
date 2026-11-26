"""`pisa setup`: the connection guide on the server (the PISA plan, "The connection guide"), for `pisa.cli`.

Eight sections, each saying what it asks, why, what a yes or a no changes and where to change it later
(`domain.guide`, English and Italian): your place in the world (time zone, language, country); then the six
categories of the app's Connections (Collegamenti), in its order and under its names (`guide.STEPS`, from
`domain.connections.CATEGORIES`): AI models (a cloud key, then every feature's model from its catalog, or your
own model server, probed), Voice (natural with Gemini Live when a Google key is set, else economy), Maps &
places (the phone's maps, OpenStreetMap, Apple Maps, Google Maps), Web research (Tavily), Accounts (Google,
connected from the app) and Notifications (on the phone itself with no key by default, relay, direct or none,
and what the server needs for each), each ending with what is set and what is next; and pairing (`pisa pair`).
Every answer has a flag, so a script can run it with `--yes` and no prompt; what a flag does not give is then
kept as it is (`--places google|apple` answers Maps & places, `--places tavily` Web research)."""

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
        server = await ai.create_server(
            server_id, None, url, None, read_timeout_s=getattr(args, "read_timeout", None)
        )
        prompt.say(guide.text("ai.server_added", lang, id=server_id))
        prompt.say(guide.text("ai.own_timeout", lang, seconds=server["read_timeout_s"], id=server_id))
        await ai.drain(within_s=60)  # the catalog's refresh with the server's models, before choosing
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
        await fill_own_slots(prompt, ai, server_id, ref, caps, lang)
        return
    await ai.update_slot("chat.conversation", ref, None)
    prompt.say(guide.text("ai.model_set", lang, model=ref))


async def fill_own_slots(
    prompt: Prompt, ai: Any, server_id: str, ref: str, caps: dict[str, Any], lang: str
) -> None:
    """Every slot the server's models can serve: each text slot from the probed model (the slot's own check
    refuses it when it lacks what the slot needs), memory search from the probed model when it embeds, else
    from another of the server's models that answers `/v1/embeddings` (probed then). On a terminal a slot
    with another model asks, Enter keeping it. Then which slots keep a model of their own (a cloud one) and
    which stay unset."""
    current = {s["id"]: s["model"] for s in await ai.slots() if s.get("model")}
    wanted: dict[str, str] = dict.fromkeys(setup_models.TEXT_SLOTS, ref)
    embed = ref if caps.get("embeddings") else await own_embedder(ai, server_id, ref)
    if embed is not None:
        wanted[setup_models.EMBED_SLOT] = embed
    set_now: list[tuple[str, str]] = []
    for slot_id in (*setup_models.TEXT_SLOTS, setup_models.EMBED_SLOT):
        model = wanted.get(slot_id)
        now = current.get(slot_id)
        if model is None:
            continue
        if prompt.interactive and now is not None and now != model and slot_id != "chat.conversation":
            question = guide.text("ai.ask_slot", lang, slot=slot_id, provider=server_id, suggested=model)
            model = prompt.ask(question, None, now).strip() or now
        if model == now:
            continue
        try:
            await ai.update_slot(slot_id, model, None)
        except PisaError:  # the model can't serve this slot: it keeps what it has
            continue
        set_now.append((slot_id, model))
    if set_now:
        prompt.say(guide.text("ai.slots_set", lang))
        for slot_id, model in set_now:
            prompt.say(f"  {slot_id}: {model}")
    after = {s["id"]: s["model"] for s in await ai.slots() if s.get("model")}
    if after.get("chat.conversation") == ref:
        prompt.say(guide.text("ai.model_set", lang, model=ref))
    if after.get(setup_models.EMBED_SLOT, "").startswith(f"{server_id}:"):
        prompt.say(guide.text("ai.own_embed", lang, model=after[setup_models.EMBED_SLOT]))
    every = (*setup_models.TEXT_SLOTS, setup_models.EMBED_SLOT)
    others = [s for s in every if s in after and not after[s].startswith(f"{server_id}:")]
    unset = [s for s in every if s not in after]
    if others:
        prompt.say(guide.text("ai.own_kept", lang, slots=", ".join(others)))
    if unset:
        prompt.say(guide.text("ai.own_unset", lang, slots=", ".join(unset)))


async def own_embedder(ai: Any, server_id: str, chat_ref: str) -> str | None:
    """Another of the server's models that answers `/v1/embeddings` (one small request each; the one found is
    probed, so its capabilities are recorded), or None."""
    rows = (await ai.models(None))["models"]
    for row in sorted((r for r in rows if r["provider"] == server_id), key=lambda r: r["ref"]):
        if row["ref"] == chat_ref or not await ai.embeds(server_id, row["model_id"]):
            continue
        found = await ai.probe(server_id, row["model_id"])
        if found.get("embeddings"):
            return str(row["ref"])
    return None


async def ai_next(prompt: Prompt, ai: Any, lang: str) -> None:
    """What is next for AI models: nothing once the conversation has a model, else choosing one."""
    slots = await ai.slots()
    model = next((s.get("model") for s in slots if s["id"] == "chat.conversation"), None)
    prompt.say(guide.text("next.none" if model else "ai.next", lang))


async def fill_slots(prompt: Prompt, args: argparse.Namespace, ai: Any, provider: str, lang: str) -> None:
    """Every text slot from the provider's catalog (`domain.setup_models`), the conversation's model as asked
    (or `--model`; Enter takes the slot's current model, else the provider's main one), memory search from any
    provider with an embeddings model; then one sentence naming what stayed off. A slot that already has a
    model is offered it as the default (the owner's setup, 24 Nov: the conversation's default was the
    catalog's `anthropic:claude-opus-5`, not the haiku it used): another slot is asked only when its current
    model differs from the provider's, and keeps it on Enter. Without a terminal the provider's models are
    set."""
    rows = (await ai.models(None))["models"]
    chosen, missing = setup_models.plan(rows, provider)
    # on a terminal each slot's current model is the default; without one (`--yes`) the provider asked for
    # sets every slot, as before
    current = {s["id"]: s["model"] for s in await ai.slots() if s.get("model")} if prompt.interactive else {}
    conversation = current.get("chat.conversation") or chosen.get("chat.conversation", "")
    ref = prompt.ask(guide.text("ai.ask_model", lang), args.model, conversation).strip()
    if ref:
        chosen["chat.conversation"] = ref
        missing = [s for s in missing if s != "chat.conversation"]
    for slot_id, suggested in list(chosen.items()):
        now = current.get(slot_id)
        if slot_id == "chat.conversation" or now is None or now == suggested:
            continue
        question = guide.text("ai.ask_slot", lang, slot=slot_id, provider=provider, suggested=suggested)
        chosen[slot_id] = prompt.ask(question, None, now).strip() or now
    for slot_id in [s for s in missing if s in current]:  # the provider has none for it: the current stays
        missing.remove(slot_id)
    chosen = {s: m for s, m in chosen.items() if current.get(s) != m}  # unchanged: nothing to set
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


async def _google_paid(prompt: Prompt, service: Any, lang: str) -> bool:
    """Whether Gemini Live may hear them, asked when not known yet (the Gemini API's terms; GDPR Art. 28, 44):
    billing on (paid), else the operator's own declaration, two questions and two yeses (in the EEA,
    Switzerland or the UK; the only person using this Pisa with this key): free for their own use, the
    talker only. Otherwise the value stays as it was. Unasked (not interactive), nothing changes."""
    from pisa.domain import google_tier

    current = await service.get()
    if not google_tier.talker_allowed(current) and prompt.interactive:
        tier: str | None = None
        if _yes(prompt.ask(guide.text("google.ask_paid", lang), None, guide.text("google.answer_no", lang))):
            tier = "paid"
        elif all(
            _yes(prompt.ask(guide.text(question, lang), None, guide.text("google.answer_no", lang)))
            for question in ("google.ask_eea", "google.ask_only_user")
        ):
            tier = "free_eea_own_use"
        if tier is not None:
            current = await service.patch({"google_ai_tier": tier})
    if google_tier.talker_allowed(current):
        return True
    prompt.say(guide.text("google.unpaid", lang))
    return False


def _yes(answer: str) -> bool:
    return answer.strip().lower()[:1] in ("y", "s")


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
    natural = "google" in keys and await _google_paid(prompt, service, lang)
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
        prompt.say(guide.text("next.none", lang))
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
        prompt.say(guide.text("talk.next", lang))


async def step_push(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, conn: Any, lang: str
) -> None:
    from pisa.services.phone_notify import phone_notify_on
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
    phone = phone_notify_on(settings)  # contract 2.3's `pisa.notify`: no key, the default
    state = {True: guide.text("push.on", lang), False: guide.text("push.off", lang)}
    prompt.say(f"  {guide.text('push.phone', lang)}")
    prompt.say(guide.text("push.state.phone", lang, phone=state[phone]))
    default = "phone" if phone else "relay" if relay else "none"
    if not offered:
        prompt.say(guide.text("push.state.direct", lang, direct=state[direct]))
        answer = prompt.ask(guide.text("push.ask.direct", lang), args.push, "phone" if phone else "none")
    else:
        prompt.say(guide.text("push.state", lang, relay=state[relay], direct=state[direct]))
        answer = prompt.ask(guide.text("push.ask", lang), args.push, default)
    # the guide names the modes in the user's language ("diretto o nessuno"): those words are the modes too
    # (an Italian "diretto" was taken for none: "Niente da impostare sul server")
    choice = _PUSH_WORDS.get(answer.strip().lower(), answer.strip().lower())
    if choice == "phone":
        prompt.say(guide.text("push.phone_ready" if phone else "push.phone_off", lang))
    elif choice == "relay" and not offered:
        prompt.say(guide.text("push.relay_off", lang))
    elif choice == "relay":
        prompt.say(guide.text("push.ready" if relay else "push.relay_missing", lang))
    elif choice == "direct":
        if direct:
            prompt.say(guide.text("push.ready", lang))
            return
        # what is missing, by name, and the exact commands for each
        unset = {"team": not settings.apns_team_id, "topic": not settings.apns_topic, "key": stored is None}
        needs = [need for need, missing in unset.items() if missing]
        missing = ", ".join(guide.text(f"push.need.{n}", lang) for n in needs)
        prompt.say(guide.text("push.direct_missing", lang, missing=missing))
        for need in needs:
            prompt.say(f"  {guide.text(f'push.cmd.{need}', lang)}")
        prompt.say(f"  {guide.text('push.cmd.restart', lang)}")
    elif choice == "none":
        prompt.say(guide.text("push.none", lang))
    else:
        choices = "phone, relay, direct, none" if offered else "phone, direct, none"
        prompt.say(guide.text("push.unknown", lang, answer=answer.strip()[:40], choices=choices))


# the notification modes as the guide names them in each language
_PUSH_WORDS = {
    "diretto": "direct", "diretta": "direct", "nessuno": "none", "nessuna": "none", "no": "none",
    "telefono": "phone", "sul telefono": "phone",
}  # fmt: skip


# the providers a key can be typed in for here (Apple Maps' is a .p8 file: `pisa secrets set-maps`)
TYPED_PLACE_KEYS = ("google", "tavily")


async def _place_service(settings: Settings, pool: Any, http: Any) -> Any:
    from pisa.infra import hard_limit
    from pisa.infra.keys import KeyStore
    from pisa.services.owner import provide
    from pisa.services.place_providers import PlaceProviderService

    return PlaceProviderService(
        pool, KeyStore(settings.secrets_key), http, team_id=settings.apns_team_id,
        owner=provide(settings, lambda: datetime.now(UTC)), hard_limits=hard_limit.limits(settings),
        disabled=settings.review_mode,
    )  # fmt: skip


async def _store_place_key(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, service: Any, choice: str, lang: str
) -> str | None:
    """A Google Maps or Tavily key typed (or read from stdin) and stored encrypted, as `PUT
    /v1/places/providers/{id}/key` stores it; the provider's id, or None when nothing was stored."""
    from pisa.infra.keys import KeyStore
    from pisa.services.place_providers import PROVIDERS

    if not KeyStore(settings.secrets_key).can_store:
        prompt.say(guide.text("places.no_store", lang))
        return None
    name = PROVIDERS[choice].name
    typed = args.places_key_stdin or prompt.interactive
    key = prompt.secret(guide.text("places.ask_key", lang, name=name)).strip() if typed else ""
    if not key:
        prompt.say(guide.text("skipped", lang))
        return None
    try:
        await service.set_key(choice, key)
    except PisaError as err:  # not a key's shape, or review mode: said, nothing stored
        prompt.say(f"{err.message} {err.action}".strip())
        return None
    prompt.say(guide.text("places.key_set", lang, name=name, id=choice))
    return choice


async def _paid_next(prompt: Prompt, service: Any, ready: dict[str, bool], providers: tuple[str, ...],
                     lang: str) -> bool:  # fmt: skip
    """The next step for a paid provider set up but waiting for the user's OK; True when one was said."""
    from pisa.domain.connections import ACKED
    from pisa.services.place_providers import PROVIDERS

    acks = await service.acks()
    waiting = [p for p in providers if p in ACKED and ready.get(p) and "cap_cents" not in acks.get(p, {})]
    for provider in waiting:
        prompt.say(guide.text("paid.next", lang, name=PROVIDERS[provider].name, id=provider))
    return bool(waiting)


async def step_maps(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, pool: Any, lang: str
) -> None:
    """Maps & places (`services.place_providers`): the phone's maps and OpenStreetMap (no key), Apple Maps and
    Google Maps (what each is for, where its key comes from, how it is set); a Google Maps key can be stored
    here. No provider call: the key is tested from a client."""
    import httpx

    async with httpx.AsyncClient() as http:  # never used: statuses and set_key call no provider
        service = await _place_service(settings, pool, http)
        ready = {s["id"]: bool(s["configured"]) for s in await service.statuses()}
        phone = settings.phone_maps_enabled and not service.disabled
        state = {True: guide.text("push.on", lang), False: guide.text("push.off", lang)}
        on = {p: state[ready.get(p, False)] for p in ("osm", "apple", "google")}
        prompt.say(guide.text("maps.state", lang, phone=state[phone], **on))
        for provider in ("apple", "google"):
            prompt.say(f"  {guide.text(f'places.{provider}', lang)}")
        given = args.places if args.places in ("google", "apple", "skip") else None
        choice = prompt.ask(guide.text("maps.ask", lang), given, "skip").strip().lower()
        if choice == "apple":
            prompt.say(guide.text("places.apple_cmd", lang))
        elif choice == "google":
            if await _store_place_key(prompt, args, settings, service, "google", lang):
                ready["google"] = True
        else:
            prompt.say(guide.text("skipped", lang))
        if not await _paid_next(prompt, service, ready, ("google",), lang):
            keyed = ready.get("apple") or ready.get("google")
            prompt.say(guide.text("next.none" if keyed else "maps.next", lang))


async def step_web(
    prompt: Prompt, args: argparse.Namespace, settings: Settings, pool: Any, lang: str
) -> None:
    """Web research: Tavily, its key stored here (encrypted), called only after the user's paid OK."""
    import httpx

    async with httpx.AsyncClient() as http:  # never used: statuses and set_key call no provider
        service = await _place_service(settings, pool, http)
        ready = {s["id"]: bool(s["configured"]) for s in await service.statuses()}
        state = {True: guide.text("push.on", lang), False: guide.text("push.off", lang)}
        prompt.say(guide.text("web.state", lang, tavily=state[ready.get("tavily", False)]))
        prompt.say(f"  {guide.text('places.tavily', lang)}")
        given = args.places if args.places in ("tavily", "skip") else None
        choice = prompt.ask(guide.text("web.ask", lang), given, "skip").strip().lower()
        if choice == "tavily":
            if await _store_place_key(prompt, args, settings, service, "tavily", lang):
                ready["tavily"] = True
        else:
            prompt.say(guide.text("skipped", lang))
        if not await _paid_next(prompt, service, ready, ("tavily",), lang):
            prompt.say(guide.text("next.none" if ready.get("tavily") else "web.next", lang))


async def step_accounts(prompt: Prompt, settings: Settings, conn: Any, lang: str) -> None:
    """Accounts: Google (Gmail and Google Calendar), connected from the app; here what is set and what is next
    (`pisa google setup` for the project and its OAuth client). Nothing is asked."""
    from pisa.services.google import is_off

    client = await (
        await conn.execute("SELECT 1 FROM connector_credentials WHERE connector = 'google'")
    ).fetchone()
    account = await (
        await conn.execute(
            "SELECT status, needs_reconnect FROM connector_accounts WHERE connector = 'google'"
        )
    ).fetchone()
    if settings.review_mode or await is_off(conn):
        state = "off"
    elif account is not None and account[1]:
        state = "needs_reconnect"
    elif account is not None and account[0] == "error":
        state = "error"
    elif account is not None and account[0] == "connected":
        state = "on"
    else:
        state = "not_connected" if client is not None else "no_client"
    prompt.say(guide.text("accounts.state", lang, google=guide.text(f"accounts.google.{state}", lang)))
    prompt.say(guide.text("next.none" if state in ("on", "off") else f"accounts.next.{state}", lang))


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

    skip = guide.skipped(args.skip)
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
                    try:
                        await ai_next(prompt, ai, lang)
                    finally:
                        await ai.drain()  # no refresh left running on the client closed here
            else:
                async with pool.connection() as conn:
                    if step == "push":
                        await step_push(prompt, args, settings, conn, lang)
                    elif step == "maps":
                        await step_maps(prompt, args, settings, pool, lang)
                    elif step == "web":
                        await step_web(prompt, args, settings, pool, lang)
                    elif step == "accounts":
                        await step_accounts(prompt, settings, conn, lang)
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
        "setup",
        help="the connection guide on the server: your place, then AI models, Voice, Maps & places, Web "
        "research, Accounts, Notifications (the app's Connections), then pairing",
    )
    setup.add_argument("--yes", action="store_true", help="ask nothing: use the flags, keep everything else")
    setup.add_argument("--lang", choices=locale.LANGUAGES, help="the guide's language (default: Pisa's)")
    setup.add_argument(
        "--skip", action="append",
        help="sections to skip: place, ai, talk (voice), maps, web, accounts, push (notifications), pair; "
        "places is maps and web (comma-separated)",
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
    setup.add_argument(
        "--read-timeout",
        type=int,
        help="own: seconds a request may wait for the server's next byte (default 60)",
    )
    setup.add_argument("--model", help="the conversation's model, <provider>:<model>")
    setup.add_argument(
        "--push", choices=("phone", "relay", "direct", "none"),
        help="the phone's notification mode (phone: scheduled on the phone itself, no key; the default)",
    )  # fmt: skip
    setup.add_argument(
        "--places", choices=("google", "tavily", "apple", "skip"),
        help="a key to store: google (Maps & places) or tavily (Web research); apple: `pisa secrets "
        "set-maps`",
    )  # fmt: skip
    setup.add_argument("--places-key-stdin", action="store_true", help="places: read the key from stdin")
    setup.add_argument("--pair", metavar="NAME", help="pair a device with this name at the end")
    setup.add_argument(
        "--signer", action="store_true", help="pairing: the device's key also signs device ops"
    )
    setup.add_argument("--yes-sas", metavar="DIGITS", help="pairing: confirm only if the phone shows these")
    setup.set_defaults(func=func)
