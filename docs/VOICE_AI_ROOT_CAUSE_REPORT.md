# Voice AI root-cause report

Evidence classes: **PROVEN** = reproduced/measured on real Asterisk 18.10 with a simulated gateway
(`backend/scripts/dev_call_harness.py`); **CODE** = read from the code; **NOT PROVEN — requires live
GSM/Synway test** (or client keys/access). Tests: **656 passed, 1 skipped** (opt-in real-provider test).

## 1. Summary

| Issue | Root cause | Status |
|---|---|---|
| 15–20 s after every question | Synway sends RTP PT13 comfort noise → Asterisk's voice-frame-driven silence detector never fires → every turn hits the 20 s cap (PROVEN) | Fixed vs simulated gateway; physical unit NOT PROVEN |
| Drop at ~2:45 | `AI_CALL_TIMEOUT_SECONDS=120` checked only at turn ends (checks at ~55/110/165 s), no goodbye (PROVEN: cut at 143 s) | Fixed; client `.env` must be edited; physical call NOT PROVEN |
| Noisy welcome / AI voice | Not localised. AI-side audio measures clean; noise added after Asterisk (gateway/GSM/echo) is unmeasurable here | NOT PROVEN — requires live GSM/Synway test |
| Barge-in | Ignored in pauses between reply chunks (2.72 s talk-over → 0.0 s); detector stuck on comfort-noise gateways; threshold missed soft callers | Fixed vs simulation |

## 2. Actual runtime architecture (CODE)

GSM → Synway SMG4008 → SIP/RTP (PCMU, PT0 160 B/20 ms; PT13 comfort noise) → Asterisk 18.10 / PJSIP →
dialplan ext 700 → `Stasis(ai-agent)` → Python ARI worker (`app/ai_call_worker.py`, `call_controller.py`) →
ARI `record` (WAV 8 kHz) → Bhashini ASR (batch, keep-alive HTTP) → RAG (PostgreSQL/pgvector + lexical index) →
Gemini (buffered) → Bhashini TTS → one DSP stage (`prepare_tts_clip`) → ARI `play` (sound file) → Asterisk → Synway.
Not present: streaming STT/LLM/TTS, ExternalMedia, WebSocket media bridge, Nginx in the media path.
Streaming is **not implemented** (a rewrite; not attempted); latency is bounded instead by fixing endpointing and
connection reuse, with sentence-chunked TTS prefetch already in place.

## 3. Latency (PROVEN, stand-in providers: ASR 1.2 + LLM 1.0 + TTS 1.0 s)

| Stage | Before (CN gateway) | After |
|---|---:|---:|
| End-of-speech detection | 16.6 s (20 s cap) | 1.3 s |
| Worker overhead (read, retrieval, prep, play request) | ~0.15–0.3 s | same |
| ASR / LLM / TTS | stand-ins | **real values NOT PROVEN — need client keys** |
| Caller stops → first reply audio | 20.2 s | 4.8–5.3 s (= 1.3 + 3.2 stand-in + ~0.3) |

The <2.5 s median / <4 s p95 target is **not demonstrated**: end-of-speech alone is 1.3 s by design
(`AI_ENDPOINT_SILENCE_MS`, tunable ≥400 ms) and real ASR/Gemini/TTS time is unmeasured. The worker's
`Turn timing` line reports every stage per turn on the client system.

## 4. Disconnect

Initiator, SIP cause and Q.850 on the physical call: **NOT PROVEN — requires client logs/pcap**. Proven: the
worker's own timer was the only hard limit found (`Call finished … ended_by=` now records who ended each call).
30-minute and 10-minute calls: **not run** (200 s call run only, 6 turns). See `CALL_DISCONNECT_ROOT_CAUSE.md`.

## 5. Audio format matrix

| Hop | Codec / rate / channels |
|---|---|
| Synway → Asterisk | PCMU 8 kHz mono (client capture); negotiated SDP not seen — verify |
| Asterisk → worker (recording) | WAV slin 8 kHz mono 16-bit |
| Worker → ASR | resampled once by `prepare_for_asr` |
| TTS output | Bhashini WAV, native rate unverified here (fixtures 48 kHz) |
| Worker → Asterisk | WAV 8 kHz mono 16-bit, one band-limited resample (none if already 8 kHz), no tempo at 1.0 |
| Asterisk → Synway | PCMU (one transcode by Asterisk) |

Avoidable steps removed: tempo/WSOLA (default off), legacy per-clip peak normalisation. Welcome file ffprobe/A–F
isolation tests and softphone-vs-GSM comparison need client access: see `SYNWAY_GSM_AB_PLAN.md` and
`CLIENT_UAT_UPDATE_20260919.md`.

## 6. Not done / BLOCKERS

Streaming STT/LLM/TTS; 5/10/30-minute live calls; 10-turn live test; RTP loss/jitter/SIP BYE capture; SMG4008 settings
and RSSI; concurrent-call and 20-turn leak tests; real provider latency; client's real knowledge base. **The system is
not declared production-ready.**

## 7. Files, config, rollback

Files: `docs/LATENCY_ROOT_CAUSE.md` (files/functions), `git show --stat 2d2ff48`. Config: `.env.example`
(`AI_CALL_TIMEOUT_SECONDS`, `AI_TTS_SPEED`, `AI_ENDPOINT_*`, `AI_TALK_DETECT_*`, `AI_AUDIO_PROFILE`, `AI_MAX_CONTEXT_CHARS`,
`RAG_SYNONYMS_PATH`). Rollback: switches in `CLIENT_UAT_UPDATE_20260919.md` §8.11, or `git revert 2d2ff48`.
