# AI voice optimization

Status: local implementation only, 2026-09-16.

This document records what is implemented in the repository and what still
requires a real Hindi call. No GSM gateway, client database, Bhashini account,
or Gemini account was used in this local session.

## Root causes identified

- The RAG path used only cosine similarity. Domain names and ASR variants
  could miss even when the source PDF contained the answer.
- Stored documents did not identify the embedding model/space, so vectors
  from a changed model could be mixed silently.
- The call loop waited for complete playback and had no caller-speech event
  handling. It therefore could not implement barge-in.
- Hindi generation rules were unnecessarily rigid: fixed short length,
  mandatory punctuation, and no natural code-mixing. That can make a vendor
  voice sound scripted even when the TTS provider is unchanged.
- TTS output already had anti-aliased resampling and level normalization, but
  there was no per-call level, silence, clipping, or speaking-rate evidence.

## Local changes

- Hybrid retrieval: vector shortlist plus bounded lexical candidates.
- Unicode normalization and small retrieval-only aliases for Lumpy Skin
  Disease, including Hindi transliteration and common ASR spellings. Aliases
  never contain an answer; the PDF remains the source of truth.
- Ingestion stores a non-secret embedding-space identifier. Explicitly
  incompatible stored spaces are excluded from future searches; legacy rows
  without metadata remain searchable until re-indexed.
- Context passed to the LLM is explicitly marked as reference knowledge, with
  answer rules to use supported facts and avoid invented facts.
- Turn logs now expose `vad_ms` (recording-endpoint/capture proxy), `asr_ms`,
  `embedding_ms`, `retrieval_ms`, `context_ms`, `llm_ms`, `tts_ms`,
  `audio_ms`, `first_audio_ms`, and `total_ms`. The `vad_ms` value is not a
  pure end-of-speech measurement because Asterisk does not currently emit
  the caller's speech-stop timestamp in this application.
- Caller and prepared TTS WAV logs include sample rate, duration, peak/RMS,
  estimated noise floor, silence, clipping, and TTS words-per-minute.
- AI playback can be stopped on `ChannelTalkingStarted`; pending prefetched
  reply chunks are discarded and the normal caller recording loop resumes.
  The AI dialplan enables Asterisk `TALK_DETECT` with conservative local
  defaults.

## Bhashini capability boundary

The current provider sends only the request fields already present in the
verified integration: Hindi language, service ID, gender, and text/audio
payload. No undocumented SSML, pitch, prosody, or speech-rate fields were
added. Playback tempo remains the local pitch-preserving audio preparation
setting and must be evaluated with real Bhashini output.

The focused real-call diagnostic and A/B procedure is documented in
[`TELEPHONY_VOICE_QUALITY.md`](TELEPHONY_VOICE_QUALITY.md). It preserves the
current path as the control and does not approve a production voice change
without a Hindi-speaking tester's result.

## Verification

Local focused verification passed:

- 66 focused tests covering audio processing, provider structure, RAG helper
  behavior, AGI routing, and the controller barge-in path.
- 1 Hindi-policy regression test.

PostgreSQL-backed ingestion/search and full call-controller integration could
not run on this Windows workspace because the configured database endpoint
refused the connection. Real latency, voice quality, GSM/RTP, echo, and
barge-in false-trigger behavior remain client-side acceptance tests.
