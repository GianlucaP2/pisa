"""Voice gate test helpers: generated voices, never anyone's. A synthetic speaker is a source-filter voice
(harmonics of a pitch contour through formant peaks scaled by its own vocal-tract length), saying random
syllables. Also a cheap spectral embedder and an energy VAD for tests without the ONNX models, and a loader of
the real models when `PISA_VOICE_MODELS_DIR` holds them (skipped otherwise)."""

import itertools
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

RATE = 16_000
# (F1, F2, F3) of a few vowels, adult average
VOWELS = (
    (730, 1090, 2440),
    (270, 2290, 3010),
    (300, 870, 2240),
    (530, 1840, 2480),
    (570, 840, 2410),
    (660, 1720, 2410),
    (440, 1020, 2240),
)
BANDWIDTHS = (90.0, 110.0, 170.0)


@dataclass(frozen=True)
class Speaker:
    f0: float  # Hz
    tract: float  # formant scale (shorter tract: higher formants)
    tilt: float  # spectral tilt, dB per octave
    breath: float  # aspiration noise share
    rate: float  # syllables per second
    seed: int


def speaker(seed: int) -> Speaker:
    r = np.random.default_rng(seed)
    female = r.random() < 0.5
    return Speaker(
        f0=float(r.uniform(170, 240) if female else r.uniform(85, 150)),
        tract=float(r.uniform(1.08, 1.22) if female else r.uniform(0.86, 1.02)),
        tilt=float(r.uniform(-14, -8)),
        breath=float(r.uniform(0.01, 0.08)),
        rate=float(r.uniform(3.5, 5.5)),
        seed=seed,
    )


def _envelope(freqs: np.ndarray, formants: np.ndarray, tilt: float) -> np.ndarray:
    amp = np.zeros_like(freqs)
    for f, b in zip(formants, BANDWIDTHS, strict=False):
        amp += 1.0 / (1.0 + ((freqs - f) / (b / 2)) ** 2)
    octaves = np.log2(np.maximum(freqs, 50.0) / 100.0)
    return amp * 10 ** (tilt * octaves / 20)


def say(s: Speaker, seconds: float, utt_seed: int, level_db: float = -20.0) -> np.ndarray:
    """`seconds` of the speaker saying random syllables (word pauses included), at about `level_db` dBFS."""
    r = np.random.default_rng(s.seed * 100_003 + utt_seed)
    n = int(seconds * RATE)
    out = np.zeros(n, dtype=np.float64)
    pos = 0
    phase = 0.0
    while pos < n:
        syl = int(RATE / s.rate * r.uniform(0.7, 1.3))
        cons = int(syl * r.uniform(0.15, 0.3))
        vowel = min(syl - cons, n - pos - cons)
        if vowel <= 0:
            break
        burst = r.standard_normal(cons) * 0.02
        spec = np.fft.rfft(burst)
        f = np.fft.rfftfreq(cons, 1 / RATE)
        burst = np.fft.irfft(spec * (f > 2500 * s.tract), n=cons)
        out[pos : pos + cons] += burst
        pos += cons
        t = np.arange(vowel)
        f0 = s.f0 * (1 + 0.08 * np.sin(2 * np.pi * r.uniform(0.5, 2) * t / RATE + r.uniform(0, 6)))
        f0 *= 1 + 0.01 * r.standard_normal(vowel).cumsum() / np.sqrt(np.arange(1, vowel + 1))
        ph = phase + 2 * np.pi * np.cumsum(f0) / RATE
        phase = float(ph[-1])
        formants = np.array(VOWELS[int(r.integers(len(VOWELS)))]) * s.tract
        k_max = int(7600 / s.f0)
        ks = np.arange(1, k_max + 1)
        amps = _envelope(ks * s.f0, formants, s.tilt)
        wave = (amps[:, None] * np.sin(ks[:, None] * ph[None, :])).sum(axis=0)
        wave += s.breath * r.standard_normal(vowel) * np.abs(wave).mean() * 3
        env = np.sin(np.pi * np.linspace(0, 1, vowel)) ** 0.5
        out[pos : pos + vowel] += wave * env
        pos += vowel
        if r.random() < 0.25:
            pos += int(RATE * r.uniform(0.05, 0.15))  # a short pause between words
    voiced = out[np.abs(out) > 1e-6]
    level = np.sqrt(np.mean(voiced**2)) if voiced.size else 1.0
    return (out / level * 10 ** (level_db / 20)).astype(np.float32)


def sentence(s: Speaker, utt_seed: int, seconds: float = 2.5, pad: float = 0.5) -> np.ndarray:
    """One utterance with `pad` seconds of silence on each side."""
    x = say(s, seconds, utt_seed)
    z = np.zeros(int(pad * RATE), dtype=np.float32)
    return np.concatenate([z, x, z])


# --- cheap models for tests ---------------------------------------------------------------------------------


class SpectralEmbedder:
    """A speaker 'embedding' for tests: the mean and spread of 40 log band energies (no ONNX needed). It tells
    the synthetic speakers apart by their formants and pitch, enough to exercise the gate end to end."""

    name = "spectral"
    version = "test"
    dim = 80

    def embed(self, samples: np.ndarray) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float32)
        size, hop = 400, 160
        if len(x) < size:
            x = np.pad(x, (0, size - len(x)))
        frames = np.lib.stride_tricks.sliding_window_view(x, size)[::hop] * np.hanning(size)
        power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        edges = np.geomspace(80, 7600, 41)
        freqs = np.fft.rfftfreq(size, 1 / RATE)
        bands = np.stack(
            [power[:, (freqs >= a) & (freqs < b)].sum(axis=1) for a, b in itertools.pairwise(edges)],
            axis=1,
        )
        log = np.log(bands + 1e-9)
        energy = log.sum(axis=1)
        keep = log[energy >= np.quantile(energy, 0.5)] if len(log) > 2 else log
        keep = keep - keep.mean(axis=1, keepdims=True)
        return np.concatenate([keep.mean(axis=0), keep.std(axis=0)]).astype(np.float32)


class EnergyVad:
    frame = 512

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        return 1.0 if float(np.sqrt(np.mean(np.square(frame)))) > 0.003 else 0.0


class SilenceTurn:
    """'Complete' once the turn's last 0.5 s is quiet."""

    def complete(self, samples: np.ndarray) -> float:
        tail = samples[-RATE // 2 :]
        return 1.0 if tail.size and float(np.sqrt(np.mean(np.square(tail)))) < 0.003 else 0.0


def fake_models():
    from pisa.services.voice_id import Models

    return Models(SpectralEmbedder(), EnergyVad, SilenceTurn())


def real_models_dir() -> str | None:
    root = os.environ.get("PISA_VOICE_MODELS_DIR")
    return root if root and (Path(root) / "silero_vad.onnx").is_file() else None


def cohort(embedder: object, n: int = 60, seed: int = 1000) -> np.ndarray:
    from pisa.domain import voice_gate

    embs = [embedder.embed(say(speaker(seed + i), 2.0, 0)) for i in range(n)]  # type: ignore[attr-defined]
    return voice_gate.unit(np.stack(embs))


async def save_cohort(pool: Any, model: str, rows: Sequence[tuple[str, np.ndarray]]) -> None:
    """The gate's impostor cohort for `model`, written straight to its table (nothing in Pisa fills it)."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM voice_cohort WHERE model = %s", (model,))
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO voice_cohort (model, lang, embedding) VALUES (%s, %s, %s)",
                [(model, lang, np.asarray(e, dtype="<f4").tobytes()) for lang, e in rows],
            )


async def save_babble(pool: Any, clips: Sequence[tuple[str, int, np.ndarray]]) -> None:
    """The bench's babble clips (langs, talkers, samples), written straight to their table."""
    from pisa.services.voice_id import float_to_pcm

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM voice_babble")
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO voice_babble (langs, talkers, pcm, seconds) VALUES (%s, %s, %s, %s)",
                [(langs, n, float_to_pcm(x), len(x) / RATE) for langs, n, x in clips],
            )
