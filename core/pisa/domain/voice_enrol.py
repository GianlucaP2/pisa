"""Their voice's enrolment. The scripts the phone shows (the server owns them), the quality
of each block as it ends, and the split that keeps 20% of every block out of the prints for the bench. Pure
rules, no I/O. Every sentence here is synthetic, written for phonetic variety (Italian vowels, double
consonants, gl/gn/sc, English th/r/w), never anyone's words.

Blocks: A, 10 Italian sentences, normal voice; B, 10 English sentences; C, free speech, 30 s per language; D,
4 sentences in a loud "noisy bar" voice; E, 3 whispered (optional, a low-trust print). The phone
plays no babble, ever (block F is gone): the server mixes their A/B/C utterances with its stored public babble
itself (`NOISY_SNRS_DB`). The test: 6 new sentences per language (`test`), read once; the server scores them
clean and in babble. A new route: blocks A and C, short (about a minute).
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np

from pisa.domain.voice_gate import RATE

Block = Literal["A", "B", "C", "D", "E"]
BLOCKS: tuple[Block, ...] = ("A", "B", "C", "D", "E")
REQUIRED: tuple[Block, ...] = ("A", "B", "C", "D")
NOISY_FROM: tuple[Block, ...] = ("A", "B", "C")  # their noisy positives, mixed by the server
NOISY_SNRS_DB = (15.0, 10.0, 5.0, 0.0)
MINI: tuple[Block, ...] = ("A", "C")  # a new route's first talk
HELD_OUT = 0.2  # of each block, kept out of the prints: the bench's owner audio
FREE_SPEECH_S = 30  # C: per language

# The print each block feeds (E is its own, low-trust; F's noisy speech feeds the normal print)
PRINT_OF: dict[Block, str] = {
    "A": "normal",
    "B": "normal",
    "C": "normal",
    "D": "loud",
    "E": "whisper",
}
TRUST = {"normal": 1.0, "loud": 1.0, "whisper": 0.8}

# Voiced speech a block needs (seconds); the mini-enrolment's are shorter
MIN_VOICED_S: dict[Block, float] = {"A": 15.0, "B": 15.0, "C": 30.0, "D": 6.0, "E": 4.0}
MINI_VOICED_S: dict[Block, float] = {"A": 8.0, "C": 15.0}
CLIP_LEVEL = 0.99  # of full scale
CLIP_SHARE = 0.001  # more than 0.1% of samples at the rail: clipped
QUIET_SNR_DB = 20.0  # quiet blocks (A-E) need this
LOW_LEVEL_DBFS = -35.0  # their speech under this: "louder"

Hint = Literal["louder", "quieter", "closer", "again"]

_IT_A = (
    "Ogni mattina Giulia beve un caffè amaro guardando il lago.",
    "Il gatto grigio si nasconde sotto la scala della cantina.",
    "Domani porterò sei bottiglie d'acqua frizzante alla festa.",
    "Gli gnocchi della nonna erano gialli, soffici e profumati.",
    "Sciare sulla neve fresca richiede pazienza e coraggio.",
    "Quel treno veloce parte alle diciassette e un quarto.",
    "Ho sognato un'enorme zucca arancione in mezzo alla piazza.",
    "Puoi chiudere la finestra prima che entri la pioggia?",
    "Tre tigri contro tre tigri, disse ridendo il bambino.",
    "Nino ha prenotato un tavolo vicino al mare per giovedì.",
)
_EN_B = (
    "The quick brown fox jumps over the lazy sleeping dog.",
    "Three thin thieves thought through the theory together.",
    "Would you remind me to water the roses on Wednesday?",
    "A rainy afternoon is perfect for reading old letters.",
    "She measured the yellow fabric twice before cutting it.",
    "Our flight to Lisbon leaves early tomorrow morning.",
    "Please check whether the bakery opens before nine.",
    "The village choir sang a joyful song in the square.",
    "Every gadget in the drawer needs fresh batteries.",
    "How far is the nearest pharmacy from the station?",
)
_FREE = {
    "it": (
        "Raccontami com'è andata la tua giornata finora.",
        "Descrivi la stanza o il posto in cui ti trovi.",
    ),
    "en": ("Tell Pisa about your day so far.", "Describe the room or the place you are in."),
}
_LOUD = {
    "it": (
        "Scusa, puoi ripetere? Qui c'è troppo rumore!",
        "Prendo un altro bicchiere d'acqua, grazie!",
        "Ci vediamo fuori tra cinque minuti!",
        "Aspetta, fammi controllare il telefono!",
    ),
    "en": (
        "Sorry, can you say that again? It's so loud in here!",
        "I'll have another glass of water, thanks!",
        "Let's meet outside in five minutes!",
        "Hold on, let me check my phone!",
    ),
}
_WHISPER = {
    "it": (
        "Parlo piano perché tutti dormono.",
        "Questa è solo una prova sottovoce.",
        "Ricordami di chiamare domani.",
    ),
    "en": (
        "I'm speaking softly because everyone is asleep.",
        "This is only a quiet test.",
        "Remind me to call tomorrow.",
    ),
}
# the test's sentences, read once after the blocks: synthetic, and unlike A's
_TEST = {
    "it": (
        "Stasera passo dalla farmacia prima di cena.",
        "Il nuovo ponte chiude per lavori fino a marzo.",
        "Mia sorella sceglie sempre il gelato al pistacchio.",
        "Alle sei e mezza suona la sveglia in cucina.",
        "Quanto costa un biglietto per Bologna domani?",
        "Ricordami di innaffiare il basilico sul balcone.",
    ),
    "en": (
        "Tonight I'll stop at the pharmacy before dinner.",
        "The new bridge is closed for repairs until March.",
        "My sister always chooses pistachio ice cream.",
        "The kitchen alarm rings at half past six.",
        "How much is a ticket to Bologna tomorrow?",
        "Remind me to water the basil on the balcony.",
    ),
}
_TITLES = {
    "it": {
        "A": "Frasi in italiano, voce normale",
        "B": "Frasi in inglese",
        "C": "Parla liberamente",
        "D": "Voce da bar rumoroso",
        "E": "Sussurrato (facoltativo)",
    },
    "en": {
        "A": "Italian sentences, normal voice",
        "B": "English sentences",
        "C": "Speak freely",
        "D": "Noisy-bar voice",
        "E": "Whispered (optional)",
    },
}


def script(lang: Literal["it", "en"]) -> dict[str, object]:
    """GET /v1/voice/enrolment/script: the blocks, their sentences (or prompts) and what each needs."""
    other = "en" if lang == "it" else "it"
    blocks = [
        _block(lang, "A", "it", list(_IT_A)),
        _block(lang, "B", "en", list(_EN_B)),
        {**_block(lang, "C", lang, [*_FREE[lang], *_FREE[other]]), "seconds_per_language": FREE_SPEECH_S},
        _block(lang, "D", lang, list(_LOUD[lang])),
        {**_block(lang, "E", lang, list(_WHISPER[lang])), "optional": True},
    ]
    mini = [
        {**_block(lang, "A", "it", list(_IT_A[:5])), "min_voiced_s": MINI_VOICED_S["A"]},
        {
            **_block(lang, "C", lang, [_FREE[lang][0], _FREE[other][0]]),
            "seconds_per_language": 15,
            "min_voiced_s": MINI_VOICED_S["C"],
        },
    ]
    return {"lang": lang, "blocks": blocks, "mini": mini, "test": list(_TEST[lang])}


def _block(ui: str, block: Block, spoken: str, sentences: list[str]) -> dict[str, object]:
    return {
        "block": block,
        "title": _TITLES[ui][block],
        "language": spoken,
        "sentences": sentences,
        "optional": False,
        "min_voiced_s": MIN_VOICED_S[block],
    }


@dataclass(frozen=True)
class Quality:
    block: str
    voiced_s: float
    snr_db: float
    clipped: bool
    ok: bool
    hint: Hint | None

    def message(self) -> dict[str, object]:
        return {
            "type": "enrol.quality",
            "block": self.block,
            "voiced_s": round(self.voiced_s, 1),
            "snr_db": round(self.snr_db, 1),
            "clipped": self.clipped,
            "ok": self.ok,
            "hint": self.hint,
        }


def quality(block: Block, pcm: np.ndarray, voiced: np.ndarray, *, mini: bool = False) -> Quality:
    """A block's quality as it ends. `pcm`: int16 samples; `voiced`: one VAD flag per sample-aligned frame
    (the frames tile `pcm`). Hints, in order: clipped → quieter; too little voice → again; a faint voice →
    louder; a noisy block → closer."""
    x = pcm.astype(np.float32) / 32768.0
    frames = len(voiced)
    size = len(x) // frames if frames else 0
    if size == 0:
        return Quality(block, 0.0, 0.0, False, False, "again")
    tiles = x[: size * frames].reshape(frames, size)
    rms = np.sqrt(np.mean(tiles**2, axis=1) + 1e-12)
    speech = rms[voiced] if voiced.any() else np.array([1e-6])
    noise = rms[~voiced] if (~voiced).any() else np.array([1e-6])
    voiced_s = float(voiced.sum() * size / RATE)
    level = 20 * np.log10(float(np.sqrt(np.mean(speech**2))) + 1e-12)
    snr = 20 * np.log10(float(np.sqrt(np.mean(speech**2))) / max(float(np.median(noise)), 1e-6))
    clipped = float(np.mean(np.abs(x) >= CLIP_LEVEL)) > CLIP_SHARE
    need = (MINI_VOICED_S if mini else MIN_VOICED_S).get(block, MIN_VOICED_S[block])
    hint: Hint | None = None
    if clipped:
        hint = "quieter"
    elif voiced_s < need:
        hint = "again"
    elif level < LOW_LEVEL_DBFS:
        hint = "louder"
    elif snr < QUIET_SNR_DB:
        hint = "closer"
    return Quality(block, voiced_s, float(snr), clipped, hint is None, hint)


def held_out(spans: list[tuple[int, int]]) -> list[bool]:
    """Which of a block's sentences (its start/end spans, in order) are held out: every fifth, the last
    included when a block has fewer than five (20%, whole sentences, never the first)."""
    n = len(spans)
    if n < 2:
        return [False] * n
    step = round(1 / HELD_OUT)
    out = [(i + 1) % step == 0 for i in range(n)]
    if not any(out):
        out[-1] = True
    return out
