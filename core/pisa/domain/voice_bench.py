"""The bench's arithmetic. Mixing their held-out speech with public babble at +10, +5,
0 and -5 dB through an HFP-like 16 kHz band-limit, replaying the gate on the hops a trial produced, the
synthesis's §3 metrics, and the per-route calibration (≤ 0.1 stranger false accepts a minute at the shortest
window). Pure numpy, no I/O: the service makes the hops (VAD, embeddings) and stores the results; nothing of
their audio leaves the server, and the report carries numbers only.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from pisa.domain import voice_gate as gate
from pisa.domain.voice_gate import RATE

SNRS_DB = (10.0, 5.0, 0.0, -5.0)
BABBLE_TALKERS = (3, 7)  # a babble clip is this many talkers at once
HFP_BAND = (100.0, 7000.0)  # wideband HFP (mSBC): AirPods under voiceChat
LEAD_S, TAIL_S = 1.0, 1.5  # babble before their first word and after their last: onset and end of turn
FA_TARGET_PER_MIN = 0.1
# a route's `ok` (the gate may turn on) also needs their false rejects ≤ 20% at +10 and +5 dB
# combined; 0 and -5 dB are reported but don't count
OWNER_FR_MAX = 0.2
GUARDED_SNRS_DB = (10.0, 5.0)
ACCEPT_WITHIN_MS = 1000  # §3: their turns or barge-ins not accepted within 1 s
RUNAWAY_MS = 10_000  # a turn still open this long after their last word
# margin and δ (the brief gives none: QUESTIONS): inside their turn a hop counts as their down to the
# strangers' 95th percentile (θ-margin), so babble after their last word is silence; a barge-in needs half the
# margin more
STRANGER_QUANTILE = 0.95
DELTA_SHARE = 0.5


def band_limit(x: np.ndarray, band: tuple[float, float] = HFP_BAND, rate: int = RATE) -> np.ndarray:
    """An HFP-like channel: the band outside `band` removed (FFT mask with 50 Hz ramps), then 16-bit
    quantisation. Float samples in [-1, 1]."""
    spec = np.fft.rfft(x.astype(np.float64))
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    lo, hi = band
    mask = np.clip((freqs - (lo - 50)) / 50, 0, 1) * np.clip(((hi + 50) - freqs) / 50, 0, 1)
    y = np.fft.irfft(spec * mask, n=len(x))
    return (np.round(np.clip(y, -1, 1) * 32767) / 32767).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12))


def babble(talkers: Sequence[np.ndarray], seconds: float, rng: np.random.Generator) -> np.ndarray:
    """Talkers at once, each at the same level, looped and offset at random, `seconds` long, at -26 dBFS."""
    n = int(seconds * RATE)
    out = np.zeros(n, dtype=np.float64)
    for t in talkers:
        t = t.astype(np.float64)
        reps = int(np.ceil((n + len(t)) / max(len(t), 1)))
        long = np.tile(t, reps)
        start = int(rng.integers(0, max(len(t), 1)))
        out += long[start : start + n] / max(rms(t), 1e-6)
    return (out / max(rms(out), 1e-9) * 10 ** (-26 / 20)).astype(np.float32)


def mix(owner: np.ndarray, noise: np.ndarray, snr_db: float, owner_level: float | None = None) -> np.ndarray:
    """Their speech over the noise at `snr_db` (their voiced level against the babble's). `noise` is at least
    as long as `owner`, which sits at offset LEAD_S."""
    level = owner_level if owner_level is not None else rms(owner)
    scaled = noise * (level / max(rms(noise), 1e-9)) / (10 ** (snr_db / 20))
    out = scaled.astype(np.float64)
    at = int(LEAD_S * RATE)
    out[at : at + len(owner)] += owner
    return np.clip(out, -1, 1).astype(np.float32)


# --- trials and their replay -------------------------------------------------------------------------------


@dataclass
class Trial:
    """One bench stream as hops (times, voice flags, scores: never audio). Owner trials know where their
    speech starts and ends (ms); stranger trials have none. `complete(at_ms)` is Smart Turn on the stream so
    far."""

    kind: str  # owner | stranger
    hops: list[gate.Hop]
    seconds: float
    onset_ms: int | None = None
    end_ms: int | None = None
    snr_db: float | None = None
    pisa_speaking: bool = False
    complete: Callable[[int], bool] = field(default=lambda at: False)


@dataclass(frozen=True)
class Replay:
    opens: list[tuple[int, gate.Open]]
    closes: list[tuple[int, gate.Close]]


def replay(trial: Trial, thresholds: gate.Thresholds) -> Replay:
    g = gate.Gate(thresholds)
    opens: list[tuple[int, gate.Open]] = []
    closes: list[tuple[int, gate.Close]] = []
    for h in trial.hops:
        h = gate.Hop(
            h.at_ms,
            h.voiced,
            h.run_ms,
            h.onset_ms,
            h.score,
            h.window_s,
            trial.pisa_speaking and g.state is not gate.State.OWNER_TURN,
        )
        for d in g.hop(h, lambda at=h.at_ms: trial.complete(at)):
            if isinstance(d, gate.Open):
                opens.append((h.at_ms, d))
            elif isinstance(d, gate.Close):
                closes.append((h.at_ms, d))
    return Replay(opens, closes)


@dataclass(frozen=True)
class Report:
    """The synthesis's §3 metrics on one bench run (numbers only)."""

    stranger_minutes: float
    fa_per_min: float  # strangers' turn starts and barge-ins, per minute
    fa_turns_per_min: float
    fa_barge_per_min: float
    owner_trials: int
    owner_false_reject: float  # their turns or barge-ins not accepted within 1 s
    owner_never: float  # … never accepted at all
    owner_fr_guarded: float  # their false rejects at +10 and +5 dB combined (the guard's number)
    onset_clipped: float  # opened turns whose pre-roll starts after their first word
    barge_ms_p50: float | None
    barge_ms_p90: float | None
    end_ms_p50: float | None
    end_ms_p90: float | None
    runaway: int
    by_snr: dict[str, float]  # false-reject share per SNR

    def stored(self) -> dict[str, object]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _pct(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def report(trials: Sequence[Trial], thresholds: gate.Thresholds) -> Report:
    minutes = sum(t.seconds for t in trials if t.kind == "stranger") / 60
    fa_turns = fa_barge = 0
    owners = [t for t in trials if t.kind == "owner"]
    late = never = clipped = opened = runaway = 0
    guarded: list[bool] = []
    barge_ms: list[float] = []
    end_ms: list[float] = []
    by_snr: dict[str, list[bool]] = {}
    for t in trials:
        r = replay(t, thresholds)
        if t.kind == "stranger":
            fa_barge += sum(1 for _, o in r.opens if o.barge)
            fa_turns += sum(1 for _, o in r.opens if not o.barge)
            continue
        assert t.onset_ms is not None and t.end_ms is not None
        mine = [(at, o) for at, o in r.opens if at >= t.onset_ms - gate.HOP_MS]
        first = mine[0] if mine else None
        missed = first is None or first[0] - t.onset_ms > ACCEPT_WITHIN_MS
        late += missed
        never += first is None
        by_snr.setdefault(f"{t.snr_db:.0f}", []).append(missed)
        if t.snr_db in GUARDED_SNRS_DB and not t.pisa_speaking:
            guarded.append(missed)
        if first is None:
            continue
        opened += 1
        clipped += first[1].preroll_from_ms > t.onset_ms
        if t.pisa_speaking:
            barge_ms.append(first[0] - t.onset_ms)
        after = [at for at, _ in r.closes if at > first[0]]
        if after:
            end_ms.append(max(0, after[0] - t.end_ms))
            runaway += after[0] - t.end_ms > RUNAWAY_MS
        else:
            runaway += 1
    n = max(len(owners), 1)
    return Report(
        stranger_minutes=round(minutes, 2),
        fa_per_min=(fa_turns + fa_barge) / minutes if minutes else 0.0,
        fa_turns_per_min=fa_turns / minutes if minutes else 0.0,
        fa_barge_per_min=fa_barge / minutes if minutes else 0.0,
        owner_trials=len(owners),
        owner_false_reject=late / n,
        owner_never=never / n,
        owner_fr_guarded=float(np.mean(guarded)) if guarded else 1.0,
        onset_clipped=clipped / max(opened, 1),
        barge_ms_p50=_pct(barge_ms, 50),
        barge_ms_p90=_pct(barge_ms, 90),
        end_ms_p50=_pct(end_ms, 50),
        end_ms_p90=_pct(end_ms, 90),
        runaway=runaway,
        by_snr={k: float(np.mean(v)) for k, v in sorted(by_snr.items())},
    )


# --- calibration -------------------------------------------------------------------------------------------


def _scores(trials: Sequence[Trial], window_s: float) -> np.ndarray:
    return np.array(
        [
            h.score
            for t in trials
            for h in t.hops
            if h.score is not None and h.voiced and abs(h.window_s - window_s) < 1e-6
        ]
    )


def calibrate(trials: Sequence[Trial]) -> tuple[gate.Thresholds, Report]:
    """A route's thresholds from its bench: the lowest θ at the shortest window whose replay keeps strangers'
    false accepts ≤ 0.1 a minute; each longer window's θ at the same quantile of strangers' scores there. The
    margin brings θ-margin down to the strangers' 95th percentile (inside their turn, babble stays silence)
    and δ is half of it. Returns them and the report at them (the user's false-reject rate goes to
    `enrol.done` and is stored)."""
    strangers = [t for t in trials if t.kind == "stranger"]
    shortest = min(gate.WINDOWS_S)
    base = _scores(strangers, shortest)
    if base.size == 0:
        base = np.array([0.0])
    per_window = {w: _scores(strangers, w) for w in gate.WINDOWS_S}
    # the strangers' quantiles, and just above the loudest of them (a hop at θ opens: ≥)
    candidates = np.unique([*np.quantile(base, np.linspace(0.5, 1.0, 101)), np.nextafter(base.max(), np.inf)])
    thetas: dict[float, float] | None = None
    for theta in candidates:
        q = float(np.mean(base < theta))
        trial = {shortest: float(theta)}
        for w, s in per_window.items():
            if w != shortest:
                top = float(np.nextafter(s.max(), np.inf)) if s.size else float(theta)
                trial[w] = top if q >= 1.0 or not s.size else float(np.quantile(s, q))
        if report(strangers, gate.Thresholds(trial, 0.0, 0.0)).fa_per_min <= FA_TARGET_PER_MIN:
            thetas = trial
            break
    if thetas is None:  # even the strictest quantile lets strangers through: one step above them all
        top = float(base.max()) + (float(base.std()) if base.size > 1 else 1.0)
        thetas = {w: top for w in gate.WINDOWS_S}
    margin = max(0.0, thetas[shortest] - float(np.quantile(base, STRANGER_QUANTILE)))
    chosen = gate.Thresholds(thetas, margin, DELTA_SHARE * margin)
    return chosen, report(trials, chosen)


def route_ok(report: Report) -> bool:
    """The gate may turn on for a route: strangers held (≤ 0.1 a minute) and their turns at +10
    and +5 dB accepted (false rejects ≤ 20%)."""
    return (
        report.owner_trials > 0
        and report.fa_per_min <= FA_TARGET_PER_MIN
        and report.owner_fr_guarded <= OWNER_FR_MAX
    )
