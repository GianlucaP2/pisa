"""Model gateway administration for the app's Settings screens: providers and keys, slots,
models with prices, spend and caps."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain.models.catalog import (
    CatalogRow,
    all_models,
    catalog_refreshed_at,
    fetch_openrouter_models,
    get_model,
    refresh_catalog,
)
from pisa.brain.models.compat import (
    Server,
    create_server,
    delete_server,
    get_caps,
    http_for,
    list_servers,
    probe_model,
    probe_unprobed,
    resolve_provider,
    save_overrides,
    save_probe,
    update_server,
)
from pisa.brain.models.pricing import most_expensive_price
from pisa.brain.models.providers import (
    PROVIDERS,
    ProviderCallError,
    ProviderSpec,
    check_key_endpoint,
    get_provider,
    list_models,
)
from pisa.brain.models.slots import SlotState, all_slots, slot_spec, update_slot
from pisa.db.settings import get_setting, set_setting
from pisa.domain import disclosure as disclosure_rules
from pisa.domain.ai import MTOK, ModelRef, missing_capabilities
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import ModelCaps, features_needing_tools
from pisa.domain.owner import Owner
from pisa.infra import hard_limit, ledger
from pisa.infra.fx import refresh_usd_to_eur, usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.speech import SpeechError, SpeechProvider
from pisa.services import disclosure

_log = get_logger("pisa.services.ai")


@dataclass(frozen=True)
class TestResult:
    ok: bool | None  # None: the provider offers no free way to check a key ("can't verify")
    message: str
    models_found: int


def _money(value: Decimal | None, places: str = "0.0001") -> float | None:
    return None if value is None else float(value.quantize(Decimal(places)))


def slot_view(state: SlotState) -> dict[str, Any]:
    return {
        "id": state.spec.id,
        "name": state.spec.name,
        "description": state.spec.description,
        "needs": list(state.spec.needs),
        "model": state.model,
        "fallback": state.fallback,
    }


def server_view(server: Server, key_last4: str | None, last_test: Any) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "type": "openai_compatible",
        "key_set": key_last4 is not None,
        "key_last4": key_last4,
        "last_test": last_test if isinstance(last_test, dict) else None,
        "keys_url": None,
        "base_url": server.base_url,
        "ca_bundle": server.ca_bundle,
        "editable": server.source == "user",
    }


def capabilities_view(model_id: str, caps: ModelCaps) -> dict[str, Any]:
    """GET /v1/ai/providers/{id}/capabilities: the effective values, the user's overrides apart."""
    return {
        "model": model_id,
        "probed_at": caps.probed_at.isoformat() if caps.probed_at else None,
        **caps.effective(),
        "overrides": dict(caps.overrides),
        "features_needing_tools": features_needing_tools(),
    }


class AiService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        http: httpx.AsyncClient,
        *,
        speech: Mapping[str, SpeechProvider],
        owner: Owner,
        ollama_base_url: str | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hard_limits: HardLimits = hard_limit.DEFAULT,
        locked: bool = False,
    ) -> None:
        self._pool = pool
        self._keys = keys
        self._http = http
        # App Store review mode: the providers are the operator's (their keys, set on the server with the
        # CLI); the app changes no key and adds no server, so the user's words go nowhere else, and it picks
        # no slot's model, no capability override and no cap (the operator's, with the CLI)
        self._locked = locked
        self._speech = speech  # by provider id: a speech-only provider's key is checked by its adapter
        self._owner = owner
        self._ollama = ollama_base_url
        self._clock = clock
        self._hard = hard_limits
        self._background: set[asyncio.Task[None]] = set()

    # --- providers and keys -------------------------------------------------------------------------------

    async def providers(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            statuses = await self._keys.statuses(conn)
            servers = await list_servers(conn, self._ollama)
            tests = {s.id: await get_setting(conn, f"provider_test:{s.id}") for s in servers}
        out: list[dict[str, Any]] = []
        for spec in PROVIDERS:
            status = statuses.get(spec.id)
            last_test: dict[str, Any] | None = None
            if status is not None and status.last_test_at is not None:
                last_test = {
                    "ok": status.last_test_ok,
                    "at": status.last_test_at.isoformat(),
                    "message": status.last_test_message or "",
                }
            out.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "type": "builtin",
                    "key_set": status is not None,
                    "key_last4": status.last4 if status is not None else None,
                    "last_test": last_test,
                    "keys_url": spec.keys_url,
                    "base_url": None,
                    "ca_bundle": None,
                    "editable": False,
                }
            )
        for server in servers:
            status = statuses.get(server.id)
            test = tests.get(server.id)
            out.append(server_view(server, status.last4 if status is not None else None, test))
        return out

    async def _spec(self, provider_id: str) -> ProviderSpec:
        async with self._pool.connection() as conn:
            spec = await resolve_provider(conn, provider_id, self._ollama)
        if spec is None:
            get_provider(provider_id)  # raises not_found, naming the built-in providers
            raise AssertionError("unreachable")
        return spec

    def _unlocked(self) -> None:
        if self._locked:
            raise PisaError(
                "connector_off",
                "This is a demo server (App Store review mode): its AI provider, models and caps are set by "
                "its operator.",
                "Use the AI settings this server already has",
            )

    async def set_key(self, provider_id: str, key: str) -> None:
        self._unlocked()
        spec = await self._spec(provider_id)
        async with self._pool.connection() as conn:
            await self._keys.set(conn, spec.id, key)
            if spec.id in disclosure_rules.VOICE_PROVIDERS:  # its key makes the server's voice speak
                await disclosure.changed(conn)
        _log.info("provider_key_set", provider=spec.id)
        self.refresh_in_background()

    async def delete_key(self, provider_id: str) -> None:
        self._unlocked()
        spec = await self._spec(provider_id)
        async with self._pool.connection() as conn:
            await self._keys.delete(conn, spec.id)
            if spec.id in disclosure_rules.VOICE_PROVIDERS:  # without its key the phone speaks
                await disclosure.changed(conn)
        _log.info("provider_key_deleted", provider=spec.id)

    async def _key(self, spec: ProviderSpec) -> str | None:
        """The provider's key: a server's is optional."""
        async with self._pool.connection() as conn:
            key = await self._keys.get(conn, spec.id) if spec.needs_key or spec.compatible else None
        if spec.needs_key and not key:
            raise PisaError(
                "provider_key_missing",
                f"No {spec.name} key is set.",
                f"Add the {spec.name} key in Settings → AI providers",
            )
        return key

    async def test(self, provider_id: str) -> TestResult:
        spec = await self._spec(provider_id)
        key = await self._key(spec)
        result = await self._check_key(spec, key)
        async with self._pool.connection() as conn:
            if spec.needs_key:
                await self._keys.record_test(conn, spec.id, result.ok, result.message)
            else:
                await set_setting(
                    conn,
                    f"provider_test:{spec.id}",
                    Jsonb({"ok": result.ok, "at": self._clock().isoformat(), "message": result.message}),
                )
        _log.info("provider_key_tested", provider=spec.id, ok=result.ok, models=result.models_found)
        if result.ok:
            self.refresh_in_background()
        return result

    async def _check_key(self, spec: ProviderSpec, key: str | None) -> TestResult:
        if spec.speech_only:  # its speech adapter's account endpoint answers for the key
            assert key is not None
            try:
                return TestResult(True, await self._speech[spec.id].check_key(key), 0)
            except SpeechError as err:
                return TestResult(False, err.message, 0)
        if spec.models_path is None:
            return TestResult(
                None,
                f"{spec.name} offers no free way to check a key, so Pisa can't verify it; "
                "it will be checked on the first call.",
                0,
            )
        try:
            if spec.aggregator:
                assert key is not None
                await check_key_endpoint(spec, key, self._http)
                found = len(await fetch_openrouter_models(self._http, key))
            else:
                found = len(await list_models(spec, key, http_for(spec, self._http)))
        except ProviderCallError as exc:
            return TestResult(False, exc.message, 0)
        except httpx.HTTPError as exc:
            return TestResult(False, f"The {spec.name} model list failed ({exc.__class__.__name__}).", 0)
        return TestResult(True, f"{found} models are available.", found)

    # --- OpenAI-compatible servers ------------------------------------------------------------------------

    async def create_server(
        self, server_id: str, name: str | None, base_url: str, ca_bundle: str | None
    ) -> dict[str, Any]:
        self._unlocked()
        async with self._pool.connection() as conn:
            server = await create_server(
                conn, server_id, name or server_id, base_url, ca_bundle, self._ollama
            )
            await disclosure.changed(conn)
        _log.info("model_server_added", provider=server.id)
        self.refresh_in_background()
        return server_view(server, None, None)

    async def update_server(self, server_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self._unlocked()
        async with self._pool.connection() as conn:
            server = await update_server(
                conn,
                server_id,
                self._ollama,
                name=changes.get("name"),
                base_url=changes.get("base_url"),
                ca_bundle=changes.get("ca_bundle"),
                clear_ca_bundle="ca_bundle" in changes and changes["ca_bundle"] is None,
            )
            status = (await self._keys.statuses(conn)).get(server.id)
            test = await get_setting(conn, f"provider_test:{server.id}")
            await disclosure.changed(conn)
        _log.info("model_server_changed", provider=server.id, fields=sorted(changes))
        self.refresh_in_background()
        return server_view(server, status.last4 if status is not None else None, test)

    async def delete_server(self, server_id: str) -> None:
        self._unlocked()
        async with self._pool.connection() as conn:
            await delete_server(conn, server_id, self._ollama)
            await disclosure.changed(conn)
        _log.info("model_server_deleted", provider=server_id)

    async def _server_spec(self, provider_id: str) -> ProviderSpec:
        spec = await self._spec(provider_id)
        if not spec.compatible:
            raise PisaError(
                "validation_failed",
                f"{spec.name} is a built-in provider: only an OpenAI-compatible server's models are probed.",
                "See the built-in providers' capabilities in GET /v1/ai/models",
            )
        return spec

    async def _model_id(self, spec: ProviderSpec, model: str | None) -> str:
        """The model asked for, or the server's only model when none is named."""
        if model:
            return model
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT model_id FROM model_catalog WHERE provider = %s ORDER BY model_id LIMIT 2",
                    (spec.id,),
                )
            ).fetchall()
        if len(rows) != 1:
            served = "several models" if rows else "no model Pisa knows yet"
            raise PisaError(
                "validation_failed",
                f"{spec.name} serves {served}, so the request must name one.",
                "Add ?model=<model id> (the ids are in GET /v1/ai/models)",
            )
        return rows[0][0]

    async def capabilities(self, provider_id: str, model: str | None) -> dict[str, Any]:
        spec = await self._server_spec(provider_id)
        model_id = await self._model_id(spec, model)
        async with self._pool.connection() as conn:
            caps = await get_caps(conn, spec.id, model_id)
        return capabilities_view(model_id, caps)

    async def probe(self, provider_id: str, model: str | None) -> dict[str, Any]:
        """One tiny real request per capability (see compat.probe_model): a minute or more on a CPU server."""
        spec = await self._server_spec(provider_id)
        model_id = await self._model_id(spec, model)
        key = await self._key(spec)
        http = http_for(spec, self._http)
        try:
            await list_models(spec, key, http)  # reachable, and the key accepted, before seven requests
        except ProviderCallError as exc:
            raise PisaError(
                "provider_error", exc.message, "Check the server and its address in Settings → AI providers"
            ) from None
        result = await probe_model(spec, key, model_id, http)
        async with self._pool.connection() as conn:
            caps = await save_probe(conn, spec.id, model_id, result.caps, self._clock())
        return capabilities_view(model_id, caps)

    async def set_overrides(
        self, provider_id: str, model: str | None, overrides: dict[str, bool]
    ) -> dict[str, Any]:
        self._unlocked()
        spec = await self._server_spec(provider_id)
        model_id = await self._model_id(spec, model)
        async with self._pool.connection() as conn:
            caps = await save_overrides(conn, spec.id, model_id, overrides, self._clock())
        _log.info("model_overrides_set", provider=spec.id, model=model_id, overrides=caps.overrides)
        return capabilities_view(model_id, caps)

    # --- catalog ------------------------------------------------------------------------------------------

    async def refresh(self) -> None:
        async with self._pool.connection() as conn:
            await refresh_usd_to_eur(conn, self._http)
            await refresh_catalog(
                conn, self._http, self._keys, ollama_base_url=self._ollama, now=self._clock()
            )

    def refresh_in_background(self) -> None:
        async def run() -> None:
            try:
                async with self._pool.connection() as conn:
                    await refresh_catalog(
                        conn, self._http, self._keys, ollama_base_url=self._ollama, now=self._clock()
                    )
            except Exception as exc:
                _log.error("catalog_refresh_failed", cause=f"{exc.__class__.__name__}: {exc}")

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def wait_background(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def drain(self, within_s: float = 5.0) -> None:
        """Before the HTTP client closes (a CLI command's end): the background refreshes finish within
        `within_s` seconds, else they are cancelled (the next refresh catches up), so none runs on a closed
        client."""
        pending = set(self._background)
        if not pending:
            return
        _, late = await asyncio.wait(pending, timeout=within_s)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)

    # --- slots and models ---------------------------------------------------------------------------------

    async def slots(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return [slot_view(s) for s in await all_slots(conn)]

    async def update_slot(self, slot_id: str, model: str, fallback: str | None) -> dict[str, Any]:
        """Set the slot's model and fallback. A model on an OpenAI-compatible server that was never probed is
        probed first, once; a server that can't be reached doesn't stop the change (the model's first use
        probes it again). `probes` reports each probe run here. Each model must be on a provider whose key was
        set (signed `provider_key`, or on the server: `pisa setup`, `pisa secrets set`) or on a model server
        that was added (signed `model_server`, `pisa setup`, or the settings' Ollama): a slot never sends the
        user's words anywhere else.
        Every connected device is told the new disclosure, from any process (`disclosure.changed`)."""
        self._unlocked()
        slot_spec(slot_id)  # an unknown slot fails before any probe
        await self._check_added(model, "model")
        if fallback:
            await self._check_added(fallback, "fallback")
        probes: list[dict[str, Any]] = []
        for ref in (model, fallback):
            probe = await self._probe_for_slot(ref) if ref else None
            if probe is not None:
                probes.append(probe)
        async with self._pool.connection() as conn:
            state = await update_slot(conn, slot_id, model, fallback)
            await disclosure.changed(conn)
        _log.info("slot_updated", slot=slot_id, model=model, fallback=fallback)
        return {**slot_view(state), "probes": probes}

    async def _check_added(self, ref_text: str, role: str) -> None:
        """400 `validation_failed` unless `ref_text` names a built-in provider with a key, or a model server
        Pisa knows (added by a signed `model_server` or on the server; the settings' Ollama)."""
        try:
            ref = ModelRef.parse(ref_text)
        except PisaError:
            return  # update_slot names what is wrong with it
        async with self._pool.connection() as conn:
            spec = await resolve_provider(conn, ref.provider, self._ollama)
            keyed = spec is not None and (not spec.needs_key or spec.id in await self._keys.statuses(conn))
        if spec is None:
            raise PisaError(
                "validation_failed",
                f"The {role} {ref_text!r} names an unknown provider {ref.provider!r}.",
                "Add the model server first in Settings → AI providers",
            )
        if not keyed:
            raise PisaError(
                "validation_failed",
                f"The {role} {ref_text!r} is on {spec.name}, which has no key set.",
                f"Add the {spec.name} key first in Settings → AI providers",
            )

    async def _probe_for_slot(self, ref_text: str) -> dict[str, Any] | None:
        """The probe of a never-probed model on an OpenAI-compatible server, as `update_slot` reports it; None
        when there was nothing to probe (a built-in provider, a model probed before, a model not in the
        catalog)."""
        try:
            ref = ModelRef.parse(ref_text)
        except PisaError:
            return None  # update_slot names what is wrong with it
        async with self._pool.connection() as conn:
            spec = await resolve_provider(conn, ref.provider, self._ollama)
            if spec is None or not spec.compatible or await get_model(conn, str(ref)) is None:
                return None
            if (await get_caps(conn, spec.id, ref.model_id)).probed_at is not None:
                return None
        found = await probe_unprobed(self._pool, self._keys, spec, ref.model_id, self._http, self._clock)
        if found.unreachable is not None:
            message = (
                f"{spec.name} could not be reached to probe {ref} ({found.unreachable.rstrip('.')}), so what "
                "it can do is not known yet. Its first use probes it again, or probe it by hand once the "
                f"server is up: `pisa models probe {ref}`, or Probe in Settings → AI providers."
            )
        else:
            have = found.caps.capabilities()
            message = f"Probed {ref}: " + (", ".join(have) if have else "no capability Pisa uses") + "."
        _log.info("slot_model_probed", model=str(ref), reached=found.unreachable is None)
        return {
            "model": str(ref),
            "probed": found.unreachable is None,
            "capabilities": list(found.caps.capabilities()),
            "message": message,
        }

    async def models(self, slot_id: str | None) -> dict[str, Any]:
        spec = slot_spec(slot_id) if slot_id else None
        async with self._pool.connection() as conn:
            refreshed = await catalog_refreshed_at(conn)
            fx = await usd_to_eur(conn)
            rows = await all_models(conn)
            keyed = set((await self._keys.statuses(conn)).keys())
            keyed |= {s.id for s in await list_servers(conn, self._ollama)}  # a server's key is optional
            usage = await ledger.slot_daily_usage(conn, spec.id, self._clock()) if spec else None
            worst: dict[str, tuple[Decimal, Decimal] | None] = {}
            if usage is not None:
                for provider in {r.provider for r in rows if not r.price_known}:
                    worst[provider] = await most_expensive_price(conn, provider)

        def key_set(row: CatalogRow) -> bool:
            return row.provider in keyed

        def estimate(row: CatalogRow) -> float | None:
            if usage is None:
                return None
            prices = (
                (row.input_usd_per_mtok, row.output_usd_per_mtok)
                if row.price_known
                else worst.get(row.provider)
            )
            if prices is None or prices[0] is None or prices[1] is None:
                return None
            monthly_usd = (usage[0] * 30 * prices[0] + usage[1] * 30 * prices[1]) / MTOK
            return _money(monthly_usd * fx, "0.01")

        models = []
        for row in rows:
            known = row.price_known
            models.append(
                {
                    "ref": row.ref,
                    "provider": row.provider,
                    "model_id": row.model_id,
                    "name": row.name,
                    "capabilities": list(row.capabilities),
                    "context_tokens": row.context_tokens,
                    "price_eur_per_mtok": {
                        "input": _money(row.input_usd_per_mtok * fx),  # type: ignore[operator]
                        "output": _money(row.output_usd_per_mtok * fx),  # type: ignore[operator]
                    }
                    if known
                    else None,
                    "price_known": known,
                    "key_set": key_set(row),
                    "compatible": not missing_capabilities(spec.needs, row.capabilities) if spec else True,
                    "est_monthly_eur_for_slot": estimate(row) if spec else None,
                    "_output_usd": row.output_usd_per_mtok,
                }
            )
        if spec:
            models.sort(
                key=lambda m: (
                    not m["compatible"],
                    m["_output_usd"] is None,
                    m["_output_usd"] if m["_output_usd"] is not None else Decimal(0),
                    m["ref"],
                )
            )
        for m in models:
            del m["_output_usd"]
        return {
            "catalog_refreshed_at": refreshed.isoformat() if refreshed else None,
            "usd_to_eur": float(fx),
            "models": models,
        }

    # --- spend and caps -----------------------------------------------------------------------------------

    async def spend(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            spend = await ledger.spend_summary(conn, self._clock(), self._owner.tz)
        return {
            "today_eur": _money(spend.today_eur),
            "month_eur": _money(spend.month_eur),
            "caps": {"daily_eur": float(spend.caps.daily_eur), "monthly_eur": float(spend.caps.monthly_eur)},
            "by_slot": [
                {
                    "slot": s.slot,
                    "today_eur": _money(s.today_eur),
                    "month_eur": _money(s.month_eur),
                    "calls_today": s.calls_today,
                }
                for s in spend.by_slot
            ],
        }

    async def set_caps(self, daily_eur: Decimal, monthly_eur: Decimal) -> dict[str, float]:
        self._unlocked()
        if daily_eur > monthly_eur:
            raise PisaError(
                "validation_failed",
                f"The daily cap ({ledger.eur(daily_eur)}) can't be higher than the monthly cap "
                f"({ledger.eur(monthly_eur)}).",
                "Lower the daily cap or raise the monthly cap",
            )
        hard = self._hard
        if daily_eur > hard.daily_eur or monthly_eur > hard.monthly_eur:
            raise PisaError(
                "validation_failed",
                f"The AI caps can't be above Pisa's hard limit ({ledger.eur(hard.daily_eur)} a day, "
                f"{ledger.eur(hard.monthly_eur)} a month, for every paid service together).",
                "Choose lower caps; only the server's operator can raise the hard limit",
            )
        async with self._pool.connection() as conn:
            await ledger.set_caps(conn, ledger.Caps(daily_eur, monthly_eur))
        _log.info("spend_caps_set", daily_eur=str(daily_eur), monthly_eur=str(monthly_eur))
        return {"daily_eur": float(daily_eur), "monthly_eur": float(monthly_eur)}
