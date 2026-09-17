# Gemini vs Sarvam-M: local-LLM feasibility and A/B test

Date: 2026-09-17

**Headline answer: Sarvam-M cannot run on the development machine this was
built on (UNSUPPORTED HARDWARE, measured below), and no Gemini API key or
Sarvam-M model weights are available in this environment, so this document
reports what was actually built, tested and measured here — real hardware
numbers, a working test harness, 34 passing new automated tests — and states
plainly what still needs the client's machine and keys. It does not contain
fabricated Gemini-vs-Sarvam-M quality or latency numbers.**

## 1. Current LLM architecture (audit)

| Aspect | Finding |
|---|---|
| Provider abstraction | Already exists: `LLMProvider` ABC (`app/providers/llm/base.py`), one method `generate_response(system_prompt, history, retrieved_context) -> LLMResponse`. No change needed to add a provider. |
| Provider selection | `app/providers/llm/factory.py::get_llm_provider(settings)`, branching on `LLM_PROVIDER`. Called once at startup in `app/ai_call_worker.py` (the long-lived call worker) and per-request in `app/api/conversation.py`. |
| Prompt construction | `Settings.system_prompt_for()` (persona + referral directory + language policy) — entirely provider-agnostic, built before any provider is called. |
| RAG context injection | Was duplicated inline in `OpenAILLMProvider`; extracted to `app/providers/llm/prompt_context.py::augment_system_prompt_with_context()` so Gemini and Sarvam-M build the identical "KNOWLEDGE CONTEXT / ANSWER RULES" block from the same inputs — required for a valid comparison (Part 3), verified by test (`test_sarvam_m_receives_the_exact_same_augmented_prompt_as_gemini`). |
| Conversation history | `app/services/conversation.py::_history_to_llm_messages()` — windowed to `AI_MAX_HISTORY_MESSAGES`, provider-agnostic. |
| Response parsing | Each provider returns `LLMResponse(text, finish_reason)` — two fields, nothing else (see `docs/NOISE_ROOT_CAUSE.md` for why this matters to the noise question). |
| Streaming | **Not implemented by either provider.** `OpenAILLMProvider` awaits `chat.completions.create()` in full; `SarvamMProvider` awaits `create_chat_completion()` in full. "First response latency" and "full response latency" are therefore the same number for both — reported as such, not disguised as two different measurements. |
| Timeout handling | `PROVIDER_TIMEOUT_SECONDS` wraps each provider's own call; `AI_TURN_TIMEOUT_SECONDS` caps the whole embed+RAG+LLM sequence in `conversation.py`. Both providers use the identical settings. |
| Retry handling | None, on either provider — a failure raises `LLMProviderError` once; `call_controller.py`'s consecutive-failure cap handles repeated failures at the call level, identically regardless of provider. |
| Token limits | `LLM_MAX_TOKENS` (default 400), shared by both. |
| Language policy | Applied in `system_prompt_for()`, before either provider is called — identical for both. |
| Spoken-text / TTS input | `speech_chunks()` → `spoken_text()`, operating purely on `LLMResponse.text` — proven provider-agnostic (see below). |

## 2. Sarvam-M local provider — what was built

- `app/services/hardware_check.py` — detects CPU, RAM (total and *currently
  available*), disk free, GPU/VRAM (via `nvidia-smi`, falling back to
  `wmic` on Windows) with zero hardcoded per-machine assumptions; scores
  against a model's actual quantized file size, not a fixed number.
- `app/providers/llm/sarvam_m_provider.py` — `LLM_PROVIDER=sarvam_m`. Runs
  the hardware check before ever touching the model; **never downloads
  anything**; requires `SARVAM_M_MODEL_PATH` to already point at an
  operator-downloaded GGUF; uses `llama-cpp-python` (optional dependency,
  lazily imported, not installed by default) as the inference backend,
  which is the practical choice for a quantized GGUF on mixed CPU/GPU
  hardware; runs inference in a thread (`asyncio.to_thread`) so it can't
  block the ARI event loop or other calls.
- `scripts/check_sarvam_hardware.py` — the go/no-go check, runnable
  standalone before any install attempt.
- `scripts/llm_ab_benchmark.py` — the A/B harness (Part 5), described below.
- `app/services/knowledge_reindex`/RAG code: **unchanged.**

### Hardware check — measured on the development machine

```
$ python scripts/check_sarvam_hardware.py
OS               : Windows 10
CPU              : Intel64 Family 6 Model 142 Stepping 12, GenuineIntel (4 logical threads)
RAM              : 7.8 GB total, 0.3-0.6 GB available now
Disk free        : 0.7 GB
GPU              : Intel(R) UHD Graphics (1.0 GB VRAM)
Target model     : sarvam-m (Q4_K_M GGUF, ~24B params)
Verdict          : UNSUPPORTED HARDWARE
Reasons:
  - only 0.7 GB free disk, need ~17.2 GB for sarvam-m (Q4_K_M GGUF, ~24B params)
  - GPU VRAM (1.0 GB) is too small to help; falling back to CPU
  - only 4 CPU threads - CPU-only inference of a 24B model at this thread count
    is very unlikely to complete within the few seconds a live phone turn allows
  - only 0.5 GB RAM available right now, need ~19.0 GB
exit code: 1
```

This is a real i3-10110U ultrabook (2 cores/4 threads, 8 GB RAM, integrated
graphics, nearly full disk) — not a stand-in number. **No download or load
was attempted**, per the requirement not to pretend a machine can run a
model it cannot. **This says nothing about the client's production server**
— run the same script there; it takes under a second and changes nothing.

### What "SUPPORTED" would look like

The check's own logic (`assess()` in `hardware_check.py`), exercised in
`tests/test_hardware_check.py` against fabricated profiles:

| Profile | Verdict |
|---|---|
| This dev laptop (4 threads, 8 GB RAM, <1 GB free disk, integrated GPU) | UNSUPPORTED HARDWARE |
| 16-thread CPU, 64 GB RAM, 500 GB free disk, no GPU | MARGINAL (will load, latency not promised) |
| 16-thread CPU, 64 GB RAM, RTX 4090 24 GB VRAM | SUPPORTED (full GPU offload) |
| Same, but an 8 GB VRAM GPU | SUPPORTED, but flagged for partial offload (slower than full) |

## 3. RAG parity (Part 3)

Nothing in `knowledge_search.py`, `knowledge_ingestion.py`,
`knowledge_reindex.py`, chunking, embeddings, pgvector or the hybrid/lexical
fallback was touched. Both providers receive the **same** retrieval:

- `scripts/llm_ab_benchmark.py` runs retrieval **once** per question and
  passes the identical `system_prompt` + `context` directly into both
  providers' `generate_response()` calls — neither provider does its own
  retrieval, so there is no code path by which the two could diverge.
- `app/services/conversation.py` now logs, every turn:
  `RAG turn provider=<name> session=<id> call=<id> turn=<n>
  retrieval_count=<n> document_ids=[...] chunk_ids=[...]` — proven by
  `tests/test_llm_provider_parity.py`, including that two different
  provider stubs given the same question get byte-identical history and
  context, and that the turn number increments correctly across a
  conversation.

## 4. TTS kept identical for the A/B (Part 4)

Not touched: `TTS_PROVIDER`, `spoken_text.py`, `speech_chunks()`,
resampling, normalization, tempo, codec handling. `speech_chunks()` and
`spoken_text()` operate purely on the string `LLMResponse.text` — see
`docs/NOISE_ROOT_CAUSE.md` for the structural proof that this cannot vary
by provider identity, only by what the provider's text actually contains
(which cleanup neutralizes regardless — `tests/test_spoken_text.py::
test_cleanup_is_identical_no_matter_which_llm_produced_the_markup`).

## 5. A/B harness — run and validated

```bash
# Harness self-check: proves the pipeline, retrieval-sharing and reporting
# work end to end, using the deterministic mock provider on both sides
# (no API key, no model weights needed).
python scripts/llm_ab_benchmark.py --provider-a mock --provider-b mock \
  --seed-demo-kb --out /tmp/sunway-llm-ab
```

Actually run on this machine, output (mock vs mock — proves the harness,
not provider quality):

| Metric | mock (A) | mock (B) |
|---|---:|---:|
| First response latency (ms, avg) | 0 | 0 |
| Full response latency (ms, avg) | 0 | 0 |
| Avg characters | 166 | 166 |
| Avg spoken sentences | 2 | 2 |
| Answered successfully | 12/12 | 12/12 |
| RAG-grounded answers (retrieval_count>0) | 7/12 | 7/12 |
| Markup/punctuation leakage | 0/12 | 0/12 |

(A and B are identical here by construction — same mock provider on both
sides. 7/12 grounded matches expectation: the greeting, the two follow-ups
that ask a bare "how long"/"what's the cure" and the unclear question have
too few lexical terms of their own to retrieve against the seeded demo
knowledge base, exactly as `build_retrieval_query`'s follow-up carry-over is
designed to handle when there IS a preceding question in history — see
question 8, which does retrieve.)

```bash
# Attempted with the real providers, on this machine:
python scripts/llm_ab_benchmark.py --provider-a gemini --provider-b sarvam_m
```

Result: both sides report `n/a (provider unavailable)` —
`provider_a.error`: `"LLM_PROVIDER=gemini but GEMINI_API_KEY is not set"`;
`provider_b.error`: `"LLM_PROVIDER=sarvam_m but SARVAM_M_MODEL_PATH is not
set..."`. The harness fails gracefully and reports exactly why, rather than
crashing or fabricating numbers.

**This is the honest limit of what could be produced here.** Real Gemini
vs. Sarvam-M quality, Hindi naturalness, verbosity and hallucination-risk
comparisons require a Gemini key and (per the hardware check) a machine that
can actually load Sarvam-M — neither exists in this environment.

## 6. Voice naturalness (Part 6)

Both providers' output is forced through the same `spoken_text()` cleanup
before synthesis (`tests/test_spoken_text.py`,
`tests/test_llm_noise_independence.py`). A local model that is less
reliably instructed than Gemini and leaves markdown or headings in its
answer will have them stripped just the same — proven, not assumed. Whether
Sarvam-M's *underlying* Hindi phrasing (word choice, formality, sentence
rhythm before cleanup) is more or less natural than Gemini's is a judgement
that needs real generations from both models, which this environment cannot
produce. **Do not switch away from Gemini on the strength of this document
alone** — it establishes the mechanism is fair, not that Sarvam-M wins.

## 7. Barge-in with both providers (Part 8)

`_on_talking_started` (`app/services/call_controller.py`) never reads
`self._llm_provider` — the LLM only runs later, inside `_run_turn`, after a
new recording has already finished. `tests/test_barge_in_with_llm_providers.py`
proves this empirically for both a "Gemini"-labelled and a "Sarvam-M
local"-labelled stub: the interruption is frozen mid-playback (not left to
finish on its own, which would prove nothing), detected, stops playback
exactly once, records the caller, and the *same configured provider*
answers the new question. Real interruption-detection latency (116 ms) was
already measured live on Asterisk 18.10 in a prior session and is provider-
independent by the same structural argument.

## 8. Latency (Part 9)

No streaming exists on either provider (see §1), so "LLM start → first
output" and "LLM start → complete" collapse to one number for both — this
is an honest architectural fact, not a missing measurement. What could be
measured here: the existing per-stage timing instrumentation in
`conversation.py` (`embedding`, `retrieval`, `context`, `llm`, `tts`,
`audio` — unchanged by this work) fires identically regardless of which
provider is selected, since it wraps `llm_provider.generate_response()`
generically.

**Sarvam-M's actual latency cannot be measured without hardware that can
run it.** The hardware check exists specifically so this claim is never
made without evidence: "local = faster" is not assumed anywhere in this
codebase.

## 9. Production configuration (Part 10)

`LLM_PROVIDER=gemini` remains the default — nothing in `.env.example`
changed that. `sarvam_m` is available for controlled testing only, and the
worker now logs, in plain text, at startup:

```
LLM provider: Gemini
```

or, if configured,

```
LLM provider: Sarvam-M local
```

No key or model path is ever logged (only the provider's `provider_name`
property, a fixed human-readable string).

## 10. Tests (Part 11)

```
pytest -q backend
```

34 new tests this pass, **408 passed, 1 skipped, 0 failed** overall (see
`docs/UAT_READINESS.md` for the full run). Breakdown:

| File | Count | Covers |
|---|---:|---|
| `test_hardware_check.py` | 9 | verdict logic against fabricated profiles (workstation GPU, CPU-only server, small GPU, insufficient disk, unknown VRAM, model-size scaling) + one real-machine sanity check |
| `test_sarvam_m_provider.py` | 16 | missing model path, hardware gate blocks before import, skip-check lab mode, missing file, missing package, successful load+generate against a stubbed `llama_cpp`, model cached across turns, identical prompt shape vs. Gemini, empty history/response, exception wrapping, timeout, factory wiring |
| `test_llm_provider_parity.py` | 3 | identical context/history across two provider stubs, RAG evidence log line correctness, turn numbering |
| `test_barge_in_with_llm_providers.py` | 2 | genuine mid-playback interruption for both a Gemini- and Sarvam-M-labelled stub, exactly-one-stop |
| `test_llm_noise_independence.py` | 3 | `LLMResponse` carries no audio data; identical processed audio regardless of provider "style"; markup never reaches the audio stage |
| `test_spoken_text.py` (+1) | — | cleanup output is identical for Gemini-style and Sarvam-style formatting |

`Sarvam-M is not marked working` beyond "loads and generates against a
stubbed backend on hardware that reports SUPPORTED" — which is what the
tests prove. It has never been loaded with real weights, because no
machine available to this session can do so usably.

## 11. Recommendation

**Do not switch production away from `LLM_PROVIDER=gemini`.** The code path
for Sarvam-M is built, tested, and gated by a real hardware check so it
cannot silently underperform on unsuitable hardware — but it has not been
exercised with real model weights or a real phone call, and this dev
machine cannot do either. Before Sarvam-M is a real candidate:

1. Run `scripts/check_sarvam_hardware.py` on the actual candidate machine
   (client server, not this laptop). Stop here if it says UNSUPPORTED.
2. If supported, download a Sarvam-M GGUF, set `SARVAM_M_MODEL_PATH`, and
   run `scripts/llm_ab_benchmark.py --provider-a gemini --provider-b
   sarvam_m --seed-demo-kb` for real latency and grounding numbers.
3. Human review of the Hindi naturalness of both outputs, per Part 6.
4. Only then, a live GSM A/B per `docs/CLIENT_UAT_CHECKLIST.md`.
