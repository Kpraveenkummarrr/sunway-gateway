"""RTP packet parsing and G.711 (PCMU/PCMA) conversion, in pure numpy.

Used by the offline RTP forensics tool (scripts/rtp_forensics.py) and by the
Synway/GSM gateway simulator used in acceptance testing
(scripts/sim_gsm_caller.py). Deliberately dependency-free: Python 3.13
removed the `audioop` module this would otherwise use, and a forensic tool
that stops working on a newer interpreter is not much of a tool.

G.711 is an 8-bit logarithmic companding of 13/14-bit linear audio. It is
lossy by design (roughly 38 dB signal-to-noise on speech), so an audio path
that is "clean" still shows a small, predictable difference after a PCMU
round trip — `ulaw_roundtrip_snr_db()` measures exactly that.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

PT_PCMU = 0
PT_PCMA = 8
PT_CN = 13  # RFC 3389 comfort noise
RTP_CLOCK_HZ = 8000
FRAME_SAMPLES = 160  # 20 ms at 8 kHz

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def lin2ulaw(pcm16: np.ndarray) -> bytes:
    """16-bit linear PCM -> G.711 mu-law bytes."""
    x = np.asarray(pcm16, dtype=np.int32)
    sign = (x >> 8) & 0x80
    x = np.where(sign != 0, -x, x)
    x = np.minimum(x, _ULAW_CLIP) + _ULAW_BIAS
    exponent = np.floor(np.log2(x)).astype(np.int32) - 7
    mantissa = (x >> (exponent + 3)) & 0x0F
    return ((~(sign | (exponent << 4) | mantissa)) & 0xFF).astype(np.uint8).tobytes()


def ulaw2lin(data: bytes) -> np.ndarray:
    """G.711 mu-law bytes -> 16-bit linear PCM."""
    u = (~np.frombuffer(data, dtype=np.uint8)).astype(np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + _ULAW_BIAS) << exponent) - _ULAW_BIAS
    return np.where(sign != 0, -sample, sample).astype(np.int16)


def alaw2lin(data: bytes) -> np.ndarray:
    """G.711 A-law bytes -> 16-bit linear PCM (decode only)."""
    a = np.frombuffer(data, dtype=np.uint8).astype(np.int32) ^ 0x55
    segment = (a & 0x70) >> 4
    t = (a & 0x0F) << 4
    t = np.where(segment == 0, t + 8, t + 0x108)
    t = np.where(segment > 1, t << np.maximum(segment - 1, 0), t)
    return np.where((a & 0x80) != 0, t, -t).astype(np.int16)


def decode_g711(payload_type: int, payload: bytes) -> np.ndarray | None:
    if payload_type == PT_PCMU:
        return ulaw2lin(payload)
    if payload_type == PT_PCMA:
        return alaw2lin(payload)
    return None


def ulaw_roundtrip_snr_db(pcm16: np.ndarray) -> float:
    """Signal-to-noise ratio (dB) of encoding to PCMU and back. This is the
    unavoidable cost of G.711 on this exact audio — anything worse than it in
    a real capture was added somewhere else."""
    original = np.asarray(pcm16, dtype=np.float64)
    decoded = ulaw2lin(lin2ulaw(pcm16)).astype(np.float64)
    noise = original - decoded
    signal_power = float(np.mean(original**2))
    noise_power = float(np.mean(noise**2))
    if noise_power <= 0 or signal_power <= 0:
        return float("inf")
    return 10.0 * float(np.log10(signal_power / noise_power))


@dataclass(frozen=True)
class RtpPacket:
    payload_type: int
    marker: bool
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse_rtp(data: bytes) -> RtpPacket | None:
    """Parses an RTP datagram; None if it is not a valid RTP v2 packet."""
    if len(data) < 12:
        return None
    b0, b1, sequence, timestamp, ssrc = struct.unpack("!BBHII", data[:12])
    if (b0 >> 6) != 2:
        return None
    header_len = 12 + 4 * (b0 & 0x0F)
    if b0 & 0x10:  # header extension
        if len(data) < header_len + 4:
            return None
        ext_words = struct.unpack("!H", data[header_len + 2 : header_len + 4])[0]
        header_len += 4 + 4 * ext_words
    payload = data[header_len:]
    if b0 & 0x20 and payload:  # padding
        pad = payload[-1]
        payload = payload[: len(payload) - pad] if 0 < pad <= len(payload) else payload
    return RtpPacket(
        payload_type=b1 & 0x7F,
        marker=bool(b1 & 0x80),
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=payload,
    )


def build_rtp(payload_type: int, sequence: int, timestamp: int, ssrc: int, payload: bytes, *, marker: bool = False) -> bytes:
    b1 = (0x80 if marker else 0x00) | (payload_type & 0x7F)
    return struct.pack("!BBHII", 0x80, b1, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF) + payload
