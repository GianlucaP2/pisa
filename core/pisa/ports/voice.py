"""The user-voice gate's models behind ports (pisa.connectors.voice_models runs them in ONNX Runtime
on the CPU). Every input is 16 kHz mono float32 in [-1, 1]; nothing here keeps audio."""

from typing import Protocol

import numpy as np


class Embedder(Protocol):
    name: str  # the config's id (resnet34)
    version: str  # the model file's checksum prefix: a print is only compared with prints of the same
    dim: int

    def embed(self, samples: np.ndarray) -> np.ndarray:
        """One speaker embedding of the window."""
        ...


class Vad(Protocol):
    frame: int  # samples per decision (Silero: 512 = 32 ms)

    def reset(self) -> None: ...

    def prob(self, frame: np.ndarray) -> float:
        """Speech probability of the next frame (stateful: frames in order)."""
        ...


class TurnEnd(Protocol):
    def complete(self, samples: np.ndarray) -> float:
        """Smart Turn's probability that the turn so far (owner-gated audio, the last 8 s) is complete."""
        ...
