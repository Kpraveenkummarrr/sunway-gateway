"""Synthesize the IVR menu prompts and install them as Asterisk custom sounds.

Background: the IVR dialplan (asterisk/etc/dialplan/ivr.conf) references
custom/ivr-welcome-menu, custom/ivr-invalid, custom/ivr-goodbye and
custom/ivr-timeout. If those files do not exist, Asterisk logs a WARNING and
silently continues with no audio — the call is answered but the caller hears
nothing (confirmed live: "call attend aagum but no voice"). This script
generates them with the project's own configured TTS provider (the same one
that renders AI replies) and writes them to a local directory; install them
into Asterisk's sounds directory as a second step (see below), because that
directory is root/asterisk-owned and this script should not need root.

Usage (from backend/, with the project's venv):
    .venv/bin/python scripts/gen_ivr_prompts.py --out /tmp/ivr_prompts

Then, as root:
    sudo cp /tmp/ivr_prompts/*.wav /usr/share/asterisk/sounds/custom/
    sudo chown asterisk:asterisk /usr/share/asterisk/sounds/custom/*.wav
    sudo asterisk -rx "dialplan reload"

Confirm Asterisk's actual sounds directory first (it is not always
/usr/share/asterisk — some installs use /var/lib/asterisk):
    sudo asterisk -rx "core show settings" | grep -i sound
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.providers.tts.factory import get_tts_provider
from app.services.audio import prepare_tts_clip
from app.services.hindi_tts_text import hindi_tts_text

# Edit these to match the department names actually configured in the admin
# panel's Departments tab; re-run this script and reinstall after any change.
PROMPTS = {
    "ivr-welcome-menu": (
        "डेमो1 के लिए एक दबाएं। डेमो2 के लिए दो दबाएं। डेमो3 के लिए तीन दबाएं। "
        "डेमो4 के लिए चार दबाएं। डेमो5 के लिए पांच दबाएं। डेमो6 के लिए छह दबाएं। "
        "सहायक से बात करने के लिए नौ दबाएं।"
    ),
    "ivr-invalid": "क्षमा करें, यह विकल्प सही नहीं है। कृपया फिर से प्रयास करें।",
    "ivr-goodbye": "आपसे बात करके अच्छा लगा। धन्यवाद।",
    "ivr-timeout": "कोई जवाब नहीं मिला। कृपया फिर से प्रयास करें।",
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("/tmp/ivr_prompts"), help="writable local directory")
    args = parser.parse_args()

    settings = Settings()
    tts = get_tts_provider(settings)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in PROMPTS.items():
        spoken = hindi_tts_text(text) if settings.ai_language == "hi" else text
        result = await tts.synthesize(spoken, language=settings.ai_language)
        clip = prepare_tts_clip(
            result.audio_bytes,
            speed=settings.ai_tts_speed,
            profile=settings.ai_audio_profile,
            target_rms_dbfs=settings.ai_tts_target_rms_dbfs,
            peak_ceiling_dbfs=settings.ai_tts_peak_ceiling_dbfs,
        )
        out_path = args.out / f"{name}.wav"
        out_path.write_bytes(clip.audio)
        print(f"wrote {out_path} ({clip.diagnostics.final_duration_s:.1f}s)")
    print(f"\nNow install as root: sudo cp {args.out}/*.wav <asterisk sounds dir>/custom/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
