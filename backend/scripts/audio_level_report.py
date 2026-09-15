"""Audio level report for call recordings — read-only noise/gain diagnosis.

Caller turn recordings are the caller's RTP audio as Asterisk decoded it, so
their levels show what the GSM/SMG leg delivers:

    cd backend && .venv/bin/python scripts/audio_level_report.py                  # latest 20 AI turn recordings
    .venv/bin/python scripts/audio_level_report.py --latest 50
    .venv/bin/python scripts/audio_level_report.py /path/a.wav /path/b.wav        # specific files

Flags per file:
  CLIPPING   >0.1% of samples at full scale — input gain too hot (distortion)
  LOW        peak below -24 dBFS — gain too low, ASR struggles
  NOISY      noise floor above -45 dBFS — audible hiss/line noise between words
             (also keeps Asterisk silence detection from ending the turn early)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.audio import AudioFormatError, measure_levels  # noqa: E402

DEFAULT_DIR = Path("/var/spool/asterisk/recording")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--latest", type=int, default=20)
    args = parser.parse_args()

    files = args.files or sorted(args.dir.glob("ai-agent__*.wav"), key=lambda p: p.stat().st_mtime)[-args.latest :]
    if not files:
        print(f"No recordings found (looked in {args.dir}).")
        return 1

    print(f"{'file':<58} {'dur':>5} {'peak':>7} {'rms':>7} {'floor':>7} {'clip':>6}  flags")
    flagged = {"CLIPPING": 0, "LOW": 0, "NOISY": 0}
    for path in files:
        try:
            lv = measure_levels(path.read_bytes())
        except (OSError, AudioFormatError) as exc:
            print(f"{path.name:<58} unreadable: {exc}")
            continue
        flags = []
        if lv.clipped_ratio > 0.001:
            flags.append("CLIPPING")
        if lv.peak_dbfs < -24:
            flags.append("LOW")
        if lv.noise_floor_dbfs > -45:
            flags.append("NOISY")
        for flag in flags:
            flagged[flag] += 1
        print(
            f"{path.name[-58:]:<58} {lv.duration_seconds:>4.1f}s {lv.peak_dbfs:>6.1f} {lv.rms_dbfs:>6.1f} "
            f"{lv.noise_floor_dbfs:>6.1f} {lv.clipped_ratio:>6.2%}  {' '.join(flags)}"
        )
    print(f"\n{len(files)} files; " + ", ".join(f"{k}: {v}" for k, v in flagged.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
