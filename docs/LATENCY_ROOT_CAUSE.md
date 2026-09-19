# The 15–20 s wait after every question — root cause, fix, evidence

**Status: FIXED against a simulated gateway on real Asterisk 18.10. NOT PROVEN — requires
live GSM/Synway test** for the physical unit's silence behaviour, and for real Bhashini and
Gemini latency (not measurable without keys).

## 1. Root cause

Asterisk ends a caller's turn (ARI `record`) when its silence detector has seen
`maxSilenceSeconds` of quiet. That detector runs **only on voice frames**. The client's
Synway sends RTP payload type 13 (RFC 3389 comfort noise) in silence — visible in the
client's capture, and Asterisk logs "Comfort noise support incomplete … turn off on client".
With comfort noise there are no voice frames, so the detector's clock never advances and every
turn runs to `AI_MAX_TURN_SECONDS` (20 s). Everything after that (ASR, retrieval, Gemini,
TTS) is ordinary; the 15–20 s was **dead air before any AI work started**.

Fingerprint in the client's existing logs: `Stage … stage=talk_finished_event_to_recording
duration_ms=` of about 15,000–19,000 (the talk-finished event arrives at the true end of
speech; the recording ends 15–19 s later).

Proven here (real Asterisk 18.10, a SIP/RTP gateway simulator that can send continuous frames,
comfort noise or nothing in silence), identical caller audio and identical stand-in provider
delays (ASR 1.2 s + LLM 1.0 s + TTS 1.0 s = 3.2 s), 100% of the question captured in every row:

| Gateway in silence | End-of-speech wait, before | after | Reply heard after caller stopped, before | after |
|---|---:|---:|---:|---:|
| Continuous voice frames | 2.0 s | **1.3 s** | 5.6 s | **4.8 s** |
| **Comfort noise (payload 13) — the client's case** | **16.6 s** (ended by the 20 s cap) | **@@CN@@ s** | **20.2 s** | **@@CNH@@ s** |
| No packets (DTX) | 4.0–4.8 s | **1.3 s** | 8.5 s | **5.1 s** |
| Comfort noise, 100 ms hangover of voice frames | — | 1.5 s | — | 5.2 s |
| Comfort noise, **no** hangover | — | 1.3 s | — | 5.2 s |
| DTX, no hangover | — | 1.4 s | — | 5.0 s |

("Before" = the previous behaviour reproduced with settings: worker detector off, tempo 1.15, legacy
audio, 120 s call limit. Everything the worker adds around the AI stages measured about 150 ms.)

## 2. What was tried and rejected — and why it matters

The first fix followed the recording **file** as Asterisk wrote it and ended the turn when it
stopped growing. On real Asterisk it **cut callers off mid-sentence**: Asterisk 18.10 writes the
recording in 32 KiB blocks (2.05 s of audio at a time; measured on a live call — the file was 44
bytes for the first 2.03 s, then 32,812 bytes), so "not grown for 1.2 s" says nothing about the
caller. A 2.76 s question was ended at 2.1 s. Unit tests had passed because they wrote the file
continuously. It was removed, not patched, and the harness now reports **speech captured %**
(recorded WAV vs what the caller said) for every turn so this class of defect cannot hide.

## 3. The fix (`app/services/endpointing.py`, wired in `call_controller.py`)

Two real-time signals Asterisk already provides; whichever fires first ends the turn
(`stop_recording`), Asterisk's own limits stay as a backstop:

1. **TALK_DETECT events.** `ChannelTalkingStarted` marks speech; `ChannelTalkingFinished` arrives
   ~170 ms after the last speech frame (measured); the worker adds the rest of the wait on its
   own clock (default total 1,200 ms). Works for continuous frames and for comfort-noise gateways
   that keep ≥ 200 ms of voice frames after the last word.
2. **RTP receive counters** (ARI `rtp_statistics`). A G.711 voice packet is 80–240 bytes, a
   comfort-noise packet 1–13, so bytes-per-packet says whether *voice frames stopped arriving* —
   for any hangover length. Measured: with 0 or 100 ms of hangover TALK_DETECT never reports
   "finished" at all; this signal covers exactly that case.

Also in this change: TALK_DETECT is reset (`remove` + `set`) at the end of each turn so the next
interruption is always reported (`docs/BARGE_IN.md`); worker-side silence wait is `AI_ENDPOINT_SILENCE_MS`.

## 4. Other latency work

| Item | Change | Evidence |
|---|---|---|
| Cold connections | Each provider connection (DNS + TCP + TLS) is opened during the welcome message and kept alive 120 s | Measured on the dev machine: DNS 13–190 ms, TCP ~60 ms, TLS ~70 ms (one 563 ms outlier) per cold connection, paid three times on a first question |
| Timeouts | Per stage: ASR 15 s, LLM 12 s, TTS 12 s (previously one 30 s value) | `tests/test_provider_connections.py` |
| SDK retries | LLM SDK retries 2 → 1 (it honours `Retry-After` up to a minute) | same |
| Not changed | Sentence-chunked TTS with prefetch, pre-rendered fixed phrases (previous pass) | — |

## 5. What to read in the client's log

`Caller finished speaking: … signal=…`, `Recording finished: … ended_by=… end_of_speech_wait_ms=…`,
`Turn timing … end_wait_ms= … heard_delay_ms=`. Expect the wait at ~1.1–1.5 s. Full checklist:
[CLIENT_UAT_UPDATE_20260919.md](CLIENT_UAT_UPDATE_20260919.md).

## 6. Not proven / residual risk

- **The physical Synway's behaviour.** Its hangover length and whether it sends comfort noise the way the simulator does are unknown; both signals were built so either case works, but only the real unit proves it.
- **A noisy GSM line** whose noise sits above the TALK_DETECT threshold (`AI_TALK_DETECT_THRESHOLD`, default 350) never reports silence; then only the RTP signal (if the gateway suppresses) or Asterisk's own 2 s detector can end the turn.
- **Real provider latency.** ASR, retrieval, Gemini and TTS were stand-ins with fixed delays; their real values need the client's keys. The log's `Turn timing` line names the slow stage.
- **Measurement hygiene.** Runs on a busy machine show event-loop stalls; the harness records the worst stall per turn, and an earlier batch taken while unit tests ran in parallel was discarded for that reason.

## 7. Rollback

`AI_ENDPOINT_MONITOR=false` returns to Asterisk's own detection only (the old behaviour);
`AI_ENDPOINT_USE_RTP_STATISTICS=false` disables just the second signal; the whole change is
`git revert` of the commit. Nothing on Asterisk, the dialplan or the Synway was changed.
