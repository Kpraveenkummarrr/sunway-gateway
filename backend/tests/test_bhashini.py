"""Structural tests for the Bhashini client, Hindi ASR/TTS providers, their
factory wiring, Hindi-only enforcement, and the audio conversion around
them. All HTTP goes through httpx.MockTransport — no network, no key, no
cost. Every Settings() here uses _env_file=None so a real backend/.env on a
deployed host can never leak into (or be exercised by) these tests.
"""

import base64
import json
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from app.core.config import LANGUAGE_POLICIES, Settings
from app.models.ai import AIMessage
from app.providers.bhashini.client import BhashiniClient, BhashiniError
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.base import LLMProvider, LLMResponse
from app.providers.stt.base import STTProviderError, TranscriptionResult
from app.providers.stt.bhashini_provider import BhashiniSTTProvider
from app.providers.stt.factory import get_stt_provider
from app.providers.stt.mock import MockSTTProvider
from app.providers.tts.base import SynthesisResult, TTSProvider, TTSProviderError
from app.providers.tts.bhashini_provider import BhashiniTTSProvider
from app.providers.tts.factory import get_tts_provider
from app.providers.tts.mock import MockTTSProvider
from app.services.audio import normalize_for_asterisk_playback, read_wav_info, resample_for_asr
from app.services.call_controller import AICallController, CallState
from app.services.conversation import create_session, handle_text_turn
from tests.audio_fixtures import make_tone_wav
from tests.fake_ari import FakeAriClient

FAKE_KEY = "fake-inference-key-SHOULD-NEVER-APPEAR-1234567890"
URL = "https://dhruva.example.test/services/inference/pipeline"
HINDI_TRANSCRIPT = "मेरा नाम महीर है"


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        ai_language="hi",
        stt_provider="bhashini",
        tts_provider="bhashini",
        bhashini_inference_url=URL,
        bhashini_inference_api_key=FAKE_KEY,
        bhashini_asr_service_id="ai4bharat/conformer-hi-gpu--t4",
        bhashini_tts_service_id="Bhashini/IITM/TTS",
        bhashini_tts_gender="female",
        provider_timeout_seconds=5.0,
    )
    base.update(overrides)
    return Settings(**base)


class _Recorder:
    """httpx.MockTransport handler that records requests and replays a response."""

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return self.response

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


def _client(recorder: _Recorder) -> BhashiniClient:
    return BhashiniClient(api_key=FAKE_KEY, inference_url=URL, transport=httpx.MockTransport(recorder))


def _asr_ok(text: str = HINDI_TRANSCRIPT) -> httpx.Response:
    return httpx.Response(
        200, json={"pipelineResponse": [{"taskType": "asr", "output": [{"source": text}], "audio": None}]}
    )


def _tts_ok(wav: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "pipelineResponse": [
                {
                    "taskType": "tts",
                    "config": {"audioFormat": "wav", "samplingRate": 48000},
                    "output": None,
                    "audio": [{"audioContent": base64.b64encode(wav).decode(), "audioUri": None}],
                }
            ]
        },
    )


# ---- shared client ----


@pytest.mark.asyncio
async def test_client_sends_raw_key_in_authorization_header_and_returns_first_result() -> None:
    recorder = _Recorder(_asr_ok())
    result = await _client(recorder).run_task(task_config={"taskType": "asr"}, input_data={})

    request = recorder.requests[0]
    assert request.headers["Authorization"] == FAKE_KEY  # raw, no "Bearer " prefix
    assert str(request.url) == URL
    assert recorder.body["pipelineTasks"] == [{"taskType": "asr"}]
    assert result["output"][0]["source"] == HINDI_TRANSCRIPT


def test_client_requires_api_key() -> None:
    with pytest.raises(BhashiniError, match="BHASHINI_INFERENCE_API_KEY"):
        BhashiniClient(api_key="", inference_url=URL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (400, "bad request"),
        (401, "authentication failed"),
        (403, "forbidden"),
        (422, "payload rejected"),
        (429, "rate limited"),
        (500, "server error"),
        (503, "server error"),
    ],
)
async def test_client_http_errors_are_classified_and_never_leak_the_key(status: int, fragment: str) -> None:
    # A hostile/buggy server echoing the key back must still not leak it.
    recorder = _Recorder(httpx.Response(status, text=f'{{"detail": "bad key {FAKE_KEY}"}}'))
    with pytest.raises(BhashiniError) as excinfo:
        await _client(recorder).run_task(task_config={}, input_data={})

    assert excinfo.value.status_code == status
    assert fragment in str(excinfo.value)
    assert FAKE_KEY not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


@pytest.mark.asyncio
async def test_client_timeout() -> None:
    recorder = _Recorder(exc=httpx.ReadTimeout("slow"))
    with pytest.raises(BhashiniError, match="timed out") as excinfo:
        await _client(recorder).run_task(task_config={}, input_data={})
    assert excinfo.value.status_code is None
    assert excinfo.value.__cause__ is None  # request object not chained into tracebacks


@pytest.mark.asyncio
async def test_client_connection_error() -> None:
    recorder = _Recorder(exc=httpx.ConnectError("name resolution failed"))
    with pytest.raises(BhashiniError, match="connection failed") as excinfo:
        await _client(recorder).run_task(task_config={}, input_data={})
    assert FAKE_KEY not in str(excinfo.value)


@pytest.mark.asyncio
async def test_client_invalid_json() -> None:
    recorder = _Recorder(httpx.Response(200, text="<html>gateway error</html>"))
    with pytest.raises(BhashiniError, match="non-JSON"):
        await _client(recorder).run_task(task_config={}, input_data={})


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"pipelineResponse": []}, {"pipelineResponse": "x"}, ["not", "a", "dict"]])
async def test_client_missing_pipeline_response(body) -> None:
    recorder = _Recorder(httpx.Response(200, json=body))
    with pytest.raises(BhashiniError, match="pipelineResponse"):
        await _client(recorder).run_task(task_config={}, input_data={})


# ---- ASR provider ----


@pytest.mark.asyncio
async def test_asr_sends_hindi_16khz_base64_wav_and_returns_devanagari_transcript() -> None:
    recorder = _Recorder(_asr_ok())
    provider = BhashiniSTTProvider(client=_client(recorder), service_id="ai4bharat/conformer-hi-gpu--t4")
    telephone_audio = make_tone_wav(duration_seconds=1.0, sample_rate=8000)

    result = await provider.transcribe(telephone_audio, language="hi")

    assert isinstance(result, TranscriptionResult)
    assert result.text == HINDI_TRANSCRIPT
    assert result.language == "hi"
    assert result.duration_seconds == pytest.approx(1.0, abs=0.01)

    # Exactly the live-verified request: no inputData.input (source=null -> 422).
    assert recorder.body["pipelineTasks"] == [
        {
            "taskType": "asr",
            "config": {"serviceId": "ai4bharat/conformer-hi-gpu--t4", "language": {"sourceLanguage": "hi"}},
        }
    ]
    assert set(recorder.body["inputData"]) == {"audio"}
    assert len(recorder.body["inputData"]["audio"]) == 1
    assert set(recorder.body["inputData"]["audio"][0]) == {"audioContent"}

    uploaded = base64.b64decode(recorder.body["inputData"]["audio"][0]["audioContent"])
    info = read_wav_info(uploaded)
    assert (info.sample_rate, info.channels, info.sample_width) == (16000, 1, 2)


@pytest.mark.asyncio
async def test_asr_uploads_speech_without_the_recordings_silence() -> None:
    import io
    import wave

    rate = 8000
    speech = make_tone_wav(duration_seconds=1.0, sample_rate=rate)
    with wave.open(io.BytesIO(speech), "rb") as wf:
        speech_frames = wf.readframes(wf.getnframes())
    recording = io.BytesIO()
    with wave.open(recording, "wb") as wf:  # 1 s pause, 1 s speech, 2 s end-of-speech silence
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * rate + speech_frames + b"\x00\x00" * 2 * rate)

    recorder = _Recorder(_asr_ok())
    provider = BhashiniSTTProvider(client=_client(recorder), service_id="ai4bharat/conformer-hi-gpu--t4")
    await provider.transcribe(recording.getvalue(), language="hi")

    uploaded = read_wav_info(base64.b64decode(recorder.body["inputData"]["audio"][0]["audioContent"]))
    assert uploaded.sample_rate == 16000
    assert uploaded.duration_seconds == pytest.approx(1.5, abs=0.1)  # was 4.0 s untrimmed
    assert set(recorder.body["inputData"]) == {"audio"}  # request shape unchanged


@pytest.mark.asyncio
async def test_asr_rejects_non_hindi_language_without_calling_api() -> None:
    recorder = _Recorder(_asr_ok())
    provider = BhashiniSTTProvider(client=_client(recorder), service_id="svc")
    with pytest.raises(STTProviderError, match="'hi' only"):
        await provider.transcribe(make_tone_wav(), language="en")
    assert recorder.requests == []


def test_asr_constructor_rejects_non_hindi_configuration() -> None:
    with pytest.raises(STTProviderError, match="AI_LANGUAGE must be 'hi'"):
        BhashiniSTTProvider(client=_client(_Recorder()), service_id="svc", language="en")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"pipelineResponse": [{"taskType": "asr", "output": []}]}),
        httpx.Response(200, json={"pipelineResponse": [{"taskType": "asr", "output": [{"source": None}]}]}),
    ],
)
async def test_asr_missing_transcript_field_is_an_error(response) -> None:
    provider = BhashiniSTTProvider(client=_client(_Recorder(response)), service_id="svc")
    with pytest.raises(STTProviderError, match="output\\[0\\].source"):
        await provider.transcribe(make_tone_wav(), language="hi")


@pytest.mark.asyncio
async def test_asr_blank_transcript_is_an_error() -> None:
    provider = BhashiniSTTProvider(client=_client(_Recorder(_asr_ok("   "))), service_id="svc")
    with pytest.raises(STTProviderError, match="no text"):
        await provider.transcribe(make_tone_wav(), language="hi")


@pytest.mark.asyncio
async def test_asr_non_wav_audio_is_rejected_before_any_request() -> None:
    recorder = _Recorder(_asr_ok())
    provider = BhashiniSTTProvider(client=_client(recorder), service_id="svc")
    with pytest.raises(STTProviderError, match="not usable PCM WAV"):
        await provider.transcribe(b"definitely not a wav file", language="hi")
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_asr_provider_error_does_not_leak_key() -> None:
    recorder = _Recorder(httpx.Response(401, text=f"invalid token {FAKE_KEY}"))
    provider = BhashiniSTTProvider(client=_client(recorder), service_id="svc")
    with pytest.raises(STTProviderError) as excinfo:
        await provider.transcribe(make_tone_wav(), language="hi")
    assert "401" in str(excinfo.value)
    assert FAKE_KEY not in str(excinfo.value)


# ---- TTS provider ----


@pytest.mark.asyncio
async def test_tts_sends_hindi_female_request_and_decodes_base64_wav() -> None:
    bhashini_wav = make_tone_wav(duration_seconds=0.5, sample_rate=48000)
    recorder = _Recorder(_tts_ok(bhashini_wav))
    provider = BhashiniTTSProvider(client=_client(recorder), service_id="Bhashini/IITM/TTS", gender="female")

    result = await provider.synthesize("नमस्ते, आपका स्वागत है।", language="hi")

    assert isinstance(result, SynthesisResult)
    assert result.audio_format == "wav"
    assert result.audio_bytes == bhashini_wav
    assert read_wav_info(result.audio_bytes).sample_rate == 48000  # native rate, normalized downstream

    task = recorder.body["pipelineTasks"][0]
    assert task["taskType"] == "tts"
    assert task["config"] == {
        "language": {"sourceLanguage": "hi"},
        "serviceId": "Bhashini/IITM/TTS",
        "gender": "female",
    }
    assert recorder.body["inputData"]["input"] == [{"source": "नमस्ते, आपका स्वागत है।"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"pipelineResponse": [{"taskType": "tts", "audio": []}]}),
        httpx.Response(200, json={"pipelineResponse": [{"taskType": "tts", "audio": [{"audioContent": None}]}]}),
        httpx.Response(200, json={"pipelineResponse": [{"taskType": "tts", "audio": [{"audioContent": ""}]}]}),
    ],
)
async def test_tts_missing_audio_content_is_an_error(response) -> None:
    provider = BhashiniTTSProvider(client=_client(_Recorder(response)), service_id="svc")
    with pytest.raises(TTSProviderError, match="audioContent"):
        await provider.synthesize("नमस्ते", language="hi")


@pytest.mark.asyncio
async def test_tts_invalid_base64_is_an_error() -> None:
    response = httpx.Response(200, json={"pipelineResponse": [{"audio": [{"audioContent": "@@not-base64@@"}]}]})
    provider = BhashiniTTSProvider(client=_client(_Recorder(response)), service_id="svc")
    with pytest.raises(TTSProviderError, match="not valid base64"):
        await provider.synthesize("नमस्ते", language="hi")


@pytest.mark.asyncio
async def test_tts_non_wav_payload_is_an_error() -> None:
    response = _tts_ok(b"ID3 this is an mp3 not a wav")
    provider = BhashiniTTSProvider(client=_client(_Recorder(response)), service_id="svc")
    with pytest.raises(TTSProviderError, match="readable PCM WAV"):
        await provider.synthesize("नमस्ते", language="hi")


@pytest.mark.asyncio
async def test_tts_rejects_empty_text_and_non_hindi_without_calling_api() -> None:
    recorder = _Recorder(_tts_ok(make_tone_wav()))
    provider = BhashiniTTSProvider(client=_client(recorder), service_id="svc")
    with pytest.raises(TTSProviderError, match="empty"):
        await provider.synthesize("   ", language="hi")
    with pytest.raises(TTSProviderError, match="'hi' only"):
        await provider.synthesize("hello", language="en")
    assert recorder.requests == []


def test_tts_constructor_validates_gender_and_language() -> None:
    with pytest.raises(TTSProviderError, match="BHASHINI_TTS_GENDER"):
        BhashiniTTSProvider(client=_client(_Recorder()), service_id="svc", gender="robot")
    with pytest.raises(TTSProviderError, match="AI_LANGUAGE must be 'hi'"):
        BhashiniTTSProvider(client=_client(_Recorder()), service_id="svc", language="en")


# ---- factories ----


def test_factories_build_bhashini_providers_from_settings() -> None:
    settings = _settings()
    stt = get_stt_provider(settings)
    tts = get_tts_provider(settings)
    assert isinstance(stt, BhashiniSTTProvider)
    assert isinstance(tts, BhashiniTTSProvider)
    assert stt._service_id == "ai4bharat/conformer-hi-gpu--t4"
    assert tts._service_id == "Bhashini/IITM/TTS"
    assert tts._gender == "female"


def test_factories_fail_clearly_without_bhashini_key() -> None:
    settings = _settings(bhashini_inference_api_key="")
    with pytest.raises(STTProviderError, match="BHASHINI_INFERENCE_API_KEY"):
        get_stt_provider(settings)
    with pytest.raises(TTSProviderError, match="BHASHINI_INFERENCE_API_KEY"):
        get_tts_provider(settings)


def test_factories_refuse_bhashini_when_language_is_not_hindi() -> None:
    settings = _settings(ai_language="en")
    with pytest.raises(STTProviderError, match="AI_LANGUAGE"):
        get_stt_provider(settings)
    with pytest.raises(TTSProviderError, match="AI_LANGUAGE"):
        get_tts_provider(settings)


def test_factories_keep_mock_available_and_reject_unknown() -> None:
    assert isinstance(get_stt_provider(_settings(stt_provider="mock")), MockSTTProvider)
    assert isinstance(get_tts_provider(_settings(tts_provider="mock")), MockTTSProvider)
    with pytest.raises(STTProviderError, match="No STT provider configured"):
        get_stt_provider(_settings(stt_provider="whisperx"))
    with pytest.raises(TTSProviderError, match="No TTS provider configured"):
        get_tts_provider(_settings(tts_provider="polly"))


# ---- audio conversion ----


def test_resample_for_asr_converts_8khz_telephone_audio_to_16khz() -> None:
    converted = resample_for_asr(make_tone_wav(duration_seconds=2.0, sample_rate=8000))
    info = read_wav_info(converted)
    assert (info.sample_rate, info.channels, info.sample_width) == (16000, 1, 2)
    assert info.duration_seconds == pytest.approx(2.0, abs=0.01)


def test_resample_for_asr_is_noop_for_16khz_mono() -> None:
    already = make_tone_wav(sample_rate=16000)
    assert resample_for_asr(already) is already


@pytest.mark.parametrize("channels", [1, 2])
def test_bhashini_48khz_tts_normalizes_to_asterisk_format(channels: int) -> None:
    source = make_tone_wav(duration_seconds=1.0, sample_rate=48000, channels=channels)
    info = read_wav_info(normalize_for_asterisk_playback(source))
    assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)
    assert info.duration_seconds == pytest.approx(1.0, abs=0.01)


# ---- Hindi-only policy ----


def test_hindi_caller_messages_are_devanagari_by_default_and_overridable() -> None:
    settings = _settings()
    for kind in ("welcome", "error", "goodbye"):
        message = settings.caller_message(kind)
        assert any("ऀ" <= ch <= "ॿ" for ch in message), f"{kind} is not Devanagari: {message}"

    assert _settings(ai_welcome_message="स्वागत है").caller_message("welcome") == "स्वागत है"
    assert "Hello" in _settings(ai_language="en").caller_message("welcome")


def test_system_prompt_adds_hindi_policy_only_for_hindi() -> None:
    # Persona off: this test is about the language policy alone (the helpline
    # persona has its own tests in test_helpline_persona.py).
    settings = _settings(ai_system_prompt="Base prompt.", ai_persona="")
    hindi = settings.system_prompt_for("hi")
    assert hindi.startswith("Base prompt.")
    assert LANGUAGE_POLICIES["hi"] in hindi
    assert "natural, spoken Hindi" in hindi and "markdown" in hindi
    assert settings.system_prompt_for("en") == "Base prompt."


class _CapturingLLM(LLMProvider):
    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:
        self.system_prompts.append(system_prompt)
        return LLMResponse(text="नमस्ते, मैं आपकी कैसे मदद कर सकती हूँ?")


@pytest.mark.asyncio
async def test_hindi_session_sends_hindi_policy_to_llm(db_session) -> None:
    settings = _settings(ai_system_prompt="Base prompt.", rag_similarity_threshold=0.99, llm_provider="mock")
    llm = _CapturingLLM()
    session = await create_session(db_session, language="hi")
    try:
        turn = await handle_text_turn(
            db_session,
            session,
            "आपका समय क्या है?",
            settings=settings,
            embedding_provider=MockEmbeddingProvider(dimensions=1536),
            llm_provider=llm,
        )
        assert LANGUAGE_POLICIES["hi"] in llm.system_prompts[0]
        assert turn.assistant_message.text.startswith("नमस्ते")
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()


# ---- call controller playback normalization ----


class _StaticTTS(TTSProvider):
    def __init__(self, result: SynthesisResult) -> None:
        self.result = result

    async def synthesize(self, text, *, voice=None, language=None) -> SynthesisResult:
        return self.result


def _controller_with_tts(tmp_path: Path, tts: TTSProvider) -> tuple[AICallController, FakeAriClient, CallState]:
    settings = _settings(
        stt_provider="mock",
        tts_provider="mock",
        asterisk_recording_spool_path=str(tmp_path / "recording"),
        provider_timeout_seconds=2.0,
    )
    ari = FakeAriClient()
    controller = AICallController(
        ari=ari,
        settings=settings,
        session_factory=None,
        embedding_provider=MockEmbeddingProvider(dimensions=1536),
        llm_provider=_CapturingLLM(),
        stt_provider=MockSTTProvider(),
        tts_provider=tts,
    )
    ari.on_play_started = lambda pid: controller._on_playback_finished({"playback": {"id": pid}})
    return controller, ari, CallState("PJSIP/test-1", uuid.uuid4(), uuid.uuid4())


@pytest.mark.asyncio
async def test_controller_always_plays_bhashini_48khz_audio_as_8khz_mono_pcm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tts = _StaticTTS(SynthesisResult(make_tone_wav(duration_seconds=0.5, sample_rate=48000), "wav"))
        controller, ari, state = _controller_with_tts(tmp_path, tts)

        await controller._synthesize_and_play(state, "नमस्ते")

        assert len(ari.played) == 1
        written = list((tmp_path / "sounds" / "ai-agent").glob("*.wav"))
        assert len(written) == 0  # ephemeral clip is removed after completion
        # Absolute path: Asterisk resolves relative sound names against its
        # data dir, where these files don't live.
        media = ari.played[0][1]
        assert Path(media.removeprefix("sound:")).is_absolute()
        info = read_wav_info(ari.played_audio[media])
        assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [SynthesisResult(b"not a wav at all", "wav"), SynthesisResult(b"ID3...", "mp3")],
)
async def test_controller_never_plays_unnormalizable_tts_audio(result: SynthesisResult) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = _controller_with_tts(Path(tmp), _StaticTTS(result))
        with pytest.raises(TTSProviderError):
            await controller._synthesize_and_play(state, "नमस्ते")
        assert ari.played == []
