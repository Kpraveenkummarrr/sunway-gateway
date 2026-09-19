"""Forensic analysis of an RTP capture: is the audio on the wire what we meant to send?

Capture ONE call, then analyse it:

    sudo tcpdump -i any -n -s 0 -w /tmp/call.pcap udp and host 192.168.0.106
    python scripts/rtp_forensics.py /tmp/call.pcap --out /tmp/call-forensics \
        --reference /var/spool/asterisk/sounds/ai-agent/<the reply>.wav

For every RTP flow (each direction, each SSRC) it reports:

  payload types    what was sent and when (PT 0 = PCMU voice, 13 = comfort
                   noise, 101 = DTMF) - and whether comfort noise ever overlaps
                   voice, which it should not
  sequence         packets expected vs received: loss, duplicates, reordering
  timestamps       whether the 20 ms / 160-sample cadence holds, and jumps
  pacing           inter-arrival spread and the RFC 3550 jitter estimate
  payload          sizes (160 bytes = 20 ms), and the decoded audio's level,
                   clipping and DC offset
  integrity        with --reference: the decoded audio aligned to the file that
                   was meant to be played, and the signal-to-noise ratio between
                   them, next to the SNR G.711 costs on that same audio - so a
                   clean path reads ~equal and anything worse was added between
                   Asterisk and the wire

The decoded audio of each flow is written as a WAV so it can be listened to.
Do not read packet size as audio quality: 160-byte packets say the cadence is
right, not that the samples are.

Reads classic pcap files (what tcpdump writes by default) with Ethernet,
Linux-cooked (-i any) or raw-IP link layers, IPv4 over UDP.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import wave
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.rtp_tools import (  # noqa: E402
    PT_CN,
    PT_PCMA,
    PT_PCMU,
    decode_g711,
    lin2ulaw,
    parse_rtp,
    ulaw2lin,
)

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LINUX_SLL = 113


@dataclass(frozen=True)
class UdpDatagram:
    t: float
    src: str
    sport: int
    dst: str
    dport: int
    payload: bytes


def read_pcap(path: Path) -> tuple[int, list[tuple[float, bytes]]]:
    data = Path(path).read_bytes()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian, nanos = "<", magic == b"\x4d\x3c\xb2\xa1"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian, nanos = ">", magic == b"\xa1\xb2\x3c\x4d"
    else:
        raise ValueError("not a classic pcap file (pcapng is not supported: capture with tcpdump -w)")
    _, _, _, _, _, linktype = struct.unpack(endian + "HHiIII", data[4:24])
    frames: list[tuple[float, bytes]] = []
    offset = 24
    while offset + 16 <= len(data):
        sec, frac, caplen, _ = struct.unpack(endian + "IIII", data[offset : offset + 16])
        offset += 16
        frames.append((sec + frac / (1e9 if nanos else 1e6), data[offset : offset + caplen]))
        offset += caplen
    return linktype, frames


def _ip_payload(linktype: int, frame: bytes) -> bytes | None:
    if linktype == LINKTYPE_ETHERNET:
        if len(frame) < 14:
            return None
        ethertype = struct.unpack("!H", frame[12:14])[0]
        offset = 14
        if ethertype == 0x8100 and len(frame) >= 18:  # 802.1Q
            ethertype = struct.unpack("!H", frame[16:18])[0]
            offset = 18
        return frame[offset:] if ethertype == 0x0800 else None
    if linktype == LINKTYPE_LINUX_SLL:
        if len(frame) < 16:
            return None
        return frame[16:] if struct.unpack("!H", frame[14:16])[0] == 0x0800 else None
    if linktype == LINKTYPE_RAW:
        return frame
    if linktype == LINKTYPE_NULL:
        return frame[4:]
    return None


def udp_datagrams(linktype: int, frames: list[tuple[float, bytes]]) -> list[UdpDatagram]:
    out: list[UdpDatagram] = []
    for t, frame in frames:
        ip = _ip_payload(linktype, frame)
        if not ip or len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 17:
            continue
        header = (ip[0] & 0x0F) * 4
        udp = ip[header:]
        if len(udp) < 8:
            continue
        sport, dport, length = struct.unpack("!HHH", udp[:6])
        out.append(
            UdpDatagram(
                t,
                ".".join(map(str, ip[12:16])),
                sport,
                ".".join(map(str, ip[16:20])),
                dport,
                udp[8 : 8 + max(0, length - 8)],
            )
        )
    return out


def _percentile(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def _levels(samples: np.ndarray) -> dict:
    if len(samples) == 0:
        return {}
    x = samples.astype(np.float64) / 32768.0
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x**2)))
    frame = 160
    frames = np.abs(x[: len(x) // frame * frame]).reshape(-1, frame).mean(axis=1) if len(x) >= frame else np.array([0.0])
    to_db = lambda v: round(20 * float(np.log10(v)), 1) if v > 1e-9 else -120.0  # noqa: E731
    return {
        "duration_s": round(len(x) / 8000, 2),
        "peak_dbfs": to_db(peak),
        "rms_dbfs": to_db(rms),
        "noise_floor_dbfs": to_db(float(np.percentile(frames, 10))),
        "clipped_percent": round(float(np.mean(np.abs(x) >= 0.999)) * 100, 4),
        "dc_offset_percent_fs": round(float(np.mean(x)) * 100, 4),
    }


def _align_and_snr(decoded: np.ndarray, reference: np.ndarray) -> dict:
    """Aligns `decoded` to `reference` (8 kHz int16) and reports SNR and
    correlation, plus the SNR a clean PCMU round trip of the reference gives."""
    ref = reference.astype(np.float64)
    dec = decoded.astype(np.float64)
    if len(ref) < 800 or len(dec) < 800:
        return {"error": "too little audio to compare"}
    # coarse alignment on the 20 ms energy envelope, then refine on samples
    env = lambda v: np.abs(v[: len(v) // 160 * 160]).reshape(-1, 160).mean(axis=1)  # noqa: E731
    e_ref, e_dec = env(ref), env(dec)
    if len(e_dec) >= len(e_ref):
        scores = np.correlate(e_dec - e_dec.mean(), e_ref - e_ref.mean(), mode="valid")
        coarse = int(np.argmax(scores)) * 160
    else:
        coarse = 0
    lo, hi = max(0, coarse - 320), coarse + 320
    best, best_score = coarse, -np.inf
    segment = ref[: min(len(ref), 16000)]
    for shift in range(lo, hi + 1, 4):
        window = dec[shift : shift + len(segment)]
        if len(window) < len(segment):
            break
        score = float(np.dot(window, segment))
        if score > best_score:
            best, best_score = shift, score
    for shift in range(max(0, best - 4), best + 5):
        window = dec[shift : shift + len(segment)]
        if len(window) < len(segment):
            break
        score = float(np.dot(window, segment))
        if score > best_score:
            best, best_score = shift, score
    aligned = dec[best : best + len(ref)]
    n = min(len(aligned), len(ref))
    aligned, ref = aligned[:n], ref[:n]
    noise = aligned - ref
    signal_power = float(np.mean(ref**2))
    snr = 10 * np.log10(signal_power / max(float(np.mean(noise**2)), 1e-9))
    clean = ulaw2lin(lin2ulaw(reference[:n])).astype(np.float64)
    clean_snr = 10 * np.log10(signal_power / max(float(np.mean((clean - ref) ** 2)), 1e-9))
    corr = float(np.corrcoef(aligned, ref)[0, 1]) if n > 1 else 0.0
    return {
        "alignment_offset_samples": int(best),
        "compared_seconds": round(n / 8000, 2),
        "correlation": round(corr, 4),
        "snr_db": round(float(snr), 1),
        "snr_of_a_clean_g711_round_trip_db": round(float(clean_snr), 1),
        "excess_distortion_db": round(float(clean_snr - snr), 1),
    }


def analyse_flow(key, packets: list[tuple[float, object]], reference: np.ndarray | None, out_dir: Path | None) -> dict:
    src, sport, dst, dport, ssrc = key
    times = [t for t, _ in packets]
    rtp = [p for _, p in packets]
    pt_hist: dict[int, int] = defaultdict(int)
    pt_first: dict[int, float] = {}
    pt_last: dict[int, float] = {}
    for t, p in packets:
        pt_hist[p.payload_type] += 1
        pt_first.setdefault(p.payload_type, t - times[0])
        pt_last[p.payload_type] = t - times[0]

    # sequence (extended for wrap-around)
    extended, wraps, previous = [], 0, None
    for p in rtp:
        if previous is not None and p.sequence < previous and previous - p.sequence > 30000:
            wraps += 1
        extended.append(p.sequence + 65536 * wraps)
        previous = p.sequence
    expected = (max(extended) - min(extended) + 1) if extended else 0
    unique = len(set(extended))
    duplicates = len(extended) - unique
    reordered = sum(1 for a, b in zip(extended, extended[1:]) if b < a)

    voice = [(t, p) for t, p in packets if p.payload_type in (PT_PCMU, PT_PCMA)]
    ts_deltas, jitter, j = [], [], 0.0
    prev_t, prev_ts = None, None
    for t, p in voice:
        if prev_ts is not None:
            dts = (p.timestamp - prev_ts) & 0xFFFFFFFF
            ts_deltas.append(dts)
            d = ((t - prev_t) * 8000) - dts
            j += (abs(d) - j) / 16.0
            jitter.append(j / 8.0)  # ms
        prev_t, prev_ts = t, p.timestamp
    arrivals = [(b[0] - a[0]) * 1000 for a, b in zip(voice, voice[1:])]
    normal = [d for d in ts_deltas if d == 160]
    sizes = defaultdict(int)
    for _, p in voice:
        sizes[len(p.payload)] += 1

    # Comfort noise must not overlap voice.
    voice_times = np.array([t for t, _ in voice]) if voice else np.array([])
    cn_overlapping_voice = 0
    for t, p in packets:
        if p.payload_type == PT_CN and len(voice_times) and np.any(np.abs(voice_times - t) < 0.030):
            cn_overlapping_voice += 1

    decoded_parts = [decode_g711(p.payload_type, p.payload) for _, p in voice]
    decoded = np.concatenate([d for d in decoded_parts if d is not None]) if decoded_parts else np.zeros(0, dtype=np.int16)

    report = {
        "flow": f"{src}:{sport} -> {dst}:{dport} ssrc={ssrc:#010x}",
        "packets": len(packets),
        "duration_s": round(times[-1] - times[0], 2),
        "payload_types": {
            str(pt): {"packets": n, "first_s": round(pt_first[pt], 2), "last_s": round(pt_last[pt], 2)}
            for pt, n in sorted(pt_hist.items())
        },
        "sequence": {
            "expected": expected,
            "received": unique,
            "lost": max(0, expected - unique),
            "loss_percent": round(100 * max(0, expected - unique) / expected, 3) if expected else 0.0,
            "duplicates": duplicates,
            "reordered": reordered,
        },
        "timestamps": {
            "voice_packets_with_160_sample_step_percent": round(100 * len(normal) / len(ts_deltas), 1) if ts_deltas else None,
            "other_steps": sorted({d for d in ts_deltas if d != 160})[:8],
        },
        "pacing_ms": {
            "inter_arrival_median": round(float(np.median(arrivals)), 2) if arrivals else None,
            "inter_arrival_p95": round(_percentile(arrivals, 95), 2) if arrivals else None,
            "inter_arrival_max": round(max(arrivals), 1) if arrivals else None,
            "rfc3550_jitter_final": round(jitter[-1], 2) if jitter else None,
            "rfc3550_jitter_max": round(max(jitter), 2) if jitter else None,
        },
        "voice_payload_sizes": dict(sorted(sizes.items())),
        "comfort_noise_packets_overlapping_voice": cn_overlapping_voice,
        "decoded_audio": _levels(decoded),
    }
    if reference is not None and len(decoded):
        report["integrity_vs_reference"] = _align_and_snr(decoded, reference)
    if out_dir is not None and len(decoded):
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"flow_{src}_{sport}_to_{dst}_{dport}_{ssrc:08x}.wav".replace(".", "-")
        with wave.open(str(out_dir / name), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(decoded.astype("<i2").tobytes())
        report["decoded_wav"] = name
    return report


def analyse(pcap: Path, *, reference_wav: Path | None = None, out_dir: Path | None = None, min_packets: int = 20) -> dict:
    linktype, frames = read_pcap(pcap)
    datagrams = udp_datagrams(linktype, frames)
    flows: dict[tuple, list] = defaultdict(list)
    for d in datagrams:
        packet = parse_rtp(d.payload)
        # Real RTP: version 2 and a payload type we can name; SIP/RTCP fail this.
        if packet is None or packet.payload_type > 34 and packet.payload_type not in (96, 101):
            continue
        flows[(d.src, d.sport, d.dst, d.dport, packet.ssrc)].append((d.t, packet))
    reference = None
    if reference_wav is not None:
        with wave.open(str(reference_wav), "rb") as wf:
            if wf.getframerate() != 8000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise SystemExit("--reference must be 8 kHz mono 16-bit (the file Asterisk was asked to play)")
            reference = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    reports = [
        analyse_flow(key, sorted(packets, key=lambda x: x[0]), reference, out_dir)
        for key, packets in sorted(flows.items(), key=lambda kv: kv[1][0][0])
        if len(packets) >= min_packets
    ]
    return {"pcap": str(pcap), "udp_datagrams": len(datagrams), "rtp_flows": reports}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--out", type=Path, help="directory for the decoded WAV of each flow and report.json")
    parser.add_argument("--reference", type=Path, help="the 8 kHz mono WAV that was played, to measure integrity")
    parser.add_argument("--min-packets", type=int, default=20)
    args = parser.parse_args()
    report = analyse(args.pcap, reference_wav=args.reference, out_dir=args.out, min_packets=args.min_packets)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
