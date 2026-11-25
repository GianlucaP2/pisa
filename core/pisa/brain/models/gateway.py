"""The model gateway: every AI call goes through `ModelGateway.run`.

resolve the slot's model → load only that provider's key → reserve the estimated cost (caps) → call →
settle the real cost. On provider_error or rate_limited, the slot's fallback model is tried once.

A model on an OpenAI-compatible server is checked against what the use needs before the call. When it lacks
a capability and was never probed (a model served before Pisa probed on assignment), it is probed once and
the check runs once more; a model probed before keeps its answer.
"""

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
from psycopg_pool import AsyncConnectionPool
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, ModelRequestTimeout, UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage

from pisa.brain.models.compat import (
    cut_of,
    for_slot,
    get_caps,
    probe_unprobed,
    read_timeout,
    resolve_provider,
    save_truncation,
)
from pisa.brain.models.pricing import call_cost_usd
from pisa.brain.models.providers import ModelFactory, ProviderSpec, build_model
from pisa.brain.models.slots import get_slot
from pisa.domain.ai import CHARS_PER_TOKEN, SLOTS_BY_ID, Capability, ModelRef
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import NEED_FIXES, NEED_WORDS, ModelCaps
from pisa.domain.owner import Owner
from pisa.failures import external_failure, redact
from pisa.infra import hard_limit, ledger
from pisa.infra.fx import usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.observability import gateway_span

TOOL_ROUND_TRIP_FACTOR = 2
_log = get_logger("pisa.models.gateway")


@dataclass
class CallUsage:
    input_tokens: int | None = (
        None  # all input tokens, cached ones included (Pydantic AI and genai-prices agree)
    )
    output_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def update(self, usage: RunUsage) -> None:
        self.input_tokens, self.output_tokens = usage.input_tokens, usage.output_tokens
        self.cache_read_tokens, self.cache_write_tokens = usage.cache_read_tokens, usage.cache_write_tokens


@dataclass
class CallContext:
    """What a call function gets: the model to use. It must fill `usage` with what the provider reported."""

    slot: str
    ref: ModelRef
    provider: ProviderSpec
    model: Model
    trace_id: str
    usage: CallUsage = field(default_factory=CallUsage)
    # Reserve one more model request of this many prompt characters (a long tool turn). Raises
    # PisaError budget_exceeded when the AI caps don't allow it.
    grow: Callable[[int], Awaitable[None]] | None = None


# Why a model's answer couldn't be used, as a word for the logs (without it, a failure has no cause logged:
# a reasoning model can spend the whole small output limit thinking).
UNUSABLE = {
    "token_limit": "it used its whole output limit before answering",
    "content_filter": "its content filter stopped the answer",
    "empty": "the answer was empty",
    "invalid_output": "the answer didn't match what Pisa asked for",
}


def unusable(exc: UnexpectedModelBehavior) -> str:
    text = str(exc.message).lower()
    if "token limit" in text:
        return "token_limit"
    if "content filter" in text:
        return "content_filter"
    if "empty" in text:
        return "empty"
    return "invalid_output"


# A model that can't serve the use leaves it to the slot's fallback, as a failing provider does.
_FALLBACK_CODES = ("provider_error", "rate_limited", "model_capability_missing")


def check_capabilities(slot: str, ref: ModelRef, spec: ProviderSpec, caps: ModelCaps, tools: bool) -> None:
    """A model on an OpenAI-compatible server is checked against what the use needs before any call: a
    missing capability fails here, naming the capability and the model, instead of as the server's error."""
    needs: list[Capability] = list(SLOTS_BY_ID[slot].needs) if slot in SLOTS_BY_ID else []
    if tools and "tools" not in needs:
        needs.insert(0, "tools")
    have = caps.capabilities()
    missing = next((n for n in needs if n not in have), None)
    if missing is None:
        return
    feature = SLOTS_BY_ID[slot].phrase if slot in SLOTS_BY_ID else slot
    probed = "" if caps.probed_at else " (the model was never probed)"
    raise PisaError(
        "model_capability_missing",
        f"{ref} on {spec.name} can't be used for {feature}: it lacks {NEED_WORDS[missing]}{probed}.",
        "Probe the model in Settings → AI providers, or choose another model in Settings → AI models"
        if caps.probed_at is None
        else NEED_FIXES.get(missing, "Choose another model in Settings → AI models"),
        extra={"capability": missing, "model": str(ref)},
    )


def unreachable_for_probe(err: PisaError, ref: ModelRef, cause: str) -> PisaError:
    """The capability error of a never-probed model whose server could not be reached to probe it: the same
    error, saying so and how to probe the model by hand."""
    message = err.message.removesuffix(".").removesuffix(" (the model was never probed)")
    return PisaError(
        "model_capability_missing",
        f"{message}. The model was never probed, and the server could not be reached for probing: "
        f"{cause.rstrip('.')}.",
        f"Probe the model once the server is up: `pisa models probe {ref}` on Pisa's server, or Probe in "
        "Settings → AI providers",
        extra=err.extra,
    )


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def _error_body(body: Any) -> dict[str, Any]:
    """The error object of a server's JSON error (`{"error": {…}}`, or the object itself)."""
    if isinstance(body, dict):
        inner = body.get("error")
        return cast(dict[str, Any], inner if isinstance(inner, dict) else body)
    return {}


def _timed_out(exc: Exception) -> bool:
    """A request that got no byte within its read timeout: the SDK's timeout under Pydantic AI's error, or
    httpx's own when a stream stalls mid-way."""
    from openai import APITimeoutError

    return isinstance(exc, httpx.TimeoutException) or isinstance(exc.__cause__, APITimeoutError)


def own_server_error(exc: Exception, spec: ProviderSpec) -> PisaError | None:
    """What an own server's failure means, from its body where it says (never the user's words): llama.cpp's
    context overflow (`exceed_context_size_error`, with the prompt's tokens and the context), its refusal of
    tools without `--jinja`, and a read timeout (not "unreachable": the server is up, still reading)."""
    name = spec.name
    if _timed_out(exc):
        seconds = read_timeout(spec)
        return PisaError(
            "provider_error",
            f"{name} did not answer within {seconds:g} s.",
            "A CPU can take minutes to read a long prompt: raise the server's read timeout (`pisa models "
            f"server {spec.id} --read-timeout <seconds>`, or Settings → AI providers), or use a smaller "
            "model",
            detail="timeout",
        )
    if not isinstance(exc, ModelHTTPError):
        return None
    body = _error_body(exc.body)
    message = str(body.get("message") or "")
    if body.get("type") == "exceed_context_size_error":
        n_ctx, n_prompt = body.get("n_ctx"), body.get("n_prompt_tokens")
        sizes = (
            f": the request holds {n_prompt} tokens and its context is {n_ctx}"
            if isinstance(n_ctx, int) and isinstance(n_prompt, int)
            else ""
        )
        return PisaError(
            "provider_error",
            f"{name} refused the request as longer than its context{sizes} (HTTP {exc.status_code}).",
            "Start the server with a larger context (llama.cpp: --ctx-size), or choose a model with more "
            "context in Settings → AI models",
            detail="context_exceeded",
        )
    if "--jinja" in message:
        return PisaError(
            "provider_error",
            f"{name} refused tool calling: llama.cpp's server needs --jinja for tools "
            f"(HTTP {exc.status_code}).",
            "Restart llama.cpp's server with --jinja, then probe the model again",
            detail="needs_jinja",
        )
    return None


def map_provider_error(exc: Exception, spec: ProviderSpec, ref: ModelRef, secret: str | None) -> PisaError:
    """Turn a Pydantic AI / SDK failure into a protocol error naming the provider and the HTTP status."""
    name = spec.name
    if spec.compatible and (own := own_server_error(exc, spec)) is not None:
        return own
    if isinstance(exc, httpx.TimeoutException):
        return PisaError("provider_error", f"{name} did not answer in time.", "Try again", detail="timeout")
    if isinstance(exc, ModelHTTPError):
        status = exc.status_code
        if status == 429:
            retry_after = (exc.headers or {}).get("retry-after")
            wait = (
                f"Try again in {retry_after} s"
                if retry_after and retry_after.isdigit()
                else "Try again in a minute"
            )
            return PisaError("rate_limited", f"{name} throttled the call (HTTP 429).", wait)
        if status in (401, 403):
            return PisaError(
                "provider_error",
                f"{name} rejected the key (HTTP {status}).",
                f"Check the {name} key in Settings → AI providers",
            )
        if status == 402:
            return PisaError(
                "provider_error",
                f"{name} refused the call: no credit is left (HTTP 402).",
                f"Add credit to your {name} account",
            )
        if status == 404:
            return PisaError(
                "provider_error",
                f"{name} doesn't know the model {ref.model_id!r} (HTTP 404).",
                "Choose another model in Settings → AI models",
            )
        if status >= 500:
            return PisaError("provider_error", f"{name} failed (HTTP {status}).", "Try again")
        return PisaError(
            "provider_error",
            f"{name} rejected the request (HTTP {status}).",
            "Try again; if it repeats, choose another model in Settings → AI models",
        )
    if isinstance(exc, ModelRequestTimeout):
        return PisaError("provider_error", f"{name} did not answer within {exc.timeout:g} s.", "Try again")
    if isinstance(exc, ModelAPIError):
        detail = redact(str(exc), secret)[:200]
        return PisaError(
            "provider_error", f"{name} could not be reached or failed ({detail.rstrip('.')}).", "Try again"
        )
    if isinstance(exc, UnexpectedModelBehavior):
        why = unusable(exc)
        return PisaError(
            "provider_error",
            f"{name} returned a response Pisa could not use ({UNUSABLE[why]}).",
            "Try again",
            detail=why,
        )
    return PisaError(
        "internal",
        f"An unexpected error happened while calling {name} ({exc.__class__.__name__}).",
        "Try again",
    )


class ModelGateway:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        *,
        owner: Owner,
        ollama_base_url: str | None = None,
        model_factory: ModelFactory = build_model,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        http: httpx.AsyncClient | None = None,
        hard_limits: HardLimits = hard_limit.DEFAULT,
    ) -> None:
        """`http`: the client a never-probed model is probed with on its first missing capability; without
        one, the capability error stands."""
        self._pool = pool
        self._keys = keys
        self._owner = owner
        self._ollama = ollama_base_url
        self._factory = model_factory
        self._clock = clock
        self._http = http
        self._hard = hard_limits

    async def run[T](
        self,
        slot: str,
        call: Callable[[CallContext], Awaitable[T]],
        *,
        prompt_chars: int,
        max_output_tokens: int,
        trace_id: str | None = None,
        tools: bool = False,
        can_fallback: Callable[[], bool] = lambda: True,
        cheap: bool = False,
        bill_reserve_on_cancel: bool = False,
        ledger_slot: str | None = None,
    ) -> T:
        """`ledger_slot`: the slot the ledger records and caps the call under, when it differs
        from the slot whose model it uses (Pisa Learn's checks: the cheapest slot's model, billed
        `learn.check`). `tools=True` reserves for one tool round-trip: twice the output estimate. `cheap`: the
        slot's fallback model first (budget guard), when the slot has one. `bill_reserve_on_cancel`: a call
        the caller cuts (a budget it times out on) settles at its reserved output, not at the 0 tokens
        reported so far."""
        bill = bill_reserve_on_cancel
        trace_id = trace_id or uuid.uuid4().hex
        if tools:
            max_output_tokens *= TOOL_ROUND_TRIP_FACTOR
        async with self._pool.connection() as conn:
            state = await get_slot(conn, slot)
        if state.model is None:
            raise PisaError(
                "model_not_configured",
                f"No model is chosen for {state.spec.phrase} ({slot}).",
                f"Choose a model for {slot} in Settings → AI models, or with `pisa models set {slot} "
                "<provider:model>` on the server",
            )
        if cheap and state.fallback:
            return await self._attempt(
                slot,
                state.fallback,
                call,
                prompt_chars,
                max_output_tokens,
                trace_id,
                bill,
                ledger_slot,
                tools,
            )
        try:
            return await self._attempt(
                slot, state.model, call, prompt_chars, max_output_tokens, trace_id, bill, ledger_slot, tools
            )
        except PisaError as err:
            if err.code not in _FALLBACK_CODES or not state.fallback or not can_fallback():
                raise
            _log.warning(
                "model_fallback", slot=slot, model=state.model, fallback=state.fallback, code=err.code
            )
            return await self._attempt(
                slot,
                state.fallback,
                call,
                prompt_chars,
                max_output_tokens,
                trace_id,
                bill,
                ledger_slot,
                tools,
            )

    async def estimate_eur(
        self,
        slot: str,
        *,
        prompt_chars: int,
        max_output_tokens: int,
        tools: bool = False,
        fallback: bool = False,
    ) -> Decimal | None:
        """What one call on the slot's model (or its fallback) is reserved at, as `run` reserves it; None
        when there is no such model. An unknown price counts as the provider's dearest (as reserved)."""
        if tools:
            max_output_tokens *= TOOL_ROUND_TRIP_FACTOR
        async with self._pool.connection() as conn:
            state = await get_slot(conn, slot)
            ref_text = state.fallback if fallback else state.model
            if ref_text is None:
                return None
            ref = ModelRef.parse(ref_text)
            spec = await resolve_provider(conn, ref.provider, self._ollama)
            if spec is None:
                return None
            fx = await usd_to_eur(conn)
            estimate = await call_cost_usd(conn, spec, ref, estimate_tokens(prompt_chars), max_output_tokens)
        return estimate.usd * fx

    async def _attempt[T](
        self,
        slot: str,
        ref_text: str,
        call: Callable[[CallContext], Awaitable[T]],
        prompt_chars: int,
        max_output_tokens: int,
        trace_id: str,
        bill_reserve_on_cancel: bool = False,
        ledger_slot: str | None = None,
        tools: bool = False,
    ) -> T:
        billed = ledger_slot or slot
        ref = ModelRef.parse(ref_text)
        prompt_tokens = estimate_tokens(prompt_chars)
        async with self._pool.connection() as conn:
            spec = await resolve_provider(conn, ref.provider, self._ollama)
            if spec is None:
                raise PisaError(
                    "model_not_configured",
                    f"The slot {slot} uses {ref}, but {ref.provider!r} is not a known provider.",
                    "Choose a model in Settings → AI models",
                )
            caps = await get_caps(conn, spec.id, ref.model_id) if spec.compatible else None
        if caps is not None:
            caps = await self._checked_caps(slot, ref, spec, caps, tools)
        # The row's id before the reservation: a cancel that lands after its commit and before the call starts
        # (the commit's answer on its way) settles it as cancelled, never leaves it reserved (swept later at
        # its whole reserve). Nothing was sent: nothing is billed. Settling a reservation that was never
        # committed changes nothing.
        call_id = uuid.uuid4()
        reserving: Decimal | None = None  # the exchange rate, once the reservation is under way
        try:
            async with self._pool.connection() as conn:
                secret = await self._keys.get(conn, spec.id) if spec.needs_key or spec.compatible else None
                if spec.needs_key and not secret:
                    feature = SLOTS_BY_ID[slot].phrase if slot in SLOTS_BY_ID else slot
                    raise PisaError(
                        "provider_key_missing",
                        f"No {spec.name} API key is set, and {feature} uses {ref}.",
                        f"Add the {spec.name} key in Settings → AI providers",
                    )
                # Build the client before reserving, so a failure here leaves no reservation behind.
                model = self._factory(spec, ref.model_id, secret, caps)
                if spec.compatible:
                    for_slot(model, slot, spec)  # the slot's thinking choice on this server
                fx = await usd_to_eur(conn)
                estimate = await call_cost_usd(conn, spec, ref, prompt_tokens, max_output_tokens)
                reserving = fx
                await ledger.reserve(
                    conn,
                    slot=billed,
                    model_ref=str(ref),
                    estimate_eur=(estimate.usd * fx).quantize(Decimal("0.000001")),
                    price_known=estimate.known,
                    trace_id=trace_id,
                    hard=self._hard,
                    now=self._clock(),
                    tz=self._owner.tz,
                    call_id=call_id,
                )
        except asyncio.CancelledError:
            if reserving is not None:
                unsent = CallUsage(input_tokens=0, output_tokens=0)
                await asyncio.shield(self._settle(call_id, spec, ref, unsent, 0, reserving, 0, "cancelled"))
            raise
        if not estimate.known:
            _log.warning(
                "model_price_unknown", model=str(ref), priced_as="provider's most expensive known price"
            )
        ctx = CallContext(slot, ref, spec, model, trace_id)
        per_request_out = max_output_tokens // TOOL_ROUND_TRIP_FACTOR or max_output_tokens

        async def grow(prompt_chars_more: int) -> None:
            async with self._pool.connection() as conn:
                more = await call_cost_usd(
                    conn, spec, ref, estimate_tokens(prompt_chars_more), per_request_out
                )
                await ledger.grow(
                    conn, call_id, extra_eur=(more.usd * fx).quantize(Decimal("0.000001")), slot=billed,
                    now=self._clock(), tz=self._owner.tz, hard=self._hard,
                )  # fmt: skip

        ctx.grow = grow
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            with gateway_span(slot, trace_id):
                value = await call(ctx)
        except asyncio.CancelledError:
            # Stopped by the device (cancel): settle what the provider reported so far; a call cut at the
            # caller's budget: at least its reserved output (the provider may well have produced it).
            if bill_reserve_on_cancel and ctx.usage.output_tokens is None:
                ctx.usage.output_tokens = max_output_tokens
            await asyncio.shield(
                self._settle(call_id, spec, ref, ctx.usage, prompt_tokens, fx, elapsed(), "cancelled")
            )
            raise
        except PisaError as err:
            await self._fail(call_id, err.code, elapsed())
            raise
        except Exception as exc:
            err = map_provider_error(exc, spec, ref, secret)
            # the provider's own reason, not only Pisa's summary of it
            status = exc.status_code if isinstance(exc, ModelHTTPError) else type(exc).__name__
            # Only a provider's own error carries its reason: Pisa's own exceptions (a tool's, a validation's,
            # the database's) may hold the user's words, so they log their class only.
            body = (
                (exc.body if exc.body is not None else str(exc))
                if isinstance(exc, ModelHTTPError)
                else (str(exc) if isinstance(exc, ModelAPIError) else type(exc).__name__)
            )
            external_failure(spec.id, f"model {slot}", status, body, secret)
            await self._fail(call_id, err.code, elapsed())
            _log.warning(
                "model_call_failed",
                slot=slot,
                model=str(ref),
                code=err.code,
                cause=err.message,
                trace_id=trace_id,
            )
            raise err from None
        await self._settle(call_id, spec, ref, ctx.usage, prompt_tokens, fx, elapsed(), None)
        if spec.compatible and (cut := cut_of(model)) is not None:
            # the server read far less than was sent: kept for the operator (`pisa models probe`, the
            # capabilities), token counts only
            async with self._pool.connection() as conn:
                await save_truncation(conn, spec.id, ref.model_id, cut)
        return value

    async def _checked_caps(
        self, slot: str, ref: ModelRef, spec: ProviderSpec, caps: ModelCaps, tools: bool
    ) -> ModelCaps:
        """The model's capabilities once the use's needs are checked against them. On a miss by a model never
        probed: one probe (one at a time per model, see `probe_unprobed`) and one more check; never a loop."""
        try:
            check_capabilities(slot, ref, spec, caps, tools)
            return caps
        except PisaError as err:
            if caps.probed_at is not None or self._http is None:
                raise
            _log.info(
                "model_probe_on_miss", slot=slot, model=str(ref), capability=err.extra.get("capability")
            )
            found = await probe_unprobed(self._pool, self._keys, spec, ref.model_id, self._http, self._clock)
            if found.unreachable is not None:
                raise unreachable_for_probe(err, ref, found.unreachable) from None
        check_capabilities(slot, ref, spec, found.caps, tools)
        return found.caps

    async def _settle(
        self,
        call_id: uuid.UUID,
        spec: ProviderSpec,
        ref: ModelRef,
        usage: CallUsage,
        prompt_tokens: int,
        fx: Decimal,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        input_tokens = usage.input_tokens if usage.input_tokens is not None else prompt_tokens
        output_tokens = usage.output_tokens or 0
        async with self._pool.connection() as conn:
            cost = await call_cost_usd(
                conn,
                spec,
                ref,
                input_tokens,
                output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )
            await ledger.settle(
                conn,
                call_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_eur=(cost.usd * fx).quantize(Decimal("0.000001")),
                price_known=cost.known,
                latency_ms=latency_ms,
                now=self._clock(),
                error_code=error_code,
            )

    async def _fail(self, call_id: uuid.UUID, code: str, latency_ms: int) -> None:
        async with self._pool.connection() as conn:
            await ledger.fail(conn, call_id, error_code=code, latency_ms=latency_ms, now=self._clock())
