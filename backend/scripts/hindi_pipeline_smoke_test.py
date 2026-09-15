"""Live smoke test for the Hindi voice pipeline — makes REAL, billed calls.

Run on the deployed host, from the backend directory, with the production
backend/.env in place:

    cd backend && .venv/bin/python scripts/hindi_pipeline_smoke_test.py
    # optional: --asr-wav /path/to/real-hindi-recording.wav  --out /tmp/sunway-smoke

Checks, in order (stops early if configuration is unusable):
  1. configuration   — required settings present (names only, never values)
  2. OpenAI LLM      — one short Hindi-policy request, reply must be Devanagari
  3. Bhashini TTS    — Hindi phrase -> WAV, normalized to 8kHz mono 16-bit PCM
  4. Bhashini ASR    — Hindi speech -> Devanagari transcript. Uses --asr-wav if
                       given; otherwise the step-3 TTS speech, downsampled to
                       8kHz to mimic an Asterisk recording (reported as such)
  5. full text turn  — Hindi text -> embeddings + RAG -> OpenAI -> Bhashini TTS

Never prints API keys, auth headers, the database URL, or base64 audio.
Generated WAVs are written to --out for listening.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.providers.llm.base import LLMMessage  # noqa: E402
from app.services.audio import (  # noqa: E402
    ASTERISK_SAMPLE_RATE,
    is_effectively_silent,
    normalize_for_asterisk_playback,
    read_wav_info,
)

RESULTS: list[tuple[str, str, str]] = []
SECRETS: list[str] = []


def scrub(text: str) -> str:
    for secret in SECRETS:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "<redacted>")
    return text


def devanagari_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum("ऀ" <= ch <= "ॿ" for ch in letters) / len(letters)


def record(name: str, status: str, detail: str) -> None:
    print(f"[{status}] {name}: {scrub(detail)}", flush=True)
    RESULTS.append((name, status, scrub(detail)))


def check_asterisk_wav(label: str, wav: bytes) -> str:
    info = read_wav_info(wav)
    ok = (info.sample_rate, info.channels, info.sample_width) == (ASTERISK_SAMPLE_RATE, 1, 2)
    if not ok:
        raise AssertionError(f"{label} is {info.sample_rate}Hz/{info.channels}ch/{info.sample_width * 8}-bit, not 8000Hz/1ch/16-bit")
    if is_effectively_silent(wav):
        raise AssertionError(f"{label} is effectively silent")
    return f"{info.sample_rate}Hz mono {info.sample_width * 8}-bit PCM, {info.duration_seconds:.2f}s, non-silent"


async def main() -> int:
    # Hindi output must print even on a console/SSH session whose locale isn't UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-wav", type=Path, help="a genuine Hindi speech WAV for the ASR check")
    parser.add_argument("--out", type=Path, default=Path("/tmp/sunway-smoke"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    SECRETS.extend(
        [
            settings.llm_api_key,
            settings.bhashini_inference_api_key,
            settings.rag_embedding_api_key,
            settings.stt_api_key,
            settings.tts_api_key,
            settings.database_url,
        ]
    )

    # ---- 1. configuration ----
    expected = {"ai_language": "hi", "stt_provider": "bhashini", "tts_provider": "bhashini", "llm_provider": "openai"}
    problems = [f"{k.upper()}={getattr(settings, k)!r} (want {v!r})" for k, v in expected.items() if getattr(settings, k) != v]
    for secret_name in ("llm_api_key", "bhashini_inference_api_key"):
        if not getattr(settings, secret_name):
            problems.append(f"{secret_name.upper()} is empty")
    summary = (
        f"llm_model={settings.llm_model or '(default gpt-4o-mini)'} asr={settings.bhashini_asr_service_id} "
        f"tts={settings.bhashini_tts_service_id}/{settings.bhashini_tts_gender} "
        f"embeddings={settings.rag_embedding_provider or '(unset)'}"
    )
    if problems:
        record("configuration", "FAIL", "; ".join(problems) + f" | {summary}")
        return 1
    record("configuration", "PASS", summary)

    from app.providers.embeddings.factory import get_embedding_provider
    from app.providers.llm.factory import get_llm_provider
    from app.providers.stt.factory import get_stt_provider
    from app.providers.tts.factory import get_tts_provider

    llm = get_llm_provider(settings)
    stt = get_stt_provider(settings)
    tts = get_tts_provider(settings)

    # ---- 2. OpenAI LLM ----
    try:
        started = time.monotonic()
        reply = await llm.generate_response(
            system_prompt=settings.system_prompt_for("hi"),
            history=[LLMMessage(role="user", content="आपका स्वागत है। कृपया हिंदी में संक्षिप्त उत्तर दें।")],
            retrieved_context=None,
        )
        ratio = devanagari_ratio(reply.text)
        detail = f"{int((time.monotonic() - started) * 1000)}ms, devanagari={ratio:.0%}, reply={reply.text!r}"
        record("OpenAI LLM (Hindi)", "PASS" if ratio >= 0.7 else "FAIL", detail)
    except Exception as exc:  # noqa: BLE001
        record("OpenAI LLM (Hindi)", "FAIL", f"{type(exc).__name__}: {exc}")

    # ---- 3. Bhashini TTS ----
    tts_wav: bytes | None = None
    try:
        started = time.monotonic()
        synthesis = await tts.synthesize("नमस्ते, आपका स्वागत है।", language="hi")
        source = read_wav_info(synthesis.audio_bytes)
        tts_wav = synthesis.audio_bytes
        (args.out / "tts_bhashini_original.wav").write_bytes(tts_wav)
        normalized = normalize_for_asterisk_playback(tts_wav)
        (args.out / "tts_asterisk_8k.wav").write_bytes(normalized)
        detail = (
            f"{int((time.monotonic() - started) * 1000)}ms, source={source.sample_rate}Hz/{source.channels}ch/"
            f"{source.sample_width * 8}-bit {source.duration_seconds:.2f}s -> {check_asterisk_wav('normalized TTS', normalized)}"
        )
        record("Bhashini TTS (Hindi)", "PASS", detail)
    except Exception as exc:  # noqa: BLE001
        record("Bhashini TTS (Hindi)", "FAIL", f"{type(exc).__name__}: {exc}")

    # ---- 4. Bhashini ASR ----
    try:
        if args.asr_wav:
            asr_input, origin = args.asr_wav.read_bytes(), f"recording {args.asr_wav.name}"
        elif tts_wav is not None:
            asr_input, origin = normalize_for_asterisk_playback(tts_wav), "Bhashini TTS speech downsampled to 8kHz"
        else:
            raise RuntimeError("no Hindi speech available (TTS failed and no --asr-wav given)")
        (args.out / "asr_input.wav").write_bytes(asr_input)
        started = time.monotonic()
        transcription = await stt.transcribe(asr_input, language="hi")
        ratio = devanagari_ratio(transcription.text)
        detail = (
            f"input={origin}, {int((time.monotonic() - started) * 1000)}ms, devanagari={ratio:.0%}, "
            f"transcript={transcription.text!r}"
        )
        record("Bhashini ASR (Hindi)", "PASS" if ratio >= 0.7 else "FAIL", detail)
    except Exception as exc:  # noqa: BLE001
        record("Bhashini ASR (Hindi)", "FAIL", f"{type(exc).__name__}: {exc}")

    # ---- 5. full text turn: RAG + OpenAI + Bhashini TTS ----
    try:
        from sqlalchemy import delete

        from app.core.db import AsyncSessionLocal, engine
        from app.models.ai import AIMessage
        from app.services.conversation import create_session, handle_text_turn

        embedder = get_embedding_provider(settings)
        async with AsyncSessionLocal() as db:
            session = await create_session(db, language="hi")
            try:
                started = time.monotonic()
                turn = await handle_text_turn(
                    db,
                    session,
                    "आपके कार्यालय का समय क्या है?",
                    settings=settings,
                    embedding_provider=embedder,
                    llm_provider=llm,
                )
                reply_text = turn.assistant_message.text or ""
                speech = await tts.synthesize(reply_text, language="hi")
                normalized = normalize_for_asterisk_playback(speech.audio_bytes)
                (args.out / "full_turn_reply_8k.wav").write_bytes(normalized)
                ratio = devanagari_ratio(reply_text)
                detail = (
                    f"{int((time.monotonic() - started) * 1000)}ms, rag_chunks={len(turn.retrieved_chunks)}, "
                    f"devanagari={ratio:.0%}, reply={reply_text!r}, audio={check_asterisk_wav('reply audio', normalized)}"
                )
                record("RAG + OpenAI + TTS turn", "PASS" if ratio >= 0.7 else "FAIL", detail)
            finally:
                await db.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
                await db.delete(session)
                await db.commit()
        await engine.dispose()
    except Exception as exc:  # noqa: BLE001
        record("RAG + OpenAI + TTS turn", "FAIL", f"{type(exc).__name__}: {exc}")

    for provider in (stt, tts):
        client = getattr(provider, "_client", None)
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    print("\n" + "=" * 60)
    for name, status, _ in RESULTS:
        print(f"{status:5} {name}")
    print(f"WAV files: {args.out}")
    return 0 if all(status == "PASS" for _, status, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
