"""G5: the voice enrolment exchange (VOICE-ID-1) in contract 2, outside AG-UI runs.

The enrolment session is a talk session opened with purpose "enrol" (POST /v1/talk/sessions). Its data channel
carries only pisa.voice.enrol.* frames: the app sends {"op": "custom", name, value}; the server sends
{"seq", "event": <AG-UI 1.0 CUSTOM event>} with no threadId or runId. Writes fixtures/enrol/*.jsonl.
Run: python -m pisa_conformance.reference.enrol
"""
from __future__ import annotations

import json
from typing import Any

from ..paths import FIXTURES

OUT = FIXTURES / "enrol"
T0 = 1763460000000


class EnrolChannel:
    """The server side of one enrolment data channel: seq from 1, one CUSTOM event per frame."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def send(self, name: str, value: dict[str, Any]) -> dict[str, Any]:
        if not name.startswith("pisa.voice.enrol."):
            raise ValueError("an enrolment channel carries only pisa.voice.enrol.* frames")
        seq = len(self.frames) + 1
        frame = {"seq": seq, "event": {"type": "CUSTOM", "timestamp": T0 + seq, "name": name, "value": value}}
        self.frames.append(frame)
        return frame


def client(name: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"op": "custom", "name": name, "value": value or {}}


QUALITY = {"voicedS": 11.5, "snrDb": 24.0, "clipped": False, "ok": True, "hint": None}
BY_SNR = {"clean": 1.0, "10": 0.95, "5": 0.9, "0": 0.7}


def client_session() -> list[dict[str, Any]]:
    out = []
    for b in "ABCDE":
        out += [client("pisa.voice.enrol.block", {"block": b, "state": "start"}), client("pisa.voice.enrol.block", {"block": b, "state": "end"})]
    out += [client("pisa.voice.enrol.test", {"state": "start"}), client("pisa.voice.enrol.test", {"state": "end"}), client("pisa.voice.enrol.finish")]
    return out


def server_ok() -> list[dict[str, Any]]:
    ch = EnrolChannel()
    for b in "ABCDE":
        q = dict(QUALITY, block=b)
        if b == "C":
            q.update(snrDb=7.5, ok=False, hint="closer")
        ch.send("pisa.voice.enrol.quality", q)
    ch.send("pisa.voice.enrol.test_result", {"acceptedOwner": 23, "ownerTotal": 24, "acceptedStrangers": 0, "strangersTotal": 40, "ok": True, "bySnr": BY_SNR})
    ch.send("pisa.voice.enrol.done", {"route": "airpods", "ok": True, "ownerFalseReject": 0.075, "strangersPerMin": 0.0, "bySnr": BY_SNR})
    return ch.frames


def server_missing() -> list[dict[str, Any]]:
    ch = EnrolChannel()
    ch.send("pisa.voice.enrol.quality", dict(QUALITY, block="A"))
    ch.send("pisa.voice.enrol.done", {"route": "speaker", "ok": False, "reason": "missing", "missing": ["B", "C", "D"]})
    return ch.frames


FILES = {"client_session": client_session, "server_ok": server_ok, "server_missing": server_missing}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in FILES.items():
        (OUT / f"{name}.jsonl").write_text("".join(json.dumps(f, ensure_ascii=False, sort_keys=True) + "\n" for f in fn()), encoding="utf-8")
        print(name)


if __name__ == "__main__":
    main()
