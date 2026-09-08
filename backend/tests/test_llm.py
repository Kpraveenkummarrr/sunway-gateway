import pytest

from app.core.config import Settings
from app.providers.llm.base import LLMMessage, LLMProviderError
from app.providers.llm.factory import get_llm_provider
from app.providers.llm.mock import NO_CONTEXT_RESPONSE, MockLLMProvider


@pytest.mark.asyncio
async def test_mock_llm_returns_no_context_response_when_nothing_retrieved() -> None:
    provider = MockLLMProvider()
    response = await provider.generate_response(
        system_prompt="You are a helpful assistant.",
        history=[LLMMessage(role="user", content="what are your hours?")],
        retrieved_context=None,
    )
    assert response.text == NO_CONTEXT_RESPONSE


@pytest.mark.asyncio
async def test_mock_llm_uses_retrieved_context_when_present() -> None:
    provider = MockLLMProvider()
    response = await provider.generate_response(
        system_prompt="You are a helpful assistant.",
        history=[LLMMessage(role="user", content="what are your hours?")],
        retrieved_context="[Source: hours.pdf]\nWe are open 9am-5pm Monday to Friday.",
    )
    assert "9am-5pm" in response.text or "hours.pdf" in response.text
    assert response.text != NO_CONTEXT_RESPONSE


@pytest.mark.asyncio
async def test_mock_llm_records_system_prompt_passed() -> None:
    provider = MockLLMProvider()
    prompt = "You are a very specific system prompt for testing."
    await provider.generate_response(
        system_prompt=prompt,
        history=[LLMMessage(role="user", content="hi")],
        retrieved_context=None,
    )
    assert provider.last_system_prompt == prompt


@pytest.mark.asyncio
async def test_mock_llm_records_conversation_history_passed() -> None:
    provider = MockLLMProvider()
    history = [
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="assistant", content="hello, how can I help?"),
        LLMMessage(role="user", content="what are your hours?"),
    ]
    await provider.generate_response(system_prompt="prompt", history=history, retrieved_context=None)
    assert provider.last_history == history


@pytest.mark.asyncio
async def test_mock_llm_records_retrieved_context_passed() -> None:
    provider = MockLLMProvider()
    context = "[Source: doc.pdf]\nSome fact."
    await provider.generate_response(
        system_prompt="prompt", history=[LLMMessage(role="user", content="hi")], retrieved_context=context
    )
    assert provider.last_retrieved_context == context


@pytest.mark.asyncio
async def test_mock_llm_rejects_empty_history() -> None:
    provider = MockLLMProvider()
    with pytest.raises(LLMProviderError):
        await provider.generate_response(system_prompt="prompt", history=[], retrieved_context=None)


@pytest.mark.asyncio
async def test_mock_llm_simulated_provider_failure() -> None:
    provider = MockLLMProvider(simulate_failure=True)
    with pytest.raises(LLMProviderError):
        await provider.generate_response(
            system_prompt="prompt", history=[LLMMessage(role="user", content="hi")], retrieved_context=None
        )


def test_factory_raises_clear_error_when_unconfigured() -> None:
    settings = Settings(llm_provider="")
    with pytest.raises(LLMProviderError):
        get_llm_provider(settings)


def test_factory_returns_mock_when_explicitly_selected() -> None:
    settings = Settings(llm_provider="mock")
    provider = get_llm_provider(settings)
    assert isinstance(provider, MockLLMProvider)


def test_factory_raises_when_openai_selected_without_api_key() -> None:
    settings = Settings(llm_provider="openai", llm_api_key="")
    with pytest.raises(LLMProviderError):
        get_llm_provider(settings)


def test_factory_rejects_unknown_provider_name() -> None:
    settings = Settings(llm_provider="some-unsupported-vendor")
    with pytest.raises(LLMProviderError):
        get_llm_provider(settings)
