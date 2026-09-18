# Final engineering pass — 2026-09-17, continued 2026-09-18

Baseline: `2fcc966` on main. Work isolated on `engineering/final-uat-20260917`.
Runtime/regression commit: `1c951f9`; diagnostic-tool commit: `b103aef`.
This is a local engineering release candidate, **not production/GSM acceptance**.
The client machine, its 46-page PDF, credentials, live SIP configuration and
audio captures were not accessible in this session. No production configuration
was changed. Existing `.env`, codec, Bhashini payload and 1.15x default remain intact.

## Proven implementation defects

| Symptom | Component/function and previous behaviour | Change | Evidence / client gate |
|---|---|---|---|
| Intermittent long dead air | `AICallController._play_and_wait` registered its listener after REST completed; a faster PlaybackFinished was lost | Allocate playback ID and listener before POST; stop timed-out playback; retry stop after a creation race | Deterministic early-event regression completes within 250 ms; old path waits until timeout. GSM timing still required |
| Overlapping/stale turns | `_on_recording_finished` accepted duplicate or old sequence events | Claim current sequence before awaiting; reject unsupported recording formats | Concurrent duplicate regression; physical interruption still required |
| Mid-call settings drift | `_refresh_settings` mutated settings shared by all calls; session language captured before refresh | Immutable per-call settings; language set after refresh; phrase cache includes language/tempo | Snapshot regression; two real calls with an admin update still required |
| Disk growth | `_write_and_play_wav` retained every synthesized answer indefinitely | Delete only the generated clip after playback completes/stops | Cleanup regression; caller recordings untouched |
| Continued work after hangup | Text/RAG/LLM turn was not raced against hangup | Cancel and await text-turn work; drain prefetched TTS cancellation | Lifecycle and interruption suites |
| Cancellation leaves stale media state | Playback cleanup did not cover cancellation during the REST request | Stop known playback ID on cancellation; clean waiter in outer finally; await cancelled provider cleanup | Deterministic cancellation/request-failure regressions |
| Unavailable retrieval could leave dead air | Controller did not catch `SearchError` | Include retrieval failures in existing safe-error path | Call-path regression coverage; fault-injection UAT required |
| Wrong/absent KB answers | Unknown vector provenance was considered searchable and healthy | Exclude unknown spaces, mark stale, require re-index | PostgreSQL regression excludes then recovers document after reindex |
| Index corruption after failed reindex | Earlier vectors assigned before a later dimension error committed failure | Validate entire batch first; validate finite values and ingestion vector count | Bad second-vector regression proves old first vector unchanged |
| Third-turn follow-up loses subject | Query builder used only immediately previous acknowledgement/pronoun question | Recover last explicit topic across short follow-ups | `lumpy → iska ilaj → haanji → aur bachav` regression; semantic review pending |
| PDF upload failure | Repository normalization still retained PostgreSQL-forbidden NUL despite the reported client-side fix | Strip NUL before chunking/embedding | Hindi combining-mark preservation regression |
| Unsupported veterinary reply | Real-provider helpline could answer even with zero retrieved context | Deterministic unavailable-information/referral response instead of calling LLM without evidence | Empty-context regression; this is not a general hallucination/medical-safety guarantee |
| Spoken URLs | Bare URLs survived spoken-text cleanup | Remove bare HTTP/www URLs | Hindi spoken-text regression |
| Configured AI fallback skipped | Empty staff list was treated as invalid department | Distinguish route-found from target-count; apply configured no-answer action | AGI variable regression; dialplan requires client reload/UAT |
| Admin authentication bypass | Password-only setup signed cookies using a public fallback string | Use private configured material; reject malformed cookies; fail closed outside dev/test | Forged-public-key cookie and production-auth regressions |
| Malformed login inputs cause errors | Constant-time string comparisons reject non-ASCII inputs with TypeError | Compare password/key bytes and reject non-ASCII cookie signatures | Unicode password/signature regression |
| False health / secret disclosure | `/ready` returned HTTP 200 and raw DB exceptions on failure | 503 for degraded readiness; generic public errors | Readiness regression |
| Admin reply-length changes ignored | LLM provider captured max_tokens only at worker startup | Apply per-turn immutable token-limit view for Gemini/OpenAI | Payload limit regression; original provider unchanged |

## Voice finding

**The location of the client's unclear/robotic GSM audio remains unknown.**
No source speech or real RTP capture was available. The existing anti-alias filter
passed a synthetic 6 kHz probe at **-78.27 dB relative alias energy**. No codec,
jitter-buffer, gain target, silence window or tempo default was blindly changed.
See [voice evidence and procedure](VOICE_ROOT_CAUSE.md).

## Decisions and blockers

- Retain Gemini and Bhashini pending a measured client comparison. Their real latency/quality/cost is unmeasured here.
- Reject mock embeddings in production. Add an opt-in CPU ONNX multilingual MiniLM candidate, with verified artifact, bounded threads, no runtime downloads and an independent embedding-space ID. It is **not selected/deployed** automatically.
- The development laptop reported 8,026 MB RAM, 479 MB available, 720 MB free on the checked disk, no NVIDIA GPU. Installing/loading candidate models was unsafe in that state. Sarvam-M 24B is not practical on the reported 8 GB CPU-only client.
- The executed Gemini/Sarvam benchmark recorded both unavailable, **0 successful responses out of 12 each**. This is a blocked benchmark, not evidence of model quality. Local DB retrieval in that run used mock embeddings and an empty index.
- True full-duplex/preroll capture is not implemented: the file-based recording loop can still lose the first syllable during interruption. TALK_DETECT can respond to noise/echo. Do not label live barge-in solved from tests.
- Ordinary staff-only IVR calls still lack a complete backend call lifecycle: attempt logging depends on an existing Call row. Deployment channel/uniqueid mapping must be verified. This remains a call-history UAT blocker, not hidden by passing API tests.
- LLM responses remain buffered; sentence-TTS prefetch is retained. First useful text is available only when the full LLM response arrives. No unsupported streaming rewrite was deployed.

## Evidence and release instructions

Baseline full suite: **408 passed, 1 skipped**, 91.68 s. The skip is the opt-in
real-provider integration test. Targeted revised call/audio/embedding suite:
**82 passed**. Interim full run: **424 passed, 2 failed, 1 skipped**; failures
were outdated retained-file and ungrounded-chain test assumptions, corrected
without disabling the assertions. Final rerun results are recorded in
[UAT readiness](UAT_READINESS.md).

On 2026-09-18 the latest selected run was **212 passed, 2 failed** (both database
connection failures), plus a separate reindex setup error. Windows-to-WSL access
to the local PostgreSQL instance remained refused even after Linux reported it
online. The final full run was interrupted after repeated connection failures;
it must be rerun before release. A final focused run after the last code edits
passed **61 tests in 12.29 s**, with compilation clean. Do not represent the 408-test baseline or the
interim 424 passes as a clean final branch result.

Generated local artifacts (gitignored, not customer speech):
`backend/data/final-uat-dsp-20260917/` and
`backend/data/final-uat-llm-20260917/`.
The six-second read-only sampler smoke run (`final-uat-probe-20260917.jsonl`)
reported local Asterisk 18.10 and DB reachable, worker app unregistered, zero
active channels and three offline test SIP endpoints. This was the local dev
instance before WSL stopped, **not the client SMG4008 or a stability pass**.

No 30-minute live stability, physical concurrency, browser workflow, real ASR,
real TTS or handset naturalness claim is made. The in-app browser skill could
not proceed because its JavaScript execution tool was unavailable.

See [acceptance matrix](CLIENT_ISSUES_STATUS.md), [exact client commands](CLIENT_UAT_CHECKLIST.md),
[RAG decision](RAG_ROOT_CAUSE.md), and [model evaluation](LLM_MODEL_EVALUATION.md).
Use `git diff --name-only 2fcc966..HEAD` on the engineering branch for the exact
committed file manifest. Rollback starts at the preserved baseline; client
configuration/database backups must be captured before applying anything.

## Exact changed-file manifest

Paths are relative to the repository root; generated audio and private local
diagnostics are deliberately excluded from Git.

The offline A/B CLI was also executed successfully on 2026-09-18 using the
synthetic 48 kHz source, writing `backend/data/final-uat-ab-smoke-20260918/`.
It reported 3.50 s raw, 2.70 s at current 1.15x processing and 3.13 s at 1.00x,
with 8 kHz output. Its provider-origin flag is false and TTS latency is null:
this confirms the diagnostic path works, not that Bhashini or GSM sounds natural.

```text
asterisk/etc/dialplan/departments.conf
asterisk/scripts/route_agi.py
backend/app/api/admin.py
backend/app/api/health.py
backend/app/core/admin_auth.py
backend/app/core/config.py
backend/app/core/security.py
backend/app/providers/embeddings/factory.py
backend/app/providers/embeddings/local_provider.py
backend/app/providers/llm/base.py
backend/app/providers/llm/openai_provider.py
backend/app/services/ari_client.py
backend/app/services/audio.py
backend/app/services/call_controller.py
backend/app/services/conversation.py
backend/app/services/knowledge_ingestion.py
backend/app/services/knowledge_reindex.py
backend/app/services/knowledge_search.py
backend/app/services/pdf_extraction.py
backend/app/services/spoken_text.py
backend/app/services/system_config.py
backend/app/services/voice_diagnostics.py
backend/requirements-local-embeddings.txt
backend/scripts/client_uat_probe.py
backend/scripts/dsp_benchmark.py
backend/scripts/rag_uat_probe.py
backend/scripts/telephony_voice_ab.py
backend/tests/fake_ari.py
backend/tests/test_bhashini.py
backend/tests/test_call_lifecycle.py
backend/tests/test_final_engineering.py
backend/tests/test_gemini_llm.py
backend/tests/test_health.py
backend/tests/test_knowledge_reindex.py
backend/tests/test_latency.py
backend/tests/test_local_embeddings.py
backend/tests/test_route_agi.py
docs/CLIENT_ISSUES_STATUS.md
docs/CLIENT_UAT_CHECKLIST.md
docs/FINAL_ROOT_CAUSE_REPORT.md
docs/LLM_MODEL_EVALUATION.md
docs/RAG_ROOT_CAUSE.md
docs/UAT_READINESS.md
docs/VOICE_ROOT_CAUSE.md
```
