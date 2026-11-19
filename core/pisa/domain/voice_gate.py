"""The user-voice gate's rules. Only their voice starts a turn or stops Pisa. Pure
rules and arithmetic (numpy), no I/O: the service feeds it one hop of evidence every 200 ms and acts on what
it decides.

- **Score**: the max over the route's prints of the AS-normed cosine (`as_norm`) of the last 1-1.5 s of voiced
  audio, against a cohort of public voices.
- **States**: `IDLE` (Pisa silent), `PISA_SPEAKING`, `OWNER_TURN`.
- **Turn start**: ≥ 300 ms voiced and the score ≥ θ (the window's threshold). The turn opens with the audio
  from 300 ms before the voice's onset (the pre-roll), so their first syllables are never cut.
- **Barge-in** (Pisa speaking): stricter, ≥ 350 ms voiced and the score ≥ θ+δ on two hops in a row.
- **Turn end**: owner-silence counts strangers' speech as silence (a voiced hop below θ-margin is not theirs).
  The turn closes on ≥ 500 ms of owner-silence with Smart Turn saying complete, or 1.5 s regardless, or after
  60 s.
- **What the phone sees** (`talk.gate`): owner / other / none, at most twice a second.

Nothing here holds audio or words: hops carry times, counts and scores only.
"""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

import numpy as np

# the ear's audio, 16 kHz mono PCM: the gate, the enrolment, the voice models, the talker's input
RATE = 16_000
# 100 ms hops, so a turn starts at 300 ms of voice and a barge-in at 350 (with 200 ms hops both
# landed at 400); inside their turn the service scores every other hop (an embedding every 200 ms)
HOP_MS = 100
WINDOWS_S = (1.0, 1.5)  # the voiced window an embedding reads: 1 s while short, 1.5 s once there is that much
RING_S = 3.0  # the audio kept in memory (strangers' too, never stored): the pre-roll and the windows
PREROLL_MS = 300  # the turn opens with the audio from 300 ms before their first scored hop of this run…
PREROLL_MAX_MS = 700  # … and never more than 700 ms before it opens, whatever the VAD's onset
LOG_MAX = 500  # the gate's own log keeps the latest decisions
START_VOICED_MS = 300  # turn start: at least this much voiced
BARGE_VOICED_MS = 350  # barge-in: stricter
BARGE_HOPS = 2  # … with θ+δ on two hops in a row
END_SILENCE_MS = 500  # owner-silence after which Smart Turn is asked
END_ANYWAY_MS = 1500  # owner-silence that closes the turn whatever Smart Turn says
TURN_CAP_MS = 60_000  # a turn never runs longer
HOP_SCORE_MS = 200  # the per-hop score reads the latest 200 ms (this hop, the one before)
TAIL_HOPS = 2  # … and a stranger's tail starts after two low hops in a row
HEARD_EVERY_MS = 500  # talk.gate at most twice a second
COHORT_TOP_K = 200  # AS-norm's top impostors (the engineering report §2.2: 200-300)


class State(StrEnum):
    IDLE = "idle"
    PISA_SPEAKING = "pisa_speaking"
    OWNER_TURN = "owner_turn"


Heard = Literal["owner", "other", "none"]


@dataclass(frozen=True)
class Thresholds:
    """A route's calibration: θ per window length (shorter windows stricter), the margin
    below θ that still counts as their voice inside their turn (hysteresis), and δ above θ for a barge-in."""

    theta: dict[float, float]  # window seconds → θ (AS-normed score)
    margin: float
    delta: float

    def at(self, window_s: float) -> float:
        """θ for a window: the threshold of the longest calibrated window not longer than it (the shortest's
        otherwise)."""
        fitting = [w for w in self.theta if w <= window_s + 1e-6]
        return self.theta[max(fitting) if fitting else min(self.theta)]

    def stored(self) -> dict[str, object]:
        return {
            "theta": {f"{w:.1f}": t for w, t in self.theta.items()},
            "margin": self.margin,
            "delta": self.delta,
        }

    @staticmethod
    def of(stored: dict[str, object]) -> "Thresholds":
        theta = stored["theta"]
        assert isinstance(theta, dict)
        return Thresholds(
            {float(w): float(t) for w, t in theta.items()},
            float(stored["margin"]),  # type: ignore[arg-type]
            float(stored["delta"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class Hop:
    """One hop's evidence. `run_ms`: voiced ms in the current run of voice (0 in silence); `onset_ms`: when
    that run began; `score`: the user score of the latest window (None with no voice to score);
    `hop_score`: inside their turn, the score of the latest HOP_SCORE_MS alone, which
    turns at once when a stranger follows them, where the 1-1.5 s window lags."""

    at_ms: int
    voiced: bool
    run_ms: int
    onset_ms: int | None
    score: float | None
    window_s: float
    pisa_speaking: bool
    hop_score: float | None = None


@dataclass(frozen=True)
class Open:
    """Their turn opens: `activityStart`, then the audio from `preroll_from_ms`. A barge-in stops Pisa's
    playout on the phone first (`talk.playout_stop`)."""

    barge: bool
    preroll_from_ms: int
    score: float
    voiced_ms: int


@dataclass(frozen=True)
class Close:
    """Their turn ends: `activityEnd`. `reason`: complete (Smart Turn), silence (1.5 s regardless) or cap."""

    reason: Literal["complete", "silence", "cap"]
    owner_silence_ms: int
    turn_ms: int


@dataclass(frozen=True)
class Shown:
    heard: Heard


Decision = Open | Close | Shown


@dataclass
class Gate:
    """The state machine. `hop` takes one hop and returns what to do; `complete` is Smart Turn's verdict on
    their turn so far, asked only after END_SILENCE_MS of owner-silence."""

    thresholds: Thresholds
    state: State = State.IDLE
    turn_from_ms: int = 0
    owner_silence_ms: int = 0
    _barge_hops: int = 0
    _last_owner: bool = False
    _shown: Heard | None = None
    _shown_at: int = -HEARD_EVERY_MS
    _last_at: int | None = None
    _step: int = HOP_MS
    _his_from: int | None = None  # this run's first hop scored as theirs (θ-margin or above): its start
    # decisions, scores and reasons (no audio), bounded
    log: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=LOG_MAX))

    @property
    def last_owner(self) -> bool:
        """Whether the last hop in their turn was theirs (a hop too short to score keeps the run's owner)."""
        return self._last_owner

    def hop(self, h: Hop, complete: Callable[[], bool] = lambda: False) -> list[Decision]:
        out: list[Decision] = []
        step = h.at_ms - self._last_at if self._last_at is not None else HOP_MS
        self._last_at = h.at_ms
        if not h.voiced or h.onset_ms is None:
            self._his_from = None  # the run ended: the next one finds its own first hop
        elif self._his_from is None and h.score is not None and h.score >= self._low(h):
            self._his_from = h.at_ms - step
        self._step = step
        if self.state is State.OWNER_TURN:
            out += self._in_turn(h, complete)
        else:
            self.state = State.PISA_SPEAKING if h.pisa_speaking else State.IDLE
            out += self._waiting(h)
        heard: Heard = (
            "owner"
            if self.state is State.OWNER_TURN and self._last_owner
            else ("other" if h.voiced else "none")
        )
        if heard != self._shown and h.at_ms - self._shown_at >= HEARD_EVERY_MS:
            self._shown, self._shown_at = heard, h.at_ms
            out.append(Shown(heard))
        return out

    def _waiting(self, h: Hop) -> list[Decision]:
        theta = self.thresholds.at(h.window_s)
        if not h.voiced or h.score is None or h.onset_ms is None:
            self._barge_hops = 0
            return []
        if self.state is State.PISA_SPEAKING:
            if h.score >= theta + self.thresholds.delta:
                self._barge_hops += 1
            else:
                self._barge_hops = 0
            if h.run_ms >= BARGE_VOICED_MS and self._barge_hops >= BARGE_HOPS:
                return [self._open(h, barge=True)]
            self._note(h, "barge_wait" if self._barge_hops else "not_owner", theta)
            return []
        if h.run_ms >= START_VOICED_MS and h.score >= theta:
            return [self._open(h, barge=False)]
        self._note(h, "too_short" if h.score >= theta else "not_owner", theta)
        return []

    def _note(self, h: Hop, reason: str, theta: float) -> None:
        if h.voiced and h.score is not None:
            self.log.append(
                {
                    "at": h.at_ms,
                    "decision": "reject",
                    "reason": reason,
                    "score": round(h.score, 3),
                    "theta": round(theta, 3),
                    "voiced_ms": h.run_ms,
                }
            )

    def _low(self, h: Hop) -> float:
        return self.thresholds.at(h.window_s) - self.thresholds.margin

    def _open(self, h: Hop, *, barge: bool) -> Open:
        assert h.onset_ms is not None and h.score is not None
        self.state, self.turn_from_ms = State.OWNER_TURN, h.at_ms
        self.owner_silence_ms, self._barge_hops, self._last_owner = 0, 0, True
        his = self._his_from if self._his_from is not None else h.at_ms - self._step
        start = max(0, his - PREROLL_MS, h.at_ms - PREROLL_MAX_MS)  # never 3 s of babble before them
        opened = Open(barge, start, h.score, h.run_ms)
        self.log.append(
            {
                "at": h.at_ms,
                "decision": "barge_in" if barge else "turn_start",
                "score": round(h.score, 3),
                "voiced_ms": h.run_ms,
            }
        )
        return opened

    def _in_turn(self, h: Hop, complete: Callable[[], bool]) -> list[Decision]:
        low = self._low(h)
        if h.voiced and h.score is not None:
            owner = h.score >= low
        else:
            owner = h.voiced and self._last_owner and h.score is None  # too little to score: the run goes on
        self._last_owner = owner
        self.owner_silence_ms = 0 if owner else self.owner_silence_ms + self._step
        turn_ms = h.at_ms - self.turn_from_ms
        reason: Literal["complete", "silence", "cap"] | None = None
        if turn_ms >= TURN_CAP_MS:
            reason = "cap"
        elif self.owner_silence_ms >= END_ANYWAY_MS:
            reason = "silence"
        elif self.owner_silence_ms >= END_SILENCE_MS and complete():
            reason = "complete"
        if reason is None:
            return []
        self.state = State.PISA_SPEAKING if h.pisa_speaking else State.IDLE
        self._last_owner = False
        self.log.append(
            {
                "at": h.at_ms,
                "decision": "turn_end",
                "reason": reason,
                "owner_silence_ms": self.owner_silence_ms,
                "turn_ms": turn_ms,
            }
        )
        return [Close(reason, self.owner_silence_ms, turn_ms)]

    def turn_ended(self, at_ms: int) -> None:
        """The turn closed from outside (the talk ended, a tap): back to waiting."""
        if self.state is State.OWNER_TURN:
            self.state = State.IDLE
            self.log.append({"at": at_ms, "decision": "turn_end", "reason": "outside"})


def stranger_tail(h: Hop, low: float, in_tail: bool, after_his: bool, low_run: int) -> tuple[bool, int]:
    """Voiced hops of their open turn whose own score is below θ-margin, TAIL_HOPS in a row
    right after their speech (the last hop scored on its own was theirs), are a stranger's: sent as silence,
    even while the lagging 1-1.5 s window still says "theirs". One low hop of their own (a quiet or
    falling word end) never starts it. The tail lasts until a hop scores as their again; a hop with no score
    of its own changes nothing. Returns the tail and the run of low hops."""
    if not h.voiced:
        return False, 0
    if h.hop_score is None:
        return in_tail, low_run
    if h.hop_score >= low or not (after_his or in_tail or low_run):
        return False, 0
    return in_tail or low_run + 1 >= TAIL_HOPS, low_run + 1


# --- scoring ----------------------------------------------------------------------------------------------


def unit(v: np.ndarray) -> np.ndarray:
    """Rows scaled to length 1 (cosine becomes a dot product)."""
    v = np.asarray(v, dtype=np.float32)
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(norm, 1e-9)


@dataclass(frozen=True)
class Stats:
    mean: float
    std: float


def cohort_stats(emb: np.ndarray, cohort: np.ndarray, k: int = COHORT_TOP_K) -> Stats:
    """AS-norm's side statistics: the mean and spread of an embedding's top-k cosine scores against the cohort
    (unit rows)."""
    scores = cohort @ unit(emb)
    top = np.sort(scores)[-min(k, len(scores)) :]
    return Stats(float(top.mean()), float(max(top.std(), 1e-3)))


@dataclass(frozen=True)
class Print:
    """One voiceprint of a route (normal, loud, whisper): its unit vector and its cohort statistics."""

    kind: str
    vector: np.ndarray
    stats: Stats
    trust: float = 1.0  # E (whisper) is low-trust: its score counts less


def as_norm(raw: float, enrol: Stats, test: Stats) -> float:
    """Adaptive symmetric normalisation of one cosine score."""
    return 0.5 * ((raw - enrol.mean) / enrol.std + (raw - test.mean) / test.std)


def owner_score(emb: np.ndarray, prints: Sequence[Print], cohort: np.ndarray) -> float:
    """The user score of one window's embedding: the max over their prints of the AS-normed cosine (a
    low-trust print counts at its trust)."""
    e = unit(emb)
    test = cohort_stats(e, cohort)
    return max(as_norm(float(p.vector @ e), p.stats, test) * p.trust for p in prints)


def make_prints(
    embeddings: dict[str, list[np.ndarray]], cohort: np.ndarray, trust: dict[str, float] | None = None
) -> list[Print]:
    """Their prints from the enrolment's embeddings, by kind: each the unit mean of its windows."""
    out = []
    for kind, embs in embeddings.items():
        if not embs:
            continue
        vector = unit(np.mean(unit(np.stack(embs)), axis=0))
        out.append(Print(kind, vector, cohort_stats(vector, cohort), (trust or {}).get(kind, 1.0)))
    return out


# --- what comes after the gate: the turns Pisa never answers
# -----------------------------------------

LANGS = frozenset({"it", "en"})
WEAK_MARGIN = 1.0  # a score this close to θ (AS-norm units) is weak: a strange language then drops the turn
_YES_NO = frozenset({"si", "sì", "no", "yes", "ok", "okay", "certo", "va", "bene", "nope", "yeah", "yep"})
_STOP = frozenset({"stop", "basta", "aspetta", "wait", "fermo", "fermati", "zitto", "silenzio"})


def dropped(
    words: Sequence[str], language: str | None, score: float | None, theta: float, *, echo: bool
) -> Literal["language", "fragment", "echo"] | None:
    """Why a gated turn gets no reply at all (None: it goes on to the brain). Silent, always:
    - a language other than Italian or English with a weak owner score;
    - a fragment: under two words, unless a stop or a yes/no word;
    - Pisa's own last line come back (echo)."""
    if echo:
        return "echo"
    lang = (language or "").split("-")[0].casefold()
    weak = score is None or score < theta + WEAK_MARGIN
    if lang and lang not in LANGS and weak:
        return "language"
    if len(words) < 2 and not (words and (words[0] in _YES_NO or words[0] in _STOP)):
        return "fragment"
    return None
