"""LLM_PROVIDER=sarvam_m — a local Sarvam-M model as an alternative to
Gemini.

No real model weights are downloaded or loaded here (this dev machine's own
hardware check reports UNSUPPORTED HARDWARE for the real 24B model — see
test_hardware_check.py). What is tested is the provider's own logic: it
must never attempt to load anything on hardware that can't run it, must
never invent a model file, must wrap llama.cpp exactly like the OpenAI
provider wraps its SDK, and must feed the model the identical
system-prompt/context shape Gemini receives.
"""

import asyncio
import sys
import time
import types

import pytest

from app.core.config import Settings
from app.providers.llm.base import LLMMessage, LLMProviderError
from app.providers.llm.factory import get_llm_provider
from app.providers.llm.prompt_context import augment_system_prompt_with_context
from app.providers.llm.sarvam_m_provider import SarvamMProvider
from app.services.hardware_check import HardwareReport, Verdict
import app.providers.llm.sarvam_m_provider as sarvam_module


def _supported_verdict(*, backend="llama.cpp with full GPU offload", gpu_layers=-1) -> Verdict:
    return Verdict(verdict="SUPPORTED", reasons=["test stub"], recommended_backend=backend, recommended_gpu_layers=gpu_layers)


def _unsupported_verdict() -> Verdict:
    return Verdict(verdict="UNSUPPORTED HARDWARE", reasons=["not enough RAM", "no GPU"], recommended_backend=None, recommended_gpu_layers=0)


def _stub_report() -> HardwareReport:
    return HardwareReport(
        os_name="Linux", cpu_name="stub", cpu_cores=8, cpu_threads=8,
        total_ram_mb=32768, available_ram_mb=24576, free_disk_mb=200 * 1024, gpus=[],
    )


class FakeLlamaModel:
    """Stands in for llama_cpp.Llama. Records what it was built and called
    with so the test can assert the provider talks to it correctly."""

    instances: list["FakeLlamaModel"] = []

    def __init__(self, *, model_path, n_ctx, n_gpu_layers, verbose=False):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.calls: list[dict] = []
        self.reply_text = "जी हाँ, लम्पी रोग मक्खियों से फैलता है।"
        self.finish_reason = "stop"
        self.raise_on_call: Exception | None = None
        self.sleep_seconds = 0.0
        FakeLlamaModel.instances.append(self)

    def create_chat_completion(self, *, messages, max_tokens):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        if self.raise_on_call:
            raise self.raise_on_call
        return {
            "choices": [
                {"message": {"content": self.reply_text}, "finish_reason": self.finish_reason}
            ]
        }


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    FakeLlamaModel.instances.clear()
    yield
    FakeLlamaModel.instances.clear()


@pytest.fixture
def fake_llama_cpp_module(monkeypatch):
    """Injects a fake `llama_cpp` package so the provider's `from llama_cpp
    import Llama` succeeds without the real (large, unavailable) package."""
    module = types.ModuleType("llama_cpp")
    module.Llama = FakeLlamaModel
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    return module


def _history() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="लम्पी रोग क्या है?")]


# ---- construction / configuration errors ----


def test_missing_model_path_is_rejected_immediately_no_hardware_check_needed() -> None:
    with pytest.raises(LLMProviderError, match="SARVAM_M_MODEL_PATH"):
        SarvamMProvider(model_path="")


def test_provider_name_identifies_it_as_local() -> None:
    provider = SarvamMProvider(model_path="/nonexistent/model.gguf")
    assert provider.provider_name == "Sarvam-M local"


# ---- hardware gate ----


@pytest.mark.asyncio
async def test_unsupported_hardware_blocks_loading_before_touching_llama_cpp(monkeypatch, tmp_path) -> None:
    model_file = tmp_path / "sarvam-m.Q4_K_M.gguf"
    model_file.write_bytes(b"not a real model, must never be read")
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _unsupported_verdict())
    # No fake llama_cpp registered: if the provider tried to import it, this
    # would fail with ModuleNotFoundError instead of the expected message.
    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)

    provider = SarvamMProvider(model_path=str(model_file))
    with pytest.raises(LLMProviderError, match="UNSUPPORTED HARDWARE"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


@pytest.mark.asyncio
async def test_hardware_check_can_be_explicitly_skipped_for_a_lab_test(
    monkeypatch, tmp_path, fake_llama_cpp_module
) -> None:
    model_file = tmp_path / "small-test-model.gguf"
    model_file.write_bytes(b"stub")

    def _fail_if_called(*a, **k):
        raise AssertionError("hardware check must not run when require_hardware_check=False")

    monkeypatch.setattr(sarvam_module, "detect_hardware", _fail_if_called)
    provider = SarvamMProvider(model_path=str(model_file), require_hardware_check=False)

    response = await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)
    assert response.text


# ---- missing model file / missing package ----


@pytest.mark.asyncio
async def test_missing_model_file_is_a_clear_error_not_a_download_attempt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    missing = tmp_path / "does-not-exist.gguf"

    provider = SarvamMProvider(model_path=str(missing))
    with pytest.raises(LLMProviderError, match="does not exist"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


@pytest.mark.asyncio
async def test_missing_llama_cpp_package_is_a_clear_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    model_file = tmp_path / "sarvam-m.gguf"
    model_file.write_bytes(b"stub")

    provider = SarvamMProvider(model_path=str(model_file))
    with pytest.raises(LLMProviderError, match="llama-cpp-python"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


# ---- successful generation against a stubbed backend ----


@pytest.mark.asyncio
async def test_a_supported_machine_loads_and_generates(monkeypatch, tmp_path, fake_llama_cpp_module) -> None:
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict(gpu_layers=-1))
    model_file = tmp_path / "sarvam-m.Q4_K_M.gguf"
    model_file.write_bytes(b"stub")

    provider = SarvamMProvider(model_path=str(model_file), context_tokens=8192, max_tokens=250, gpu_layers=-1)
    response = await provider.generate_response(
        system_prompt="Base prompt.",
        history=_history(),
        retrieved_context="Lumpy Skin Disease spreads through biting flies.",
    )

    assert response.text == "जी हाँ, लम्पी रोग मक्खियों से फैलता है।"
    assert response.finish_reason == "stop"
    loaded = FakeLlamaModel.instances[0]
    assert loaded.model_path == str(model_file)
    assert loaded.n_ctx == 8192
    assert loaded.n_gpu_layers == -1
    assert loaded.calls[0]["max_tokens"] == 250


@pytest.mark.asyncio
async def test_the_model_is_loaded_once_and_reused_across_turns(monkeypatch, tmp_path, fake_llama_cpp_module) -> None:
    calls = {"detect": 0}

    def _counting_detect():
        calls["detect"] += 1
        return _stub_report()

    monkeypatch.setattr(sarvam_module, "detect_hardware", _counting_detect)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    model_file = tmp_path / "sarvam-m.gguf"
    model_file.write_bytes(b"stub")

    provider = SarvamMProvider(model_path=str(model_file))
    await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)
    await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)

    assert calls["detect"] == 1, "hardware check and model load must happen once, not per turn"
    assert len(FakeLlamaModel.instances) == 1


# ---- fairness: identical context shape as Gemini/OpenAI ----


@pytest.mark.asyncio
async def test_sarvam_m_receives_the_exact_same_augmented_prompt_as_gemini(
    monkeypatch, tmp_path, fake_llama_cpp_module
) -> None:
    """Part 3's requirement made concrete: the system message handed to the
    local model must be byte-identical to the one OpenAILLMProvider (used
    for Gemini) builds from the same inputs."""
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    model_file = tmp_path / "sarvam-m.gguf"
    model_file.write_bytes(b"stub")

    system_prompt = "You are the LUVAS helpline."
    context = "Lumpy Skin Disease spreads through biting flies, mosquitoes and ticks."
    history = [
        LLMMessage(role="user", content="लम्पी रोग कैसे फैलता है?"),
    ]

    provider = SarvamMProvider(model_path=str(model_file))
    await provider.generate_response(system_prompt=system_prompt, history=history, retrieved_context=context)
    sarvam_system_message = FakeLlamaModel.instances[0].calls[0]["messages"][0]["content"]

    expected = augment_system_prompt_with_context(system_prompt, context)
    assert sarvam_system_message == expected


# ---- error handling ----


@pytest.mark.asyncio
async def test_empty_history_is_rejected_like_every_other_provider(tmp_path) -> None:
    provider = SarvamMProvider(model_path=str(tmp_path / "x.gguf"))
    with pytest.raises(LLMProviderError, match="empty conversation history"):
        await provider.generate_response(system_prompt="p", history=[], retrieved_context=None)


@pytest.mark.asyncio
async def test_an_inference_exception_is_wrapped_not_leaked(monkeypatch, tmp_path, fake_llama_cpp_module) -> None:
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    model_file = tmp_path / "sarvam-m.gguf"
    model_file.write_bytes(b"stub")

    provider = SarvamMProvider(model_path=str(model_file))
    # Force the fake to raise on its first call by pre-registering an
    # instance-configuring subclass.
    original_init = FakeLlamaModel.__init__

    def _init_that_breaks(self, *a, **k):
        original_init(self, *a, **k)
        self.raise_on_call = RuntimeError("stub backend failure")

    monkeypatch.setattr(FakeLlamaModel, "__init__", _init_that_breaks)

    with pytest.raises(LLMProviderError, match="Sarvam-M local inference failed"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


@pytest.mark.asyncio
async def test_a_slow_local_model_times_out_cleanly(monkeypatch, tmp_path, fake_llama_cpp_module) -> None:
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    model_file = tmp_path / "sarvam-m.gguf"
    model_file.write_bytes(b"stub")

    original_init = FakeLlamaModel.__init__

    def _init_that_sleeps(self, *a, **k):
        original_init(self, *a, **k)
        self.sleep_seconds = 0.4

    monkeypatch.setattr(FakeLlamaModel, "__init__", _init_that_sleeps)

    provider = SarvamMProvider(model_path=str(model_file), timeout_seconds=0.05)
    with pytest.raises(LLMProviderError, match="timed out"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


@pytest.mark.asyncio
async def test_an_empty_reply_is_rejected(monkeypatch, tmp_path, fake_llama_cpp_module) -> None:
    monkeypatch.setattr(sarvam_module, "detect_hardware", _stub_report)
    monkeypatch.setattr(sarvam_module, "assess", lambda report, req: _supported_verdict())
    model_file = tmp_path / "sarvam-m.gguf"
    model_file.write_bytes(b"stub")

    original_init = FakeLlamaModel.__init__

    def _init_that_replies_empty(self, *a, **k):
        original_init(self, *a, **k)
        self.reply_text = ""

    monkeypatch.setattr(FakeLlamaModel, "__init__", _init_that_replies_empty)

    provider = SarvamMProvider(model_path=str(model_file))
    with pytest.raises(LLMProviderError, match="empty response"):
        await provider.generate_response(system_prompt="p", history=_history(), retrieved_context=None)


# ---- factory ----


def test_factory_selects_sarvam_m_and_requires_a_model_path() -> None:
    settings = Settings(llm_provider="sarvam_m", sarvam_m_model_path="")
    with pytest.raises(LLMProviderError, match="SARVAM_M_MODEL_PATH"):
        get_llm_provider(settings)


def test_factory_builds_a_sarvam_m_provider_when_a_path_is_configured(tmp_path) -> None:
    settings = Settings(
        llm_provider="sarvam_m",
        sarvam_m_model_path=str(tmp_path / "model.gguf"),
        sarvam_m_context_tokens=2048,
        sarvam_m_gpu_layers=-1,
        llm_max_tokens=300,
    )
    provider = get_llm_provider(settings)
    assert isinstance(provider, SarvamMProvider)
    assert provider.provider_name == "Sarvam-M local"


def test_the_default_provider_is_still_gemini_capable_unaffected_by_sarvam_m() -> None:
    """Adding sarvam_m must not disturb the existing gemini/openai/mock
    selection — LLM_PROVIDER=gemini remains the production default."""
    settings = Settings(llm_provider="gemini", gemini_api_key="fake-key-for-structural-test-only")
    provider = get_llm_provider(settings)
    assert provider.provider_name == "Gemini"
