"""The user-voice gate. Pure rules (the state machine, AS-norm, the filters, the
enrolment's quality and split, the bench and its calibration), the talkers' client-driven setup, and a talk
run with the gate in front of a fake talker. Every voice here is generated (tones or tests/voice_synth.py)."""

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from pisa.connectors import talker_gemini as G
from pisa.connectors import talker_xai as X
from pisa.connectors import voice_models as vm
from pisa.domain import voice_bench as bench
from pisa.domain import voice_enrol as enrol
from pisa.domain import voice_gate as gate
from pisa.domain.whisper import Fresh, Kind, Priority
from pisa.ports.talker import AskBrain, Audio, OwnerWords, PisaWords, Speaking, TalkerConfig
from pisa.services import talk
from pisa.services import voice_id as vid
from pisa.services.talk import Hooks, TalkRun, Thought
from tests import voice_synth as vs
from tests.test_live_talk import FakeLink, FakeSession, _until
from tests.virtual_time import run_talk

RATE = 16_000
TH = gate.Thresholds({1.0: 2.0, 1.5: 1.5}, margin=0.5, delta=0.5)


def hop(
    at: int, score: float | None, run: int, *, voiced: bool = True, j: bool = False, w: float = 1.0
) -> gate.Hop:
    return gate.Hop(at, voiced, run if voiced else 0, (at - run) if voiced else None, score, w, j)


# --- 1. the state machine
# --------------------------------------------------------------------------------------


def test_a_turn_starts_on_300_ms_of_his_voice_with_the_pre_roll_and_never_on_a_stranger() -> None:
    g = gate.Gate(TH)
    assert not [d for d in g.hop(hop(200, 3.0, 200)) if isinstance(d, gate.Open)]  # 200 ms: too short
    (opened,) = [d for d in g.hop(hop(400, 3.0, 400)) if isinstance(d, gate.Open)]
    assert not opened.barge and opened.preroll_from_ms == 0 and g.state is gate.State.OWNER_TURN
    g2 = gate.Gate(TH)
    g2.hop(hop(1800, 3.0, 200))  # their first scored hop of this run (1.7-1.8 s): too short to open
    later = [d for d in g2.hop(hop(2000, 3.0, 400)) if isinstance(d, gate.Open)]
    assert later[0].preroll_from_ms == 1800 - 100 - 300  # 300 ms before their first scored hop (one hop long)
    g3 = gate.Gate(TH)
    for i in range(1, 30):  # a stranger talks for 6 s: below θ, never a turn
        assert not [d for d in g3.hop(hop(i * 200, 1.9, i * 200)) if isinstance(d, gate.Open)]
    assert g3.state is gate.State.IDLE and g3.log[-1]["reason"] == "not_owner"


def test_a_barge_in_needs_350_ms_and_theta_plus_delta_on_two_hops() -> None:
    g = gate.Gate(TH)
    assert not [d for d in g.hop(hop(200, 2.4, 200, j=True)) if isinstance(d, gate.Open)]  # θ, not θ+δ
    assert not [d for d in g.hop(hop(400, 2.6, 400, j=True)) if isinstance(d, gate.Open)]  # one hop only
    (opened,) = [d for d in g.hop(hop(600, 2.6, 600, j=True)) if isinstance(d, gate.Open)]
    assert opened.barge
    g2 = gate.Gate(TH)
    g2.hop(hop(200, 2.6, 200, j=True))
    assert not [d for d in g2.hop(hop(400, 2.6, 300, j=True)) if isinstance(d, gate.Open)]  # 300 ms < 350
    assert [d for d in g2.hop(hop(600, 2.6, 400, j=True)) if isinstance(d, gate.Open)]


def test_the_turn_ends_on_owner_silence_strangers_counted_as_silence() -> None:
    g = gate.Gate(TH)
    g.hop(hop(400, 3.0, 400))
    at = 400
    for _ in range(2):  # 400 ms of a stranger's voice: owner-silence, not yet the end
        at += 200
        assert not [d for d in g.hop(hop(at, 0.2, 600)) if isinstance(d, gate.Close)]
    at += 200
    (closed,) = [d for d in g.hop(hop(at, 0.2, 800), lambda: True) if isinstance(d, gate.Close)]
    assert closed.reason == "complete" and closed.owner_silence_ms == 600
    # Smart Turn says "not complete": 1.5 s regardless
    g = gate.Gate(TH)
    g.hop(hop(400, 3.0, 400))
    ends = []
    for i in range(1, 9):
        ends += [
            d
            for d in g.hop(hop(400 + i * 200, None, 0, voiced=False), lambda: False)
            if isinstance(d, gate.Close)
        ]
    assert [e.reason for e in ends] == ["silence"] and ends[0].owner_silence_ms == 1500 + 100  # 8 hops
    # their voice for over a minute: the cap
    g = gate.Gate(TH)
    g.hop(hop(400, 3.0, 400))
    caps = [
        d
        for i in range(1, 400)
        for d in g.hop(hop(400 + i * 200, 3.0, 400 + i * 200))
        if isinstance(d, gate.Close)
    ]
    assert caps[0].reason == "cap"


def test_inside_his_turn_a_score_just_under_theta_is_still_his_hysteresis() -> None:
    g = gate.Gate(TH)
    g.hop(hop(400, 3.0, 400))
    for i in range(1, 12):  # 1.7 > θ - margin (1.5): still them for 2.2 s, never an end
        assert not [
            d
            for d in g.hop(hop(400 + i * 200, 1.7, 400 + i * 200), lambda: True)
            if isinstance(d, gate.Close)
        ]


def test_the_phone_sees_heard_at_most_twice_a_second() -> None:
    g = gate.Gate(TH)
    shown = []
    for i in range(1, 11):  # 2 s alternating stranger and silence every hop
        voiced = i % 2 == 0
        shown += [
            (i * 200, d.heard)
            for d in g.hop(hop(i * 200, 0.1, 200, voiced=voiced))
            if isinstance(d, gate.Shown)
        ]
    gaps = np.diff([at for at, _ in shown])
    assert len(shown) >= 2 and (gaps >= 500).all()


def test_thresholds_per_window_and_their_storage() -> None:
    assert TH.at(1.0) == 2.0 and TH.at(1.5) == 1.5 and TH.at(1.2) == 2.0 and TH.at(0.5) == 2.0
    assert gate.Thresholds.of(TH.stored()) == TH


# --- 2. the score: AS-norm over the route's prints
# ------------------------------------------------------------


def test_as_norm_max_over_prints_and_a_low_trust_print_counts_less() -> None:
    rng = np.random.default_rng(0)
    cohort = gate.unit(rng.standard_normal((300, 32)))
    me = gate.unit(rng.standard_normal(32))
    loud = gate.unit(me + 0.3 * rng.standard_normal(32))
    prints = gate.make_prints({"normal": [me], "loud": [loud]}, cohort)
    assert gate.owner_score(me, prints, cohort) > 5  # far above the cohort
    assert gate.owner_score(gate.unit(rng.standard_normal(32)), prints, cohort) < 2.5
    whisper = gate.make_prints({"whisper": [me]}, cohort, {"whisper": 0.5})
    assert gate.owner_score(me, whisper, cohort) == pytest.approx(
        gate.owner_score(me, prints[:1], cohort) * 0.5, rel=1e-4
    )


def test_the_hopper_scores_1_to_1_5_s_of_voice_every_100_ms_and_keeps_3_s() -> None:
    seen: list[tuple[int, int]] = []

    def score(window: np.ndarray) -> float:
        seen.append((len(window), 0))
        return 1.0

    h = vid.Hopper(vs.EnergyVad(), score)
    tone = 0.1 * np.sin(2 * np.pi * 300 * np.arange(5 * RATE) / RATE).astype(np.float32)
    hops = []
    for i in range(0, len(tone), 320):
        hops += h.push(tone[i : i + 320])
    # 100 ms hops (the VAD's 32 ms frames make them 96 or 128 ms apart)
    assert 49 <= len(hops) <= 50 and all(b.at_ms - a.at_ms in (96, 128) for a, b in itertools.pairwise(hops))
    windows = [w for w, _ in seen]
    assert RATE in windows and windows[-1] == int(
        1.5 * RATE
    )  # 1 s first, 1.5 s once there is that much voice
    assert all(w in (RATE, int(1.5 * RATE)) for w in windows[windows.index(RATE) :])
    assert hops[-1].window_s == 1.5 and len(h.ring) <= 3 * RATE
    assert hops[7].onset_ms == 0 and 700 <= hops[7].run_ms <= 800
    h.every = 2  # inside their turn: every other hop scored
    before = len(seen)
    for i in range(0, RATE, 320):
        h.push(tone[i : i + 320])
    assert len(seen) - before in (4, 5, 6)


# --- 3. the gate in a talk: only their turns reach the talker
# ---------------------------------------------------


class ToneEmbedder:
    """A deterministic 'speaker': the dominant tone's 100 Hz bucket."""

    name, version, dim = "tone", "t", 20

    def embed(self, samples: np.ndarray) -> np.ndarray:
        spec = np.abs(np.fft.rfft(samples))
        f = float(np.argmax(spec[1:]) + 1) * RATE / len(samples)
        v = np.full(self.dim, 0.01, dtype=np.float32)
        v[min(self.dim - 1, int(f // 100))] = 1.0
        return v


def tone(freq: float, seconds: float, amp: float = 0.1) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * freq * np.arange(int(seconds * RATE)) / RATE)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.float32)


def tone_profile() -> tuple[vid.Profile, np.ndarray, vid.Models]:
    emb = ToneEmbedder()
    cohort = gate.unit(np.stack([emb.embed(tone(f, 1.0)) for f in range(500, 2000, 50)]))
    prints = gate.make_prints({"normal": [emb.embed(tone(300, 1.0))]}, cohort)
    th = gate.Thresholds({1.0: 2.0, 1.5: 2.0}, 0.5, 0.5)
    profile = vid.Profile("airpods", "tone", "t", prints, th, 60.0, True, datetime.now(UTC), {})
    return profile, cohort, vid.Models(emb, vs.EnergyVad, vs.SilenceTurn())


class GatedSession(FakeSession):
    input_rate = 16000

    def __init__(self, log: list[Any]) -> None:
        super().__init__()
        self.log = log

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)
        self.log.append(("audio", len(pcm)))

    async def activity_start(self) -> None:
        self.log.append(("start",))

    async def activity_end(self) -> None:
        self.log.append(("end",))


class StreamLink(FakeLink):
    def __init__(self, audio: np.ndarray, log: list[Any], playing_from_ms: int | None = None) -> None:
        super().__init__()
        self.stream, self.log, self.done = audio, log, asyncio.Event()
        self.playing_from_ms = playing_from_ms
        self.at_ms = 0

    async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
        assert rate == RATE
        for i in range(0, len(self.stream), 320):  # 20 ms frames
            self.at_ms = i * 1000 // RATE
            if self.playing_from_ms is not None and self.at_ms >= self.playing_from_ms:
                self.buffered_ms = 500  # Pisa is speaking on the phone
            yield vid.float_to_pcm(self.stream[i : i + 320])
            await asyncio.sleep(0)
        self.done.set()
        await asyncio.Event().wait()

    def send(self, message: dict[str, Any]) -> None:
        super().send(message)
        self.log.append(("phone", message["type"]))

    def clear(self) -> None:
        super().clear()
        self.playing_from_ms = None


def gated_run(
    audio: np.ndarray, playing_from_ms: int | None = None
) -> tuple[TalkRun, GatedSession, StreamLink, list[Any]]:
    log: list[Any] = []
    session, link = GatedSession(log), StreamLink(audio, log, playing_from_ms)
    seen: list[Any] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        seen.append(words)
        return Thought("Ok.", Kind.SAY)

    async def fresh() -> Fresh:
        return Fresh("adesso")

    async def ok() -> bool:
        return True

    async def exchange(e: Any) -> None:
        return None

    run = TalkRun(
        uuid.uuid4(),
        session,
        link,
        Hooks(think=think, fresh=fresh, meter=ok, exchange=exchange),
        "it-IT",
        Fresh("adesso"),
    )
    profile, cohort, models = tone_profile()
    run.gate = vid.LiveGate(models, profile, cohort, run)
    return run, session, link, seen


def _forwarded_freqs(session: GatedSession) -> float:
    x = vid.pcm_to_float(b"".join(session.audio))
    spec = np.abs(np.fft.rfft(x))
    return float(np.argmax(spec[1:]) + 1) * RATE / len(x)


def test_only_his_turn_reaches_the_talker_with_its_pre_roll_and_a_stranger_never() -> None:
    audio = np.concatenate([silence(1.0), tone(300, 1.2), silence(2.0), tone(900, 1.5), silence(2.0)])
    run, session, link, _ = gated_run(audio)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await asyncio.wait_for(link.done.wait(), 10)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    asyncio.run(go())
    starts = [e for e in session.log if e[0] == "start"]
    ends = [e for e in session.log if e[0] == "end"]
    assert len(starts) == 1 and len(ends) == 1  # their one turn; the stranger's 1.5 s opened nothing
    first_audio = session.log.index(next(e for e in session.log if e[0] == "audio"))
    assert session.log.index(starts[0]) < first_audio  # activityStart, then the audio
    assert abs(_forwarded_freqs(session) - 300) < 20  # only their voice, never the 900 Hz stranger
    sent_s = sum(len(a) for a in session.audio) / 2 / RATE
    assert 1.2 <= sent_s <= 3.0  # their 1.2 s, the pre-roll and the turn's tail; never the 7.7 s stream
    first = vid.pcm_to_float(session.audio[0])
    assert np.abs(first[: int(0.2 * RATE)]).max() < 1e-3 and len(first) >= int(0.5 * RATE)  # the pre-roll
    gates = [e for e in run.timeline.events if e["kind"] == "gate"]
    assert [g["decision"] for g in gates if g["decision"] != "reject"] == ["turn_start", "turn_end"]
    assert any(g["decision"] == "reject" for g in gates)  # the stranger's run: scores only, no audio
    assert not any(k in g for g in gates for k in ("text", "words", "audio", "pcm"))  # never audio or words
    heard = [m["heard"] for m in link.sent if m["type"] == "talk.gate"]
    assert "owner" in heard and "other" in heard


def test_his_barge_in_stops_the_phone_first_then_starts_his_turn_and_a_stranger_never_stops_pisa() -> None:
    audio = np.concatenate([silence(0.5), tone(900, 1.5), silence(0.5), tone(300, 1.2), silence(2.0)])
    run, session, link, _ = gated_run(audio, playing_from_ms=0)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await asyncio.wait_for(link.done.wait(), 10)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    asyncio.run(go())
    stop = session.log.index(("phone", "talk.playout_stop"))
    start = session.log.index(("start",))
    assert stop < start and link.cleared >= 1  # the phone stops Pisa's audio first
    assert session.log.count(("phone", "talk.playout_stop")) == 1  # the stranger's 1.5 s stopped nothing
    assert [e["by"] for e in run.timeline.events if e["kind"] == "barge_in"] == ["gate"]


def test_without_the_gate_today_s_path_sends_every_frame() -> None:
    run, session, link, _ = gated_run(np.concatenate([tone(900, 0.5)]))
    run.gate = None
    session.input_rate = RATE

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await asyncio.wait_for(link.done.wait(), 10)
        await asyncio.sleep(0.02)
        run.end("owner")
        await task

    asyncio.run(go())
    assert len(session.audio) == 25 and not [e for e in session.log if e[0] == "start"]


# --- 3b. the talkers in client-driven mode
# ---------------------------------------------------------------------


def _cfg(client: bool) -> TalkerConfig:
    return TalkerConfig(
        key="sk-test-xxxx",
        voice="Aoede",
        lang="it-IT",
        instructions="rules",
        route="airpods",
        client_activity=client,
    )


def test_gemini_s_own_detection_is_off_and_no_interruption_only_when_the_gate_is_on() -> None:
    on = G.connect_config(_cfg(True)).realtime_input_config
    assert on is not None and on.automatic_activity_detection is not None
    assert on.automatic_activity_detection.disabled is True
    assert on.activity_handling == G.types.ActivityHandling.NO_INTERRUPTION
    off = G.connect_config(_cfg(False)).realtime_input_config
    assert off is not None and off.automatic_activity_detection is not None
    assert off.automatic_activity_detection.disabled is False and off.activity_handling is None
    # the same instructions either way: nothing added to any prompt
    assert G.connect_config(_cfg(True)).system_instruction == G.connect_config(_cfg(False)).system_instruction


def test_gemini_gives_the_language_it_heard_and_its_start_and_end_of_activity() -> None:
    message = G.types.LiveServerMessage(
        server_content=G.types.LiveServerContent(
            input_transcription=G.types.Transcription(
                text="où est la gare", finished=True, language_code="fr-FR"
            )
        )
    )
    (words,) = [e for e in G.parse(message) if isinstance(e, OwnerWords)]
    assert words.lang == "fr-FR" and words.final

    sent: list[dict[str, Any]] = []

    class Live:
        async def send_realtime_input(self, **kw: Any) -> None:
            sent.append(kw)

    import contextlib

    s = G.GeminiLiveSession(Live(), contextlib.AsyncExitStack(), client_activity=True)

    async def go() -> None:
        await s.activity_start()
        await s.activity_end()
        await s.end_turn()  # with the gate, a hand end is the gate's end

    asyncio.run(go())
    assert "activity_start" in sent[0] and "activity_end" in sent[1] and "activity_end" in sent[2]


def test_xai_has_no_server_vad_with_the_gate_and_commits_his_turn() -> None:
    assert X.session_update(_cfg(True))["session"]["turn_detection"] == {"type": None}
    assert X.session_update(_cfg(False))["session"]["turn_detection"] == {"type": "server_vad"}
    sent: list[str] = []

    class Ws:
        async def send(self, data: str) -> None:
            sent.append(data)

    s = X.GrokVoiceSession(Ws(), client_activity=True)  # type: ignore[arg-type]
    s._responding = True

    async def go() -> None:
        await s.activity_start()
        await s.activity_end()

    asyncio.run(go())
    kinds = [__import__("json").loads(e)["type"] for e in sent]
    assert kinds == [
        "response.cancel",
        "input_audio_buffer.clear",
        "input_audio_buffer.commit",
        "response.create",
    ]


# --- 4. the filters: a dropped turn gets no reply at all
# -------------------------------------------------------


def test_what_is_dropped_before_the_brain() -> None:
    assert gate.dropped(["où", "est", "la", "gare"], "fr-FR", 2.2, 2.0, echo=False) == "language"
    assert gate.dropped(["où", "est", "la", "gare"], "fr-FR", 5.0, 2.0, echo=False) is None  # strong: theirs
    assert gate.dropped(["dove", "vado"], "it-IT", 2.1, 2.0, echo=False) is None
    assert gate.dropped(["allora"], "it-IT", 5.0, 2.0, echo=False) == "fragment"
    assert gate.dropped(["basta"], "it-IT", 5.0, 2.0, echo=False) is None
    assert gate.dropped(["sì"], "it-IT", 5.0, 2.0, echo=False) is None
    assert gate.dropped(["va", "bene"], "it-IT", 5.0, 2.0, echo=True) == "echo"


class StubGate:
    def __init__(self, score: float) -> None:
        self.last_score, self.theta, self.is_open = score, 2.0, False

    async def feed(self, pcm: bytes) -> None:
        return None


def _filtered(words: str, lang: str, score: float) -> tuple[list[Any], FakeSession, FakeLink, TalkRun]:
    session, link, seen = GatedSession([]), FakeLink(), []

    async def think(w: str, trace: str, yes_by_code: bool) -> Thought:
        seen.append(w)
        return Thought("Ok.", Kind.SAY)

    async def fresh() -> Fresh:
        return Fresh("adesso")

    async def ok() -> bool:
        return True

    async def exchange(e: Any) -> None:
        return None

    run = TalkRun(
        uuid.uuid4(),
        session,
        link,
        Hooks(think=think, fresh=fresh, meter=ok, exchange=exchange),
        "it-IT",
        Fresh("adesso"),
    )
    run.gate = StubGate(score)  # type: ignore[assignment]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.activity_end("complete")  # their turn closed at the gate: the reply waits for its words
        session.queue.put_nowait(Audio(b"\x01\x01"))
        session.queue.put_nowait(OwnerWords(words, final=True, lang=lang))
        session.queue.put_nowait(Audio(b"\x02\x02"))
        session.queue.put_nowait(PisaWords("Non ho capito.", final=True))
        session.queue.put_nowait(Speaking(False))
        await asyncio.sleep(0.2)
        run.end("owner")
        await task

    run_talk(go())
    return seen, session, link, run


def test_a_stranger_s_language_with_a_weak_score_gets_no_reply_at_all() -> None:
    seen, _, link, run = _filtered("où est la gare", "fr-FR", 2.2)
    assert seen == [] and link.played == []  # no thought, nothing of Pisa's reply played
    assert not [m for m in link.sent if m["type"] == "talk.said"]
    assert any(m.get("ignored") == "language" for m in link.sent)
    assert [e["reason"] for e in run.timeline.events if e["kind"] == "gate"] == ["language"]


def test_a_fragment_is_dropped_and_his_italian_is_answered_with_the_held_reply() -> None:
    seen, _, link, _ = _filtered("allora", "it-IT", 5.0)
    assert seen == [] and link.played == []
    seen, _, link, _ = _filtered("che ore sono adesso", "it-IT", 2.1)
    assert seen == ["che ore sono adesso"] and link.played == [b"\x01\x01", b"\x02\x02"]  # held, then played


def test_the_held_reply_plays_when_no_words_come_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk, "JUDGE_WAIT_S", 0.05)
    session, link = GatedSession([]), FakeLink()

    async def think(w: str, trace: str, yes_by_code: bool) -> Thought:
        return Thought("Ok.", Kind.SAY)

    async def fresh() -> Fresh:
        return Fresh("adesso")

    async def ok() -> bool:
        return True

    async def exchange(e: Any) -> None:
        return None

    run = TalkRun(
        uuid.uuid4(),
        session,
        link,
        Hooks(think=think, fresh=fresh, meter=ok, exchange=exchange),
        "it-IT",
        Fresh("adesso"),
    )
    run.gate = StubGate(5.0)  # type: ignore[assignment]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.activity_end("complete")
        session.queue.put_nowait(Audio(b"\x01\x01"))
        await _until(lambda: link.played == [b"\x01\x01"])
        run.end("owner")
        await task

    run_talk(go())


# --- 5. "cerco…" before a slow tool
# --------------------------------------------------------------------------


def test_cerco_is_said_at_once_once_per_turn_through_a_whisper() -> None:
    run_, session, _, _ = gated_run(silence(0.1))
    run_.gate = None

    async def go() -> None:
        await run_.working("Cerco, un attimo.")
        await run_.working("Cerco, un attimo.")

    asyncio.run(go())
    (said,) = session.whispers
    assert said.kind is Kind.SAY and said.priority is Priority.NOW and said.text == "Cerco, un attimo."
    assert [e["kind"] for e in run_.timeline.events].count("working") == 1


# --- the enrolment's rules
# -------------------------------------------------------------------------------------


def test_the_script_has_every_block_synthetic_and_the_mini_one() -> None:
    for lang in ("it", "en"):
        s = enrol.script(lang)  # type: ignore[arg-type]
        blocks = {b["block"]: b for b in s["blocks"]}  # type: ignore[index]
        assert list(blocks) == ["A", "B", "C", "D", "E"]  # No F, the phone plays no babble
        assert len(blocks["A"]["sentences"]) == 10 and blocks["A"]["language"] == "it"
        assert len(blocks["B"]["sentences"]) == 10 and blocks["B"]["language"] == "en"
        assert (
            len(blocks["D"]["sentences"]) == 4
            and len(blocks["E"]["sentences"]) == 3
            and blocks["E"]["optional"]
        )
        assert [b["block"] for b in s["mini"]] == ["A", "C"]  # type: ignore[index]
        mini = {b["block"]: b for b in s["mini"]}  # type: ignore[index]
        assert len(mini["A"]["sentences"]) == 5 and mini["C"]["seconds_per_language"] == 15
        test = s["test"]
        assert len(test) == 6 and not set(test) & set(blocks["A"]["sentences"])  # type: ignore[arg-type]


def test_a_block_s_quality_and_its_hint() -> None:
    frame = 512
    voice = (0.2 * np.sin(2 * np.pi * 200 * np.arange(20 * RATE) / RATE)).astype(np.float32)
    quiet = np.concatenate(
        [voice, 0.001 * np.random.default_rng(0).standard_normal(5 * RATE).astype(np.float32)]
    )
    pcm = (quiet * 32767).astype(np.int16)
    flags = np.array([i * frame < len(voice) for i in range(len(pcm) // frame)])
    q = enrol.quality("A", pcm[: len(flags) * frame], flags)
    assert q.ok and q.hint is None and q.voiced_s >= 19 and q.snr_db > 40
    clipped = (np.clip(quiet * 6, -1, 1) * 32767).astype(np.int16)
    assert enrol.quality("A", clipped[: len(flags) * frame], flags).hint == "quieter"
    short = flags.copy()
    short[100:] = False
    assert enrol.quality("A", pcm[: len(flags) * frame], short).hint == "again"
    faint = (quiet * 0.01 * 32767).astype(np.int16)
    assert enrol.quality("A", faint[: len(flags) * frame], flags).hint == "louder"


def test_a_fifth_of_each_block_is_held_out() -> None:
    assert enrol.held_out([(0, 1)] * 10) == [False, False, False, False, True] * 2
    assert enrol.held_out([(0, 1)] * 3) == [False, False, True]
    assert enrol.held_out([(0, 1)]) == [False]


# --- 6, 7. the bench and the calibration
# -------------------------------------------------------------------------


def test_the_band_limit_and_the_mix_at_an_snr() -> None:
    x = (
        0.1 * np.sin(2 * np.pi * 50 * np.arange(RATE) / RATE)
        + 0.1 * np.sin(2 * np.pi * 1000 * np.arange(RATE) / RATE)
    ).astype(np.float32)
    y = bench.band_limit(x)
    spec = np.abs(np.fft.rfft(y))
    assert spec[50] < 0.01 * spec[1000]  # 50 Hz gone, 1 kHz kept
    owner = tone(300, 1.0)
    noise = vs.say(vs.speaker(3), 3.5, 0)
    for snr in bench.SNRS_DB:
        mixed = bench.mix(owner, noise, snr).astype(np.float64)
        at = int(bench.LEAD_S * RATE)
        mixed[at : at + len(owner)] -= owner
        assert abs(20 * np.log10(bench.rms(owner) / bench.rms(mixed)) - snr) < 0.1


def _synthetic_trials(models: vid.Models) -> tuple[list[bench.Trial], vid.Scorer]:
    owner = vs.speaker(1)
    coh = vs.cohort(models.embedder, 40)
    enrol_embs = [models.embedder.embed(vs.say(owner, 1.5, u)) for u in range(12)]
    prints = gate.make_prints({"normal": enrol_embs}, coh)
    scorer = vid.Scorer(prints, coh, models.embedder)
    held = [vs.say(owner, 2.0, 100 + u) for u in range(3)]
    babble = [
        bench.babble(
            [vs.say(vs.speaker(500 + 10 * c + t), 4.0, t) for t in range(4)], 8.0, np.random.default_rng(c)
        )
        for c in range(3)
    ]
    return vid.bench_trials(models, scorer, held, babble, stranger_s=60.0), scorer


def test_the_bench_reports_the_synthesis_metrics_and_calibration_holds_strangers_under_target() -> None:
    models = vs.fake_models()
    trials, _ = _synthetic_trials(models)
    owners = [t for t in trials if t.kind == "owner"]
    assert len(owners) == 3 * 5 and {t.snr_db for t in owners} == set(bench.SNRS_DB)
    assert sum(t.seconds for t in trials if t.kind == "stranger") == pytest.approx(60.0)
    _, report = bench.calibrate(trials)
    assert report.fa_per_min <= bench.FA_TARGET_PER_MIN  # item 7: ≤ 0.1 a minute at the shortest window
    stored = report.stored()
    for key in ("fa_per_min", "owner_false_reject", "onset_clipped", "barge_ms_p50", "end_ms_p50", "by_snr"):
        assert key in stored
    # a threshold below the strangers' scores lets them in: the target fails without calibration
    loose = gate.Thresholds({w: -10.0 for w in gate.WINDOWS_S}, 0.1, 0.1)
    assert bench.report(trials, loose).fa_per_min > bench.FA_TARGET_PER_MIN


@pytest.mark.skipif(vs.real_models_dir() is None, reason="PISA_VOICE_MODELS_DIR holds no ONNX models")
def test_the_real_models_load_and_run_on_the_cpu() -> None:
    root = vs.real_models_dir()
    assert root is not None
    for name in vm.EMBEDDERS:
        e = vm.SherpaEmbedder(root, name)
        assert e.embed(vs.say(vs.speaker(1), 1.5, 0)).shape == (e.dim,)
    vad = vm.silero(root)()
    speech = vs.say(vs.speaker(2), 1.0, 0)
    assert max(vad.prob(speech[i : i + 512]) for i in range(0, len(speech) - 512, 512)) > 0.5
    vad.reset()
    assert vad.prob(np.zeros(512, dtype=np.float32)) < 0.5
    p = vm.SmartTurn(root).complete(np.concatenate([speech, silence(1.0)]))
    assert 0.0 <= p <= 1.0
    assert vm.whisper_features(speech).shape == (80, 800)


def test_a_model_file_missing_or_altered_names_itself(tmp_path: Any) -> None:
    with pytest.raises(vm.ModelMissing, match=r"silero_vad\.onnx is missing"):
        vm.path_of(str(tmp_path), "silero_vad")
    (tmp_path / "silero_vad.onnx").write_bytes(b"x" * 10)
    with pytest.raises(vm.ModelMissing, match="is missing"):
        vm.path_of(str(tmp_path), "silero_vad")
    assert all(len(m.sha256) == 64 and m.licence for m in vm.MODELS.values())


def test_the_mel_filters_match_whisper_s_shape() -> None:
    m = vm.mel_filters()
    assert m.shape == (80, 201) and (m >= 0).all() and (m.sum(axis=1) > 0).all()


def test_an_altered_model_file_of_the_right_size_is_refused_at_load(tmp_path: Any) -> None:
    spec = vm.MODELS["silero_vad"]
    (tmp_path / spec.file).write_bytes(b"x" * spec.size)
    with pytest.raises(vm.ModelMissing, match="pinned checksum"):
        vm.path_of(str(tmp_path), "silero_vad")
    assert "/main/" not in vm.MODELS["smart_turn"].url  # pinned to a revision


# --- the review's fixes
# -------------------------------------------------------------------------------------------


def _drive(run: TalkRun, link: "StreamLink") -> str:
    async def go() -> str:
        task = asyncio.create_task(run.run())
        await asyncio.wait_for(link.done.wait(), 10)
        await asyncio.sleep(0.05)
        run.end("owner")
        return await task

    return asyncio.run(go())


def test_babble_running_straight_into_him_sends_at_most_700_ms_of_pre_roll() -> None:
    audio = np.concatenate([tone(900, 4.0), tone(300, 1.2), silence(2.0)])  # no gap: one VAD run from 0 s
    run, session, link, _ = gated_run(audio)
    _drive(run, link)
    assert session.log.count(("start",)) == 1
    preroll = vid.pcm_to_float(session.audio[0])
    assert len(preroll) <= int(0.7 * RATE)  # never 3 s of strangers before them


def _seconds_of(x: np.ndarray, freq: float) -> float:
    """How long `freq` dominates the audio, in 20 ms pieces."""
    pieces = [x[i : i + 320] for i in range(0, len(x) - 320, 320)]
    hits = 0
    for p in pieces:
        if np.abs(p).max() < 1e-3:
            continue
        spec = np.abs(np.fft.rfft(p, n=1600))
        hits += abs(int(np.argmax(spec[1:]) + 1) * 10 - freq) < 60
    return hits * 0.02


def test_a_stranger_after_his_words_reaches_the_talker_as_silence() -> None:
    audio = np.concatenate([silence(1.0), tone(300, 1.2), tone(900, 2.5), silence(2.0)])
    run, session, link, _ = gated_run(audio)
    _drive(run, link)
    sent = vid.pcm_to_float(b"".join(session.audio))
    # The per-hop score cuts the stranger's tail at once, while the 1-1.5 s window still
    # says "his" (0.72 s leaked before it; raw it would be 2+ s)
    assert _seconds_of(sent, 900) <= 0.2
    assert _seconds_of(sent, 300) >= 1.0


def _stub_run(score: float = 5.0) -> tuple[TalkRun, "GatedSession", FakeLink, list[str]]:
    session, link, seen = GatedSession([]), FakeLink(), []

    async def think(w: str, trace: str, yes_by_code: bool) -> Thought:
        seen.append(w)
        return Thought("Ok.", Kind.SAY)

    async def fresh() -> Fresh:
        return Fresh("adesso")

    async def ok() -> bool:
        return True

    async def exchange(e: Any) -> None:
        return None

    hooks = Hooks(think=think, fresh=fresh, meter=ok, exchange=exchange)
    run = TalkRun(uuid.uuid4(), session, link, hooks, "it-IT", Fresh("adesso"))
    run.gate = StubGate(score)  # type: ignore[assignment]
    return run, session, link, seen


def test_silence_covers_only_the_dropped_reply_and_its_call_gets_a_silent_result() -> None:
    run, session, link, _ = _stub_run(2.2)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.activity_end("complete")
        session.queue.put_nowait(OwnerWords("où est la gare", final=True, lang="fr-FR"))
        session.queue.put_nowait(AskBrain("c9", "dov'è la stazione"))
        session.queue.put_nowait(Audio(b"\x01\x01"))
        session.queue.put_nowait(Speaking(False))  # the dropped reply ends
        session.queue.put_nowait(Audio(b"\x02\x02"))  # Pisa's next line (a notice, say) plays
        await _until(lambda: link.played == [b"\x02\x02"])
        run.end("owner")
        await task

    asyncio.run(go())
    ((call, answer),) = session.answers
    assert call == "c9" and answer.priority is Priority.SILENT


def test_a_new_turn_under_a_pending_reply_never_plays_that_reply() -> None:
    run, session, link, _ = _stub_run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.activity_end("complete")
        session.queue.put_nowait(OwnerWords("che ore sono adesso", final=True, lang="it-IT"))
        await asyncio.sleep(0.05)
        await run.activity_start(False, 5.0)  # they speak again before Pisa's reply began
        session.queue.put_nowait(Audio(b"\x01\x01"))  # the older reply: never played
        session.queue.put_nowait(PisaWords("Sono le tre.", final=True))
        session.queue.put_nowait(Speaking(False))
        await asyncio.sleep(0.1)
        await run.activity_end("complete")
        session.queue.put_nowait(OwnerWords("e domani piove", final=True, lang="it-IT"))
        session.queue.put_nowait(Audio(b"\x02\x02"))  # the new turn's reply plays
        await _until(lambda: b"\x02\x02" in link.played)
        run.end("owner")
        await task

    run_talk(go())
    assert b"\x01\x01" not in link.played


def test_a_route_change_under_the_gate_ends_the_talk_with_route() -> None:
    run, _, link, _ = gated_run(silence(0.2))

    async def go() -> str:
        task = asyncio.create_task(run.run())
        link.messages.put_nowait({"type": "talk.route", "route": "speaker"})
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(go()) == "route"
    assert link.sent[-1] == {"type": "talk.ended", "reason": "route"}


class Target:
    def __init__(self, speaking: bool = False) -> None:
        self.calls: list[Any] = []
        self.speaking = speaking

    async def activity_start(self, barge: bool, score: float) -> None:
        self.calls.append("start")

    async def owner_audio(self, pcm: bytes) -> None:
        self.calls.append("audio")

    async def activity_end(self, reason: str) -> None:
        self.calls.append(("end", reason))

    def pisa_speaking(self) -> bool:
        return self.speaking

    def phone(self, message: dict[str, Any]) -> None:
        self.calls.append(message["type"])

    def timeline_gate(self, **data: Any) -> None:
        self.calls.append(("gate", data.get("decision")))

    def gate_failed(self, detail: str) -> None:
        self.calls.append("failed")

    def profile_gone(self) -> None:
        self.calls.append("gone")


class Breaking(ToneEmbedder):
    broken = False

    def embed(self, samples: np.ndarray) -> np.ndarray:
        if self.broken:
            raise RuntimeError("onnx")
        return super().embed(samples)


def _live(target: Target, still: Any = None) -> tuple[vid.LiveGate, Breaking]:
    profile, cohort, _ = tone_profile()
    emb = Breaking()
    gate_ = vid.LiveGate(
        vid.Models(emb, vs.EnergyVad, vs.SilenceTurn()), profile, cohort, target, still=still
    )  # type: ignore[arg-type]
    return gate_, emb


async def _feed(g: vid.LiveGate, x: np.ndarray) -> None:
    for i in range(0, len(x), 320):
        await g.feed(vid.float_to_pcm(x[i : i + 320]))


def test_a_failing_model_closes_his_turn_and_three_failures_end_the_talk() -> None:
    target = Target()
    g, emb = _live(target)

    async def go() -> None:
        await _feed(g, np.concatenate([silence(0.5), tone(300, 0.8)]))
        assert g.is_open
        emb.broken = True
        await _feed(g, tone(300, 0.6))

    asyncio.run(go())
    assert ("end", "error") in target.calls and not g.is_open
    assert target.calls.count("failed") >= 1 and g.errors >= 3


def test_his_prints_deleted_under_a_live_talk_open_no_turn() -> None:
    target = Target()

    async def gone() -> bool:
        return False

    g, _ = _live(target, still=gone)
    asyncio.run(_feed(g, np.concatenate([silence(0.5), tone(300, 1.0)])))
    assert "gone" in target.calls and "start" not in target.calls


def test_a_gated_talk_with_no_audio_closes_his_turn_then_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk, "TICK_S", 0.01)
    monkeypatch.setattr(talk, "GATE_NO_FRAMES_CLOSE_S", 0.05)
    monkeypatch.setattr(talk, "GATE_NO_FRAMES_END_S", 0.3)
    run, _, _, _ = _stub_run()
    closed: list[str] = []

    class Open(StubGate):
        is_open = True

        async def close_now(self, reason: str = "outside") -> None:
            closed.append(reason)
            self.is_open = False

    run.gate = Open(5.0)  # type: ignore[assignment]
    run.gate.is_open = True  # type: ignore[misc]
    assert run_talk(asyncio.wait_for(run.run(), 5)) == "idle"
    assert closed == ["no_audio"] and "no audio from the phone" in run.close_detail


def test_a_dropped_turn_leaves_no_words_in_the_timeline_or_on_the_phone() -> None:
    run, session, link, _ = _stub_run(2.2)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.activity_start(False, 2.2)
        session.queue.put_nowait(OwnerWords("où est", final=False, lang="fr-FR"))
        await asyncio.sleep(0.05)
        await run.activity_end("complete")
        session.queue.put_nowait(OwnerWords("où est la gare", final=True, lang="fr-FR"))
        await _until(lambda: any(m.get("ignored") for m in link.sent))
        run.end("owner")
        await task

    run_talk(go())
    assert all(not e.get("text") for e in run.timeline.events if e["kind"] == "owner_words")
    (dropped,) = [m for m in link.sent if m.get("ignored")]
    assert dropped["text"] == "" and dropped["ignored"] == "language"


def test_the_gate_s_log_is_bounded() -> None:
    g = gate.Gate(TH)
    for i in range(1, 3000):
        g.hop(hop(i * 100, 0.1, i * 100))
    assert len(g.log) == gate.LOG_MAX


def test_the_server_scores_his_test_reading_clean_and_in_babble_against_public_strangers() -> None:
    """§1.2: no babble on the phone; the server mixes it and replays the stored strangers."""
    profile, cohort, models = tone_profile()
    reading = np.concatenate(
        [np.concatenate([silence(0.6), tone(300, 1.5)]) for _ in range(6)] + [silence(0.6)]
    )
    babble = [tone(1500, 6.0, 0.02)]
    strangers = [
        np.concatenate([silence(0.3), tone(900, 2.0)]),
        np.concatenate([silence(0.3), tone(1200, 2.0)]),
    ]
    r = vid.test_result(models, profile.prints, cohort, reading, babble, strangers)
    assert r["owner_total"] == 6 * 4 and r["strangers_total"] == 2 and r["accepted_strangers"] == 0
    assert set(r["by_snr"]) == {"clean", "10", "5", "0"} and r["by_snr"]["clean"] == 1.0
    assert r["ok"] == (r["accepted_owner"] >= 0.8 * 24)


def test_the_owner_guard_needs_his_turns_at_10_and_5_db() -> None:
    """§1.4: strangers held is not enough."""
    base = bench.Report(2.0, 0.0, 0.0, 0.0, 10, 0.5, 0.1, 0.15, 0.0, None, None, None, None, 0, {})
    assert bench.route_ok(base)
    from dataclasses import replace

    assert not bench.route_ok(replace(base, owner_fr_guarded=0.21))
    assert not bench.route_ok(replace(base, fa_per_min=0.2))
    assert not bench.route_ok(replace(base, owner_trials=0))


def test_the_server_mixes_his_noisy_positives_itself() -> None:
    """§1.1: with babble stored, each kept A/B/C utterance is embedded clean and mixed."""
    _profile, cohort, _models = tone_profile()
    counted: list[int] = []

    class Counting(ToneEmbedder):
        def embed(self, samples: np.ndarray) -> np.ndarray:
            counted.append(len(samples))
            return super().embed(samples)

    m = vid.Models(Counting(), vs.EnergyVad, None)
    x = tone(300, 1.2)
    blocks = [("A", x, [(0, len(x))], [False]), ("D", x, [(0, len(x))], [False])]
    vid.make_route_prints(m, cohort, blocks, [])
    clean = len(counted)
    counted.clear()
    vid.make_route_prints(m, cohort, blocks, [tone(1500, 3.0, 0.02)])
    assert len(counted) == clean + 1  # A gets a noisy copy; D (the loud print) doesn't


def test_the_microphone_mode_is_an_app_event_family_of_its_own() -> None:
    from pisa.domain import app_events

    (kept,) = app_events.sanitize([{"event": "microphone_mode", "site": "airpods", "state": "voiceIsolation",
                                    "at": "2026-11-08T10:00:00+00:00"}])  # fmt: skip
    assert kept["site"] == "airpods" and kept["state"] == "voiceIsolation"
    assert app_events.family("microphone_mode") == "microphone" and app_events.PER_DAY["microphone"] == 20
    assert app_events.families("microphone_mode") == ("microphone_mode",)
