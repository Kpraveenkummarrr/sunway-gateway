"""Structural tests for LLM_PROVIDER=gemini: Gemini through its official
OpenAI-compatible endpoint, reusing OpenAILLMProvider with a base_url.
Fake SDK/HTTP clients only — no network, no key, no cost. Settings use
_env_file=None so a real backend/.env can never be picked up here.
"""

import base64
import json

import httpx
import pytest

from tests.test_lsd_knowledge_retrieval import lsd_corpus  # noqa: F401 - shared real-DB fixture
from sqlalchemy import delete

from app.core.config import LANGUAGE_POLICIES, Settings
from app.models.ai import AIMessage
from app.providers.bhashini.client import BhashiniClient
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.base import LLMMessage, LLMProviderError
from app.providers.llm.factory import get_llm_provider
from app.providers.llm.openai_provider import OpenAILLMProvider
from app.providers.stt.bhashini_provider import BhashiniSTTProvider
from app.providers.tts.bhashini_provider import BhashiniTTSProvider
from app.services.audio import normalize_for_asterisk_playback, read_wav_info
from app.services.conversation import create_session, handle_text_turn
from tests.audio_fixtures import make_tone_wav
from tests.fake_openai import FakeAPIError, FakeChatCompletions, FakeOpenAIClient

FAKE_GEMINI_KEY = "fake-gemini-key-NEVER-PRINT-0123456789"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
HINDI_REPLY = "लम्पी रोग में पशु को तेज बुखार और त्वचा पर गांठें हो सकती हैं।"


@pytest.mark.asyncio
async def test_runtime_token_limit_is_applied_without_mutating_other_calls():
    provider = get_llm_provider(_settings())
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text=HINDI_REPLY))
    await provider.for_max_tokens(96).generate_response(system_prompt="test", history=_history(), retrieved_context="test")
    assert provider._client.chat.completions.calls[-1]["max_tokens"] == 96
    assert provider._max_tokens == 400


@pytest.mark.asyncio
async def test_real_helpline_does_not_call_model_when_retrieval_is_empty(db_session, monkeypatch):
    import app.services.conversation as conversation

    async def empty_search(*args, timings=None, **kwargs):
        timings["rag"] = 0
        return []

    monkeypatch.setattr(conversation, "search_chunks", empty_search)
    provider = get_llm_provider(_settings())
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="must never be spoken"))
    session = await create_session(db_session, language="hi")
    try:
        result = await handle_text_turn(db_session, session, "इसका इलाज क्या है?", settings=_settings(),
            embedding_provider=MockEmbeddingProvider(dimensions=1536), llm_provider=provider)
        assert result.finish_reason == "grounding_unavailable"
        assert provider._client.chat.completions.calls == []
        assert "पशु चिकित्सक" in result.assistant_message.text
    finally:
        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        ai_language="hi",
        llm_provider="gemini",
        gemini_api_key=FAKE_GEMINI_KEY,
        llm_api_key="",
        provider_timeout_seconds=5.0,
        llm_max_tokens=400,
    )
    base.update(overrides)
    return Settings(**base)


def _history() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="लम्पी रोग के लक्षण क्या हैं?")]


# ---- factory ----


def test_gemini_factory_reuses_openai_provider_with_gemini_endpoint() -> None:
    provider = get_llm_provider(_settings())

    assert isinstance(provider, OpenAILLMProvider)
    assert provider._base_url == GEMINI_URL
    assert provider._model == "gemini-2.5-flash"
    assert provider._provider_name == "Gemini"
    assert provider._reasoning_effort == "none"
    assert provider._api_key == FAKE_GEMINI_KEY
    assert provider._max_tokens == 400


def test_gemini_factory_requires_gemini_key_not_openai_key() -> None:
    with pytest.raises(LLMProviderError, match="GEMINI_API_KEY"):
        get_llm_provider(_settings(gemini_api_key="", llm_api_key="sk-openai-key-present"))


def test_gemini_model_and_reasoning_are_configurable() -> None:
    provider = get_llm_provider(_settings(gemini_model="gemini-2.5-flash-lite", gemini_reasoning_effort="low"))
    assert provider._model == "gemini-2.5-flash-lite"
    assert provider._reasoning_effort == "low"
    assert get_llm_provider(_settings(gemini_reasoning_effort=""))._reasoning_effort is None


def test_openai_provider_still_available_and_unchanged() -> None:
    provider = get_llm_provider(_settings(llm_provider="openai", llm_api_key="sk-test", gemini_api_key=""))
    assert isinstance(provider, OpenAILLMProvider)
    assert provider._base_url is None
    assert provider._provider_name == "OpenAI"
    assert provider._reasoning_effort is None


def test_real_sdk_client_targets_gemini_base_url_without_network() -> None:
    provider = get_llm_provider(_settings())
    client = provider._get_client()  # constructs openai.AsyncOpenAI; no request is made
    assert str(client.base_url) == GEMINI_URL


# ---- request / response handling ----


@pytest.mark.asyncio
async def test_gemini_request_disables_thinking_and_sends_hindi_policy() -> None:
    provider = get_llm_provider(_settings(ai_system_prompt="Base prompt."))
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text=HINDI_REPLY))

    result = await provider.generate_response(
        system_prompt=_settings(ai_system_prompt="Base prompt.").system_prompt_for("hi"),
        history=_history(),
        retrieved_context="लम्पी रोग: बुखार, त्वचा पर गांठें।",
    )

    assert result.text == HINDI_REPLY
    call = provider._client.chat.completions.calls[0]
    assert call["model"] == "gemini-2.5-flash"
    assert call["reasoning_effort"] == "none"
    assert call["max_tokens"] == 400
    system = call["messages"][0]
    assert system["role"] == "system"
    assert LANGUAGE_POLICIES["hi"] in system["content"]
    assert "लम्पी रोग: बुखार" in system["content"]
    assert call["messages"][1] == {"role": "user", "content": "लम्पी रोग के लक्षण क्या हैं?"}


@pytest.mark.asyncio
async def test_openai_request_never_sends_reasoning_effort() -> None:
    provider = OpenAILLMProvider(api_key="sk-test")
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="hello"))
    await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)
    assert "reasoning_effort" not in provider._client.chat.completions.calls[0]


@pytest.mark.asyncio
async def test_gemini_errors_are_labelled_and_redact_the_key() -> None:
    provider = get_llm_provider(_settings())
    provider._client = FakeOpenAIClient(
        completions=FakeChatCompletions(error=FakeAPIError(f"API key not valid: {FAKE_GEMINI_KEY}"))
    )
    with pytest.raises(LLMProviderError) as excinfo:
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)
    assert str(excinfo.value).startswith("Gemini LLM request failed")
    assert FAKE_GEMINI_KEY not in str(excinfo.value)


@pytest.mark.asyncio
async def test_gemini_empty_reply_reports_finish_reason() -> None:
    provider = get_llm_provider(_settings())
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="", finish_reason="length"))
    with pytest.raises(LLMProviderError, match="Gemini LLM returned an empty response \\(finish_reason=length\\)"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


@pytest.mark.asyncio
async def test_gemini_timeout_is_labelled() -> None:
    provider = get_llm_provider(_settings(provider_timeout_seconds=0.05))
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text=HINDI_REPLY, delay=1.0))
    with pytest.raises(LLMProviderError, match="Gemini LLM request timed out"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


# ---- full chain: Bhashini ASR -> RAG -> Gemini -> Bhashini TTS ----


@pytest.mark.asyncio
async def test_full_hindi_chain_asr_rag_gemini_tts(db_session, lsd_corpus) -> None:
    heard = "पशुओं में लम्पी रोग के लक्षण क्या हैं?"

    def bhashini(request: httpx.Request) -> httpx.Response:
        task = json.loads(request.content)["pipelineTasks"][0]["taskType"]
        if task == "asr":
            return httpx.Response(200, json={"pipelineResponse": [{"output": [{"source": heard}]}]})
        wav = make_tone_wav(duration_seconds=0.5, sample_rate=48000)
        return httpx.Response(200, json={"pipelineResponse": [{"audio": [{"audioContent": base64.b64encode(wav).decode()}]}]})

    def client() -> BhashiniClient:
        return BhashiniClient(api_key="fake-bhashini", transport=httpx.MockTransport(bhashini))

    settings = _settings(ai_system_prompt="Base prompt.", rag_similarity_threshold=0.99)
    stt = BhashiniSTTProvider(client=client(), service_id="ai4bharat/conformer-hi-gpu--t4")
    tts = BhashiniTTSProvider(client=client(), service_id="Bhashini/IITM/TTS")
    llm = get_llm_provider(settings)
    llm._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text=HINDI_REPLY))

    session = await create_session(db_session, language="hi")
    try:
        transcription = await stt.transcribe(make_tone_wav(duration_seconds=1.0, sample_rate=8000), language="hi")
        turn = await handle_text_turn(
            db_session,
            session,
            transcription.text,
            settings=settings,
            embedding_provider=MockEmbeddingProvider(dimensions=1536),
            llm_provider=llm,
        )
        speech = await tts.synthesize(turn.assistant_message.text, language="hi")
        playback = read_wav_info(normalize_for_asterisk_playback(speech.audio_bytes))

        assert turn.user_message.text == heard
        assert turn.assistant_message.text == HINDI_REPLY
        sent = llm._client.chat.completions.calls[0]
        assert LANGUAGE_POLICIES["hi"] in sent["messages"][0]["content"]
        assert sent["messages"][-1] == {"role": "user", "content": heard}
        assert (playback.sample_rate, playback.channels, playback.sample_width) == (8000, 1, 2)
    finally:
        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()
