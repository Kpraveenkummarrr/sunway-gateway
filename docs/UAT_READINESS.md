# UAT Readiness

Date: 2026-09-17 · Baseline commit: `4f6be0c`

Evidence rule: **VERIFIED** means measured on a real system and reproducible;
**LOCAL ONLY** means proven on the development Asterisk with stand-in
providers; **CLIENT REQUIRED** means it cannot be established without the
client's gateway, SIM cards, provider keys or knowledge base.

The development environment has **no SMG4008-8G, no SIM, no Bhashini key and
no Gemini key**. Anything touching those is CLIENT REQUIRED by definition —
it is not a claim about whether the code is correct.

## Client complaints

| # | Complaint | Root cause found | Fix | Test | Status |
|---|---|---|---|---|---|
| 1 | Response lag | 8 s end-of-speech wait dominated every turn; whole reply synthesized before any audio; welcome re-synthesized per call | End-of-speech 8 s → 2 s; sentence-chunked TTS with pipelined playback; fixed phrases pre-rendered once; per-stage turn timings | `test_call_lifecycle.py`, live extension-700 timings | VERIFIED locally: caller-stops-to-reply 10.68 s → 3.41 s. Live Bhashini/Gemini timings CLIENT REQUIRED |
| 2 | Robotic voice | Flat 1.0× delivery, long TTS silence padding, beep before every turn, bookish Hindi prompt | Configurable tempo (1.15×, pitch preserved), edge silence trimmed, conversational Hindi policy, **turn beep now off by default** | `test_audio.py`, `test_call_lifecycle.py` beep tests | LOCAL ONLY — naturalness is a human judgement, CLIENT REQUIRED |
| 3 | RAG not reading the database | Embedding-space mismatch between ingested vectors and the configured provider; vector-only retrieval missed lexical matches | Hybrid retrieval (vector + lexical) with embedding-space metadata | `test_knowledge_search.py`, `test_rag_context.py` | LOCAL ONLY — the **client must re-index** with the configured provider; real PDF retrieval CLIENT REQUIRED |
| 4 | AI ignores interruptions | Never verified end to end; no evidence Asterisk delivered talk events | `TALK_DETECT(set)=200,500` before Stasis; `ChannelTalkingStarted` → stop playback → discard queued chunks → record | Live call + `test_call_controller.py` | **VERIFIED on real Asterisk 18.10: interruption detected in 116 ms, AI stopped 2.3 s into a 10.4 s clip, recording started 136 ms after the caller spoke.** GSM false-trigger rate CLIENT REQUIRED |
| 5 | Noise / unclear audio | `audioop.ratecv` downsampled 48 kHz → 8 kHz with **no anti-alias filter**: 6 kHz energy folded into the phone band at 0 dB | Band-limited resampler, peak normalisation to −3 dBFS, edge fades | `test_audio.py` alias test | VERIFIED locally: alias 0.0 dB → **−79.8 dB**. GSM-path noise CLIENT REQUIRED |
| 6 | Call Centre / GUI untested | Only unit-tested | Live routing call; panel served and exercised against real Asterisk health | `test_call_centre.py`, `test_admin_panel.py`, live runs | PARTIAL — browser click-through and GSM routing CLIENT REQUIRED |

## Acceptance categories

| | Category | Status | Evidence |
|---|---|---|---|
| A | AI response latency | LOCAL ONLY | 10.68 s → 3.41 s on real Asterisk with a stand-in TTS latency model |
| B | Hindi ASR | CLIENT REQUIRED | Request shape verified against the live-proven Postman payload; accuracy needs the key |
| C | Hindi TTS | CLIENT REQUIRED | Format/level/tempo verified; voice naturalness needs the key and a human ear |
| D | Voice clarity | VERIFIED (local) | Aliasing −79.8 dB, no clipping, ≤60 ms edge silence |
| E | Natural voice | PARTIAL | Tempo, trimming, beep removal, conversational prompt in place; human judgement outstanding |
| F | RAG retrieval | LOCAL ONLY | Hybrid retrieval tested; client re-index + real questions outstanding |
| G | Barge-in | **VERIFIED (real Asterisk)** | 116 ms detection, playback stopped mid-clip, recording started, 0 errors |
| H | Call Centre | PARTIAL | Live call dialled 3 destinations in configured order, then AI fallback |
| I | Admin GUI | PARTIAL | Panel served (23.7 KB, 22 endpoints), health read real Asterisk; no browser click-through |
| J | Security | LOCAL ONLY | Password/session/API-key tests; secrets never returned; `llm_api_key` write rejected |
| K | Concurrency | PARTIAL | Per-call isolation tested; multi-channel GSM load CLIENT REQUIRED |
| L | Stability | NOT RUN | No 30-minute call executed |

## Test result (this session)

```
pytest -q  →  283 passed, 1 skipped, 0 failed
```

The skip is the opt-in real-provider test. A previous report recorded the
DB-backed suite as unrunnable (PostgreSQL refused the connection); it runs
now, so the full suite result is real rather than partial.

## Must be done on the client machine

1. **Re-index the knowledge base** with the configured embedding provider —
   otherwise retrieval stays weak regardless of code (complaint 3).
2. `pip install -r requirements.txt` (numpy is required), `alembic upgrade
   head`, restart the worker and backend.
3. Run `scripts/hindi_pipeline_smoke_test.py` for real Bhashini/Gemini
   timings and audio levels.
4. Real GSM calls: IVR digits, department routing with staff mobiles,
   AI answer quality in Hindi, **interruption during an AI sentence**, and
   `scripts/audio_level_report.py` on the resulting recordings.
5. Click through the admin panel in a browser.
6. One 30-minute call for category L.

## Cannot be verified without the physical gateway

GSM codec negotiation and transcoding, RTP jitter/packet loss, echo and
one-way audio, SIM channel capacity and concurrent GSM calls, DTMF detection
mode from the SMG4008, and barge-in false triggers caused by GSM line noise
or echo of the AI's own voice.
