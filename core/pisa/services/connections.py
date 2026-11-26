"""Collegamenti (Connections): `GET /v1/connections`, every outside service in six categories
(`domain.connections`), read from the services that own each one. Read-only: nothing here calls a provider or
changes a key; no key material beyond the last four characters the providers' own endpoints already show.

What is absent: a feature that is off on this install (`PISA_EXPERIMENTAL`, App Store review mode): the
server's voices (`tts_server`), the xAI talker, the places and web providers (`places`), the Google account
(`google`), the push relay (`push_relay`). In review mode the AI settings are the operator's: no key
action."""

from typing import TYPE_CHECKING, Any

from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.domain import connections as rules
from pisa.domain import experimental
from pisa.services import phone_notify
from pisa.services.google import is_off
from pisa.services.place_providers import PROVIDERS as PLACE_PROVIDERS

if TYPE_CHECKING:
    from pisa.services.runtime import Runtime

Entry = dict[str, Any]


def _entry(
    id_: str,
    kind: str,
    category: rules.Category,
    name: str,
    state: rules.State,
    *,
    provider: str | None = None,
    paid: bool = False,
    data_out: str,
    actions: list[rules.Action] | tuple[rules.Action, ...] = (),
    key_last4: str | None = None,
    slots: list[str] | None = None,
    paid_ack: dict[str, Any] | None = None,
    needs_paid_ack: bool = False,
    month_spent_cents: int | None = None,
) -> Entry:
    return {
        "id": id_,
        "kind": kind,
        "category": category,
        "name": name,
        "provider": provider,
        "state": state,
        "paid": paid,
        "paid_ack": paid_ack,
        "needs_paid_ack": needs_paid_ack,
        "month_spent_cents": month_spent_cents,
        "data_out": data_out,
        "docs": rules.DOCS[category],
        "actions": [a for a in rules.ACTIONS if a in actions],
        "key_last4": key_last4,
        "slots": slots or [],
    }


def _tested(state: rules.State, last_test: dict[str, Any] | None) -> rules.State:
    """A key whose last test failed shows as an error until it passes again."""
    return "error" if state == "on" and last_test is not None and last_test.get("ok") is False else state


async def _ai(runtime: "Runtime") -> tuple[list[Entry], dict[str, dict[str, Any]]]:
    """The AI providers and model servers (the speech-only ones are voices), with the slots each serves."""
    providers = await runtime.ai.providers()
    slots = await runtime.ai.slots()
    locked = runtime.settings.review_mode  # the operator's AI settings: no key changes from the app
    by_provider: dict[str, list[str]] = {}
    for slot in slots:
        for ref in (slot["model"], slot["fallback"]):
            if ref:
                used = by_provider.setdefault(ref.partition(":")[0], [])
                if slot["id"] not in used:
                    used.append(slot["id"])
    out: list[Entry] = []
    for p in providers:
        spec = PROVIDERS_BY_ID.get(p["id"])
        if spec is not None and spec.speech_only:
            continue
        builtin = p["type"] == "builtin"
        state: rules.State = "on" if (p["key_set"] or not builtin) else "not_set_up"
        actions: list[rules.Action] = [] if locked else ["set_key"]
        if p["key_set"] and not locked:
            actions.append("delete_key")
        if p["key_set"] or not builtin:
            actions.append("test")
        out.append(
            _entry(
                f"ai.{p['id']}",
                "ai_provider" if builtin else "model_server",
                "ai",
                p["name"],
                _tested(state, p["last_test"]),
                provider=p["id"],
                paid=builtin,  # a cloud provider bills by the token (the catalog's prices); their server not
                data_out=rules.data_out("ai.builtin" if builtin else "ai.server", p["name"]),
                actions=actions,
                key_last4=p["key_last4"],
                slots=by_provider.get(p["id"], []),
            )
        )
    return out, {p["id"]: p for p in providers}


def _voice(runtime: "Runtime", providers: dict[str, dict[str, Any]]) -> list[Entry]:
    """The phone's own voice, the server's voices (when on) and the natural-voice talkers."""
    locked = runtime.settings.review_mode
    out = [_entry("voice.device", "voice", "voice", "iPhone voice", "on", provider="device",
                  data_out=rules.data_out("voice.device"))]  # fmt: skip
    wanted: list[tuple[str, str]] = []
    if experimental.on("tts_server", runtime.enabled):
        wanted += [("openai", "OpenAI voices"), ("elevenlabs", "ElevenLabs")]
    wanted.append(("gemini", "Natural voice (Gemini Live)"))
    if experimental.on("talker_xai", runtime.enabled):
        wanted.append(("xai", "Natural voice (xAI)"))
    for voice, name in wanted:
        key_provider = rules.VOICE_KEYS[voice]
        p = providers.get(key_provider)
        keyed = bool(p and p["key_set"])
        actions: list[rules.Action] = [] if locked else ["set_key"]
        if keyed and not locked:
            actions.append("delete_key")
        if keyed:
            actions.append("test")
        out.append(
            _entry(
                f"voice.{voice}",
                "talker" if voice in ("gemini", "xai") else "voice",
                "voice",
                name,
                _tested("on" if keyed else "not_set_up", p["last_test"] if p else None),
                provider=key_provider,
                paid=True,  # billed by the provider, under the AI caps (`domain.voice`'s prices)
                data_out=rules.data_out(f"voice.{voice}"),
                actions=actions,
                key_last4=p["key_last4"] if p else None,
            )
        )
    return out


async def _places(runtime: "Runtime") -> tuple[list[Entry], list[Entry]]:
    """Maps & places, then web research: the phone's maps and every place provider, with the paid OK."""
    service = runtime.place_providers
    statuses = {s["id"]: s for s in await service.statuses()}
    acks = await service.acks()
    maps: list[Entry] = []
    phone_on = runtime.settings.phone_maps_enabled and not service.disabled
    maps.append(
        _entry("maps.phone", "phone_maps", "maps", "iPhone Maps", "on" if phone_on else "off",
               provider="phone", data_out=rules.data_out("maps.phone"))
    )  # fmt: skip
    web: list[Entry] = []
    for provider, info in PLACE_PROVIDERS.items():
        status = statuses[provider]
        category: rules.Category = "web" if info.jobs == ("web",) else "maps"
        paid = provider in rules.ACKED
        ack = acks.get(provider, {})
        acked = "cap_cents" in ack
        configured = bool(status["configured"])
        if info.keyless:
            state: rules.State = "on" if configured else "off"
            actions: list[rules.Action] = ["test"] if configured else []
        else:
            state = "on" if configured else "not_set_up"
            actions = ["set_key"]
            if configured:
                actions.extend(("delete_key", "test"))
        if service.disabled:  # review mode: no provider is ever called (the category is absent there anyway)
            state, actions = "off", []
        needs_ack = paid and configured and not acked and not service.disabled
        if needs_ack:
            state = "not_set_up"  # set up, but never called until the user's OK
            actions = [a for a in actions if a != "test"]
        if paid and not service.disabled:
            actions.append("ack_paid")
        entry = _entry(
            f"{category}.{provider}",
            "web_provider" if category == "web" else "place_provider",
            category,
            info.name,
            _tested(state, status["last_test"]),
            provider=provider,
            paid=paid,
            data_out=rules.data_out(f"{category}.{provider}"),
            actions=actions,
            key_last4=status["key_last4"],
            paid_ack={"cap_cents": ack["cap_cents"], "at": ack["at"]} if acked else None,
            needs_paid_ack=needs_ack,
            month_spent_cents=ack.get("spent_cents") if paid else None,
        )
        (web if category == "web" else maps).append(entry)
    return maps, web


async def _accounts(runtime: "Runtime") -> list[Entry]:
    google = runtime.google
    async with runtime.pool.connection() as conn:
        account = await google.account(conn)
        switched_off = await is_off(conn)  # their switch in Settings → Connections
    connector = await google.connector()
    state: rules.State
    if google.disabled or switched_off:
        state = "off"
    elif account is not None and account.needs_reconnect:
        state = "needs_reconnect"
    elif account is not None and account.status == "error":
        state = "error"
    elif account is not None and account.status == "connected":
        state = "on"
    else:
        state = "not_set_up"
    actions: list[rules.Action] = []
    if not google.disabled:
        if connector["configured"] and state in ("not_set_up", "needs_reconnect", "error"):
            actions.append("connect")
        if account is not None:
            actions.append("disconnect")
    return [
        _entry("accounts.google", "account", "accounts", "Google (Gmail and Google Calendar)", state,
               provider="google", data_out=rules.data_out("accounts.google"), actions=actions)
    ]  # fmt: skip


async def _notifications(runtime: "Runtime") -> list[Entry]:
    push = runtime.push
    async with runtime.pool.connection() as conn:
        works = await phone_notify.phone_works(conn, runtime.clock())
        apns_key = (
            await (await conn.execute("SELECT 1 FROM server_secrets WHERE name = 'apns'")).fetchone()
        ) is not None
    phone: rules.State = (
        "off" if not phone_notify.phone_notify_on(runtime.settings) else "on" if works else "not_set_up"
    )
    out = [
        _entry("notifications.phone", "phone_notify", "notifications", "On the phone", phone,
               provider="phone", data_out=rules.data_out("notifications.phone"))
    ]  # fmt: skip
    if experimental.on("push_relay", runtime.enabled):
        out.append(
            _entry("notifications.relay", "push_relay", "notifications", "Push relay",
                   "on" if push.relay_configured() else "not_set_up", provider="relay",
                   data_out=rules.data_out("notifications.relay"))
        )  # fmt: skip
    direct: list[rules.Action] = ["set_key", "delete_key"] if apns_key else ["set_key"]
    out.append(
        _entry("notifications.direct", "apns", "notifications", "Apple push (direct)",
               "on" if push.configured() and apns_key else "not_set_up", provider="apns",
               data_out=rules.data_out("notifications.direct"), actions=direct)
    )  # fmt: skip
    return out


async def overview(runtime: "Runtime") -> dict[str, Any]:
    """The six categories in order, each with its entries; and the hard monthly limit no paid cap may pass."""
    ai, providers = await _ai(runtime)
    by: dict[rules.Category, list[Entry]] = {c: [] for c in rules.CATEGORIES}
    by["ai"] = ai
    by["voice"] = _voice(runtime, providers)
    if experimental.on("places", runtime.enabled):
        by["maps"], by["web"] = await _places(runtime)
    if experimental.on("google", runtime.enabled):
        by["accounts"] = await _accounts(runtime)
    by["notifications"] = await _notifications(runtime)
    async with runtime.pool.connection() as conn:
        lang = str((await runtime.owner.settings(conn))["language"])
    for entries in by.values():
        for entry in entries:  # the guided setup, in their language
            entry["setup"] = rules.setup_steps(entry["id"], lang)
            entry["free_tier"] = rules.free_tier(entry["id"], lang)
    return {
        "categories": [
            {"id": c, "name": rules.CATEGORY_NAMES[c], "entries": by[c]} for c in rules.CATEGORIES
        ],
        "hard_limit_monthly_cents": runtime.place_providers.hard_monthly_cents(),
    }
