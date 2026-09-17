"""Can this machine run Sarvam-M locally? Measures, doesn't guess.

    cd backend && .venv/bin/python scripts/check_sarvam_hardware.py

Exit code 0 = SUPPORTED or MARGINAL (a local run is worth attempting).
Exit code 1 = UNSUPPORTED HARDWARE (do not download the model on this box).

This has to be run on the machine that would actually run the model - a
developer laptop's result says nothing about the client's production server,
and vice versa.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.hardware_check import SARVAM_M_Q4, assess, detect_hardware, format_report  # noqa: E402


def main() -> int:
    report = detect_hardware()
    verdict = assess(report, SARVAM_M_Q4)
    print(format_report(report, verdict, SARVAM_M_Q4))
    print()
    if verdict.verdict == "UNSUPPORTED HARDWARE":
        print("Do not download sarvam-m on this machine - it will not run usably.")
        return 1
    print(f"Set LLM_PROVIDER=sarvam_m and SARVAM_M_MODEL_PATH to a downloaded GGUF to try it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
