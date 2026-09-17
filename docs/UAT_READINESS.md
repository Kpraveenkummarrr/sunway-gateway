# UAT Readiness

Date: 2026-09-17 · Third pass — Sarvam-M feasibility and noise root-cause · Baseline commit: `713eceb`

Evidence rule: **VERIFIED** means measured on a real system and reproducible;
**LOCAL ONLY** means proven on the development Asterisk with stand-in
providers; **CLIENT REQUIRED** means it cannot be established without the
client's gateway, SIM cards, provider keys or knowledge base.

The development environment has **no SMG4008-8G, no SIM, no Bhashini key and
no Gemini key**. Anything touching those is CLIENT REQUIRED by definition —
it is not a claim about whether the code is correct.

## Client complaints

The full matrix — root cause, fix, automated evidence, live evidence,
remaining action and status for every issue — is in
[CLIENT_ISSUES_STATUS.md](CLIENT_ISSUES_STATUS.md). Summary:

| Client Issue | Fix | Automated Evidence | Live Evidence | Remaining Action | Status |
|---|---|---|---|---|---|
| 1. Time lag | End-of-speech 2 s, chunked+pipelined TTS, pre-rendered phrases, shorter first chunk | `test_latency.py` (6); first-chunk text −17% | 10.68 s → 3.41 s on real Asterisk | Time a real GSM call (AI-01) | CLIENT UAT REQUIRED |
| 2. Robotic voice | Tempo 1.15× pitch-preserved, no beep, helpline persona, markup stripped before TTS. **LLM (Gemini) proven not a contributor** — cleanup identical regardless of provider | `test_spoken_text.py` (20), `test_audio.py` (26), `test_llm_noise_independence.py` (3); F0 148.1→148.1 Hz at 1.15× | A/B bundle measured offline | Listening decision on B1 vs B2 (AI-13) | CLIENT UAT REQUIRED |
| 3. KB retrieval | Hybrid retrieval, follow-up topic carry-over, **re-index command + index status** | `test_lsd_knowledge_retrieval.py` (22), `test_knowledge_reindex.py` (11) | Stale→re-index→searchable proven on real PostgreSQL | Re-index the real PDF, run the mandatory questions (KB-01…KB-05) | CLIENT UAT REQUIRED |
| 4. Barge-in | TALK_DETECT → stop playback → discard queued chunks → record; **now proven identical across LLM providers** | `test_barge_in_matrix.py` (9 scenarios), `test_barge_in_with_llm_providers.py` (2) | **116 ms detection on real Asterisk** | GSM false-trigger rate (AI-10) | CLIENT UAT REQUIRED |
| 5. Noise | Band-limited resampler, −3 dBFS normalisation, edge fades. **LLM ruled out this pass; remaining candidates narrowed to Asterisk/RTP/Synway/GSM** | `test_audio.py` (26), `test_llm_noise_independence.py` (3); alias 0.0 → −79.8 dB (re-measured fresh, matches prior figure exactly) | G.711 previews measured | Stage-by-stage capture procedure in [NOISE_ROOT_CAUSE.md](NOISE_ROOT_CAUSE.md); `audio_level_report.py` on real recordings (AI-12) | CLIENT UAT REQUIRED |
| 6. Call centre / GUI | Live checklist; no structural change this pass | `test_call_centre.py`, `test_admin_panel.py`, `test_route_agi.py` | Live routing call through 3 destinations then AI fallback | Checklist §1 and §2 | CLIENT UAT REQUIRED |
| 7. Helpline behaviour | `AI_PERSONA=lsd_helpline` with guardrails and escalation | `test_helpline_persona.py` (22) | — | AI-14, AI-15, AI-17 | CLIENT UAT REQUIRED |
| 8. District centres | Operator-controlled directory, invented centres impossible | Unlisted district refuses; broken file names nothing | — | **Client must supply the Haryana list** | BLOCKED |
| 9. Reference PDF comparison | — | — | — | **The PDF is not in the workspace** — add it | BLOCKED |
| 10. Sarvam-M local LLM | Hardware-gated `LLM_PROVIDER=sarvam_m` provider, identical RAG context to Gemini, A/B harness | `test_hardware_check.py` (9), `test_sarvam_m_provider.py` (16), `test_llm_provider_parity.py` (3) | **Hardware check run for real: UNSUPPORTED HARDWARE on the dev machine.** A/B harness run and validated with mocks + graceful "provider unavailable" for real names | Run the hardware check on the client's server; see [LLM_AB_TEST.md](LLM_AB_TEST.md) | CLIENT UAT REQUIRED |

## Acceptance categories

| | Category | Status | Evidence |
|---|---|---|---|
| A | AI response latency | LOCAL ONLY | 10.68 s → 3.41 s on real Asterisk with a stand-in TTS latency model |
| B | Hindi ASR | CLIENT REQUIRED | Request shape verified against the live-proven Postman payload; accuracy needs the key |
| C | Hindi TTS | CLIENT REQUIRED | Format/level/tempo verified; voice naturalness needs the key and a human ear |
| D | Voice clarity | VERIFIED (local) | Aliasing −79.8 dB, no clipping, ≤60 ms edge silence |
| E | Natural voice | PARTIAL | Tempo (F0 unchanged at 1.15×), trimming, beep removal, persona, and markup stripped before TTS; human judgement outstanding |
| F | RAG retrieval | LOCAL ONLY | Hybrid retrieval, follow-up carry-over and all seven mandatory questions tested; re-index proven end to end on real PostgreSQL; the client's own PDF outstanding |
| G | Barge-in | **VERIFIED (real Asterisk)** | 116 ms detection, playback stopped mid-clip, recording started, 0 errors; 9-scenario edge matrix automated |
| H | Call Centre | PARTIAL | Live call dialled 3 destinations in configured order, then AI fallback |
| I | Admin GUI | PARTIAL | Panel served (23.7 KB, 22 endpoints), health read real Asterisk; no browser click-through |
| J | Security | LOCAL ONLY | Password/session/API-key tests; secrets never returned; `llm_api_key` write rejected |
| K | Concurrency | PARTIAL | Per-call isolation tested; multi-channel GSM load CLIENT REQUIRED |
| L | Stability | NOT RUN | No 30-minute call executed |
| M | Helpline behaviour/guardrails | LOCAL ONLY | Persona and directory asserted by 22 tests; model's actual Hindi wording CLIENT REQUIRED |
| N | Knowledge grounding for LSD questions | LOCAL ONLY | 15 deterministic retrieval tests across Hindi/Hinglish/transliterated/short/follow-up queries |
| O | Local LLM (Sarvam-M) feasibility | **MEASURED: UNSUPPORTED on dev hardware** | Real hardware check run (4 threads, 8 GB RAM, <1 GB disk, integrated GPU); provider code built and tested against a stubbed backend; never exercised with real weights (no machine here can run them) |
| P | LLM-noise independence | VERIFIED (local, structural + empirical) | `LLMResponse` carries no audio fields; identical processed audio regardless of provider style, on a re-measured −79.8 dB alias baseline |

## Test result (this session)

```
pytest -q  →  408 passed, 1 skipped, 0 failed
```

The skip is the opt-in real-provider test. 34 tests were added in this pass:
hardware detection and verdict logic, the Sarvam-M provider (against a
stubbed `llama_cpp`, never real weights), LLM-provider RAG parity and
evidence logging, barge-in across providers, and the LLM/audio-noise
independence proof.

## Must be done on the client machine

The full list is [CLIENT_UAT_CHECKLIST.md](CLIENT_UAT_CHECKLIST.md). The
minimum to get there:

```bash
cd /home/admin1/sunway-gateway && git pull
cd backend && source .venv/bin/activate
pip install -r requirements.txt && alembic upgrade head
python scripts/reindex_knowledge.py            # must end with "stale / unsearchable : 0"
sudo systemctl restart sunway-backend sunway-ai-worker
```

1. Supply the district → diagnostic centre list and set
   `REFERRAL_DIRECTORY_PATH`; until then the agent names no centre at all.
2. Add the client reference PDF to the repository so the behaviour comparison
   can be completed.
3. Re-index the knowledge base — retrieval cannot be judged before this is
   clean (complaint 3).
4. Run `scripts/hindi_pipeline_smoke_test.py` for real Bhashini/Gemini
   timings, and `scripts/telephony_voice_ab.py` for the voice A/B decision.
5. Run `scripts/check_sarvam_hardware.py` if a local LLM is still wanted —
   stop there if it reports UNSUPPORTED HARDWARE; do not install
   `llama-cpp-python` or download a model first.
6. Work through [NOISE_ROOT_CAUSE.md](NOISE_ROOT_CAUSE.md)'s capture
   procedure to isolate the remaining GSM-path noise.
7. Work through the checklist: GSM calls, browser clicks, the barge-in rows
   and one 30-minute call.

## Cannot be verified without the physical gateway

GSM codec negotiation and transcoding, RTP jitter/packet loss, echo and
one-way audio, SIM channel capacity and concurrent GSM calls, DTMF detection
mode from the SMG4008, barge-in false triggers caused by GSM line noise or
echo of the AI's own voice, and — new this pass — which of Bhashini,
Asterisk media, Synway transcoding or the GSM line itself the residual
noise/disturbance is actually in (the LLM has been ruled out; see
[NOISE_ROOT_CAUSE.md](NOISE_ROOT_CAUSE.md)). Real Sarvam-M latency and
Hindi-quality numbers also cannot be produced here — no machine available
to this session passes its own hardware check.
