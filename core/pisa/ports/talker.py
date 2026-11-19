"""The talker: a full-duplex speech model, Pisa's mouth and ears, behind one port. Two adapters,
both final: Grok Voice (xAI, the default) and Gemini Live (pisa.connectors.talker_*).

A session takes the user's audio (16-bit PCM mono at `input_rate`) and gives Pisa's audio back (at
`output_rate`), the user's words and Pisa's words as text, and `ask_brain` calls; Pisa's brain answers them,
and reaches the talker at any time, with whispers (pisa.domain.whisper). Nothing here logs audio or words.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from pisa.domain.whisper import Whisper

# The talker's one tool, the same for every talker (model input: its words are what the talker reads).
# Only how the brain's answer comes back differs: `ask_brain_description(answer)`.
ASK_BRAIN_NAME = "ask_brain"
ASK_BRAIN_PARAMETERS = {
    "type": "object",
    "properties": {"question": {"type": "string", "description": "What the user wants, in a few words"}},
    "required": ["question"],
}


def ask_brain_description(answer: str) -> str:
    """The tool's description; `answer` says how its answer comes back ("a whisper")."""
    return (
        "Ask Pisa's brain: anything you don't know from your instructions or a whisper (the calendar, tasks, "
        "places, hours, travel, email, what Pisa did) and anything to do (add, move, cancel, remember, "
        f"send). Pisa's brain reads the user's own words; its answer comes back as {answer}."
    )


@dataclass(frozen=True)
class TalkerConfig:
    key: str
    voice: str
    lang: str  # it-IT | en-US
    instructions: str  # pisa.domain.whisper.instructions: the rules, the day's summary, Fresh
    resume: str | None = None  # a provider's resumption handle, when it gives one
    # where the user hears Pisa (speaker, airpods, glasses, …): on the speaker Pisa's own voice must not
    # start a turn (the talker hears speech start less eagerly there)
    route: str | None = None
    # the provider's own affect inference (Gemini's affective dialog) stays off unless the user
    # turns it on; nothing in Pisa sets it yet (no setting), so every session today runs without it
    affect: bool = False
    # the user-voice gate is on for this talk: the provider's own voice detection is off,
    # Pisa sends the start and end of each of their turns (`activity_start` / `activity_end`) and only their
    # turns' audio; nothing else changes (the instructions are the same: nothing is added to any prompt)
    client_activity: bool = False


# --- what a session emits --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Audio:
    pcm: bytes  # Pisa's voice, 16-bit PCM mono at the session's output_rate


@dataclass(frozen=True)
class OwnerWords:
    text: str
    final: bool  # the user's turn ended (the provider's transcription is complete)
    delta: bool = False  # inside an adapter only: a piece to add (the port always gives the words so far)
    lang: str | None = None  # the language the provider heard (Gemini's languageCode)


@dataclass(frozen=True)
class PisaWords:
    text: str  # what Pisa said, as spoken
    final: bool  # the end of Pisa's answer (the response is done)
    interrupted: bool = False  # the talker cut this line itself (Gemini's `interrupted`)


@dataclass(frozen=True)
class AskBrain:
    call_id: str
    question: str  # the talker's own words for what it needs; Pisa's brain reads the user's words


@dataclass(frozen=True)
class Speaking:
    on: bool  # Pisa's voice started (True) or stopped (False): for talk.state


@dataclass(frozen=True)
class OwnerSpeaking:
    on: bool  # the provider heard the user start (True) or stop (False) speaking


@dataclass(frozen=True)
class Resumable:
    handle: str  # the provider's handle to resume this session (Gemini: valid 2 h)


@dataclass(frozen=True)
class Closed:
    reason: Literal["provider", "error"]  # the provider ended it, or it failed
    detail: str = ""  # the provider's reason (never the words)


TalkerEvent = Audio | OwnerWords | PisaWords | AskBrain | Speaking | OwnerSpeaking | Resumable | Closed


class TalkerSession(Protocol):
    input_rate: int
    output_rate: int

    async def send_audio(self, pcm: bytes) -> None:
        """The user's audio, as it arrives (16-bit PCM mono at input_rate)."""
        ...

    async def whisper(self, w: Whisper) -> None:
        """A whisper not tied to an ask_brain call: Fresh, a correction, a done, a question code asks."""
        ...

    async def answer(self, call_id: str, w: Whisper) -> None:
        """The brain's answer to an ask_brain call, as a whisper (its priority decides when it is said)."""
        ...

    async def stop(self) -> None:
        """Barge-in by touch: Pisa stops speaking at once."""
        ...

    async def truncate(self, audio_end_ms: int) -> None:
        """Barge-in: what Pisa said ends where the user stopped hearing it (the provider's record of it)."""
        ...

    async def activity_start(self) -> None:
        """The user's turn starts (the gate heard their voice); their audio follows, pre-roll
        first."""
        ...

    async def activity_end(self) -> None:
        """The user's turn ends (the gate's end of turn): the provider answers it."""
        ...

    async def end_turn(self) -> None:
        """The user's voice ran long with no words from the talker: end their turn by hand, so
        the talker transcribes what it has (Gemini transcribes at a turn's end only)."""
        ...

    def events(self) -> AsyncIterator[TalkerEvent]: ...

    async def close(self) -> None: ...


class Talker(Protocol):
    id: str  # xai | gemini
    name: str
    key_provider: str  # the key Pisa uses (Settings → AI providers): xai | google
    default_voice: str
    voices: tuple[str, ...]  # the voices that speak Italian, for Settings
    usd_per_min: float  # FACT: the provider's price, for the ledger's estimate and the minutes report

    async def open(self, cfg: TalkerConfig) -> TalkerSession:
        """A new session. Raises TalkerError when the provider refuses (a key, the network, a quota)."""
        ...


class TalkerError(Exception):
    """The provider refused or failed: the message names it and the cause (never the words or the key)."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        # The provider's own words for the refusal (Google's "Unknown name 'proactivity' at 'setup'"), for the
        # log: without them a 1007 says nothing. Cut short; a provider's error never holds the key.
        self.reason = (reason or "")[:400] or None
