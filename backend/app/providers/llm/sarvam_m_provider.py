"""Sarvam-M as a local LLM (LLM_PROVIDER=sarvam_m) — an alternative to the
paid Gemini API, run entirely on the operator's own hardware.

Sarvam-M (sarvamai/sarvam-m on Hugging Face) is a 24B-parameter model. That
is not a small download or a small memory footprint, so this provider:

  * never downloads anything itself — the operator places an already
    downloaded GGUF file at SARVAM_M_MODEL_PATH; a missing file is a clear
    configuration error, not a trigger to fetch one;
  * checks the machine's hardware before attempting to load the model (see
    app.services.hardware_check) and refuses with "UNSUPPORTED HARDWARE"
    plus the specific reasons rather than trying and failing opaquely, or
    succeeding technically but too slowly for a live phone call;
  * uses llama.cpp (via the `llama-cpp-python` package) as the inference
    backend, which is the practical choice for running a quantized GGUF
    model on a mix of CPU and consumer GPU — not a hard dependency of the
    rest of the application, imported lazily exactly like the `openai`
    package is for the Gemini/OpenAI provider.

Receives and answers with the exact same system-prompt/context shape as
OpenAILLMProvider (see app.providers.llm.prompt_context) so a Gemini vs
Sarvam-M comparison is testing the model, not two different prompts.
"""

import asyncio
from pathlib import Path

from app.providers.llm.base import LLMMessage, LLMProvider, LLMProviderError, LLMResponse
from app.providers.llm.prompt_context import augment_system_prompt_with_context
from app.services.hardware_check import SARVAM_M_Q4, assess, detect_hardware, format_report

DEFAULT_CONTEXT_TOKENS = 4096


class SarvamMProvider(LLMProvider):
    def __init__(
        self,
        *,
        model_path: str,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        max_tokens: int = 400,
        gpu_layers: int = 0,
        timeout_seconds: float = 30.0,
        require_hardware_check: bool = True,
    ) -> None:
        if not model_path or not model_path.strip():
            raise LLMProviderError(
                "LLM_PROVIDER=sarvam_m but SARVAM_M_MODEL_PATH is not set. Download a Sarvam-M "
                "GGUF (e.g. a Q4_K_M quantization of sarvamai/sarvam-m from Hugging Face) and "
                "point SARVAM_M_MODEL_PATH at it - this provider does not download the model "
                "itself."
            )
        self._model_path = Path(model_path)
        self._context_tokens = context_tokens
        self._max_tokens = max_tokens
        self._gpu_layers = gpu_layers
        self._timeout_seconds = timeout_seconds
        self._require_hardware_check = require_hardware_check
        self._model = None
        self._hardware_checked = False

    @property
    def provider_name(self) -> str:
        return "Sarvam-M local"

    def _check_hardware_or_raise(self) -> None:
        if self._hardware_checked or not self._require_hardware_check:
            return
        report = detect_hardware()
        verdict = assess(report, SARVAM_M_Q4)
        if verdict.verdict == "UNSUPPORTED HARDWARE":
            raise LLMProviderError(
                "UNSUPPORTED HARDWARE: this machine cannot run Sarvam-M locally with acceptable "
                "performance.\n" + format_report(report, verdict, SARVAM_M_Q4)
            )
        self._hardware_checked = True

    def _get_model(self):
        if self._model is not None:
            return self._model

        self._check_hardware_or_raise()

        if not self._model_path.is_file():
            raise LLMProviderError(
                f"SARVAM_M_MODEL_PATH={self._model_path} does not exist. Download the GGUF file "
                "and place it there before selecting LLM_PROVIDER=sarvam_m."
            )

        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise LLMProviderError(
                "LLM_PROVIDER=sarvam_m requires the 'llama-cpp-python' package "
                "(pip install llama-cpp-python) - it is not a default dependency because most "
                "deployments use the Gemini API instead."
            ) from exc

        try:
            self._model = Llama(
                model_path=str(self._model_path),
                n_ctx=self._context_tokens,
                n_gpu_layers=self._gpu_layers,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 - surface any load failure uniformly
            raise LLMProviderError(f"Sarvam-M local model failed to load: {exc}") from exc
        return self._model

    async def generate_response(
        self,
        *,
        system_prompt: str,
        history: list[LLMMessage],
        retrieved_context: str | None,
    ) -> LLMResponse:
        if not history:
            raise LLMProviderError("Cannot generate a response with empty conversation history")

        model = self._get_model()
        full_system_prompt = augment_system_prompt_with_context(system_prompt, retrieved_context)
        messages = [{"role": "system", "content": full_system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in history]

        def _run_inference():
            # llama-cpp-python is synchronous and CPU/GPU-bound — run off the
            # event loop so it can't block other calls (e.g. a second caller's
            # turn, or the ARI WebSocket) while it grinds through tokens.
            return model.create_chat_completion(
                messages=messages,
                max_tokens=self._max_tokens,
            )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_inference), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise LLMProviderError(
                f"Sarvam-M local inference timed out after {self._timeout_seconds}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface any provider error uniformly
            raise LLMProviderError(f"Sarvam-M local inference failed: {exc}") from exc

        choices = result.get("choices") or []
        text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
        finish_reason = choices[0].get("finish_reason") if choices else None
        if not text:
            raise LLMProviderError(
                f"Sarvam-M local model returned an empty response (finish_reason={finish_reason})"
            )

        return LLMResponse(text=text, finish_reason=finish_reason)
