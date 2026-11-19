"""Pisa knows its user's voice. Only their voice starts a turn or stops Pisa.

- `VoiceStore`: their consent, the prints per route (sealed), the enrolment recordings (sealed, deletable),
  and the public cohort and babble (maintenance → voice-cohort). Erase family `voice`.
- `Hopper`: the phone's audio in memory only (a 3 s ring), Silero VAD, and every 200 ms one hop of evidence:
  voiced or not, the voiced run, and the user score of the last 1-1.5 s of voice.
- `LiveGate`: a talk's gate. It feeds the hops to `pisa.domain.voice_gate.Gate` and acts: `activityStart` with
  the pre-roll, the audio of their turn only, `activityEnd`; `talk.playout_stop` first on a barge-in;
  `talk.gate` for the phone. Strangers' audio never leaves the ring: never sent to the talker, never stored,
  never logged.
- `Enrolment`: an enrol session (no talker, no model money): the blocks the phone names are recorded, judged
  (`enrol.quality`), the 60-second check scored (`enrol.test_result`), and at the end the prints made, the
  route's thresholds calibrated on its bench (`enrol.done`), all stored.
- `bench`: the bench's streams (their held-out speech in babble at +10…-5 dB through a 16 kHz band-limit, and
  strangers alone) as hops, for `pisa.domain.voice_bench`.

Logs carry ids, counts, scores and timings only.
"""

import asyncio
import dataclasses
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import numpy as np
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import voice_bench as bench_rules
from pisa.domain import voice_enrol as enrol_rules
from pisa.domain import voice_gate as gate_rules
from pisa.domain.errors import PisaError
from pisa.domain.voice_gate import RATE
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.voice import Embedder, TurnEnd, Vad

_log = get_logger("pisa.voice")
HOP = RATE * gate_rules.HOP_MS // 1000
VOICED_PROB = 0.5  # Silero's speech probability
RUN_GAP_MS = 400  # a voiced run ends after this much unvoiced audio: a breath doesn't move the onset
TURN_COMPLETE = 0.5  # Smart Turn's probability that counts as complete
BENCH_STRANGER_S = 120.0  # the enrolment's bench: two minutes of strangers alone
BENCH_BABBLE_CLIPS = 12
TEST_STRANGERS = 12  # single public strangers the enrolment's test replays alone


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


# --- the store
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    route: str
    model: str
    model_version: str
    prints: list[gate_rules.Print]
    thresholds: gate_rules.Thresholds
    samples_s: float
    ok: bool
    enrolled_at: datetime
    bench: dict[str, Any]


class VoiceStore:
    def __init__(self, pool: AsyncConnectionPool, keys: KeyStore) -> None:
        self._pool = pool
        self._keys = keys

    async def consent(self, version: str, device_id: uuid.UUID | None) -> datetime:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO voice_consents (id, version, device_id) VALUES (%s, %s, %s) RETURNING "
                    "created_at",
                    (uuid.uuid4(), version, device_id),
                )
            ).fetchone()
        assert row is not None
        return row[0]

    async def consent_at(self, conn: AsyncConnection | None = None) -> datetime | None:
        async def read(c: AsyncConnection) -> datetime | None:
            row = await (await c.execute("SELECT max(created_at) FROM voice_consents")).fetchone()
            return row[0] if row else None

        if conn is not None:
            return await read(conn)
        async with self._pool.connection() as c:
            return await read(c)

    async def routes(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT route, created_at, samples_s, ok FROM voice_profiles ORDER BY route"
                )
            ).fetchall()
        return [
            {"route": r[0], "enrolled_at": r[1], "samples_s": round(float(r[2]), 1), "ok": r[3]} for r in rows
        ]

    async def profile(self, route: str, model: str, version: str) -> Profile | None:
        """The route's profile, when it was made by this model (a print never meets another model's)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT model, model_version, prints, calibration, samples_s, ok, created_at, bench FROM "
                    "voice_profiles WHERE route = %s",
                    (route,),
                )
            ).fetchone()
        if row is None or row[0] != model or row[1] != version:
            return None
        sealed = json.loads(self._keys.unseal(bytes(row[2])))
        prints = [
            gate_rules.Print(
                p["kind"],
                np.asarray(p["vector"], dtype=np.float32),
                gate_rules.Stats(p["mean"], p["std"]),
                p["trust"],
            )
            for p in sealed
        ]
        return Profile(
            route,
            row[0],
            row[1],
            prints,
            gate_rules.Thresholds.of(row[3]),
            float(row[4]),
            row[5],
            row[6],
            row[7],
        )

    async def save_profile(
        self,
        route: str,
        model: str,
        version: str,
        prints: Sequence[gate_rules.Print],
        thresholds: gate_rules.Thresholds,
        bench: dict[str, Any],
        samples_s: float,
        ok: bool,
    ) -> None:
        sealed = self._keys.seal(
            json.dumps(
                [
                    {
                        "kind": p.kind,
                        "vector": [round(float(v), 6) for v in p.vector],
                        "mean": p.stats.mean,
                        "std": p.stats.std,
                        "trust": p.trust,
                    }
                    for p in prints
                ]
            )
        )
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO voice_profiles (route, model, model_version, prints, calibration, bench, "
                "samples_s, ok) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (route) DO UPDATE SET "
                "model = EXCLUDED.model, model_version = EXCLUDED.model_version, prints = EXCLUDED.prints, "
                "calibration = EXCLUDED.calibration, bench = EXCLUDED.bench, samples_s = EXCLUDED.samples_s, "
                "ok = EXCLUDED.ok, created_at = now()",
                (route, model, version, sealed, Jsonb(thresholds.stored()), Jsonb(bench), samples_s, ok),
            )

    async def save_recording(
        self, route: str, block: str, pcm: bytes, spans: list[tuple[int, int]], held: list[bool]
    ) -> uuid.UUID:
        ident = uuid.uuid4()
        sealed = self._keys.seal_bytes(pcm)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO voice_recordings (id, route, block, pcm, seconds, spans, held_out) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s)",
                (ident, route, block, sealed, len(pcm) / 2 / RATE, Jsonb(spans), Jsonb(held)),
            )
        return ident

    async def recordings(self, route: str) -> list[tuple[str, np.ndarray, list[tuple[int, int]], list[bool]]]:
        """The route's latest enrolment: each block's audio (the newest recording of each), its spans and
        which are held out."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT DISTINCT ON (block) block, pcm, spans, held_out FROM voice_recordings WHERE "
                    "route = %s ORDER BY block, created_at DESC",
                    (route,),
                )
            ).fetchall()
        return [
            (r[0], pcm_to_float(self._keys.unseal_bytes(bytes(r[1]))), [tuple(s) for s in r[2]], r[3])
            for r in rows
        ]

    async def delete_route(self, route: str) -> int:
        async with self._pool.connection() as conn:
            gone = (await conn.execute("DELETE FROM voice_profiles WHERE route = %s", (route,))).rowcount
            gone += (await conn.execute("DELETE FROM voice_recordings WHERE route = %s", (route,))).rowcount
        return gone

    async def delete_all(self) -> int:
        """Everything of their voice at once: prints, recordings and the consent (the gate is off after)."""
        async with self._pool.connection() as conn, conn.transaction():
            gone = 0
            for table in ("voice_profiles", "voice_recordings", "voice_consents"):
                gone += (await conn.execute(f"DELETE FROM {table}")).rowcount  # noqa: S608 (fixed names)
        return gone

    async def cohort(self, model: str) -> np.ndarray:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT embedding FROM voice_cohort WHERE model = %s", (model,))
            ).fetchall()
        if not rows:
            return np.zeros((0, 0), dtype=np.float32)
        return gate_rules.unit(np.stack([np.frombuffer(bytes(r[0]), dtype="<f4") for r in rows]))

    async def babble(self, limit: int = BENCH_BABBLE_CLIPS) -> list[np.ndarray]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT pcm FROM voice_babble WHERE talkers > 1 ORDER BY id LIMIT %s", (limit,)
                )
            ).fetchall()
        return [pcm_to_float(bytes(r[0])) for r in rows]

    async def strangers(self, limit: int = TEST_STRANGERS) -> list[np.ndarray]:
        """Single public strangers (FLEURS test, one talker each) the test replays alone."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT pcm FROM voice_babble WHERE talkers = 1 ORDER BY id LIMIT %s", (limit,)
                )
            ).fetchall()
        return [pcm_to_float(bytes(r[0])) for r in rows]

    async def has_profile(self, route: str, version: str) -> bool:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM voice_profiles WHERE route = %s AND model_version = %s AND ok",
                    (route, version),
                )
            ).fetchone()
        return row is not None


# --- the models together
# -------------------------------------------------------------------------------------


@dataclass
class Models:
    """The gate's models (pisa.connectors.voice_models in production, fakes in tests). `vad` makes a fresh
    stateful VAD per stream, cheaply: its model is built once, with the rest, in `VoiceIdService.loaded`'s
    thread, never on the event loop."""

    embedder: Embedder
    vad: Callable[[], Vad]
    turn: TurnEnd | None


@dataclass
class Scorer:
    prints: list[gate_rules.Print]
    cohort: np.ndarray
    embedder: Embedder

    def __call__(self, window: np.ndarray) -> float:
        return gate_rules.owner_score(self.embedder.embed(window), self.prints, self.cohort)


# --- the hopper: audio → hops
# ---------------------------------------------------------------------------------


@dataclass
class Hopper:
    """The audio in memory (a 3 s ring, never stored) → one hop every 100 ms. `score` is None to make hops
    with no scores (enrolment: VAD only)."""

    vad: Vad
    score: Callable[[np.ndarray], float] | None
    ring: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    flags: list[bool] = field(default_factory=list)  # one per VAD frame in the ring
    at_ms: int = 0  # the time of the ring's end (ms of audio since the stream began)
    _pending: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _hop_fill: int = 0
    _onset_ms: int | None = None
    _quiet_ms: int = 0
    _hops: int = 0
    every: int = 1  # score every hop (waiting for them) or every other (inside their turn)
    per_hop: bool = False  # inside their turn: each voiced hop also scored alone

    @property
    def ring_start_ms(self) -> int:
        return self.at_ms - len(self.ring) * 1000 // RATE

    def push(self, samples: np.ndarray) -> list[gate_rules.Hop]:
        """New audio; the hops completed by it (usually zero or one)."""
        out: list[gate_rules.Hop] = []
        self._pending = np.concatenate([self._pending, samples])
        frame = self.vad.frame
        while len(self._pending) >= frame:
            chunk, self._pending = self._pending[:frame], self._pending[frame:]
            self.flags.append(self.vad.prob(chunk) >= VOICED_PROB)
            self.ring = np.concatenate([self.ring, chunk])
            self.at_ms += frame * 1000 // RATE
            keep = int(gate_rules.RING_S * RATE) // frame * frame
            if len(self.ring) > keep:
                drop = (len(self.ring) - keep) // frame
                self.ring = self.ring[drop * frame :]
                self.flags = self.flags[drop:]
            self._hop_fill += frame
            if self._hop_fill >= HOP:
                self._hop_fill -= HOP
                out.append(self._hop())
        return out

    def _hop(self) -> gate_rules.Hop:
        frame = self.vad.frame
        recent = self.flags[-max(1, HOP // frame) :]
        voiced = sum(recent) * 2 >= len(recent)
        self._hops += 1
        if voiced:
            if self._onset_ms is None:  # the run's first voiced frame, back through the ring
                first = len(self.flags) - 1
                while first > 0 and not self.flags[first]:
                    first -= 1
                while first > 0 and self.flags[first - 1]:
                    first -= 1
                self._onset_ms = self.ring_start_ms + first * frame * 1000 // RATE
            self._quiet_ms = 0
        else:
            self._quiet_ms += gate_rules.HOP_MS
            if self._quiet_ms >= RUN_GAP_MS:
                self._onset_ms = None
        window, window_s = self.window()
        score = None
        if voiced and self.score is not None and (self.every == 1 or self._hops % self.every == 0):
            score = self.score(window)
        run = self.at_ms - self._onset_ms if voiced and self._onset_ms is not None else 0
        hop_score = None
        if voiced and self.per_hop and self.score is not None:
            hop_score = self.score(self.ring[-gate_rules.HOP_SCORE_MS * RATE // 1000 :])
        return gate_rules.Hop(
            self.at_ms, voiced, run, self._onset_ms if voiced else None, score, window_s, False, hop_score
        )

    def window(self) -> tuple[np.ndarray, float]:
        """The last 1-1.5 s of voice in the ring (1.5 s once there is that much; else 1 s of the latest audio,
        voice or not)."""
        frame = self.vad.frame
        voiced = [self.ring[i * frame : (i + 1) * frame] for i, f in enumerate(self.flags) if f]
        voice = np.concatenate(voiced) if voiced else np.zeros(0, dtype=np.float32)
        longest = max(gate_rules.WINDOWS_S)
        if len(voice) >= longest * RATE:
            return voice[-int(longest * RATE) :], longest
        shortest = min(gate_rules.WINDOWS_S)
        if len(voice) >= shortest * RATE:
            return voice[-int(shortest * RATE) :], shortest
        return self.ring[-int(shortest * RATE) :], shortest

    def between(self, from_ms: int, to_ms: int | None = None) -> np.ndarray:
        """The ring's audio from `from_ms` to `to_ms` (its end by default), what is still there of it."""
        start = max(0, (from_ms - self.ring_start_ms) * RATE // 1000)
        end = len(self.ring) if to_ms is None else max(start, (to_ms - self.ring_start_ms) * RATE // 1000)
        return self.ring[start:end]


# --- the live gate
# -------------------------------------------------------------------------------------------


class GateTarget(Protocol):
    """What the gate drives in a talk (pisa.services.talk.TalkRun implements it)."""

    async def activity_start(self, barge: bool, score: float) -> None: ...

    async def owner_audio(self, pcm: bytes) -> None: ...

    async def activity_end(self, reason: str) -> None: ...

    def pisa_speaking(self) -> bool: ...

    def phone(self, message: dict[str, Any]) -> None: ...

    def timeline_gate(self, **data: Any) -> None: ...

    def gate_failed(self, detail: str) -> None: ...

    def profile_gone(self) -> None: ...


GATE_ERRORS_MAX = 3  # the gate failing this often in one talk ends it


@dataclass
class Step:
    """One hop worked out off the event loop: the hop, the gate's decisions, and the audio of the hop for
    the talker when their turn is open (a voiced hop that isn't theirs is silence)."""

    hop: gate_rules.Hop
    decisions: list[gate_rules.Decision]
    out: np.ndarray | None


@dataclass
class LiveGate:
    """One talk's gate. `feed` takes the phone's audio (16-bit PCM, 16 kHz) as it arrives. The VAD, the
    embeddings and Smart Turn run in a worker thread, never on the event loop. Their turn's audio
    reaches the talker one hop late (≤ 100 ms), hop by hop: a voiced hop scored as not their goes as silence.
    """

    models: Models
    profile: Profile
    cohort: np.ndarray
    target: GateTarget
    still: Callable[[], Awaitable[bool]] | None = None  # the profile still there (checked at each turn start)
    gate: gate_rules.Gate = field(init=False)
    hopper: Hopper = field(init=False)
    _open: bool = False
    _sent_to: int = 0  # the ring time their turn's audio was sent up to (ms)
    _turn: list[np.ndarray] = field(default_factory=list)  # their turn so far, strangers zeroed (Smart Turn)
    _tail: bool = False  # a stranger's speech right after theirs, by the per-hop score
    _his_hop: bool = False  # … the last hop scored on its own was theirs
    _low_hops: int = 0  # … the low hops in a row since (two start the tail)
    _run_best: float | None = None
    _run_voiced: int = 0
    last_score: float | None = None  # their turn's opening score (a weak score)
    opens: int = 0
    rejected_runs: int = 0
    errors: int = 0

    def __post_init__(self) -> None:
        self.gate = gate_rules.Gate(self.profile.thresholds)
        self.hopper = Hopper(
            self.models.vad(), Scorer(self.profile.prints, self.cohort, self.models.embedder)
        )

    @property
    def theta(self) -> float:
        return self.profile.thresholds.at(min(gate_rules.WINDOWS_S))

    @property
    def is_open(self) -> bool:
        return self._open

    async def close_now(self, reason: str = "outside") -> None:
        """Their turn closes here (the talk ends, the phone's audio stopped, the gate failed); its words still
        come."""
        if self._open:
            self._open, self._turn, self._tail, self._his_hop, self._low_hops = False, [], False, False, 0
            self.hopper.every, self.hopper.per_hop = 1, False
            self.gate.turn_ended(self.hopper.at_ms)
            await self.target.activity_end(reason)
            self.target.timeline_gate(decision="turn_end", reason=reason)

    async def feed(self, pcm: bytes) -> None:
        samples = pcm_to_float(pcm)
        speaking = self.target.pisa_speaking()
        try:
            steps = await asyncio.to_thread(self._work, samples, speaking)
        except Exception as err:  # a model failed: their open turn closes, the talk goes on
            self.errors += 1
            self.hopper = Hopper(
                self.models.vad(), Scorer(self.profile.prints, self.cohort, self.models.embedder)
            )
            self.gate = gate_rules.Gate(self.profile.thresholds)
            _log.warning("voice_gate_failed", error=type(err).__name__, errors=self.errors)
            self.target.timeline_gate(decision="error", error=type(err).__name__, errors=self.errors)
            await self.close_now("error")
            if self.errors >= GATE_ERRORS_MAX:
                self.target.gate_failed(f"the voice gate failed {self.errors} times ({type(err).__name__})")
            return
        for step in steps:
            await self._act(step)

    def _work(self, samples: np.ndarray, speaking: bool) -> list[Step]:
        """Off the loop: the hops this audio completes, each with its decisions and its audio."""
        steps = []
        for raw in self.hopper.push(samples):
            hop = dataclasses.replace(raw, pisa_speaking=speaking)
            out = None
            if self._open:
                chunk = self.hopper.between(self._sent_to, hop.at_ms)
                low = self.theta - self.gate.thresholds.margin
                self._tail, self._low_hops = gate_rules.stranger_tail(
                    hop, low, self._tail, self._his_hop, self._low_hops
                )
                if hop.hop_score is not None:
                    self._his_hop = hop.hop_score >= low
                his = not self._tail and (
                    not hop.voiced
                    or (hop.score is None and self.gate.last_owner)
                    or (hop.score is not None and hop.score >= low)
                )
                out = chunk if his else np.zeros_like(chunk)
                self._sent_to = hop.at_ms
                if hop.voiced:
                    self._turn.append(out)
            decisions = self.gate.hop(hop, self._complete)
            steps.append(Step(hop, decisions, out))
        return steps

    def _complete(self) -> bool:
        if self.models.turn is None or not self._turn:
            return False
        return self.models.turn.complete(np.concatenate(self._turn[-80:])) >= TURN_COMPLETE

    async def _act(self, step: Step) -> None:
        hop = step.hop
        if step.out is not None and len(step.out):
            await self.target.owner_audio(float_to_pcm(step.out))
        if not self._open and hop.voiced and hop.score is not None:
            self._run_best = hop.score if self._run_best is None else max(self._run_best, hop.score)
            self._run_voiced = hop.run_ms
        elif not hop.voiced and self._run_best is not None and not self._open:
            # a run of voice that never opened a turn: one gate event (its best score), never its audio
            self.rejected_runs += 1
            self.target.timeline_gate(
                decision="reject",
                score=round(self._run_best, 3),
                theta=round(self.theta, 3),
                voiced_ms=self._run_voiced,
            )
            self._run_best = None
        for d in step.decisions:
            match d:
                case gate_rules.Open(barge=barge, preroll_from_ms=start, score=score, voiced_ms=voiced):
                    if self.still is not None and not await self.still():
                        # their prints were deleted under a live talk: no turn opens on them
                        self.gate.turn_ended(hop.at_ms)
                        self.target.profile_gone()
                        return
                    self._open, self._run_best, self.opens = True, None, self.opens + 1
                    self.hopper.every = 2  # inside their turn: an embedding every 200 ms …
                    self.hopper.per_hop = True  # … and each voiced hop's own (the stranger's tail)
                    self.last_score = score
                    if barge:
                        self.target.phone({"type": "talk.playout_stop"})  # the phone stops Pisa first
                    await self.target.activity_start(barge, score)
                    preroll = self.hopper.between(start, hop.at_ms)
                    self._sent_to = hop.at_ms
                    self._turn = [preroll]
                    await self.target.owner_audio(float_to_pcm(preroll))  # at once, then hop by hop
                    self.target.timeline_gate(
                        decision="barge_in" if barge else "turn_start",
                        score=round(score, 3),
                        voiced_ms=voiced,
                        preroll_ms=len(preroll) * 1000 // RATE,
                    )
                case gate_rules.Close(reason=reason, owner_silence_ms=silent, turn_ms=turn_ms):
                    self._open, self._turn, self._tail, self._his_hop, self._low_hops = (
                        False,
                        [],
                        False,
                        False,
                        0,
                    )
                    self.hopper.every, self.hopper.per_hop = 1, False
                    await self.target.activity_end(reason)
                    self.target.timeline_gate(
                        decision="turn_end", reason=reason, owner_silence_ms=silent, turn_ms=turn_ms
                    )
                case gate_rules.Shown(heard=heard):
                    self.target.phone({"type": "talk.gate", "heard": heard})


# --- the enrolment
# --------------------------------------------------------------------------------------------


def utterances(flags: Sequence[bool], frame: int, gap_frames: int = 12) -> list[tuple[int, int]]:
    """A block's utterances as sample spans: voiced runs split by at least `gap_frames` of silence (≈ 0.4
    s)."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    quiet = 0
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= gap_frames:
                spans.append((start * frame, (i - quiet + 1) * frame))
                start, quiet = None, 0
    if start is not None:
        spans.append((start * frame, (len(flags) - quiet) * frame))
    return [s for s in spans if s[1] - s[0] >= RATE // 4]


def embed_windows(
    embedder: Embedder, x: np.ndarray, seconds: float = 1.5, step_s: float = 0.75
) -> list[np.ndarray]:
    """Embeddings of overlapping windows over speech (the whole of it when shorter than one window)."""
    size, step = int(seconds * RATE), int(step_s * RATE)
    if len(x) <= size:
        return [embedder.embed(x)] if len(x) >= RATE // 2 else []
    return [embedder.embed(x[i : i + size]) for i in range(0, len(x) - size + 1, step)]


def voice_flags(vad: Vad, x: np.ndarray) -> np.ndarray:
    vad.reset()
    frame = vad.frame
    n = len(x) // frame
    return np.array([vad.prob(x[i * frame : (i + 1) * frame]) >= VOICED_PROB for i in range(n)], dtype=bool)


def stream_hops(models: Models, scorer: Callable[[np.ndarray], float], x: np.ndarray) -> list[gate_rules.Hop]:
    hopper = Hopper(models.vad(), scorer)
    out: list[gate_rules.Hop] = []
    for i in range(0, len(x), HOP):
        out += hopper.push(x[i : i + HOP])
    return out


def owner_trial(
    models: Models,
    scorer: Callable[[np.ndarray], float],
    utt: np.ndarray,
    snr_db: float | None,
    noise: np.ndarray | None,
    *,
    barge: bool = False,
) -> bench_rules.Trial:
    """One stream of theirs: LEAD_S before their utterance and TAIL_S after it, in babble at `snr_db` (their
    voiced level against the babble's), or in silence when `snr_db` is None (clean). Band-limited."""
    utt = bench_rules.band_limit(utt)
    need = len(utt) + int((bench_rules.LEAD_S + bench_rules.TAIL_S) * RATE)
    if snr_db is None or noise is None:
        x = np.zeros(need, dtype=np.float32)
        at = int(bench_rules.LEAD_S * RATE)
        x[at : at + len(utt)] = utt
    else:
        flags = voice_flags(models.vad(), utt)
        frame = models.vad().frame
        mask = np.zeros(len(utt), dtype=bool)
        mask[: len(flags) * frame] = np.repeat(flags, frame)
        level = bench_rules.rms(utt[mask]) if mask.any() else bench_rules.rms(utt)
        x = bench_rules.mix(utt, np.resize(noise, need), snr_db, level)
    hops = stream_hops(models, scorer, x)
    onset = int(bench_rules.LEAD_S * 1000)
    end = onset + len(utt) * 1000 // RATE
    return bench_rules.Trial(
        "owner", hops, len(x) / RATE, onset, end, snr_db, barge, _completer(models, x, onset)
    )


def bench_trials(
    models: Models,
    scorer: Callable[[np.ndarray], float],
    owner: Sequence[np.ndarray],
    babble: Sequence[np.ndarray],
    *,
    stranger_s: float = BENCH_STRANGER_S,
    seed: int = 7,
) -> list[bench_rules.Trial]:
    """The bench's streams as hops: each held-out utterance of their in babble at +10, +5, 0 and -5 dB (idle,
    and the 0 dB ones again while Pisa speaks: barge-in), then strangers alone for `stranger_s` (idle and
    while Pisa speaks), all band-limited. Smart Turn reads their gated audio when the gate asks."""
    rng = np.random.default_rng(seed)
    trials: list[bench_rules.Trial] = []
    clips = [bench_rules.band_limit(b) for b in babble] or [np.zeros(RATE, dtype=np.float32)]
    for i, utt in enumerate(owner):
        for snr in bench_rules.SNRS_DB:
            for barge in (False, True) if snr == 0.0 else (False,):
                noise = clips[int(rng.integers(len(clips)))]
                trials.append(owner_trial(models, scorer, utt, snr, noise, barge=barge))
        _log.debug("voice_bench_owner", n=i)
    trials += stranger_trials(models, scorer, clips, stranger_s)
    return trials


def stranger_trials(
    models: Models, scorer: Callable[[np.ndarray], float], clips: Sequence[np.ndarray], seconds: float
) -> list[bench_rules.Trial]:
    """Strangers alone for `seconds`, the clips in turn, half of them while Pisa speaks."""
    trials = []
    left, k = seconds, 0
    while left > 0 and clips:
        clip = clips[k % len(clips)]
        take = min(left, len(clip) / RATE)
        x = clip[: int(take * RATE)]
        trials.append(
            bench_rules.Trial("stranger", stream_hops(models, scorer, x), take, pisa_speaking=bool(k % 2))
        )
        left -= take
        k += 1
    return trials


def _completer(models: Models, x: np.ndarray, onset_ms: int) -> Callable[[int], bool]:
    cache: dict[int, bool] = {}

    def complete(at_ms: int) -> bool:
        if models.turn is None:
            return False
        if at_ms not in cache:
            start = max(0, (onset_ms - gate_rules.PREROLL_MS) * RATE // 1000)
            cache[at_ms] = models.turn.complete(x[start : at_ms * RATE // 1000]) >= TURN_COMPLETE
        return cache[at_ms]

    return complete


@dataclass
class Built:
    prints: list[gate_rules.Print]
    thresholds: gate_rules.Thresholds
    report: bench_rules.Report
    samples_s: float
    ok: bool


def make_route_prints(
    models: Models,
    cohort: np.ndarray,
    blocks: Sequence[tuple[str, np.ndarray, list[tuple[int, int]], list[bool]]],
    babble: Sequence[np.ndarray],
    *,
    seed: int = 11,
) -> tuple[list[gate_rules.Print], list[np.ndarray], float]:
    """Their prints from the blocks' kept utterances (80%), with the noisy positives the server makes itself
    (no babble on the phone): each kept A/B/C utterance also mixed with the stored public babble at
    one of +15, +10, +5 and 0 dB. Returns the prints, the held-out utterances (the bench's) and the seconds
    of their speech in them."""
    rng = np.random.default_rng(seed)
    clips = [bench_rules.band_limit(b) for b in babble]
    by_kind: dict[str, list[np.ndarray]] = {}
    held: list[np.ndarray] = []
    samples = 0
    for block, x, spans, out in blocks:
        kind = enrol_rules.PRINT_OF[block]  # type: ignore[index]
        for (a, b), is_held in zip(spans, out, strict=False):
            utt = x[a:b]
            if is_held:
                if block != "E":  # a whisper is no turn to bench
                    held.append(utt)
                continue
            samples += len(utt)
            clean = bench_rules.band_limit(utt)
            by_kind.setdefault(kind, []).extend(embed_windows(models.embedder, clean))
            if block in enrol_rules.NOISY_FROM and clips:
                snr = float(enrol_rules.NOISY_SNRS_DB[int(rng.integers(len(enrol_rules.NOISY_SNRS_DB)))])
                noise = np.resize(
                    clips[int(rng.integers(len(clips)))], len(clean) + int(bench_rules.LEAD_S * RATE)
                )
                noisy = bench_rules.mix(clean, noise, snr)[int(bench_rules.LEAD_S * RATE) :]
                by_kind[kind].extend(embed_windows(models.embedder, noisy))
    prints = gate_rules.make_prints(by_kind, cohort, enrol_rules.TRUST)
    return prints, held, samples / RATE


def build(
    models: Models,
    cohort: np.ndarray,
    blocks: Sequence[tuple[str, np.ndarray, list[tuple[int, int]], list[bool]]],
    babble: Sequence[np.ndarray],
) -> Built:
    """Their prints, then the route's thresholds from its bench (their held-out utterances and strangers'
    babble). `ok`, the gate may turn on, needs both strangers ≤ 0.1 a minute and their false
    rejects ≤ 20% at +10 and +5 dB. CPU work: run it in a thread."""
    prints, held, seconds = make_route_prints(models, cohort, blocks, babble)
    if not prints:
        raise PisaError(
            "validation_failed", "The enrolment holds no usable speech.", "Record the blocks again"
        )
    scorer = Scorer(prints, cohort, models.embedder)
    trials = bench_trials(models, scorer, held, babble)
    thresholds, report = bench_rules.calibrate(trials)
    return Built(prints, thresholds, report, seconds, bench_rules.route_ok(report))


# --- the enrolment's test ---------------------------------------------------------------

TEST_SNRS_DB = (10.0, 5.0, 0.0)
TEST_OK_SHARE = 0.8  # of their trials accepted within 1 s, and no stranger
TEST_STRANGER_BABBLE_S = 60.0  # strangers' babble the test's provisional thresholds are set on


def test_result(
    models: Models,
    prints: list[gate_rules.Print],
    cohort: np.ndarray,
    reading: np.ndarray,
    babble: Sequence[np.ndarray],
    strangers: Sequence[np.ndarray],
) -> dict[str, Any]:
    """Their test reading scored on the server: each of their utterances clean and in stored babble at +10, +5
    and 0 dB, and the stored public strangers replayed alone, through the gate at thresholds set on strangers'
    babble (as calibration sets them). `ok`: ≥ 80% of their trials accepted within 1 s and no stranger
    accepted."""
    scorer = Scorer(prints, cohort, models.embedder)
    clips = [bench_rules.band_limit(b) for b in babble]
    thresholds, _ = bench_rules.calibrate(stranger_trials(models, scorer, clips, TEST_STRANGER_BABBLE_S))
    flags = voice_flags(models.vad(), reading)
    spans = utterances(list(flags), models.vad().frame)
    rng = np.random.default_rng(5)
    by: dict[str, list[bool]] = {"clean": [], "10": [], "5": [], "0": []}
    for a, b in spans:
        utt = reading[a:b]
        for snr in (None, *TEST_SNRS_DB):
            noise = clips[int(rng.integers(len(clips)))] if clips and snr is not None else None
            trial = owner_trial(models, scorer, utt, snr, noise)
            replayed = bench_rules.replay(trial, thresholds)
            assert trial.onset_ms is not None
            ok = any(trial.onset_ms - gate_rules.HOP_MS <= at <= trial.onset_ms + bench_rules.ACCEPT_WITHIN_MS
                     for at, _ in replayed.opens)  # fmt: skip
            by["clean" if snr is None else f"{snr:.0f}"].append(ok)
    accepted_strangers = 0
    for x in strangers:
        x = bench_rules.band_limit(x)
        trial = bench_rules.Trial("stranger", stream_hops(models, scorer, x), len(x) / RATE)
        accepted_strangers += bool(bench_rules.replay(trial, thresholds).opens)
    trials = [v for vs in by.values() for v in vs]
    accepted = sum(trials)
    ok = bool(trials) and accepted >= TEST_OK_SHARE * len(trials) and accepted_strangers == 0
    return {
        "accepted_owner": accepted,
        "owner_total": len(trials),
        "accepted_strangers": accepted_strangers,
        "strangers_total": len(strangers),
        "ok": ok,
        "by_snr": {
            k: round(sum(v) / len(v), 3) if v else 0.0 for k, v in by.items()
        },  # never null: the phone decodes numbers
    }


@dataclass
class Enrolment:
    """An enrol session (`purpose: "enrol"`): no talker, no model money. The phone names the blocks; the
    server records their audio, judges each, scores the test, and builds the route's prints at the end."""

    session_id: uuid.UUID
    route: str
    models: Models
    store: VoiceStore
    send: Callable[[dict[str, Any]], None]
    mini: bool = False
    _block: str | None = None
    _testing: bool = False
    _clean: float | None = None  # the check's accept rate in quiet (the bench has babble only)
    _audio: list[np.ndarray] = field(default_factory=list)
    _test: list[np.ndarray] = field(default_factory=list)
    blocks: dict[str, tuple[np.ndarray, list[tuple[int, int]], list[bool]]] = field(default_factory=dict)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: str = ""

    def audio(self, pcm: bytes) -> None:
        """The phone's audio: kept only inside a block or the test (in memory until the block ends)."""
        if self._block is not None:
            self._audio.append(pcm_to_float(pcm))
        elif self._testing:
            self._test.append(pcm_to_float(pcm))

    def done(self, ok: bool, reason: str | None = None, **more: Any) -> None:
        extra = {"reason": reason} if reason else {}
        self.send({"type": "enrol.done", "route": self.route, "ok": ok, **extra, **more})

    def failed(self, reason: str) -> None:
        """Whatever failed, the phone hears it and the session ends."""
        self.done(False, reason)
        self.outcome = f"failed: {reason}"
        self.finished.set()

    async def message(self, body: dict[str, Any]) -> None:
        kind = body.get("type")
        if kind == "enrol.block":
            block, state = str(body.get("block") or ""), body.get("state")
            if block not in enrol_rules.BLOCKS:
                return
            if state == "start":
                self._block, self._audio = block, []
            elif state == "end" and self._block == block:
                await self._end_block(block)
        elif kind == "enrol.test":
            if body.get("state") == "start":
                self._testing, self._test = True, []
            elif body.get("state") == "end" and self._testing:
                self._testing = False
                await self._check()
        elif kind == "enrol.finish":
            await self._finish()
        elif kind == "enrol.cancel":
            self.outcome = "cancelled"
            self.finished.set()

    async def _end_block(self, block: str) -> None:
        x = np.concatenate(self._audio) if self._audio else np.zeros(0, dtype=np.float32)
        self._block, self._audio = None, []
        vad = self.models.vad()
        flags = await asyncio.to_thread(voice_flags, vad, x)
        pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
        q = enrol_rules.quality(block, pcm[: len(flags) * vad.frame], flags, mini=self.mini)  # type: ignore[arg-type]
        spans = utterances(list(flags), vad.frame)
        held = enrol_rules.held_out(spans)
        if q.ok or block == "E":
            self.blocks[block] = (x, spans, held)
        self.send(q.message())
        _log.info(
            "voice_enrol_block",
            session_id=str(self.session_id),
            block=block,
            ok=q.ok,
            voiced_s=round(q.voiced_s, 1),
            snr_db=round(q.snr_db, 1),
            utterances=len(spans),
        )

    def _blocks(self) -> list[tuple[str, np.ndarray, list[tuple[int, int]], list[bool]]]:
        return [(b, x, spans, held) for b, (x, spans, held) in self.blocks.items()]

    async def _check(self) -> None:
        """Their test reading (6 new sentences, no babble on the phone) scored on the server,
        clean and in stored babble, against stored public strangers. Their audio stays in memory."""
        x = np.concatenate(self._test) if self._test else np.zeros(0, dtype=np.float32)
        self._test = []
        cohort = await self.store.cohort(self.models.embedder.name)
        babble = await self.store.babble()
        strangers = await self.store.strangers()

        def score() -> dict[str, Any]:
            prints, _, _ = make_route_prints(self.models, cohort, self._blocks(), babble)
            if not prints:
                raise PisaError("validation_failed", "No block recorded yet.", "Record a block first")
            return test_result(self.models, prints, cohort, x, babble, strangers)

        try:
            result = await asyncio.to_thread(score)
        except PisaError:
            result = {"accepted_owner": 0, "owner_total": 0, "accepted_strangers": 0,
                      "strangers_total": len(strangers), "ok": False,
                      "by_snr": {"clean": 0.0, "10": 0.0, "5": 0.0, "0": 0.0}}  # fmt: skip
        self._clean = float(result["by_snr"]["clean"]) if result["owner_total"] else None
        self.send({"type": "enrol.test_result", **result})

    def _by_snr(self, false_reject: dict[str, float]) -> dict[str, float]:
        """The verdict's accept rate per condition: the bench's babble levels, and `clean` from the 60-second
        check (without a check, the +10 dB rate, a lower bound for quiet)."""
        out = {k: round(1 - v, 3) for k, v in false_reject.items()}
        clean = self._clean if self._clean is not None else out.get("10")
        return {"clean": round(clean, 3), **out} if clean is not None else out

    async def _finish(self) -> None:
        need = enrol_rules.MINI if self.mini else enrol_rules.REQUIRED
        missing = [b for b in need if b not in self.blocks]
        if missing:
            self.done(False, "missing", missing=missing)
            return
        if await self.store.consent_at() is None:  # consent withdrawn meanwhile: nothing is stored
            self.failed("consent")
            return
        cohort = await self.store.cohort(self.models.embedder.name)
        babble = await self.store.babble()
        started = time.monotonic()
        try:
            built = await asyncio.to_thread(build, self.models, cohort, self._blocks(), babble)
        except PisaError:
            self.failed("no_speech")
            return
        for block, (x, spans, held) in self.blocks.items():
            await self.store.save_recording(self.route, block, float_to_pcm(x), spans, held)
        r = built.report
        await self.store.save_profile(
            self.route,
            self.models.embedder.name,
            self.models.embedder.version,
            built.prints,
            built.thresholds,
            r.stored(),
            built.samples_s,
            built.ok,
        )
        _log.info(
            "voice_enrolled",
            session_id=str(self.session_id),
            route=self.route,
            ok=built.ok,
            samples_s=round(built.samples_s, 1),
            fa_per_min=round(r.fa_per_min, 3),
            owner_false_reject=round(r.owner_false_reject, 3),
            guarded=round(r.owner_fr_guarded, 3),
            seconds=round(time.monotonic() - started, 1),
        )
        self.done(
            built.ok,
            owner_false_reject=round(r.owner_fr_guarded, 3),
            strangers_per_min=round(r.fa_per_min, 3),
            by_snr=self._by_snr(r.by_snr),
        )
        self.outcome = "done"
        self.finished.set()


# --- the service --------------------------------------------------------------------------------------------


class VoiceIdService:
    """The voice gate's front: consent, profiles, deletion, the enrol sessions, and a talk's gate."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        models: Callable[[], Models] | None,
        *,
        gate_on: bool = True,
    ) -> None:
        self.store = VoiceStore(pool, keys)
        self._models_factory = models
        self._models: Models | None = None
        self.gate_on = gate_on  # PISA_VOICE_GATE: the one switch back to today's path
        self.enrolments: dict[uuid.UUID, Enrolment] = {}

    def models(self) -> Models:
        if self._models is None:
            if self._models_factory is None:
                raise PisaError(
                    "internal",
                    "The voice models aren't configured on this server.",
                    "Set PISA_VOICE_MODELS_DIR and rebuild Pisa's image",
                )
            try:
                self._models = self._models_factory()
            except Exception as err:  # a missing file names itself
                raise PisaError(
                    "internal",
                    f"The voice models can't load: {str(err).rstrip('.')}.",
                    "Rebuild Pisa's image (the models are fetched at its build)",
                ) from None
        return self._models

    async def loaded(self) -> Models:
        """The models, the first load off the event loop (it reads and checks ≈ 40 MB)."""
        return self._models if self._models is not None else await asyncio.to_thread(self.models)

    async def state(self) -> dict[str, Any]:
        consent = await self.store.consent_at()
        routes = await self.store.routes()
        on = self.gate_on and consent is not None and any(r["ok"] for r in routes)
        return {"routes": routes, "consent_at": consent, "gate": "on" if on else "off"}

    async def gate_for(self, route: str | None) -> tuple[Profile, np.ndarray] | None:
        """The gate's profile for a talk on this route, or None (today's path): the switch, their consent, a
        profile of this route made by this model that passed its guard (`ok`), and the cohort
        it is normalised against."""
        if not self.gate_on or route is None or self._models_factory is None:
            return None
        if await self.store.consent_at() is None:
            return None
        try:
            models = await self.loaded()
        except PisaError:
            _log.warning("voice_models_unavailable")
            return None
        profile = await self.store.profile(route, models.embedder.name, models.embedder.version)
        if profile is None or not profile.ok:
            return None
        cohort = await self.store.cohort(models.embedder.name)
        if cohort.size == 0:
            return None
        return profile, cohort

    async def still(self, route: str, version: str) -> bool:
        """LOW: their prints of this route are still there (a deletion during a live talk)."""
        return await self.store.consent_at() is not None and await self.store.has_profile(route, version)

    async def can_enrol(self) -> Models:
        """An enrolment may start: their consent is given (no consent, no recording), the models load, and the
        public cohort is built. Raises PisaError naming what is missing."""
        if await self.store.consent_at() is None:
            raise PisaError(
                "voice_consent_missing",
                "Their consent to record their voice isn't given.",
                "Ask it first: POST /v1/voice/consent",
            )
        models = await self.loaded()
        if (await self.store.cohort(models.embedder.name)).size == 0:
            raise PisaError(
                "voice_cohort_missing",
                "The public voices Pisa compares their voice with aren't built yet.",
                "Run maintenance → voice-cohort once",
            )
        return models

    async def start_enrolment(
        self, session_id: uuid.UUID, route: str, send: Callable[[dict[str, Any]], None]
    ) -> Enrolment:
        models = await self.can_enrol()
        enrolled = {r["route"] for r in await self.store.routes()}
        mini = bool(enrolled) and route not in enrolled  # a new route: the 1-minute mini-enrolment
        e = Enrolment(session_id, route, models, self.store, send, mini=mini)
        self.enrolments[session_id] = e
        return e


async def run_enrolment(
    e: Enrolment,
    audio: Callable[[], Any],
    messages: asyncio.Queue[dict[str, Any]],
    close: Callable[[], Awaitable[None]],
    *,
    limit_s: float = 900.0,
) -> str:
    """An enrol session's loop: the phone's audio into the blocks, its messages, until finish or cancel (or
    15 minutes); then the peer closes. The recordings are stored only at finish. Any error in
    a message's handling ends the session with `enrol.done` ok false and a reason."""

    async def pump() -> None:
        async for pcm in audio():
            e.audio(pcm)

    async def listen() -> None:
        while not e.finished.is_set():
            body = await messages.get()
            try:
                await e.message(body)
            except Exception as err:
                _log.exception("voice_enrolment_message_failed", session_id=str(e.session_id))
                e.failed("error" if not isinstance(err, PisaError) else err.code)

    tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]
    try:
        await asyncio.wait_for(e.finished.wait(), limit_s)
    except TimeoutError:
        e.outcome = "timeout"
        e.done(False, "timeout")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close()
    return e.outcome or "closed"
