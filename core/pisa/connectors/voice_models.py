"""The gate's models, run in ONNX Runtime on the CPU.

- **Speaker embeddings** through sherpa-onnx (Apache-2.0): WeSpeaker ResNet34-LM (VoxCeleb) by default,
  3D-Speaker CAM++ (VoxCeleb) behind `PISA_VOICE_EMBEDDER`. ResNet34 is the default because CAM++'s
  export scores the same audio very differently at 1 s and 1.5 s (cosine 0.61), ResNet34 doesn't (0.93).
- **VAD**: Silero VAD v4 (MIT), sherpa-onnx's 16 kHz export, 512-sample frames (32 ms).
- **End of turn**: Smart Turn v3.2 (BSD-2-Clause), its ONNX CPU build, on Whisper log-mel features (computed
  here in numpy: 80 bands, 8 s).

The files are fetched once, at image build (`python -m pisa.connectors.voice_models <dir>`, Dockerfile), each
checked against its pinned SHA-256; never at request time. A missing or altered file is an error that names
it.
"""

import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from pisa.domain.voice_gate import RATE
from pisa.logging import get_logger

_log = get_logger("pisa.voice.models")


@dataclass(frozen=True)
class ModelFile:
    file: str
    url: str
    sha256: str
    size: int
    licence: str


_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
MODELS: dict[str, ModelFile] = {
    "resnet34": ModelFile(
        "wespeaker_en_voxceleb_resnet34_LM.onnx",
        f"{_SHERPA}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx",
        "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
        26_530_550,
        "Apache-2.0 (WeSpeaker code); weights follow VoxCeleb (CC BY 4.0)",
    ),
    "silero_vad": ModelFile(
        "silero_vad.onnx",
        f"{_SHERPA}/asr-models/silero_vad.onnx",
        "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        643_854,
        "MIT (Silero VAD v4)",
    ),
    "smart_turn": ModelFile(
        "smart-turn-v3.2-cpu.onnx",
        # pinned to the repository's revision, never `main`
        "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/"
        "smart-turn-v3.2-cpu.onnx",
        "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
        8_679_182,
        "BSD-2-Clause (Smart Turn v3.2)",
    ),
}
EMBEDDERS = ("resnet34",)


class ModelMissing(Exception):
    """A model file is missing or altered: the message names it and the fix."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def path_of(models_dir: str, name: str) -> Path:
    """The model's file, checked at load against its pinned size and SHA-256 (not only at the
    image's build): a missing or altered file names itself."""
    spec = MODELS[name]
    path = Path(models_dir) / spec.file
    if not path.is_file() or path.stat().st_size != spec.size:
        raise ModelMissing(f"The voice model {spec.file} is missing in {models_dir}: rebuild Pisa's image")
    if _checked(str(path), path.stat().st_mtime_ns) != spec.sha256:
        raise ModelMissing(f"The voice model {spec.file} doesn't match its pinned checksum: rebuild Pisa")
    return path


@cache
def _checked(path: str, mtime_ns: int) -> str:
    return sha256(Path(path))


def fetch(models_dir: str, names: tuple[str, ...] = tuple(MODELS)) -> list[str]:
    """Download the models not present yet, each checked against its pinned checksum (a mismatch deletes the
    file and fails). Returns the files fetched."""
    import httpx  # only the fetch needs the network

    root = Path(models_dir)
    root.mkdir(parents=True, exist_ok=True)
    fetched = []
    for name in names:
        spec = MODELS[name]
        path = root / spec.file
        if path.is_file() and sha256(path) == spec.sha256:
            continue
        part = path.with_suffix(".part")
        with httpx.stream("GET", spec.url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        if sha256(part) != spec.sha256:
            part.unlink()
            raise ModelMissing(f"{spec.file} from {spec.url} doesn't match its pinned checksum")
        part.rename(path)
        fetched.append(spec.file)
    return fetched


# --- the embedder
# --------------------------------------------------------------------------------------------


class SherpaEmbedder:
    def __init__(self, models_dir: str, name: str = "resnet34", threads: int = 1) -> None:
        import sherpa_onnx

        if name not in EMBEDDERS:
            raise ModelMissing(f"Unknown speaker embedder {name!r}: use one of {', '.join(EMBEDDERS)}")
        self._so = sherpa_onnx
        self.name = name
        self.version = MODELS[name].sha256[:12]
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(path_of(models_dir, name)), num_threads=threads
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dim = int(self._extractor.dim)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(RATE, np.ascontiguousarray(samples, dtype=np.float32))
        stream.input_finished()
        return np.asarray(self._extractor.compute(stream), dtype=np.float32)


# --- the VAD ------------------------------------------------------------------------------------------------


def _session(path: Path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class SileroVad:
    """One stream's VAD: its own state over a shared ONNX session (ONNX Runtime's `run` is thread-safe)."""

    frame = 512

    def __init__(self, session: Any) -> None:
        self._session = session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.ascontiguousarray(frame, dtype=np.float32)[None, :]
        p, self._h, self._c = self._session.run(None, {"x": x, "h": self._h, "c": self._c})
        return float(np.asarray(p).reshape(-1)[0])


def silero(models_dir: str) -> Callable[[], SileroVad]:
    """Item 3: the VAD's ONNX session is built here, once (in `VoiceIdService.loaded`'s thread),
    and every stream (a talk's gate, its restart after an error, an enrolment block) gets a fresh state over
    it: nothing is built on the event loop."""
    session = _session(path_of(models_dir, "silero_vad"))
    return lambda: SileroVad(session)


# --- Smart Turn v3.2
# -----------------------------------------------------------------------------------------

N_FFT, HOP, N_MELS, TURN_S = 400, 160, 80, 8


@cache
def mel_filters() -> np.ndarray:
    """Whisper's mel filterbank (Slaney scale and norm, 80 bands, 0-8 kHz), as librosa makes it."""

    def hz_to_mel(f: np.ndarray) -> np.ndarray:
        f = np.asarray(f, dtype=np.float64)
        mel = f / (200.0 / 3)
        log_t = f >= 1000.0
        return np.where(log_t, 15.0 + np.log(np.maximum(f, 1e-9) / 1000.0) / (np.log(6.4) / 27.0), mel)

    def mel_to_hz(m: np.ndarray) -> np.ndarray:
        m = np.asarray(m, dtype=np.float64)
        f = m * (200.0 / 3)
        log_t = m >= 15.0
        return np.where(log_t, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), f)

    fft_freqs = np.linspace(0, RATE / 2, 1 + N_FFT // 2)
    mel_pts = mel_to_hz(np.linspace(hz_to_mel(np.array(0.0)), hz_to_mel(np.array(RATE / 2)), N_MELS + 2))
    fdiff = np.diff(mel_pts)
    ramps = mel_pts[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_pts[2 : N_MELS + 2] - mel_pts[:N_MELS])
    return (weights * enorm[:, None]).astype(np.float32)


def whisper_features(samples: np.ndarray) -> np.ndarray:
    """Smart Turn's input: the last 8 s (zero-padded at the start), normalised, as Whisper's log-mel
    (80 x 800)."""
    n = TURN_S * RATE
    x = np.asarray(samples, dtype=np.float32)[-n:]
    x = np.pad(x, (n - len(x), 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x, (N_FFT // 2, N_FFT // 2), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    window = np.hanning(N_FFT + 1)[:-1].astype(np.float32)
    power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    mel = mel_filters() @ power[:-1].T
    log = np.log10(np.maximum(mel, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)


class SmartTurn:
    def __init__(self, models_dir: str) -> None:
        self._session = _session(path_of(models_dir, "smart_turn"))

    def complete(self, samples: np.ndarray) -> float:
        out = self._session.run(None, {"input_features": whisper_features(samples)[None]})
        return float(np.asarray(out[0]).reshape(-1)[0])


if __name__ == "__main__":  # the image build: python -m pisa.connectors.voice_models /app/models/voice
    done = fetch(sys.argv[1] if len(sys.argv) > 1 else "models/voice")
    print("fetched:", ", ".join(done) or "nothing (all present)")
