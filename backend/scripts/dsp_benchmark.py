"""Produce listenable SYNTHETIC probes, not Hindi/TTS or GSM evidence."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from app.services.audio import _write_pcm, _read_pcm, normalize_for_asterisk_playback
from app.services.voice_diagnostics import build_voice_ab_variants, describe_wav


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    rate = 48000
    t = np.arange(rate * 3) / rate
    signal = sum(0.20 / harmonic * np.sin(2 * np.pi * 180 * harmonic * t) for harmonic in range(1, 12))
    source = _write_pcm(np.concatenate([np.zeros(9600), signal, np.zeros(14400)]), rate)
    started = time.perf_counter()
    variants = build_voice_ab_variants(source, current_speed=1.15)
    report = {"evidence_type": "synthetic DSP only; no provider or GSM audio", "files": {}}
    for name, audio in variants.items():
        name = name.replace("original_bhashini", "synthetic_source")
        (args.out / name).write_bytes(audio)
        report["files"][name] = describe_wav(audio)
    report["bundle_generation_ms"] = round((time.perf_counter() - started) * 1000, 2)
    stopband = _write_pcm(0.5 * np.sin(2 * np.pi * 6000 * t), rate)
    filtered, _ = _read_pcm(normalize_for_asterisk_playback(stopband))
    interior = filtered[800:-800]
    report["6000hz_alias_relative_db"] = float(20 * np.log10(max(np.sqrt(np.mean(interior ** 2)), 1e-12) / (0.5 / np.sqrt(2))))
    (args.out / "dsp_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
