"""Pisa's spoken voice: each sentence of a reply (or of a `notify`) becomes audio with the
voice the user picked, streamed to the device as `say_audio` while the text keeps flowing as `say`.

- **When**: the provider is OpenAI or ElevenLabs and its key is set. Otherwise (the Device voice, no key) no
  audio: the phone speaks the text itself.
- **Who**: only a device whose hello lists the provider's format (`device.audioFormats`); a `notify` only
  when it will be spoken (companion mode with earbuds, or glasses), not for a banner.
- **Streaming**: a sentence goes to the provider as soon as it is complete (the same boundaries as `say`),
  while earlier ones are still playing; the audio goes out in order, each chunk as it arrives, and an empty
  chunk with `final` closes it.
- **Failures**: a failed sentence is retried once; then the audio stops, and its final chunk says
  `complete: false` with `spoken_chars` so the phone speaks the rest (no `error` to the device); `cancel` (or
  a new utterance) stops synthesis at once, with no more chunks.
- **Spend**: each sentence is one `llm_calls` row (slot `voice.speak`) under the AI caps, at real prices
  (pisa.domain.voice); once the provider answered 200 it is billed, even when cancelled. Over a cap: no audio,
  and one `info` notice a day.
- Never logs the text or the audio.
"""

import asyncio
import base64
import time
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import voice as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.infra import hard_limit, ledger
from pisa.infra.fx import usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.infra.spend import period_starts
from pisa.logging import get_logger
from pisa.ports.speech import SpeechError, SpeechProvider, SpeechUsage
from pisa.services.notifications import enqueue

type _Out = asyncio.Queue[bytes | BaseException | None]  # one sentence's audio, then None or what stopped it
RESTED_KEY = "voice_rested"  # {provider, day} the chosen voice rests (no credits)
_LATER = b"later"  # not yet arrived (a marker compared by identity)
CHUNK_MAX = 262_144  # bytes of audio per say_audio (its data is base64, at most 400,000 characters)
_log = get_logger("pisa.voice")

# (seq, audio, final, format, sample_rate, spoken_chars, sentence_end) → True when it went out on the socket.
# spoken_chars is set on the final chunk only when the audio stopped early (`complete: false`); sentence_end
# on the chunk whose audio ends a sentence.
SendAudio = Callable[[int, bytes, bool, str, int, int | None, bool], Awaitable[bool]]


@dataclass(frozen=True)
class VoiceConfig:
    provider: SpeechProvider
    key: str
    model: str
    voice: str
    speed: float
    lang: str
    # read once a turn, with the voice, not again for each sentence (two queries less before each)
    prices: tuple[Decimal, Decimal] | None = None
    rate: Decimal | None = None


class VoiceService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        providers: dict[str, SpeechProvider],
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hard_limits: HardLimits = hard_limit.DEFAULT,
    ) -> None:
        self._pool = pool
        self._keys = keys
        self._providers = providers
        self._hard = hard_limits
        self._owner = owner
        self._clock = clock

    async def config(self) -> VoiceConfig | None:
        """The voice to speak with now, or None when the phone speaks (Device, or no key, or the chosen
        provider resting today for lack of credits)."""
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            chosen = settings["voice"]
            provider = self._providers.get(chosen["provider"])
            key = await self._keys.get(conn, provider.id) if provider is not None else None
            voice = chosen["voice"] or (provider.default_voice if provider is not None else None)
            if provider is None or not key or not voice:
                return None
            if await get_setting(conn, RESTED_KEY) == {"provider": provider.id, "day": self._today()}:
                return None  # no credits today: the phone speaks, and no call to the provider until tomorrow
            model = chosen["model"] or provider.default_model
            prices = (
                await self._token_prices(conn, provider.id, model) if provider.priced_by == "tokens" else None
            )
            rate = await usd_to_eur(conn)
        return VoiceConfig(
            provider, key, model, voice, float(chosen["speed"]), settings["language"], prices, rate
        )

    async def ready(self) -> VoiceConfig | None:
        """The voice, read ahead of the first sentence, and the provider's connection opened (a cold
        TLS connection to the provider was part of each reply's first-audio wait)."""
        cfg = await self.config()
        if cfg is not None:
            await cfg.provider.warm()
        return cfg

    async def spoken_aloud(self, device_id: uuid.UUID) -> bool:
        """A `notify` is spoken (not a banner) in companion mode with earbuds, or with the glasses on."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT (companion = 'on' AND earbuds = 'connected') OR glasses = 'on' "
                    "FROM device_presence WHERE device_id = %s",
                    (device_id,),
                )
            ).fetchone()
        return bool(row and row[0])

    async def synthesize(
        self,
        text: str,
        cfg: VoiceConfig,
        trace_id: str,
        out: Callable[[bytes], Awaitable[None]],
        ended: Callable[[], None] | None = None,
        marks: dict[str, float] | None = None,
    ) -> None:
        """One sentence: reserved, streamed to `out`, settled. Raises SpeechError, or PisaError
        budget_exceeded when a cap is reached (nothing is spoken then). `ended` is called as soon as the audio
        is whole, before the settling (so the sentence's end can ride on its last chunk). `marks` gets the
        moments the sentence was reserved and asked of the provider."""
        pid, model, priced_by = cfg.provider.id, cfg.model, cfg.provider.priced_by
        async with self._pool.connection() as conn:
            prices = cfg.prices or (
                await self._token_prices(conn, pid, model)
                if priced_by == "tokens"
                else rules.OPENAI_USD_PER_MTOK
            )
            rate = cfg.rate or await usd_to_eur(conn)
            estimate = rules.estimate_usd(priced_by, model, len(text), prices) * rate
            call_id = await ledger.reserve(
                conn, slot=rules.SLOT, model_ref=f"{pid}:{model}", estimate_eur=estimate, price_known=True,
                trace_id=trace_id, now=self._clock(), tz=self._owner.tz, hard=self._hard,
            )  # fmt: skip
        if marks is not None:
            marks.setdefault("reserved", time.monotonic())
        usage = SpeechUsage()
        started = time.monotonic()
        if marks is not None:
            marks.setdefault("requested", started)
        try:
            async for chunk in cfg.provider.stream(
                text, key=cfg.key, model=model, voice=cfg.voice, speed=cfg.speed, usage=usage
            ):
                await out(chunk)
            if ended is not None:
                ended()
        except BaseException as exc:  # an error or a cancel: the row never stays reserved
            code = (
                "cancelled" if isinstance(exc, asyncio.CancelledError)
                else "provider_error" if isinstance(exc, SpeechError) else "internal"
            )  # fmt: skip
            await self._close(call_id, cfg, text, usage, prices, rate, started, code)  # the caller waits
            raise
        await self._close(call_id, cfg, text, usage, prices, rate, started, None)

    async def _close(
        self, call_id: uuid.UUID, cfg: VoiceConfig, text: str, usage: SpeechUsage,
        prices: tuple[Decimal, Decimal], rate: Decimal, started: float, error: str | None,
    ) -> None:  # fmt: skip
        """Settle the sentence's row. Once the provider answered 200 it bills, whatever happens next: a cancel
        or a broken stream is settled at its price (ElevenLabs: the characters it billed; OpenAI: its tokens,
        else the estimate), never at 0. Refused before that: failed, at 0."""
        model, priced_by = cfg.model, cfg.provider.priced_by
        latency_ms = int((time.monotonic() - started) * 1000)
        async with self._pool.connection() as conn:
            if not usage.accepted:
                await ledger.fail(conn, call_id, error_code=error or "internal", latency_ms=latency_ms,
                                  now=self._clock())  # fmt: skip
                return
            characters = usage.characters or len(text)
            if (
                error is not None and priced_by == "tokens"
            ):  # a broken stream has no usage event: the estimate
                usage.input_tokens = usage.output_tokens = None
            cost = rules.cost_usd(
                priced_by, model, characters, usage.input_tokens, usage.output_tokens, prices
            )
            await ledger.settle(
                conn, call_id,
                # ElevenLabs bills characters: they are recorded as its input "tokens".
                input_tokens=usage.input_tokens if usage.input_tokens is not None else characters,
                output_tokens=usage.output_tokens or 0, cost_eur=(cost * rate).quantize(Decimal("0.000001")),
                price_known=True, latency_ms=latency_ms, now=self._clock(), error_code=error,
            )  # fmt: skip

    async def _token_prices(
        self, conn: AsyncConnection, provider_id: str, model: str
    ) -> tuple[Decimal, Decimal]:
        """A token-priced provider's prices for the model from the catalog, else the constants
        (pisa.domain.voice)."""
        row = await (
            await conn.execute(
                "SELECT input_usd_per_mtok, output_usd_per_mtok FROM model_catalog WHERE ref = %s",
                (f"{provider_id}:{model}",),
            )
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return rules.OPENAI_USD_PER_MTOK
        return Decimal(row[0]), Decimal(row[1])

    def _today(self) -> str:
        day_start, _ = period_starts(self._clock(), self._owner.tz)
        return day_start.date().isoformat()

    async def rest(self, provider: SpeechProvider) -> None:
        """The provider said its account has no credits: it isn't called again today (their day,
        their zone), the phone speaks, and they are told once (one notice for that provider and day)."""
        now = self._clock()
        rested = {"provider": provider.id, "day": self._today()}
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.voice.rest'))")  # one notice
            if await get_setting(conn, RESTED_KEY) == rested:
                return
            await set_setting(conn, RESTED_KEY, Jsonb(rested))
            lang = await self._owner.lang(conn)
            table = rules.NO_CREDIT_NOTICE
            await enqueue(
                conn, kind="info", text=table.get(lang, table["en-US"]).format(name=provider.name), lang=lang,
                now=now, timezone=self._owner.tz, path="voice.rested",
            )  # fmt: skip
        _log.warning("voice_provider_rested", provider=provider.id)

    async def cap_notice(self) -> None:
        """Over an AI cap: one `info` notice a day that the phone speaks instead."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            # Two replies over the cap at once: one notice (the lock holds until the transaction ends).
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.voice.cap_notice'))")
            settings = await self._owner.settings(conn)
            day_start, _ = period_starts(now, self._owner.tz)
            today = day_start.date().isoformat()
            if await get_setting(conn, "voice_cap_notified_on") == today:
                return
            await set_setting(conn, "voice_cap_notified_on", Jsonb(today))
            lang = settings["language"]
            await enqueue(
                conn,
                kind="info",
                text=rules.CAP_NOTICE[lang],
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                path="voice.cap",
            )
        _log.warning("voice_cap_reached")

    # --- the app: voices and the preview ------------------------------------------------------------------

    async def voices(self, provider_id: str) -> list[dict[str, Any]]:
        if provider_id not in rules.PROVIDERS:
            raise PisaError(
                "validation_failed", "The provider must be openai, elevenlabs or device.", "Fix the request"
            )
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            provider = self._providers.get(provider_id)
            key = await self._keys.get(conn, provider_id) if provider is not None else None
        if provider is None:
            return []  # the Device voices are the phone's own
        if provider.voices_need_key and not key:
            raise _key_missing(provider.name)
        try:
            found = await provider.voices(key)
        except SpeechError as err:
            raise PisaError("provider_error", err.message, "Try again in a minute") from None
        return [
            {
                "id": v.id,
                "name": v.name,
                "languages": list(v.languages),
                "preview_text": rules.PREVIEW_TEXT[lang],
            }
            for v in found
        ]

    async def preview(self, text: str | None) -> bytes:
        """The chosen voice saying `text` (null: a fixed sentence in the user's language), as mp3."""
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            provider = self._providers.get(settings["voice"]["provider"])
            key = await self._keys.get(conn, provider.id) if provider is not None else None
        if provider is None:
            raise PisaError(
                "validation_failed",
                "The Device voice is the phone's own: Pisa previews only the OpenAI and ElevenLabs voices.",
                "Preview it on the phone",
            )
        if not key:
            raise _key_missing(provider.name)
        cfg = await self.config()
        if cfg is None:  # the key is set: no voice is chosen, and the provider has no default
            raise PisaError(
                "validation_failed",
                f"No {provider.name} voice is chosen.",
                "Pick a voice in Settings → Voice",
            )
        audio = bytearray()

        async def collect(chunk: bytes) -> None:
            audio.extend(chunk)

        try:
            await self.synthesize(text or rules.PREVIEW_TEXT[cfg.lang], cfg, uuid.uuid4().hex, collect)
        except SpeechError as err:
            raise PisaError("provider_error", err.message, "Try again, or pick another voice") from None
        return bytes(audio)


def _key_missing(name: str) -> PisaError:
    return PisaError(
        "provider_key_missing", f"No {name} key is set.", f"Add the {name} key in Settings → AI providers"
    )


class Speaker:
    """The audio of one reply (or one notify). `feed` each piece of the text as it is sent (the reply's `say`
    deltas, in order), then `finish`; `cancel` stops it at once. `first_audio_at` (monotonic) is when the
    first chunk went out.

    Only for a device that plays the provider's format (its hello's `device.audioFormats`); otherwise
    nothing is synthesised, or paid for. Chunks go out as they arrive; an empty final chunk closes the audio.
    A sentence that fails is retried once (when none of its audio went out yet); if it fails again, or the
    cap is reached, the final chunk says `complete: false` with `spoken_chars`, where the sentence started in
    the text, and the phone speaks the rest. When no audio went out at all, nothing is
    sent: the phone speaks the whole text after its 1.5 s wait."""

    def __init__(self, voice: VoiceService, send: SendAudio, trace_id: str, formats: Collection[str]) -> None:
        self._voice = voice
        self._send = send
        self._trace = trace_id
        self._formats = frozenset(formats)
        self._queue: asyncio.Queue[tuple[str, int] | None] = asyncio.Queue()  # (text, where it starts)
        self._task: asyncio.Task[None] | None = None
        self._offset = 0
        self._seq = 0
        self.first_audio_at: float | None = None
        # the first sentence's way to the phone, in moments (ids and times only): fed (the first text
        # sentence), config, reserved, requested (the TTS request), received (its first chunk), sent.
        self.marks: dict[str, float] = {}
        self._config: asyncio.Task[VoiceConfig | None] | None = None

    def warm(self) -> None:
        """At the turn's start, while the model thinks: the voice, its prices and its key are read now, and
        the provider's connection is opened, so the first sentence goes straight to synthesis."""
        if self._formats and self._config is None:
            self._config = asyncio.create_task(self._voice.ready())

    def feed(self, text: str) -> None:
        # spoken_chars counts the reply's text as the phone holds it: each delta trimmed, empty ones dropped,
        # joined with one space (code points).
        piece = text.strip()
        if not piece:
            return
        start = self._offset + (1 if self._offset else 0)
        self._offset = start + len(piece)
        if not self._formats:
            return
        self.marks.setdefault("fed", time.monotonic())
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        self._queue.put_nowait((text, start))

    def finish(self) -> None:
        if self._task is not None:
            self._queue.put_nowait(None)

    async def done(self) -> None:
        if self._task is not None:
            await self._task

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._config is not None and not self._config.done():
            self._config.cancel()

    async def _run(self) -> None:
        try:
            cfg = await (self._config if self._config is not None else self._voice.config())
            self.marks["config"] = time.monotonic()
            if cfg is not None and cfg.provider.format in self._formats:
                await self._speak(cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("voice_failed", trace_id=self._trace)
        finally:
            self._log_first_audio()

    def _log_first_audio(self) -> None:
        """One line a turn: how long each step took before the first audio went out (ms from the first text
        sentence; ids and times only)."""
        fed = self.marks.get("fed")
        if fed is None:
            return
        steps = {k: round((v - fed) * 1000) for k, v in self.marks.items() if k != "fed"}
        _log.info("voice_first_audio", trace_id=self._trace, warmed=self._config is not None, **steps)

    async def _speak(self, cfg: VoiceConfig) -> None:
        streams: asyncio.Queue[tuple[int, _Out] | None] = asyncio.Queue()
        synths: list[asyncio.Task[None]] = []

        async def synth(text: str, out: _Out) -> None:
            produced = 0  # chunks of this sentence already handed on: then a retry would repeat them

            async def put(chunk: bytes) -> None:
                nonlocal produced
                produced += 1
                self.marks.setdefault("received", time.monotonic())
                out.put_nowait(chunk)

            for attempt in (1, 2):
                try:
                    await self._voice.synthesize(
                        text, cfg, self._trace, put, lambda: out.put_nowait(None), self.marks
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # everything reaches the consumer: a reply never hangs
                    over_cap = isinstance(err, PisaError) and err.code == "budget_exceeded"
                    no_credit = isinstance(err, SpeechError) and err.no_credit  # never again
                    if attempt == 2 or over_cap or no_credit or produced:
                        out.put_nowait(err)
                        return
                    _log.info("voice_retry", trace_id=self._trace, provider=cfg.provider.id)

        async def intake() -> None:  # each sentence to the provider as soon as it is complete
            while (item := await self._queue.get()) is not None:
                out: _Out = asyncio.Queue()
                synths.append(asyncio.create_task(synth(item[0], out)))
                streams.put_nowait((item[1], out))
            streams.put_nowait(None)

        taker = asyncio.create_task(intake())
        stopped_at: int | None = None  # where in the text the audio stopped early
        close = True
        try:
            while (stream := await streams.get()) is not None:
                start, out = stream
                item, said, marked = await out.get(), False, False
                while item is not None:
                    if isinstance(item, BaseException):
                        await self._stopped(cfg, item)
                        stopped_at = start
                        return
                    # REFLEX: the sentence's end rides on its last chunk when that is already known (the
                    # provider closed the sentence); otherwise an empty chunk marks it, with no wait.
                    after = out.get_nowait() if not out.empty() else _LATER
                    for at in range(0, len(item), CHUNK_MAX):
                        marked = after is None and at + CHUNK_MAX >= len(item)
                        if not await self._emit(cfg, item[at : at + CHUNK_MAX], False, None, marked):
                            close = False  # the socket is gone
                            return
                        said = True
                    item = await out.get() if after is _LATER else after
                if said and not marked and not await self._emit(cfg, b"", False, None, True):
                    close = False
                    return
        except asyncio.CancelledError:
            close = False  # cancel: no more chunks at all
            raise
        finally:
            taker.cancel()
            for task in synths:
                task.cancel()
            # Each cancelled sentence settles its row before the speaker ends (a second cancel can't cut it).
            await asyncio.shield(asyncio.gather(*synths, return_exceptions=True))
            if close and self._seq > 0:
                await self._emit(cfg, b"", True, stopped_at)

    async def _stopped(self, cfg: VoiceConfig, err: BaseException) -> None:
        if isinstance(err, PisaError) and err.code == "budget_exceeded":
            await self._voice.cap_notice()
        elif isinstance(err, SpeechError) and err.no_credit:
            await self._voice.rest(cfg.provider)  # the phone speaks the rest, and every reply today
        elif isinstance(err, SpeechError):
            _log.warning(
                "voice_provider_failed", trace_id=self._trace, provider=cfg.provider.id, cause=err.message
            )
        else:
            _log.warning("voice_stopped", trace_id=self._trace, cause=err.__class__.__name__,
                         code=getattr(err, "code", None))  # fmt: skip

    async def _emit(
        self, cfg: VoiceConfig, data: bytes, final: bool, spoken: int | None, sentence_end: bool = False
    ) -> bool:
        fmt, rate = cfg.provider.format, cfg.provider.sample_rate
        sent = await self._send(self._seq, data, final, fmt, rate, spoken, sentence_end)
        self._seq += 1
        if sent and data and self.first_audio_at is None:
            self.first_audio_at = time.monotonic()
            self.marks["sent"] = self.first_audio_at
        return sent


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
