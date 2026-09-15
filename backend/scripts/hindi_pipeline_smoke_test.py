"""Live smoke test for the Hindi voice pipeline — makes REAL, billed calls.

Run on the deployed host, from the backend directory, with the production
backend/.env in place:

    cd backend && .venv/bin/python scripts/hindi_pipeline_smoke_test.py
    # optional: --asr-wav /path/to/real-hindi-recording.wav  --out /tmp/sunway-smoke

Checks, in order (stops early if configuration is unusable):
  1. configuration — required settings present (names only, never values)
  2. LLM           — one short Hindi-policy request (Gemini or OpenAI, per
                     LLM_PROVIDER); reply must be Devanagari
  3. Bhashini TTS  — a Hindi question -> WAV, normalized to 8kHz mono 16-bit PCM
  4. Bhashini ASR  — Hindi speech -> Devanagari transcript. Uses --asr-wav if
                     given; otherwise the step-3 speech downsampled to 8kHz to
                     mimic an Asterisk recording (reported as such)
  5. full chain    — step-4 speech -> Bhashini ASR -> embeddings + RAG -> LLM
                     -> Bhashini TTS -> 8kHz playback WAV

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
    measure_levels,
    normalize_for_asterisk_playback,
    prepare_tts_for_playback,
    read_wav_info,
)
from app.services.call_controller import speech_chunks  # noqa: E402

SPOKEN_QUESTION = "पशुओं में लम्पी रोग के लक्षण क्या हैं?"
LLM_KEY_SETTING = {"gemini": "gemini_api_key", "openai": "llm_api_key"}

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


def ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


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
    parser.add_argument("--asr-wav", type=Path, help="a genuine Hindi speech WAV for the ASR/full-chain checks")
    parser.add_argument("--out", type=Path, default=Path("/tmp/sunway-smoke"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    SECRETS.extend(
        [
            settings.llm_api_key,
            settings.gemini_api_key,
            settings.bhashini_inference_api_key,
            settings.rag_embedding_api_key,
            settings.stt_api_key,
            settings.tts_api_key,
            settings.database_url,
        ]
    )

    # ---- 1. configuration ----
    llm_name = (settings.llm_provider or "").strip().lower()
    problems = [
        f"{k.upper()}={getattr(settings, k)!r} (want {v!r})"
        for k, v in {"ai_language": "hi", "stt_provider": "bhashini", "tts_provider": "bhashini"}.items()
        if getattr(settings, k) != v
    ]
    if llm_name not in LLM_KEY_SETTING:
        problems.append(f"LLM_PROVIDER={settings.llm_provider!r} (want 'gemini' or 'openai')")
    elif not getattr(settings, LLM_KEY_SETTING[llm_name]):
        problems.append(f"{LLM_KEY_SETTING[llm_name].upper()} is empty")
    if not settings.bhashini_inference_api_key:
        problems.append("BHASHINI_INFERENCE_API_KEY is empty")

    llm_model = (
        f"{settings.gemini_model} (reasoning_effort={settings.gemini_reasoning_effort or 'default'})"
        if llm_name == "gemini"
        else settings.llm_model or "gpt-4o-mini"
    )
    summary = (
        f"llm={llm_name}/{llm_model} asr={settings.bhashini_asr_service_id} "
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
    llm_label = f"{llm_name.capitalize()} LLM (Hindi)"

    # ---- 2. LLM ----
    try:
        started = time.monotonic()
        reply = await llm.generate_response(
            system_prompt=settings.system_prompt_for("hi"),
            history=[LLMMessage(role="user", content="आपका स्वागत है। कृपया हिंदी में संक्षिप्त उत्तर दें।")],
            retrieved_context=None,
        )
        ratio = devanagari_ratio(reply.text)
        detail = f"{ms_since(started)}ms, devanagari={ratio:.0%}, finish={reply.finish_reason}, reply={reply.text!r}"
        record(llm_label, "PASS" if ratio >= 0.7 else "FAIL", detail)
    except Exception as exc:  # noqa: BLE001
        record(llm_label, "FAIL", f"{type(exc).__name__}: {exc}")

    # ---- 3. Bhashini TTS ----
    tts_wav: bytes | None = None
    try:
        started = time.monotonic()
        synthesis = await tts.synthesize(SPOKEN_QUESTION, language="hi")
        tts_ms = ms_since(started)
        source = read_wav_info(synthesis.audio_bytes)
        tts_wav = synthesis.audio_bytes
        (args.out / "tts_bhashini_original.wav").write_bytes(tts_wav)
        raw = measure_levels(tts_wav)
        started = time.monotonic()
        prepared = prepare_tts_for_playback(tts_wav, speed=settings.ai_tts_speed)
        prep_ms = ms_since(started)
        (args.out / "tts_asterisk_8k.wav").write_bytes(prepared)
        detail = (
            f"tts={tts_ms}ms prep={prep_ms}ms | raw {source.sample_rate}Hz/{source.channels}ch/{source.sample_width * 8}-bit "
            f"{source.duration_seconds:.2f}s peak={raw.peak_dbfs:.1f}dBFS clipped={raw.clipped_ratio:.3%} "
            f"lead/trail silence={raw.leading_silence_seconds:.2f}/{raw.trailing_silence_seconds:.2f}s | "
            f"played at {settings.ai_tts_speed:.2f}x: {check_asterisk_wav('prepared TTS', prepared)}"
        )
        record("Bhashini TTS (Hindi)", "PASS", detail)
    except Exception as exc:  # noqa: BLE001
        record("Bhashini TTS (Hindi)", "FAIL", f"{type(exc).__name__}: {exc}")

    # ---- 4. Bhashini ASR ----
    caller_audio: bytes | None = None
    transcript: str | None = None
    try:
        if args.asr_wav:
            caller_audio, origin = args.asr_wav.read_bytes(), f"recording {args.asr_wav.name}"
        elif tts_wav is not None:
            caller_audio, origin = normalize_for_asterisk_playback(tts_wav), "Bhashini TTS speech downsampled to 8kHz"
        else:
            raise RuntimeError("no Hindi speech available (TTS failed and no --asr-wav given)")
        (args.out / "asr_input.wav").write_bytes(caller_audio)
        started = time.monotonic()
        transcription = await stt.transcribe(caller_audio, language="hi")
        transcript = transcription.text
        ratio = devanagari_ratio(transcript)
        detail = f"input={origin}, {ms_since(started)}ms, devanagari={ratio:.0%}, transcript={transcript!r}"
        record("Bhashini ASR (Hindi)", "PASS" if ratio >= 0.7 else "FAIL", detail)
    except Exception as exc:  # noqa: BLE001
        record("Bhashini ASR (Hindi)", "FAIL", f"{type(exc).__name__}: {exc}")

    # ---- 5. full chain: speech -> ASR -> RAG -> LLM -> TTS ----
    chain_label = f"ASR -> RAG -> {llm_name.capitalize()} -> TTS"
    try:
        if caller_audio is None:
            raise RuntimeError("no caller audio available (see ASR step)")

        from sqlalchemy import delete

        from app.core.db import AsyncSessionLocal, engine
        from app.models.ai import AIMessage
        from app.services.conversation import create_session, handle_text_turn

        embedder = get_embedding_provider(settings)
        async with AsyncSessionLocal() as db:
            session = await create_session(db, language="hi")
            try:
                started = time.monotonic()
                heard = (await stt.transcribe(caller_audio, language="hi")).text
                asr_ms = ms_since(started)

                started = time.monotonic()
                turn = await handle_text_turn(
                    db, session, heard, settings=settings, embedding_provider=embedder, llm_provider=llm
                )
                turn_ms = ms_since(started)
                reply_text = turn.assistant_message.text or ""

                # Before: whole reply in one TTS request. After: first chunk only.
                started = time.monotonic()
                speech = await tts.synthesize(reply_text, language="hi")
                whole_tts_ms = ms_since(started)
                chunks = speech_chunks(reply_text, truncated=turn.finish_reason == "length")
                started = time.monotonic()
                first = await tts.synthesize(chunks[0], language="hi")
                first_tts_ms = ms_since(started)
                started = time.monotonic()
                prepared = prepare_tts_for_playback(speech.audio_bytes, speed=settings.ai_tts_speed)
                prep_ms = ms_since(started)
                (args.out / "full_chain_reply_8k.wav").write_bytes(prepared)
                timings = turn.timings

                ratio = devanagari_ratio(reply_text)
                detail = (
                    f"heard={heard!r} | asr={asr_ms}ms embed={timings.get('embed')}ms rag={timings.get('rag')}ms "
                    f"llm={timings.get('llm')}ms | reply_chars={len(reply_text)} chunks={len(chunks)} | "
                    f"time to first audio: whole-reply TTS {asr_ms + turn_ms + whole_tts_ms}ms -> "
                    f"first-chunk TTS {asr_ms + turn_ms + first_tts_ms}ms (tts {whole_tts_ms} -> {first_tts_ms}ms, prep {prep_ms}ms) | "
                    f"rag_chunks={len(turn.retrieved_chunks)} devanagari={ratio:.0%} | reply={reply_text!r} | "
                    f"audio={check_asterisk_wav('reply audio', prepared)} "
                    f"(+ {settings.ai_end_of_speech_silence_seconds}s end-of-speech wait on a real call)"
                )
                record(chain_label, "PASS" if ratio >= 0.7 else "FAIL", detail)
            finally:
                await db.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
                await db.delete(session)
                await db.commit()
        await engine.dispose()
    except Exception as exc:  # noqa: BLE001
        record(chain_label, "FAIL", f"{type(exc).__name__}: {exc}")

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
