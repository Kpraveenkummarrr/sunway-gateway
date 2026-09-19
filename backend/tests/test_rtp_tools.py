"""G.711 codec helpers and the RTP capture analyser.

These support the audio forensics: a capture of a real call is decoded and its
cadence, loss and comfort-noise behaviour checked. Proven here against packets
whose properties are known exactly, so a clean report on a real capture means
something (and a bad one is not an artefact of the analyser).
"""

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from app.services.rtp_tools import (
    FRAME_SAMPLES,
    PT_CN,
    PT_PCMU,
    build_rtp,
    decode_g711,
    lin2ulaw,
    parse_rtp,
    ulaw2lin,
    ulaw_roundtrip_snr_db,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rtp_forensics  # noqa: E402


def _speechlike(seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(8000 * seconds)) / 8000
    return (9000 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype(np.int16)


# ---- G.711 ----


def test_mu_law_round_trips_within_its_expected_precision() -> None:
    pcm = _speechlike()
    decoded = ulaw2lin(lin2ulaw(pcm))
    assert decoded.shape == pcm.shape
    assert 30 <= ulaw_roundtrip_snr_db(pcm) <= 45  # what G.711 costs on speech-level audio


def test_mu_law_encoding_is_one_byte_per_sample_and_silence_is_ff() -> None:
    encoded = lin2ulaw(np.zeros(160, dtype=np.int16))
    assert len(encoded) == 160 and set(encoded) == {0xFF}


def test_mu_law_survives_full_scale_without_wrapping() -> None:
    loud = np.array([32767, -32768, 32000, -32000], dtype=np.int16)
    decoded = ulaw2lin(lin2ulaw(loud))
    assert np.all(np.sign(decoded) == np.sign(loud))


def test_only_g711_payload_types_are_decoded() -> None:
    assert decode_g711(PT_PCMU, lin2ulaw(_speechlike(0.02))) is not None
    assert decode_g711(PT_CN, b"\x2d") is None  # comfort noise carries no audio
    assert decode_g711(101, b"\x00" * 4) is None  # DTMF


# ---- RTP packets ----


def test_a_built_packet_parses_back_to_the_same_fields() -> None:
    packet = parse_rtp(build_rtp(PT_PCMU, 513, 160_000, 0xDEADBEEF, b"\xff" * 160, marker=True))
    assert packet is not None
    assert (packet.payload_type, packet.sequence, packet.timestamp, packet.ssrc, packet.marker) == (
        PT_PCMU, 513, 160_000, 0xDEADBEEF, True)
    assert len(packet.payload) == 160


def test_things_that_are_not_rtp_are_rejected() -> None:
    assert parse_rtp(b"") is None
    assert parse_rtp(b"INVITE sip:700@127.0.0.1 SIP/2.0\r\n") is None
    assert parse_rtp(bytes([0x40]) + bytes(20)) is None  # version 1


# ---- the capture analyser, on a synthetic pcap ----


def _pcap(path: Path, packets: list[tuple[float, bytes]], *, src="10.0.0.5", dst="10.0.0.9") -> None:
    def frame(payload: bytes) -> bytes:
        udp = struct.pack("!HHHH", 15000, 16000, 8 + len(payload), 0) + payload
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                         bytes(map(int, src.split("."))), bytes(map(int, dst.split("."))))
        return bytes(12) + b"\x08\x00" + ip + udp  # Ethernet header (zero MACs) + IPv4 + UDP

    with path.open("wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))  # classic pcap, Ethernet
        for t, payload in packets:
            data = frame(payload)
            fh.write(struct.pack("<IIII", int(t), int((t % 1) * 1e6), len(data), len(data)))
            fh.write(data)


def _voice_packets(count: int, *, start_seq: int = 100, skip: set[int] = frozenset(), jitter_ms: float = 0.0):
    pcm = _speechlike(count * 0.02)
    out = []
    for i in range(count):
        if i in skip:
            continue
        chunk = pcm[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        t = 1000.0 + i * 0.020 + (jitter_ms / 1000 if i % 2 else 0)
        out.append((t, build_rtp(PT_PCMU, start_seq + i, 5000 + i * FRAME_SAMPLES, 0x11223344, lin2ulaw(chunk))))
    return out


def test_a_clean_stream_reads_as_clean(tmp_path) -> None:
    pcap = tmp_path / "clean.pcap"
    _pcap(pcap, _voice_packets(200))
    report = rtp_forensics.analyse(pcap)
    flow = report["rtp_flows"][0]
    assert flow["sequence"]["lost"] == 0 and flow["sequence"]["duplicates"] == 0
    assert flow["timestamps"]["voice_packets_with_160_sample_step_percent"] == 100.0
    assert flow["voice_payload_sizes"] == {"160": 200} or flow["voice_payload_sizes"] == {160: 200}
    assert flow["pacing_ms"]["inter_arrival_median"] == pytest.approx(20.0, abs=0.5)
    assert flow["comfort_noise_packets_overlapping_voice"] == 0


def test_lost_packets_are_counted_exactly(tmp_path) -> None:
    pcap = tmp_path / "lossy.pcap"
    _pcap(pcap, _voice_packets(200, skip={50, 51, 120}))
    flow = rtp_forensics.analyse(pcap)["rtp_flows"][0]
    assert flow["sequence"]["lost"] == 3
    assert flow["sequence"]["loss_percent"] == pytest.approx(1.5, abs=0.01)


def test_pacing_jitter_shows_up_in_the_report(tmp_path) -> None:
    steady, shaky = tmp_path / "steady.pcap", tmp_path / "shaky.pcap"
    _pcap(steady, _voice_packets(200))
    _pcap(shaky, _voice_packets(200, jitter_ms=12.0))
    j_steady = rtp_forensics.analyse(steady)["rtp_flows"][0]["pacing_ms"]["rfc3550_jitter_max"]
    j_shaky = rtp_forensics.analyse(shaky)["rtp_flows"][0]["pacing_ms"]["rfc3550_jitter_max"]
    assert j_shaky > j_steady + 1.0


def test_comfort_noise_mixed_into_voice_is_flagged(tmp_path) -> None:
    packets = _voice_packets(100)
    packets += [(1000.0 + i * 0.020 + 0.005, build_rtp(PT_CN, 500 + i, 5000 + i * FRAME_SAMPLES, 0x11223344, b"\x2d"))
                for i in range(30)]
    pcap = tmp_path / "mixed.pcap"
    _pcap(pcap, packets)
    flow = rtp_forensics.analyse(pcap)["rtp_flows"][0]
    assert flow["comfort_noise_packets_overlapping_voice"] > 0
    assert set(flow["payload_types"]) == {"0", "13"}


def test_the_decoded_audio_can_be_compared_with_the_file_that_was_meant_to_play(tmp_path) -> None:
    import wave

    pcm = _speechlike(4.0)
    reference = tmp_path / "reply.wav"
    with wave.open(str(reference), "wb") as wf:
        wf.setnchannels(1), wf.setsampwidth(2), wf.setframerate(8000)
        wf.writeframes(pcm.astype("<i2").tobytes())
    pcap = tmp_path / "call.pcap"
    packets = []
    for i in range(200):
        chunk = pcm[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        packets.append((1000.0 + i * 0.020, build_rtp(PT_PCMU, 1 + i, i * FRAME_SAMPLES, 0xABCD, lin2ulaw(chunk))))
    _pcap(pcap, packets)
    flow = rtp_forensics.analyse(pcap, reference_wav=reference, out_dir=tmp_path / "out")["rtp_flows"][0]
    integrity = flow["integrity_vs_reference"]
    # a clean path reads about the same as the unavoidable G.711 cost on this audio
    assert integrity["snr_db"] == pytest.approx(integrity["snr_of_a_clean_g711_round_trip_db"], abs=2.0)
    assert abs(integrity["excess_distortion_db"]) < 2.0
    assert integrity["correlation"] > 0.99
    assert (tmp_path / "out" / flow["decoded_wav"]).exists()
