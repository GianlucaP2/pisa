"""Natural voice: a WebRTC talk with the server's live voice model, with aiortc (`pip install aiortc`).

    python talk_webrtc.py [--state FILE] [--seconds 20] [--client-frames frames.jsonl]

The microphone goes up as an Opus audio track, Pisa's voice comes back on the same peer, and one data channel,
label "pisa" with subprotocol "pisa.v2", carries contract 2 frames both ways. When the server can't talk right now
(409 talk_economy, provider_key_missing or budget_exceeded, 502 talker_unavailable), the client talks in economy
instead (voice_economy.py). `--client-frames` writes the frames this client would send on the channel, for
`pisa-conformance stream --client`.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pisa_example import PisaError, State, arguments, http_client
from render import Conversation
from session import Session

ECONOMY = {"talk_economy", "provider_key_missing", "budget_exceeded", "talker_unavailable"}


# [guide:offer]
async def offer() -> tuple[Any, Any, str]:
    """A peer with the microphone and the "pisa" data channel; its offer with every ICE candidate (no trickle)."""
    from aiortc import RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    peer = RTCPeerConnection()
    # A real client sends its microphone with echo cancellation on, e.g. aiortc.contrib.media.MediaPlayer
    # ("default", format="pulse") on Linux. AudioStreamTrack is silence: enough to open the talk.
    peer.addTrack(AudioStreamTrack())
    channel = peer.createDataChannel("pisa", protocol="pisa.v2")  # without pisa.v2 the server closes it
    await peer.setLocalDescription(await peer.createOffer())  # aiortc gathers every candidate here
    return peer, channel, peer.localDescription.sdp


# [/guide:offer]


# [guide:open]
async def open_talk(session: Session, sdp: str, lang: str = "en-US") -> dict[str, Any]:
    """POST /v1/talk/sessions: 201 {session_id, sdp_answer, ice_servers}."""
    return await session.request(
        "POST",
        "/v1/talk/sessions",
        json={
            "sdp_offer": sdp,
            "trigger": "press",  # press, wake, intent, notice, proactive (with ask_id) or glasses
            "lang": lang,  # it-IT or en-US
            "route": "speaker",  # speaker, receiver, airpods, headphones, car or glasses
            # "first_text": "words said with the wake phrase, recognised on the device",
        },
    )


# [/guide:open]


# [guide:channel]
def wire(peer: Any, channel: Any, screen: Conversation, reply_path: Path) -> Any:
    """The data channel speaks /v2/live's frames (its own seq from 1); Pisa's voice is the remote audio
    track. Returns the recorder of Pisa's voice (start it, and stop it at the end)."""
    from aiortc.contrib.media import MediaRecorder

    recorder = MediaRecorder(str(reply_path))  # a real client plays the track instead

    @channel.on("open")
    def _open() -> None:
        channel.send(json.dumps({"op": "ping"}))

    @channel.on("message")
    def _message(text: str) -> None:
        screen.frame(json.loads(text))  # pisa.voice.state, .transcript, .gate, TEXT_MESSAGE_*, surfaces...

    @peer.on("track")
    def _track(track: Any) -> None:  # Pisa's voice: Opus at 48 kHz, 20 ms frames; play it as it comes
        recorder.addTrack(track)

    return recorder


CHANNEL_FRAMES = [  # what a client sends on the talk's channel (no hello, no run: those go on /v2/live)
    {"op": "ping"},
    {"op": "cancel", "runId": "run-of-the-exchange-in-progress"},  # a touch barge-in: Pisa stops talking
    # the audio route changed: speaker, receiver, airpods, headphones, car or glasses
    {"op": "custom", "name": "pisa.presence",
     "value": {"event": "earbuds_connected", "state": {"route": "airpods"}}},
    {"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.choose", "surfaceId": "talk:choice-1",
     "sourceComponentId": "c1", "timestamp": "2026-11-18T10:00:00Z", "context": {"optionId": "early"}}}},
]  # fmt: skip
# [/guide:channel]


async def main() -> int:
    parser = arguments(__doc__ or "")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--client-frames", type=Path)
    parser.add_argument("--reply", type=Path, default=Path("pisa-voice.wav"))
    args = parser.parse_args()
    if args.client_frames:  # only write them (no server needed)
        args.client_frames.write_text("".join(json.dumps(f) + "\n" for f in CHANNEL_FRAMES))
        return 0
    state = State.load(args.state)
    peer, channel, sdp = await offer()
    print(f"offer: {len(sdp)} bytes of SDP, audio and the 'pisa' channel")
    try:
        async with http_client(state) as http:
            session = Session(http, state)
            # [guide:fallback]
            try:
                talk = await open_talk(session, sdp)
            except PisaError as err:
                if err.code in ECONOMY:  # not now: talk in economy over /v2/live (voice_economy.py)
                    print(f"natural voice is off ({err.code}: {err.message}): talk in economy")
                    return 0
                raise
            # [/guide:fallback]
            from aiortc import RTCSessionDescription

            screen = Conversation()
            recorder = wire(peer, channel, screen, args.reply)
            await peer.setRemoteDescription(RTCSessionDescription(talk["sdp_answer"], "answer"))
            await recorder.start()
            await asyncio.sleep(args.seconds)  # talk; its last run ends with result {"talkEnded": reason}
            await session.request(
                "DELETE", f"/v1/talk/sessions/{talk['session_id']}", params={"reason": "press"}
            )
            await recorder.stop()
            return 0
    finally:
        await peer.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
