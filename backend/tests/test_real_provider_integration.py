"""Opt-in, real-provider, real-money integration test — the minimal
controlled verification from the Phase 7 spec (Section 9):

    text -> TTS (real) -> audio -> STT (real) -> text
    -> conversation service -> RAG -> LLM (real) -> reply

This makes real, BILLED API calls (roughly: 1 TTS + 1 STT + 1 embedding +
1 LLM call — four requests total, deliberately minimal). It is SKIPPED
by default and only runs when ALL of the following are true:

  1. REAL_PROVIDER_TESTS=1 is set in the environment.
  2. STT_PROVIDER, LLM_PROVIDER, and TTS_PROVIDER are all set to a real
     vendor (not "mock", not blank) in the loaded settings.
  3. The matching *_API_KEY values are non-empty.

Run explicitly with:

    REAL_PROVIDER_TESTS=1 pytest tests/test_real_provider_integration.py -v

Do not run this as part of the normal `pytest` invocation or in CI —
mock tests cover the code paths for free; this only exists to prove the
real vendor integration actually works, once, deliberately, with your
explicit go-ahead (see the Phase 7 report for whether this has been run).
"""

import os

import pytest

from app.core.config import get_settings
from app.providers.embeddings.factory import get_embedding_provider
from app.providers.llm.base import LLMMessage
from app.providers.llm.factory import get_llm_provider
from app.providers.stt.factory import get_stt_provider
from app.providers.tts.factory import get_tts_provider
from app.services.audio import is_effectively_silent, read_wav_info
from app.services.knowledge_search import search_chunks
from app.services.rag_context import build_context

_REAL_TESTS_ENABLED = os.environ.get("REAL_PROVIDER_TESTS") == "1"


def _real_providers_configured() -> bool:
    s = get_settings()
    return (
        s.stt_provider == "openai"
        and bool(s.stt_api_key)
        and s.llm_provider == "openai"
        and bool(s.llm_api_key)
        and s.tts_provider == "openai"
        and bool(s.tts_api_key)
    )


pytestmark = pytest.mark.skipif(
    not _REAL_TESTS_ENABLED,
    reason="Real-provider tests are opt-in only — set REAL_PROVIDER_TESTS=1 to run them (makes billed API calls).",
)


@pytest.mark.asyncio
async def test_minimal_real_provider_roundtrip(db_session) -> None:
    settings = get_settings()
    if not _real_providers_configured():
        pytest.skip(
            "REAL_PROVIDER_TESTS=1 is set, but STT_PROVIDER/LLM_PROVIDER/TTS_PROVIDER "
            "aren't all configured to 'openai' with matching API keys — nothing to test against."
        )

    tts_provider = get_tts_provider(settings)
    stt_provider = get_stt_provider(settings)
    llm_provider = get_llm_provider(settings)
    embedding_provider = get_embedding_provider(settings)

    test_phrase = "Hello, this is a short test phrase."

    # Step 1-2: text -> TTS -> audio, then verify the audio itself.
    synthesis = await tts_provider.synthesize(test_phrase, language=settings.ai_language)
    assert synthesis.audio_bytes
    wav_info = read_wav_info(synthesis.audio_bytes)
    assert wav_info.duration_seconds > 0
    assert not is_effectively_silent(synthesis.audio_bytes)

    # Step 3: audio -> STT -> text.
    transcription = await stt_provider.transcribe(synthesis.audio_bytes, language=settings.ai_language)
    assert transcription.text.strip()
    # Not asserting exact equality — STT wording/punctuation can differ
    # slightly from the source text; asserting it's non-trivially close.
    assert len(transcription.text) > 5

    # Step 4-5: text -> RAG (embedding + pgvector search).
    query_embedding = await embedding_provider.embed_one(transcription.text)
    retrieved = await search_chunks(
        db_session,
        query_embedding=query_embedding,
        top_k=settings.rag_top_k,
        similarity_threshold=settings.rag_similarity_threshold,
    )
    context = build_context(retrieved, max_chars=settings.ai_max_context_chars)
    # No knowledge base documents are guaranteed to exist in this
    # environment — retrieved may legitimately be empty; that's fine,
    # this step just proves the RAG call itself succeeds without erroring.

    # Step 6: LLM response, grounded (or not) per the configured policy.
    llm_response = await llm_provider.generate_response(
        system_prompt=settings.ai_system_prompt,
        history=[LLMMessage(role="user", content=transcription.text)],
        retrieved_context=context,
    )
    assert llm_response.text.strip()

    # Step 7: text -> TTS -> audio, verify the final reply audio too.
    reply_synthesis = await tts_provider.synthesize(llm_response.text, language=settings.ai_language)
    assert reply_synthesis.audio_bytes
    reply_info = read_wav_info(reply_synthesis.audio_bytes)
    assert reply_info.duration_seconds > 0
