"""A stand-in for the Synway GSM gateway: one SIP call with a controllable RTP source.

Acceptance testing needs the *same RTP behaviour* the real gateway shows, and
the real gateway is not always reachable. This places a real SIP call into a
real Asterisk (UDP, PCMU, 20 ms packets) and lets a test decide exactly what
the "gateway" sends while the caller is silent:

  frames  continuous low-level PCMU frames - a gateway with silence
          suppression OFF (what Asterisk's silence detector expects)
  cn      RFC 3389 comfort-noise packets (payload type 13) and no voice
          frames - a gateway with VAD/comfort noise ON, which is what the
          client's packet capture shows the SMG4008 doing during silence
  dtx     nothing at all during silence (plain discontinuous transmission)

It also records every RTP packet Asterisk sends back, with arrival times, so a
test can measure what a gateway would actually receive: when the AI's reply
starts, the gaps between reply chunks, when playback stops after an
interruption - and can decode the received PCMU for level/quality analysis.

Development/acceptance tool only. It speaks just enough SIP for an
identify-by-IP endpoint (no authentication, no registration).
"""

from __future__ import annotations

import asyncio
import random
import re
import struct
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.services.rtp_tools import (
    FRAME_SAMPLES,
    PT_CN,
    PT_PCMU,
    build_rtp,
    decode_g711,
    lin2ulaw,
    parse_rtp,
)

FRAME_SECONDS = 0.020


@dataclass(frozen=True)
class RxPacket:
    t: float
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: bool
    payload: bytes


@dataclass
class SimEvent:
    t: float
    name: str
    detail: dict = field(default_factory=dict)


def _parse_sip(data: bytes) -> tuple[str, list[tuple[str, str]], str]:
    text = data.decode("utf-8", errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.append((name.strip().lower(), value.strip()))
    return lines[0], headers, body


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key == name:
            return value
    return None


class _SipProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "SimGsmCaller") -> None:
        self.owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self.owner._on_sip(data, addr)


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "SimGsmCaller") -> None:
        self.owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self.owner._on_rtp(time.monotonic(), data)


class SimGsmCaller:
    def __init__(
        self,
        *,
        target: tuple[str, int] = ("127.0.0.1", 5060),
        extension: str = "700",
        local_ip: str = "127.0.0.1",
        sip_port: int = 5090,
        rtp_port: int = 15000,
        silence_mode: str = "frames",
        cn_interval_ms: int = 200,
        noise_dbfs: float = -62.0,
        offer_cn: bool = True,
    ) -> None:
        if silence_mode not in ("frames", "cn", "dtx"):
            raise ValueError("silence_mode must be frames, cn or dtx")
        self.target = target
        self.extension = extension
        self.local_ip = local_ip
        self.sip_port = sip_port
        self.rtp_port = rtp_port
        self.silence_mode = silence_mode
        self.cn_interval = cn_interval_ms / 1000.0
        self.offer_cn = offer_cn
        self.call_id = f"{uuid.uuid4().hex}@{local_ip}"
        self.local_tag = uuid.uuid4().hex[:10]
        self.remote_tag = ""
        self.remote_contact = f"sip:{target[0]}:{target[1]}"
        self.remote_rtp: tuple[str, int] | None = None
        self.negotiated_payloads: list[str] = []
        self.events: list[SimEvent] = []
        self.rx: list[RxPacket] = []
        self.remote_hangup_at: float | None = None
        self.answered = asyncio.Event()
        self.failed: str | None = None
        self._cseq = 1
        self._sip: _SipProtocol | None = None
        self._rtp: _RtpProtocol | None = None
        self._tx_task: asyncio.Task | None = None
        self._stop = False
        self._ssrc = random.getrandbits(32)
        self._seq = random.getrandbits(16)
        self._ts = random.getrandbits(32)
        self._speech: deque[bytes] = deque()
        self._speech_drained = asyncio.Event()
        self._speech_drained.set()
        self._in_talkspurt = False
        self._last_cn = 0.0
        rng = np.random.default_rng(7)
        noise = rng.normal(0.0, 10 ** (noise_dbfs / 20) * 32768, FRAME_SAMPLES * 50)
        self._noise = [lin2ulaw(noise[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES].astype(np.int16)) for i in range(50)]
        self._noise_i = 0
        self.tx_packets = 0
        self.tx_by_type: dict[int, int] = {}
        # When each speech frame was ACTUALLY sent: a measurement that assumes perfect
        # pacing would blame the worker for any lag in this simulator.
        self.speech_sent_at: list[float] = []

    # ------------------------------------------------------------ helpers
    def mark(self, _event: str, **detail) -> float:
        now = time.monotonic()
        self.events.append(SimEvent(now, _event, detail))
        return now

    def first_event(self, name: str) -> SimEvent | None:
        return next((e for e in self.events if e.name == name), None)

    # ---------------------------------------------------------------- SIP
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sip_transport, self._sip = await loop.create_datagram_endpoint(
            lambda: _SipProtocol(self), local_addr=(self.local_ip, self.sip_port)
        )
        rtp_transport, self._rtp = await loop.create_datagram_endpoint(
            lambda: _RtpProtocol(self), local_addr=(self.local_ip, self.rtp_port)
        )

    def _send_sip(self, message: str) -> None:
        assert self._sip and self._sip.transport
        self._sip.transport.sendto(message.encode("utf-8"), self.target)

    def _via(self) -> str:
        return f"SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{uuid.uuid4().hex[:12]};rport"

    def _sdp(self) -> str:
        payloads = "0 13 101" if self.offer_cn else "0 101"
        lines = [
            "v=0",
            f"o=simgsm 1 1 IN IP4 {self.local_ip}",
            "s=sim-gsm-gateway",
            f"c=IN IP4 {self.local_ip}",
            "t=0 0",
            f"m=audio {self.rtp_port} RTP/AVP {payloads}",
            "a=rtpmap:0 PCMU/8000",
        ]
        if self.offer_cn:
            lines.append("a=rtpmap:13 CN/8000")
        lines += ["a=rtpmap:101 telephone-event/8000", "a=fmtp:101 0-16", "a=ptime:20", "a=sendrecv"]
        return "\r\n".join(lines) + "\r\n"

    async def invite(self, timeout: float = 10.0) -> None:
        sdp = self._sdp()
        msg = (
            f"INVITE sip:{self.extension}@{self.target[0]}:{self.target[1]} SIP/2.0\r\n"
            f"Via: {self._via()}\r\n"
            "Max-Forwards: 70\r\n"
            f'From: "SimGSM" <sip:simgsm@{self.local_ip}>;tag={self.local_tag}\r\n'
            f"To: <sip:{self.extension}@{self.target[0]}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self._cseq} INVITE\r\n"
            f"Contact: <sip:simgsm@{self.local_ip}:{self.sip_port}>\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
        )
        self.mark("invite_sent")
        self._send_sip(msg)
        try:
            await asyncio.wait_for(self.answered.wait(), timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"no answer to INVITE within {timeout}s ({self.failed or 'no response'})") from None
        if self.failed:
            raise RuntimeError(self.failed)
        self._stop = False
        self._tx_task = asyncio.create_task(self._tx_loop())

    def _on_sip(self, data: bytes, addr) -> None:  # noqa: ANN001
        start, headers, body = _parse_sip(data)
        if start.startswith("SIP/2.0"):
            code = int(start.split()[1])
            cseq = _header(headers, "cseq") or ""
            if "INVITE" in cseq:
                if code >= 300:
                    self.failed = f"INVITE rejected: {start}"
                    self.answered.set()
                elif code == 200:
                    to = _header(headers, "to") or ""
                    match = re.search(r"tag=([^;>\s]+)", to)
                    self.remote_tag = match.group(1) if match else ""
                    contact = _header(headers, "contact") or ""
                    uri = re.search(r"<([^>]+)>", contact)
                    if uri:
                        self.remote_contact = uri.group(1)
                    ip = re.search(r"c=IN IP4 (\S+)", body)
                    port = re.search(r"m=audio (\d+)", body)
                    if ip and port:
                        self.remote_rtp = (ip.group(1), int(port.group(1)))
                    self.negotiated_payloads = re.findall(r"a=rtpmap:(\d+) (\S+)", body)
                    self.mark("200_ok", rtp=self.remote_rtp, payloads=self.negotiated_payloads)
                    self._send_ack()
                    self.answered.set()
            return
        method = start.split()[0]
        if method == "BYE":
            self.remote_hangup_at = self.mark("remote_bye")
            via = [f"Via: {v}" for k, v in headers if k == "via"]
            reply = (
                "SIP/2.0 200 OK\r\n" + "\r\n".join(via) + "\r\n"
                f"From: {_header(headers, 'from')}\r\nTo: {_header(headers, 'to')}\r\n"
                f"Call-ID: {_header(headers, 'call-id')}\r\nCSeq: {_header(headers, 'cseq')}\r\n"
                "Content-Length: 0\r\n\r\n"
            )
            assert self._sip and self._sip.transport
            self._sip.transport.sendto(reply.encode(), addr)
            self._stop = True
        elif method in ("OPTIONS", "INFO", "UPDATE", "NOTIFY"):
            via = [f"Via: {v}" for k, v in headers if k == "via"]
            reply = (
                "SIP/2.0 200 OK\r\n" + "\r\n".join(via) + "\r\n"
                f"From: {_header(headers, 'from')}\r\nTo: {_header(headers, 'to')}\r\n"
                f"Call-ID: {_header(headers, 'call-id')}\r\nCSeq: {_header(headers, 'cseq')}\r\n"
                "Content-Length: 0\r\n\r\n"
            )
            assert self._sip and self._sip.transport
            self._sip.transport.sendto(reply.encode(), addr)

    def _dialog_headers(self, method: str) -> str:
        return (
            f"Via: {self._via()}\r\nMax-Forwards: 70\r\n"
            f'From: "SimGSM" <sip:simgsm@{self.local_ip}>;tag={self.local_tag}\r\n'
            f"To: <sip:{self.extension}@{self.target[0]}>;tag={self.remote_tag}\r\n"
            f"Call-ID: {self.call_id}\r\nCSeq: {self._cseq} {method}\r\nContent-Length: 0\r\n\r\n"
        )

    def _send_ack(self) -> None:
        self._send_sip(f"ACK {self.remote_contact} SIP/2.0\r\n" + self._dialog_headers("ACK"))
        self.mark("ack_sent")

    async def bye(self) -> None:
        if self.remote_hangup_at is not None:
            return
        self._cseq += 1
        self._send_sip(f"BYE {self.remote_contact} SIP/2.0\r\n" + self._dialog_headers("BYE"))
        self.mark("bye_sent")
        await asyncio.sleep(0.2)

    async def close(self) -> None:
        self._stop = True
        if self._tx_task:
            self._tx_task.cancel()
            await asyncio.gather(self._tx_task, return_exceptions=True)
        for proto in (self._sip, self._rtp):
            if proto and proto.transport:
                proto.transport.close()

    # ---------------------------------------------------------------- RTP
    def _send_rtp(self, payload_type: int, payload: bytes, *, marker: bool = False) -> None:
        if not (self.remote_rtp and self._rtp and self._rtp.transport):
            return
        packet = build_rtp(payload_type, self._seq, self._ts, self._ssrc, payload, marker=marker)
        self._rtp.transport.sendto(packet, self.remote_rtp)
        self._seq = (self._seq + 1) & 0xFFFF
        self.tx_packets += 1
        self.tx_by_type[payload_type] = self.tx_by_type.get(payload_type, 0) + 1

    async def _tx_loop(self) -> None:
        next_t = time.monotonic()
        while not self._stop:
            now = time.monotonic()
            if next_t > now:
                await asyncio.sleep(next_t - now)
            elif now - next_t > 0.1:
                next_t = now  # fell behind; do not burst to catch up
            next_t += FRAME_SECONDS
            self._tick(time.monotonic())
            self._ts = (self._ts + FRAME_SAMPLES) & 0xFFFFFFFF

    def _tick(self, now: float) -> None:
        if self._speech:
            frame = self._speech.popleft()
            first = not self._in_talkspurt
            if first:
                self.mark("speech_frame_first")
            self._in_talkspurt = True
            self.speech_sent_at.append(now)
            self._send_rtp(PT_PCMU, frame, marker=first)
            if not self._speech:
                self.mark("speech_frame_last")
                self._speech_drained.set()
            return
        self._in_talkspurt = False
        if self.silence_mode == "frames":
            self._send_rtp(PT_PCMU, self._noise[self._noise_i % len(self._noise)])
            self._noise_i += 1
        elif self.silence_mode == "cn" and now - self._last_cn >= self.cn_interval:
            self._last_cn = now
            self._send_rtp(PT_CN, bytes([45]))  # RFC 3389 noise level: -45 dBov

    def _on_rtp(self, t: float, data: bytes) -> None:
        packet = parse_rtp(data)
        if packet is None:
            return
        self.rx.append(RxPacket(t, packet.payload_type, packet.sequence, packet.timestamp, packet.ssrc, packet.marker, packet.payload))

    # ------------------------------------------------------------ actions
    async def speak(self, pcm8k: np.ndarray, *, wait: bool = True) -> None:
        """Sends `pcm8k` (int16, 8 kHz) as caller speech."""
        pcm = np.asarray(pcm8k, dtype=np.int16)
        pad = (-len(pcm)) % FRAME_SAMPLES
        if pad:
            pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
        encoded = lin2ulaw(pcm)
        self._speech_drained.clear()
        for i in range(0, len(encoded), FRAME_SAMPLES):
            self._speech.append(encoded[i : i + FRAME_SAMPLES])
        if wait:
            await self._speech_drained.wait()

    def stop_speaking(self) -> None:
        self._speech.clear()
        self._speech_drained.set()

    # ----------------------------------------------------------- analysis
    def audio_bursts(self, min_gap: float = 0.30) -> list[dict]:
        """Groups received voice packets into bursts separated by >= min_gap
        seconds of nothing: each burst is one continuous piece of playback."""
        voice = [p for p in self.rx if p.payload_type in (0, 8)]
        bursts: list[dict] = []
        for p in voice:
            if bursts and p.t - bursts[-1]["end"] < min_gap:
                bursts[-1]["end"] = p.t
                bursts[-1]["packets"] += 1
            else:
                bursts.append({"start": p.t, "end": p.t, "packets": 1})
        return bursts

    def rx_audio(self, start: float | None = None, end: float | None = None) -> np.ndarray:
        """Decoded 8 kHz audio of the received voice packets in a time window."""
        chunks = []
        for p in self.rx:
            if start is not None and p.t < start:
                continue
            if end is not None and p.t > end:
                continue
            samples = decode_g711(p.payload_type, p.payload)
            if samples is not None:
                chunks.append(samples)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)

    def save_rx_wav(self, path: Path, start: float | None = None, end: float | None = None) -> None:
        samples = self.rx_audio(start, end)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(samples.astype("<i2").tobytes())


def load_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        raw = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
        if wf.getnchannels() > 1:
            raw = raw.reshape(-1, wf.getnchannels()).mean(axis=1).astype(np.int16)
    return raw, rate
