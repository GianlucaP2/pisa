"""The media router: one WebRTC peer per conversation between the phone and Pisa's server, with
aiortc (BSD-3, the WebRTC stack under Pipecat's own single-peer transport). The phone sends its microphone as
Opus with its voice processing on (echo cancellation: Pisa never hears itself); Pisa's voice goes back on the
same peer; one data channel, "pisa", carries JSON both ways (protocol/README.md "CONV-1").

How the channel speaks is its dialect, chosen by the subprotocol the phone opens it with (`pisa.v2`): the
talk's own messages go out through it and the phone's frames come in through it. A channel opened without a
subprotocol this server speaks is refused: it is closed at once and nothing goes out on it.

The talker's audio is 16-bit PCM mono at its own rate; this module resamples the phone's audio to it and paces
Pisa's voice out in 20 ms frames (aiortc's Opus encoder resamples to 48 kHz). Nothing here logs audio or
words.

Self-hosted reach (`VoiceNet`): the media can take its ports from a fixed UDP range
(`PISA_VOICE_UDP_PORTS`) and offer one address the phone can reach (`PISA_ICE_HOST`, or the IPv4 address in
`PISA_ICE_HOST_FILE`, which the Tailscale sidecar writes) as its host candidate, like a 1:1 NAT mapping: the
compose file publishes (or, on Tailscale, relays: `pisa.voice_relay`) that range to the API. aioice
(aiortc's ICE) binds port 0 on every interface and has no such options, so the peer's ICE connections gather
through `RangedConnection`. A peer whose media never connects within `PISA_VOICE_CONNECT_TIMEOUT_S` is
`unreachable`: its talk ends `fallback`, and the phone talks over /v2/live.
"""

import asyncio
import fractions
import ipaddress
import json
import random
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import av
from aioice import Candidate, Connection
from aioice.candidate import candidate_foundation, candidate_priority
from aioice.ice import StunProtocol, TransportPolicy, server_reflexive_candidate
from aioice.turn import UDP_SOCKET_BUFFER_SIZE
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError

from pisa.logging import get_logger

PTIME_MS = 20  # one frame of Pisa's voice
CHANNEL = "pisa"  # the data channel the phone opens
_log = get_logger("pisa.talk.media")


class MediaUnreachable(Exception):
    """The peer has no way in for the phone (no ICE candidate: the UDP port range is taken)."""


class VoiceTrack(MediaStreamTrack):
    """Pisa's voice to the phone: the talker's PCM as it arrives, silence between, one frame every 20 ms."""

    kind = "audio"

    def __init__(self, rate: int) -> None:
        super().__init__()
        self._rate = rate
        self._buffer = bytearray()
        self._start: float | None = None
        self._sent = 0  # samples
        self._until = 0.0  # the playout clock: when what was pushed has been heard (monotonic)

    def push(self, pcm: bytes) -> None:
        self._buffer += pcm
        self._until = max(self._until, time.monotonic()) + len(pcm) / 2 / self._rate

    def clear(self) -> None:
        """Barge-in: what Pisa was about to say is dropped at once."""
        self._buffer.clear()
        self._until = 0.0

    @property
    def buffered_ms(self) -> int:
        """What is still to be heard, by the playout clock (2: never by the buffer,
        which a vanished phone never drains)."""
        return max(0, int((self._until - time.monotonic()) * 1000))

    @property
    def playing(self) -> bool:
        return self.buffered_ms > 0

    async def recv(self) -> av.AudioFrame:
        samples = self._rate * PTIME_MS // 1000
        if self._start is None:
            self._start = time.monotonic()
        else:
            wait = self._start + self._sent / self._rate - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        size = samples * 2
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(chunk.ljust(size, b"\0"))
        frame.sample_rate = self._rate
        frame.pts = self._sent
        frame.time_base = fractions.Fraction(1, self._rate)
        self._sent += samples
        return frame


class ChannelDialect(Protocol):
    """How the "pisa" data channel speaks: the talk's messages out as frames, the phone's frames in as the
    talk's messages. A dialect may also answer a frame itself (a pong, an error) through its sink."""

    def attach(self, sink: Callable[[str], None]) -> None:
        """Frames, as text, go to `sink` from now on."""
        ...

    def out(self, message: dict[str, Any]) -> None:
        """One of the talk's messages, written as the dialect's frames."""
        ...

    def into(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """One frame from the phone → the talk's messages it carries (none for a frame the dialect answers or
        drops)."""
        ...


class RefusedChannel:
    """A channel opened without a subprotocol this server speaks: nothing goes out, nothing comes in."""

    def attach(self, sink: Callable[[str], None]) -> None:
        pass

    def out(self, message: dict[str, Any]) -> None:
        pass

    def into(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return []


# the dialect for the subprotocol the phone opened the channel with (None: none this server speaks)
Dialects = Callable[[str], ChannelDialect | None]


@dataclass(frozen=True)
class VoiceNet:
    """Where the talk's media listens and what it offers the phone (the settings, `voice_net`)."""

    ports: range | None = None  # None: any free port
    host: str = ""  # the address advertised as the host candidate; "": each interface's own
    connect_timeout_s: float = 15.0


def voice_net(settings: Any) -> VoiceNet:
    """`PISA_VOICE_UDP_PORTS`, `PISA_ICE_HOST` (else the address in `PISA_ICE_HOST_FILE`, read now: called at
    each talk) and `PISA_VOICE_CONNECT_TIMEOUT_S`, from the settings."""
    host = settings.ice_host.strip() or host_from_file(settings.ice_host_file.strip())
    return VoiceNet(settings.voice_ports, host, settings.voice_connect_timeout_s)


def host_from_file(path: str) -> str:
    """The IPv4 address in the file at `path` (the tailscale sidecar's `tailscale ip -4`); "" when there's no
    path, no file yet (the node isn't logged in) or the file holds anything else, which is logged at debug."""
    if not path:
        return ""
    try:
        with open(path, "rb") as file:
            text = file.read(64).decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("talk_ice_host_file_unreadable", reason=type(exc).__name__)
        return ""
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        _log.debug("talk_ice_host_file_invalid")
        return ""


async def resolve_host(host: str) -> str | None:
    """The advertised host as an IP (a name is resolved now, IPv4 first); None when it can't be."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_DGRAM)
    except OSError:
        return None
    ips = [str(info[4][0]) for info in infos]
    return next((ip for ip in ips if ":" not in ip), ips[0] if ips else None)


async def bind_in(
    factory: Callable[[], asyncio.DatagramProtocol], address: str, ports: range | None
) -> tuple[asyncio.DatagramTransport, Any] | None:
    """A UDP socket on `address` at a free port of `ports` (any port when None), tried from a random start so
    concurrent talks spread over the range; None when every port is taken."""
    loop = asyncio.get_running_loop()
    if ports is None:
        order: list[int] = [0]
    else:
        start = random.randrange(len(ports))  # noqa: S311 (spreading, not a secret)
        order = [*ports[start:], *ports[:start]]
    for port in order:
        try:
            transport, protocol = await loop.create_datagram_endpoint(factory, local_addr=(address, port))
        except OSError:
            continue
        return transport, protocol
    return None


class RangedConnection(Connection):
    """aioice's ICE connection with the host candidates on `net.ports`, offered at `net.host` when set (the
    sockets then listen on every interface: the address the phone reaches is mapped to them outside, by a
    published port or the Tailscale relay). Server-reflexive (STUN) and relayed (TURN) candidates are kept."""

    net: VoiceNet = VoiceNet()

    async def get_component_candidates(
        self,
        component: int,
        addresses: list[str],
        timeout: int = 5,  # noqa: ASYNC109 (aioice's signature)
    ) -> list[Candidate]:
        net = self.net
        if net.ports is None and not net.host:
            return await super().get_component_candidates(component, addresses, timeout)
        advertised = await resolve_host(net.host) if net.host else None
        if net.host and advertised is None:
            _log.warning("talk_ice_host_unresolved")  # the setting's name only: the host can be the user's
        if advertised is not None:
            addresses = ["::" if ":" in advertised else "0.0.0.0"]  # noqa: S104 (on purpose: see the class)
        candidates: list[Candidate] = []
        host_protocols: list[StunProtocol] = []
        for address in addresses:
            bound = await bind_in(lambda: StunProtocol(self), address, net.ports)
            if bound is None:
                _log.warning("talk_udp_ports_exhausted", ports=f"{net.ports}")
                continue
            transport, protocol = bound
            sock = transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_SOCKET_BUFFER_SIZE)
            name = transport.get_extra_info("sockname")
            host = advertised or name[0]
            protocol.local_candidate = Candidate(
                foundation=candidate_foundation("host", "udp", host), component=component, transport="udp",
                priority=candidate_priority(component, "host"), host=host, port=name[1], type="host",
            )  # fmt: skip
            host_protocols.append(protocol)
            if self._transport_policy == TransportPolicy.ALL:
                candidates.append(protocol.local_candidate)
        self._protocols += host_protocols
        # the TURN candidate (and nothing else: no addresses) from aioice's own gathering
        candidates += await super().get_component_candidates(component, [], timeout)
        if self.stun_server and advertised is None:  # an advertised host already is the reachable one
            stun_server = self.stun_server
            found = await asyncio.gather(
                *(server_reflexive_candidate(p, stun_server) for p in host_protocols
                  if ipaddress.ip_address(name_of(p)).version == 4),
                return_exceptions=True,
            )  # fmt: skip
            candidates += [one[0] for one in found if not isinstance(one, BaseException)]
        return candidates


def name_of(protocol: StunProtocol) -> str:
    candidate = protocol.local_candidate
    return candidate.host if candidate is not None else "::"


def _ranged(pc: RTCPeerConnection, net: VoiceNet) -> None:
    """Every ICE transport of the peer gathers through `RangedConnection` (before its gathering: called
    between the remote description and the local one)."""
    transports = [t.receiver.transport for t in pc.getTransceivers()]
    if pc.sctp is not None:
        transports.append(pc.sctp.transport)
    for dtls in transports:
        connection = dtls.transport.iceGatherer._connection
        if type(connection) is Connection:
            connection.__class__ = RangedConnection
            connection.net = net  # type: ignore[attr-defined]


def ice_servers(config: str) -> list[dict[str, Any]]:
    """The ICE servers setting (`PISA_TALK_ICE_SERVERS`, a JSON list); [] when unset or unreadable."""
    try:
        servers = json.loads(config or "[]")
    except ValueError:
        _log.warning("talk_ice_servers_unreadable")
        return []
    return [s for s in servers if isinstance(s, dict) and s.get("urls")] if isinstance(servers, list) else []


class MediaPeer:
    """One phone's conversation: its microphone in, Pisa's voice out, the "pisa" data channel."""

    def __init__(
        self, servers: list[dict[str, Any]], output_rate: int, dialects: Dialects, net: VoiceNet | None = None
    ) -> None:
        self._net = net or VoiceNet()
        self._connected = False  # the media has connected at least once
        self.unreachable = False  # it never did: failed, or not within the connect timeout
        self._watch: asyncio.Task[None] | None = None
        config = RTCConfiguration(
            iceServers=[
                RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
                for s in servers
            ]
        )
        self._pc = RTCPeerConnection(configuration=config)
        self._voice = VoiceTrack(output_rate)
        self._pc.addTrack(self._voice)
        self._mic: asyncio.Future[MediaStreamTrack] = asyncio.get_running_loop().create_future()
        self._channel: RTCDataChannel | None = None
        self._dialects = dialects
        self._dialect: ChannelDialect | None = None  # known once the phone opens the channel
        self._pending: list[dict[str, Any]] = []  # the talk's messages before that
        self._outbox: list[str] = []  # frames before the channel is open
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.on_closed: Callable[[], None] | None = None

        @self._pc.on("track")
        def _track(track: MediaStreamTrack) -> None:
            if track.kind == "audio" and not self._mic.done():
                self._mic.set_result(track)

        @self._pc.on("datachannel")
        def _datachannel(channel: RTCDataChannel) -> None:
            if channel.label != CHANNEL or self._channel is not None:
                return
            self._channel = channel
            self._speak(str(channel.protocol or ""))

            @channel.on("open")
            def _open() -> None:
                self._flush()

            @channel.on("message")
            def _message(message: object) -> None:
                try:
                    body = json.loads(message) if isinstance(message, str) else None
                except ValueError:
                    body = None
                if isinstance(body, dict) and self._dialect is not None:
                    for one in self._dialect.into(body):
                        self.messages.put_nowait(one)

            if channel.readyState == "open":
                self._flush()

        @self._pc.on("connectionstatechange")
        async def _state() -> None:
            # the phone vanished (app killed, network lost): what was buffered
            # is dropped and the run ends (TalkService wires `on_closed`), so nothing is metered for nobody.
            if self._pc.connectionState == "connected":
                self._connected = True
            if self._pc.connectionState in ("failed", "closed"):
                self.unreachable = not self._connected
                self._voice.clear()
                if self.on_closed is not None:
                    self.on_closed()

    async def answer(self, offer_sdp: str) -> str:
        """The phone's offer in, Pisa's answer out (ICE gathered: the answer carries every candidate)."""
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        _ranged(self._pc, self._net)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        sdp = self._pc.localDescription.sdp
        if "a=candidate:" not in sdp:  # no port free, nothing bound: the phone could never reach this peer
            raise MediaUnreachable("no ICE candidate")
        self._watch = asyncio.create_task(self._connect_within(self._net.connect_timeout_s))
        return sdp

    async def _connect_within(self, seconds: float) -> None:
        """The media connects within `seconds` of the answer, or the peer is unreachable and closes (its
        talk ends `fallback`: voice unavailable, the phone goes on over /v2/live)."""
        await asyncio.sleep(seconds)
        if not self._connected and self._pc.connectionState != "closed":
            _log.info("talk_media_unreachable", after_s=seconds, ice=self._pc.iceConnectionState)
            self.unreachable = True
            await self._pc.close()

    async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
        """The user's voice as 16-bit PCM mono at `rate`, as it arrives."""
        track = await self._mic
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            for out in resampler.resample(frame):  # type: ignore[arg-type]
                yield bytes(out.planes[0])[: out.samples * 2]

    def play(self, pcm: bytes) -> None:
        self._voice.push(pcm)

    def clear(self) -> None:
        self._voice.clear()

    @property
    def playing(self) -> bool:
        return self._voice.playing

    @property
    def buffered_ms(self) -> int:
        return self._voice.buffered_ms

    def _speak(self, subprotocol: str) -> None:
        """The channel's dialect, by its subprotocol; the talk's messages kept until now go out in it. A
        subprotocol this server doesn't speak (none: a v1 phone) refuses the channel: it closes."""
        chosen = self._dialects(subprotocol)
        if chosen is None:
            _log.warning("talk_channel_refused", subprotocol=subprotocol[:40] or "none")
            self._dialect = RefusedChannel()
            self._pending = []
            if self._channel is not None:
                self._channel.close()
            return
        self._dialect = chosen
        self._dialect.attach(self._frame)
        pending, self._pending = self._pending, []
        for message in pending:
            self._dialect.out(message)

    def send(self, message: dict[str, Any]) -> None:
        if self._dialect is None:
            self._pending.append(message)
        else:
            self._dialect.out(message)

    def _frame(self, text: str) -> None:
        self._outbox.append(text)
        self._flush()

    def _flush(self) -> None:
        channel = self._channel
        if channel is None or channel.readyState != "open":
            return
        while self._outbox:
            channel.send(self._outbox.pop(0))

    async def close(self) -> None:
        if self._watch is not None and self._watch is not asyncio.current_task():
            self._watch.cancel()
        await self._pc.close()
