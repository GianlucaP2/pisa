"""Port for Pisa's spoken voice: a provider turns one sentence into streamed audio. The
adapters (OpenAI, ElevenLabs) live in pisa.connectors; no key ever reaches the phone."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pisa.domain.voice import PricedBy

AudioFormat = Literal["mp3", "aac_adts", "pcm16"]


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    languages: tuple[str, ...]  # "it-IT", "en-US"


@dataclass
class SpeechUsage:
    """What one synthesis used, filled in as it streams: the provider's own counts when it gives them."""

    characters: int = 0
    accepted: bool = False  # the provider answered 200: from then on it bills, even if the stream breaks
    input_tokens: int | None = None  # token-priced providers (OpenAI)
    output_tokens: int | None = None
    audio_bytes: int = 0
    chunks: list[int] = field(default_factory=list)  # sizes, for tests and the log


class SpeechError(Exception):
    """The provider refused or failed: the message names the cause (never the text or the key)."""

    def __init__(self, message: str, status: int | None = None, *, no_credit: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.no_credit = no_credit  # the account has no credits left (not a passing limit)


class SpeechProvider(Protocol):
    id: str
    name: str
    format: AudioFormat
    sample_rate: int
    default_voice: str | None  # for a null voice setting; None: no audio until the user picks one
    # tokens: priced from the model catalog's row (a broken stream has no usage: the estimate); characters:
    # by the characters billed (pisa.domain.voice)
    priced_by: PricedBy
    voices_need_key: bool  # its voices are the account's own, listed with its key (else a fixed list)

    @property
    def default_model(self) -> str: ...  # the catalog's model for this provider, looked up when asked

    def stream(
        self, text: str, *, key: str, model: str, voice: str, speed: float, usage: SpeechUsage
    ) -> AsyncIterator[bytes]:
        """The audio of `text`, chunk by chunk as the provider sends it. Raises SpeechError."""
        ...

    async def warm(self) -> None:
        """Open the connection ahead of the first sentence (best effort, never billed)."""
        ...

    async def voices(self, key: str | None) -> list[Voice]: ...

    async def check_key(self, key: str) -> str:
        """A short sentence on success; raises SpeechError with the cause."""
        ...
