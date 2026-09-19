"""Provider connections: kept warm between turns, bounded in time, never sending a secret early.

Measured from a development machine, each cold connection to a provider costs
DNS 13-190 ms + TCP ~60 ms + TLS ~70 ms (one outlier 563 ms). The first question
of a call used to pay that for ASR, the LLM and TTS. The connections are now
opened while the welcome message plays and kept alive between turns, and each
provider has its own, shorter timeout so a stuck one cannot hold a caller for
half a minute.
"""

import httpx
import pytest

from app.core.config import Settings
from app.providers.bhashini.client import BhashiniClient
from app.providers.llm.base import LLMProvider
from app.providers.llm.factory import get_llm_provider
from app.providers.llm.openai_provider import OpenAILLMProvider
from app.providers.stt.factory import get_stt_provider
from app.providers.tts.factory import get_tts_provider

SECRET = "sk-test-secret-value"


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "development",
        "ai_language": "hi",
        "bhashini_inference_api_key": SECRET,
        "gemini_api_key": SECRET,
        "llm_api_key": SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_bhashini_warm_up_makes_one_cheap_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(405)  # the endpoint's answer is irrelevant

    client = BhashiniClient(api_key=SECRET, transport=httpx.MockTransport(handler))
    await client.warm_up()
    assert [r.method for r in seen] == ["HEAD"]
    await client.aclose()


@pytest.mark.asyncio
async def test_the_warm_up_request_carries_no_credentials() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = BhashiniClient(api_key=SECRET, transport=httpx.MockTransport(handler))
    await client.warm_up()
    assert "authorization" not in {k.lower() for k in seen[0].headers}
    assert SECRET not in str(seen[0].url)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), RuntimeError("boom")])
async def test_a_failed_warm_up_never_raises(failure) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    client = BhashiniClient(api_key=SECRET, transport=httpx.MockTransport(handler))
    await client.warm_up()  # returns normally: the real request will report any real problem
    await client.aclose()


@pytest.mark.asyncio
async def test_the_default_provider_warm_up_is_a_harmless_no_op() -> None:
    class Bare(LLMProvider):
        provider_name = "bare"

        async def generate_response(self, *, system_prompt, history, retrieved_context):  # noqa: ANN001
            raise NotImplementedError

    await Bare().warm_up()


@pytest.mark.asyncio
async def test_openai_compatible_llm_warm_up_never_raises_without_the_package_or_network() -> None:
    provider = OpenAILLMProvider(api_key=SECRET, model="m", timeout_seconds=1.0, base_url="http://127.0.0.1:9/")
    await provider.warm_up()  # nothing listens on port 9: must be swallowed


def test_each_provider_gets_its_own_shorter_timeout() -> None:
    settings = _settings(stt_provider="bhashini", tts_provider="bhashini", llm_provider="gemini",
                         provider_timeout_seconds=30.0)
    assert get_stt_provider(settings)._client._timeout_seconds == settings.stt_timeout_seconds == 15.0
    assert get_tts_provider(settings)._client._timeout_seconds == settings.tts_timeout_seconds == 12.0
    assert get_llm_provider(settings)._timeout_seconds == settings.llm_timeout_seconds == 12.0


def test_the_overall_provider_timeout_still_caps_the_per_stage_timeouts() -> None:
    settings = _settings(stt_provider="bhashini", provider_timeout_seconds=5.0)
    assert get_stt_provider(settings)._client._timeout_seconds == 5.0


def test_connections_are_kept_alive_for_the_configured_time() -> None:
    settings = _settings(stt_provider="bhashini", llm_provider="gemini", http_keepalive_seconds=45.0)
    assert get_stt_provider(settings)._client._keepalive_expiry_seconds == 45.0
    assert get_llm_provider(settings)._keepalive_expiry_seconds == 45.0


def test_the_llm_sdk_retries_once_not_twice() -> None:
    """The SDK honours a server's Retry-After (up to a minute): two retries could
    leave a phone caller in silence far longer than they will wait."""
    provider = OpenAILLMProvider(api_key=SECRET, model="m", timeout_seconds=12.0)
    client = provider._get_client()
    assert client.max_retries == 1
