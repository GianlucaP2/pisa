"""Gemini Live, Pisa's second talker: switched on when the user adds a Google (Gemini) key and picks it
in Settings. Through google-genai's Live client (its own types): the user's audio as realtime input (16 kHz
PCM), Pisa's audio back at 24 kHz, both transcripts, `ask_brain` as a NON_BLOCKING function, and the whispers
as the function's responses with Gemini's own scheduling: NOW → INTERRUPT, PAUSE → WHEN_IDLE, SILENT → SILENT.
A whisper with no call is client content (a turn Pisa's brain
adds). Context window compression keeps a long session going; the resumption handle (valid 2 h) is kept for
the next.

The message mapping is pure (`connect_config`, `function_response`, `client_turn`, `parse`), so it is tested
without a connection. Never logs the words or the audio.
"""

import contextlib
import re
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from pisa.domain import model_catalog
from pisa.domain.voice_gate import RATE
from pisa.domain.whisper import Priority, Whisper, framed
from pisa.failures import external_failure, reason_of, redact
from pisa.logging import get_logger
from pisa.ports.talker import (
    ASK_BRAIN_NAME,
    ASK_BRAIN_PARAMETERS,
    AskBrain,
    Audio,
    Closed,
    OwnerSpeaking,
    OwnerWords,
    PisaWords,
    Resumable,
    Speaking,
    TalkerConfig,
    TalkerError,
    TalkerEvent,
    ask_brain_description,
)

INPUT_RATE, OUTPUT_RATE = RATE, 24_000  # the ear's rate in
PREFIX_PADDING_MS = 200  # the audio kept from before the start of speech was detected
# on the phone's speaker Pisa's own voice reached the mic and cut Pisa off. There
# speech starts less eagerly (low sensitivity) and needs a longer run of speech before it counts.
SPEAKER_PREFIX_PADDING_MS = 500
SILENCE_MS = 800  # the user's pause that ends their turn
SPEAKER_SILENCE_MS = 500  # on the speaker, with the end of speech HIGH
_log = get_logger("pisa.talker.gemini")
SCHEDULING = {
    Priority.NOW: types.FunctionResponseScheduling.INTERRUPT,
    Priority.PAUSE: types.FunctionResponseScheduling.WHEN_IDLE,
    Priority.SILENT: types.FunctionResponseScheduling.SILENT,
}
ASK_BRAIN = types.FunctionDeclaration(
    name=ASK_BRAIN_NAME,
    description=ask_brain_description("this call's response"),
    parameters_json_schema=ASK_BRAIN_PARAMETERS,
    behavior=types.Behavior.NON_BLOCKING,  # the talker keeps talking while Pisa's brain works
)


def realtime_input(cfg: TalkerConfig) -> types.RealtimeInputConfig:
    """With the user-voice gate on, Gemini's own voice detection is off and a start of
    activity never interrupts Pisa (Pisa stops its own playout on their barge-in); Pisa sends `activityStart`
    and `activityEnd` for their turns only. Off: today's automatic detection, unchanged."""
    if cfg.client_activity:
        return types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
            activity_handling=types.ActivityHandling.NO_INTERRUPTION,
        )
    speaker = cfg.route == "speaker"  # set when the session opens (Gemini's setup is once)
    # the user's voice must start a turn. The phone's voice processing keeps the voice quiet; Gemini's
    # defaults may not take it as speech. High start sensitivity hears the user begin; low end sensitivity and
    # 800 ms of silence don't cut the user off mid-sentence.
    return types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW
            if speaker
            else types.StartSensitivity.START_SENSITIVITY_HIGH,
            # on the speaker a LOW end never fired over continuous sound around the user: there the end is
            # HIGH and the silence shorter; the start stays LOW (Pisa's own voice from the speaker)
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH
            if speaker
            else types.EndSensitivity.END_SENSITIVITY_LOW,
            prefix_padding_ms=SPEAKER_PREFIX_PADDING_MS if speaker else PREFIX_PADDING_MS,
            silence_duration_ms=SPEAKER_SILENCE_MS if speaker else SILENCE_MS,
        )
    )


def connect_config(cfg: TalkerConfig) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=cfg.instructions,
        # No `language_code`: Google's Live guide says native-audio models (Gemini 3.8 Live) "automatically
        # choose the appropriate language and don't support explicitly setting the language code". The
        # session's language reaches Gemini through the instructions and the transcription's hint below.
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.voice)
            ),
        ),
        tools=[types.Tool(function_declarations=[ASK_BRAIN])],
        # without a language, speech can be transcribed as another language (Italian as French). The one
        # place the session's language reaches Gemini's transcription: drop `language_codes` if Google
        # refuses it
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[cfg.lang]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=realtime_input(cfg),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        session_resumption=types.SessionResumptionConfig(handle=cfg.resume),
        # Gemini's affective dialog (its own reading of the user's emotions) is off unless they
        # turns it on (`cfg.affect`). Off is the field left out, never `False`: like `proactivity` below,
        # Google's v1 API refuses the field outside its v1alpha preview
        enable_affective_dialog=True if cfg.affect else None,
        # No `proactivity` (staying silent on an aside): Google's API refuses the field outside its v1alpha
        # preview.
    )


def function_response(call_id: str, w: Whisper, *, more: bool = False) -> types.FunctionResponse:
    """The brain's answer to a NON_BLOCKING ask_brain call; `more` while Pisa's brain may still send another
    (a HOLD first, the answer after)."""
    return types.FunctionResponse(
        id=call_id,
        name=ASK_BRAIN_NAME,
        response={"whisper": framed(w)},
        scheduling=SCHEDULING[w.priority],
        will_continue=more,
    )


def client_turn(w: Whisper) -> tuple[types.Content, bool]:
    """A whisper with no call: Pisa's brain's own turn; it is answered (turn_complete) unless SILENT."""
    return types.Content(role="user", parts=[types.Part(text=framed(w))]), w.priority is not Priority.SILENT


def joined(heard: str, piece: str) -> str:
    """The user's words so far, with Gemini's next piece. A piece that restates the words so far
    (Gemini sends the whole transcription again, e.g. with `finished`) replaces them, so a question is never
    there twice in one final. Any other piece is a delta, appended."""
    said, again = _plain(heard), _plain(piece)
    if said and (again == said or (len(said.split()) >= 3 and again.startswith(said))):
        return piece.lstrip()
    return heard + piece


def _plain(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def parse(message: types.LiveServerMessage) -> list[TalkerEvent]:
    out: list[TalkerEvent] = []
    content = message.server_content
    if content is not None:
        if content.input_transcription and content.input_transcription.text:
            heard = content.input_transcription
            out.append(OwnerWords(heard.text or "", final=bool(heard.finished), lang=heard.language_code))
        if content.model_turn:
            for part in content.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    out.append(Audio(part.inline_data.data))
        if content.output_transcription and content.output_transcription.text:
            out.append(PisaWords(content.output_transcription.text, final=False))
        if content.interrupted:  # the user talked over Pisa: what is buffered on the phone must stop too
            out.append(OwnerSpeaking(True))
        if content.turn_complete or content.interrupted:
            out.append(PisaWords("", final=True, interrupted=bool(content.interrupted)))
            out.append(Speaking(False))
    if message.tool_call:
        for call in message.tool_call.function_calls or []:
            if call.name == ASK_BRAIN_NAME:
                question = str((call.args or {}).get("question") or "")
                out.append(AskBrain(call.id or "", question))
    update = message.session_resumption_update
    if update is not None and update.resumable and update.new_handle:
        out.append(Resumable(update.new_handle))
    if message.go_away is not None:
        _log.info("talker_go_away", provider="gemini", time_left=str(message.go_away.time_left))
    return out


def signals(message: types.LiveServerMessage) -> list[dict[str, Any]]:
    """Item 1 (`maintenance → voice-gate-check`): what one server message carries for their turn's
    end, in order: every transcription (input, interim input, output) with `finished` and `languageCode`,
    Pisa's audio, a tool call, and the turn's signals (`generationComplete`, `turnComplete`,
    `waitingForInput`, `interrupted`). The check runs it on a synthetic sentence only: the text is that
    sentence."""
    out: list[dict[str, Any]] = []
    c = message.server_content
    if c is not None:
        for kind, t in (
            ("input", c.input_transcription),
            ("interim_input", c.interim_input_transcription),
            ("output", c.output_transcription),
        ):
            if t is not None and (t.text or t.finished or t.language_code):
                out.append({"event": kind, "text": t.text or "", "finished": t.finished,
                            "lang": t.language_code})  # fmt: skip
        if c.model_turn and any(p.inline_data and p.inline_data.data for p in c.model_turn.parts or []):
            out.append({"event": "audio"})
        for flag in ("generation_complete", "turn_complete", "waiting_for_input", "interrupted"):
            if getattr(c, flag):
                out.append({"event": flag})
    if message.tool_call:
        out += [{"event": "tool_call", "name": f.name} for f in message.tool_call.function_calls or []]
    return out


class GeminiLiveSession:
    input_rate = INPUT_RATE
    output_rate = OUTPUT_RATE

    def __init__(
        self, session: Any, stack: contextlib.AsyncExitStack, *, client_activity: bool = False
    ) -> None:
        self._session = session
        self._client = client_activity  # the gate gives their turns' start and end
        self._stack = stack
        self._speaking = False
        self._heard = ""  # the user's words so far in this turn (Gemini sends them in pieces)
        self._said = ""  # Pisa's words so far in this answer
        self._held: list[Whisper] = []  # whispers waiting for Gemini's turn to end
        self._ended = False  # the user's end of speech was given for the words heard so far
        self._lang: str | None = None  # the language Gemini heard in this turn
        # with the gate (client activity) Gemini never says `finished` on their words, and
        # its `turnComplete` comes after Pisa's whole reply: so their turn's words are final when Pisa's reply
        # starts (or Gemini says it generated it, or its turn completes), once `activityEnd` was sent: what
        # was heard by then, with its language. Pieces after that grow them, given once more at
        # `turnComplete`.
        self._closing = False  # activityEnd sent: their words are final at the reply's first sign
        self._given: str | None = None  # their words given as final for this turn (pieces after it held back)

    async def send_audio(self, pcm: bytes) -> None:
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={INPUT_RATE}")
        )

    async def activity_start(self) -> None:
        self._heard, self._lang, self._closing, self._given = "", None, False, None  # their new turn's words
        await self._session.send_realtime_input(activity_start=types.ActivityStart())

    async def activity_end(self) -> None:
        self._closing = True
        await self._session.send_realtime_input(activity_end=types.ActivityEnd())

    def _gated_final(self, *, early: bool = False) -> OwnerWords | None:
        """Item 1: their words as heard so far, final, once Pisa's reply shows (client activity).
        `early` (Pisa's first audio, words or call), one word heard by then is likely the start
        of their sentence with the rest still being transcribed: it waits for `generationComplete` (or the
        talk's JUDGE_WAIT_S on the interim words)."""
        if not (self._client and self._closing and self._heard.strip()):
            return None
        if early and len(re.findall(r"\w+", self._heard)) < 2:
            return None
        self._closing, self._given = False, self._heard
        return OwnerWords(self._heard, True, lang=self._lang)

    async def end_turn(self) -> None:
        if self._client:  # the gate ends their turns
            await self.activity_end()
            return
        # with automatic activity detection on, the audio stream's end is the turn's end Gemini
        # takes by hand (an activity end is for manual detection only); the next audio starts a new stream
        await self._session.send_realtime_input(audio_stream_end=True)

    async def whisper(self, w: Whisper) -> None:
        if self._speaking and w.priority is not Priority.NOW:
            # a client turn interrupts what Gemini is saying: PAUSE and SILENT wait for its turn to end
            self._held.append(w)
            return
        turn, complete = client_turn(w)
        await self._session.send_client_content(turns=turn, turn_complete=complete)

    async def _flush_held(self) -> None:
        held, self._held = self._held, []
        for w in held:
            await self.whisper(w)

    async def answer(self, call_id: str, w: Whisper) -> None:
        await self._session.send_tool_response(function_responses=[function_response(call_id, w)])

    async def stop(self) -> None:
        # what waited to be said after this turn isn't said after a stop: kept as context only
        self._held = [Whisper(w.kind, Priority.SILENT, w.text, id=w.id) if w.priority is Priority.PAUSE else w
                      for w in self._held]  # fmt: skip
        # Gemini has no "cancel the response" event: an empty client turn interrupts what it is saying.
        turn = types.Content(role="user", parts=[types.Part(text=".")])
        await self._session.send_client_content(turns=turn, turn_complete=False)

    async def truncate(self, audio_end_ms: int) -> None:
        """Gemini cuts its own record of an interrupted turn: nothing to send."""

    async def events(self) -> AsyncIterator[TalkerEvent]:
        try:
            while True:
                async for message in self._session.receive():
                    content = message.server_content
                    for out in parse(message):
                        if isinstance(out, OwnerWords) and self._client:
                            self._heard = joined(self._heard, out.text)
                            self._lang = out.lang or self._lang
                            if self._given is not None:
                                continue  # after their final: held back, given grown at turnComplete
                            if not out.final:
                                yield OwnerWords(self._heard, False, lang=self._lang)
                                continue
                            self._closing = True  # Gemini said `finished`: final now
                            if (given := self._gated_final()) is not None:
                                yield given
                            continue
                        if self._client and isinstance(out, Audio | PisaWords | AskBrain):
                            if (given := self._gated_final(early=True)) is not None:
                                yield given  # Pisa's reply starts: their words are what was heard by now
                        if isinstance(out, OwnerWords):  # the port gives the words so far
                            if not self._heard:
                                self._ended = False  # a new utterance: its end is still to come
                            self._heard = joined(self._heard, out.text)
                            self._lang = out.lang or self._lang
                            out = OwnerWords(self._heard, out.final, lang=self._lang)
                            if out.final:
                                self._heard, self._lang = "", None
                        if isinstance(out, PisaWords):
                            self._said += out.text
                            out = PisaWords(self._said, out.final, out.interrupted)
                            if out.final:
                                self._said = ""
                        if (
                            isinstance(out, Audio | AskBrain)
                            and self._heard
                            and not self._ended
                            and not self._client
                        ):
                            # Gemini sends no end of speech: the user stopped when Pisa starts answering
                            self._ended = True
                            yield OwnerSpeaking(False)
                        if isinstance(out, Audio) and not self._speaking:
                            self._speaking = True
                            yield Speaking(True)
                        if isinstance(out, Speaking):
                            if not self._speaking:
                                continue
                            self._speaking = False
                        yield out
                    if (
                        self._client
                        and content is not None
                        and (content.generation_complete or content.turn_complete)
                    ):
                        if (given := self._gated_final()) is not None:
                            yield given
                        if content.turn_complete and self._given is not None:
                            if _plain(self._heard) != _plain(self._given):  # pieces came after: the whole
                                yield OwnerWords(self._heard, True, lang=self._lang)
                            self._heard, self._lang, self._given = "", None, None
                    if content is not None and content.turn_complete and not content.interrupted:
                        # RAIL: the user's words are final only when the
                        # transcription says "finished", or once Gemini finished its own turn uninterrupted
                        # (the user said nothing more): never when audio or a call merely arrives, which
                        # would act on "cancella la riunione" before "di giovedì".
                        if self._heard:
                            yield OwnerWords(self._heard, True, lang=self._lang)
                            self._heard, self._lang = "", None
                        await self._flush_held()
        except genai_errors.APIError as exc:
            # Google's own reason for the close (a 1011, a quota, a policy), not just its code
            external_failure("gemini_live", "session", exc.code, exc.message)
            yield Closed("provider", f"{exc.code} {reason_of(exc.message) or ''}".strip())
        except (OSError, ValueError) as exc:
            external_failure("gemini_live", "session", type(exc).__name__, exc)
            yield Closed("error", f"{exc.__class__.__name__}: {reason_of(exc) or ''}".strip(" :"))

    async def close(self) -> None:
        await self._stack.aclose()


def refusal_of(exc: BaseException) -> tuple[int | None, str]:
    """(the provider's code, the kind of failure) of a Live connect that failed outside an HTTP error: the
    WebSocket's close code (1007 a bad setup, 1008 an unknown model or no access, 1011 the server's) or its
    handshake's HTTP status."""
    rcvd = getattr(exc, "rcvd", None)  # websockets' ConnectionClosed: the close frame Google sent
    if rcvd is not None and getattr(rcvd, "code", None):
        return int(rcvd.code), exc.__class__.__name__
    response = getattr(exc, "response", None)  # websockets' InvalidStatus: the handshake's answer
    status = getattr(response, "status_code", None)
    return (int(status) if isinstance(status, int) else None), exc.__class__.__name__


def safe_reason(exc: BaseException, key: str) -> str:
    """The provider's own words for a failure, for the log: never the key (Live puts it in the URL)."""
    rcvd = getattr(exc, "rcvd", None)
    return redact(str(getattr(rcvd, "reason", "") or exc), key)


class GeminiLive:
    id = "gemini"
    name = "Gemini Live"
    key_provider = "google"
    default_voice = "Aoede"
    # Gemini's prebuilt voices (each speaks every Live language, Italian included); candidates to compare by
    # ear.
    voices: tuple[str, ...] = ("Aoede", "Kore", "Leda", "Zephyr", "Puck", "Charon", "Fenrir", "Orus")
    usd_per_min = 0.023  # FACT: audio in $3/M tokens (~$0.005/min) + audio out $12/M (~$0.018/min)

    async def open(self, cfg: TalkerConfig) -> GeminiLiveSession:
        client = genai.Client(api_key=cfg.key)
        stack = contextlib.AsyncExitStack()
        try:
            session = await stack.enter_async_context(
                client.aio.live.connect(
                    model=model_catalog.default_model("talker", "gemini"), config=connect_config(cfg)
                )
            )
        except genai_errors.APIError as exc:
            await stack.aclose()
            if exc.code in (401, 403):
                raise TalkerError(
                    f"Google key rejected by Gemini Live (HTTP {exc.code}): add a valid key in Settings → AI "
                    "providers.",
                    exc.code,
                ) from None
            raise TalkerError(
                f"Gemini Live refused the session (HTTP {exc.code}).", exc.code, safe_reason(exc, cfg.key)
            ) from None
        except OSError as exc:
            await stack.aclose()
            raise TalkerError(
                f"Gemini Live is unreachable ({exc.__class__.__name__}).", None, safe_reason(exc, cfg.key)
            ) from None
        except Exception as exc:  # a closed handshake (an unknown model, no Live access, a region, a quota)
            await stack.aclose()
            status, kind = refusal_of(exc)
            raise TalkerError(
                f"Gemini Live refused the session ({kind}" + (f" {status}" if status else "") + ").",
                status,
                safe_reason(exc, cfg.key),
            ) from None
        return GeminiLiveSession(session, stack, client_activity=cfg.client_activity)
