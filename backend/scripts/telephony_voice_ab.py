"""Create a controlled Bhashini -> telephony A/B listening bundle.

This makes a real, potentially billed Bhashini call unless --input-wav is
provided.  It never prints or writes credentials.  The generated report is a
safe backup of the effective non-secret voice configuration used for the run.

Run on the deployed host from ``backend``::

    .venv/bin/python scripts/telephony_voice_ab.py --out /var/tmp/sunway-voice-ab

Offline re-analysis of an already captured provider WAV::

    .venv/bin/python scripts/telephony_voice_ab.py \
        --input-wav /path/to/original-bhashini.wav --out /var/tmp/sunway-voice-ab
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.services.audio import ASTERISK_SAMPLE_RATE, prepare_tts_for_playback  # noqa: E402
from app.services.voice_diagnostics import build_voice_ab_variants, describe_wav, g711_roundtrip  # noqa: E402

DEFAULT_TEXT = (
    "नमस्कार। पशुओं में लम्पी रोग के सामान्य लक्षण बुखार, त्वचा पर गांठें और दूध कम होना हैं।"
)


async def _load_effective_settings(base: Settings, *, env_only: bool) -> tuple[Settings, str]:
    if env_only:
        return base, "environment only (--env-only)"
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.system_config import effective_settings

        async def load() -> Settings:
            async with AsyncSessionLocal() as db:
                return await effective_settings(db, base)

        return await asyncio.wait_for(load(), timeout=3.0), "environment plus system_config database overrides"
    except Exception as exc:
        raise RuntimeError(f"Cannot read effective settings ({type(exc).__name__}); use --env-only explicitly for an offline test") from None


async def _synthesize(settings: Settings, text: str) -> tuple[bytes, int]:
    from app.providers.tts.factory import get_tts_provider

    provider = get_tts_provider(settings)
    started = time.monotonic()
    try:
        result = await provider.synthesize(text, language="hi")
        tts_ms = int((time.monotonic() - started) * 1000)
    finally:
        client = getattr(provider, "_client", None)
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()
    if result.audio_format != "wav":
        raise RuntimeError(f"Bhashini returned unsupported format {result.audio_format!r}")
    return result.audio_bytes, tts_ms


def _write_files(output_dir: Path, files: dict[str, bytes], *, word_count: int) -> dict[str, dict]:
    reports: dict[str, dict] = {}
    for name, audio in files.items():
        (output_dir / name).write_bytes(audio)
        reports[name] = describe_wav(audio, word_count=word_count)
    return reports


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Generate objective Hindi telephony voice A/B artifacts")
    parser.add_argument("--out", type=Path, default=Path("/tmp/sunway-voice-ab"))
    parser.add_argument("--text", help="exact transcript of input audio, or phrase for live synthesis")
    parser.add_argument("--input-wav", type=Path, help="existing original Bhashini WAV; skips the billed API call")
    parser.add_argument("--current-speed", type=float, help="override the effective current speed for reproduction")
    parser.add_argument(
        "--candidate-service-id",
        help="optional second Bhashini serviceId; only use one confirmed available for this API key",
    )
    parser.add_argument("--env-only", action="store_true", help="do not read admin-panel overrides from PostgreSQL")
    args = parser.parse_args()

    if args.text is not None and not args.text.strip():
        parser.error("--text must not be blank")
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("--out must be a new or empty directory; previous evidence will not be overwritten")

    base = Settings()
    settings, settings_source = await _load_effective_settings(base, env_only=args.env_only)
    current_speed = args.current_speed if args.current_speed is not None else settings.ai_tts_speed
    if not math.isfinite(current_speed) or not 0.8 <= current_speed <= 1.6:
        parser.error("--current-speed must be finite and between 0.8 and 1.6")
    if args.candidate_service_id and args.input_wav:
        parser.error("--candidate-service-id cannot be combined with --input-wav")
    args.out.mkdir(parents=True, exist_ok=True)
    text = args.text if args.text is not None else (None if args.input_wav else DEFAULT_TEXT)

    safe_config = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "settings_source": settings_source,
        "tts_provider": settings.tts_provider,
        "language": settings.ai_language,
        "bhashini_service_id": settings.bhashini_tts_service_id,
        "bhashini_gender": settings.bhashini_tts_gender,
        "current_local_tempo": current_speed,
        "candidate_local_tempo": 1.0,
        "target_playback_rate_hz": ASTERISK_SAMPLE_RATE,
    }
    (args.out / "safe_current_voice_config.json").write_text(
        json.dumps(safe_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.input_wav is not None:
        source_wav = args.input_wav.read_bytes()
        tts_ms: int | None = None
        source_origin = f"offline input {args.input_wav.name}"
    else:
        if settings.tts_provider.strip().lower() != "bhashini" or settings.ai_language != "hi":
            print("ERROR: live run requires TTS_PROVIDER=bhashini and AI_LANGUAGE=hi", file=sys.stderr)
            return 2
        source_wav, tts_ms = await _synthesize(settings, text)
        source_origin = "live Bhashini response"

    word_count = len(text.split()) if text else 0
    variants = build_voice_ab_variants(source_wav, current_speed=current_speed, candidate_speed=1.0)
    report = {
        "configuration": safe_config,
        "source_origin": source_origin,
        "text": text,
        "input_provider_verified": args.input_wav is None,
        "word_count": word_count,
        "tts_generation_ms": tts_ms,
        "files": _write_files(args.out, variants, word_count=word_count),
        "limitations": [
            "G.711 files are offline quantisation previews, not proof of the negotiated codec.",
            "No file simulates RTP loss/jitter, SMG4008 processing, GSM radio, or a handset speaker.",
            "Naturalness and intelligibility must be scored by a Hindi-speaking listener.",
        ],
    }

    if args.candidate_service_id:
        candidate_settings = settings.model_copy(update={"bhashini_tts_service_id": args.candidate_service_id})
        candidate_source, candidate_tts_ms = await _synthesize(candidate_settings, text)
        candidate_native = prepare_tts_for_playback(candidate_source, speed=1.0)
        candidate_files = {
            "M1_candidate_model_original.wav": candidate_source,
            "M2_candidate_model_native_8k.wav": candidate_native,
            "M3_candidate_model_ulaw_preview.wav": g711_roundtrip(candidate_native, codec="ulaw"),
            "M4_candidate_model_alaw_preview.wav": g711_roundtrip(candidate_native, codec="alaw"),
        }
        report["candidate_model"] = {
            "service_id": args.candidate_service_id,
            "gender": settings.bhashini_tts_gender,
            "tts_generation_ms": candidate_tts_ms,
            "files": _write_files(args.out, candidate_files, word_count=word_count),
        }

    report_path = args.out / "voice_ab_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    source = report["files"]["A_original_bhashini.wav"]
    current = report["files"]["B1_current_8k.wav"]
    candidate = report["files"]["B2_candidate_native_cadence_8k.wav"]
    print(
        f"Configured Bhashini service={settings.bhashini_tts_service_id} gender={settings.bhashini_tts_gender} "
        f"input_origin={source_origin}; "
        f"source={source['sample_rate_hz']}Hz/{source['duration_seconds']:.2f}s tts={tts_ms}ms"
    )
    print(
        f"Current={current_speed:.2f}x/{current['duration_seconds']:.2f}s; "
        f"candidate=1.00x/{candidate['duration_seconds']:.2f}s; target={ASTERISK_SAMPLE_RATE}Hz"
    )
    print(f"A/B WAVs and safe report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
