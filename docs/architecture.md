# Architecture

## Status legend

Every capability below is tagged:

- **READY WITHOUT GATEWAY** — can be fully built and tested using SIP softphones, no SMG4004 needed.
- **REQUIRES PHYSICAL SMG4004** — cannot be verified or safely implemented until the physical gateway is available; behavior is not to be assumed.

## Call flow

```
GSM Caller
   |  (GSM)
   v
Synway SMG4004 GSM Gateway              [REQUIRES PHYSICAL SMG4004]
   |  (SIP + RTP)
   v
Asterisk (PJSIP)                        [READY WITHOUT GATEWAY — implemented and tested with
   |                                       SIP softphones/SIPp, see docs/asterisk.md]
   |
   +--> IVR (DTMF menu)                 [READY WITHOUT GATEWAY — implemented and tested]
   |       |
   |       +--> Staff routing (Sales / Support / Accounts)   [READY WITHOUT GATEWAY for SIP leg;
   |       |                                                   REQUIRES PHYSICAL SMG4004 for outbound GSM leg]
   |       v
   |    Staff mobile (via SMG4004 outbound GSM channel)       [REQUIRES PHYSICAL SMG4004]
   |
   +--> ARI ("ai-agent" Stasis app)     [READY WITHOUT GATEWAY — implemented and tested
   |                                      live via ARI + Asterisk CLI, see below]
   +--> AI Call Controller (app/services/call_controller.py)
           |
           +--> Conversation Service (Phase 5, reused unmodified)
                   |
                   +--> STT (provider abstraction)                       [READY — abstraction + mock tested;
                   |                                                       real provider untested, no credentials]
                   +--> RAG (PostgreSQL + pgvector, PDF knowledge base)   [READY WITHOUT GATEWAY —
                   |                                                       implemented and tested, see below]
                   +--> LLM (provider abstraction, grounded answers only) [READY — abstraction + mock tested;
                   |                                                       real provider untested, no credentials]
                   +--> TTS (provider abstraction)                       [READY — abstraction + mock tested;
                   |                                                       real provider untested, no credentials]
                   |
                   +--> Escalate to staff routing if low-confidence / requested   [NOT YET IMPLEMENTED]
```

## AI conversation orchestration (Phase 5)

Status: **READY** (text/audio API, file/buffer based) — no telephony or
SMG4004 involvement, no real-time streaming.

```
Audio (buffer, e.g. an uploaded file)
   |
   v
STT (provider abstraction: "openai" | "mock")
   |
   v
Conversation Service (app/services/conversation.py)
   |  - creates/loads an ai_sessions row, isolated by session id
   |  - stores the caller's message (ai_messages)
   v
RAG / pgvector (reuses Phase 4 search_chunks() as-is)
   |  - embeds the query, top-K + similarity-threshold filtered search
   |  - bounded context string built from results (app/services/rag_context.py),
   |    capped at AI_MAX_CONTEXT_CHARS — never the whole document
   v
LLM (provider abstraction: "openai" | "mock")
   |  - configurable system prompt (AI_SYSTEM_PROMPT, has a built-in
   |    default) + windowed conversation history (AI_MAX_HISTORY_MESSAGES)
   |    + the bounded RAG context
   |  - if no knowledge was retrieved, the prompt policy is what decides
   |    the response (mock provider mirrors this with a fixed "no
   |    supporting knowledge" answer; a real LLM is instructed the same way)
   v
Conversation Service
   |  - stores the assistant's reply (ai_messages)
   v
TTS (provider abstraction: "openai" | "mock")
   |
   v
Audio (buffer)
```

**Providers**: `app/providers/{stt,llm,tts}/` — each has a `base.py` ABC,
a deterministic `mock.py` (dev/test only, clearly documented as not
recreating real behavior), an `openai_provider.py` (lazy-imports `openai`,
never called unless the provider is explicitly configured), and a
`factory.py` that fails loudly if unconfigured rather than silently
falling back to a mock. No real provider call has been made — no working
API credentials were available this phase (see the Phase 5 report). The
abstractions and mock-backed pipeline are fully implemented and tested.

**API**: `POST /api/conversation/sessions`, `GET
/api/conversation/sessions/{id}`, `POST
/api/conversation/sessions/{id}/messages` (primary dev/test path — text
in, text out), `POST /api/conversation/sessions/{id}/audio` (file-based
audio in, base64 audio out), `POST
/api/conversation/sessions/{id}/end`. All gated behind `INTERNAL_API_KEY`,
same as the Phase 4 knowledge endpoints.

**Session isolation**: every session is its own `ai_sessions` row;
`ai_sessions.call_id` was made nullable (migration `c88159bbc9cf`) since
these sessions aren't attached to a phone call yet. All message
reads/writes are scoped by `session_id` — verified with a dedicated
cross-session-isolation test.

**NOT YET (as of Phase 5)**: Asterisk/ARI call control (added in Phase 6,
below), real-time audio streaming, GSM/SMG4004 integration, production
voice calling, staff escalation logic.

## Asterisk AI call control (Phase 6)

Status: **READY WITHOUT GATEWAY** — implemented and verified live against
the real WSL Asterisk instance (ARI auth, call answer, welcome tone,
recording, a full turn, hangup, and two-concurrent-call isolation). No
SMG4004/GSM involvement; no real-time bidirectional audio streaming
(explicitly out of scope this phase — see "Known limitations" below).

```
SIP call to extension 700 (AI_TEST_EXTENSION)
   |
   v
Asterisk dialplan (asterisk/etc/dialplan/ai_agent.conf)
   |  Stasis(ai-agent) — hands the channel to ARI, does nothing else
   v
ARI ("ai-agent" Stasis app, localhost:8088 only — never public)
   |
   v
AI Call Controller (app/services/call_controller.py, run via
   |  app/ai_call_worker.py — a standalone process, not part of the
   |  FastAPI web server, since it holds a long-lived ARI WebSocket)
   |
   |  On StasisStart:
   |    - creates a `calls` row (asterisk_channel_id, direction, caller
   |      number, ai_handled=true) and an `ai_sessions` row linked to it
   |      (reuses the existing Phase 2 tables — no new tables needed)
   |    - answers the channel, plays a configurable welcome message
   |      (AI_WELCOME_MESSAGE), starts recording the caller's turn
   |
   |  On RecordingFinished (one caller turn):
   |    - reads the recorded file from ASTERISK_RECORDING_SPOOL_PATH
   |    - calls the Phase 5 conversation service (handle_text_turn, via
   |      STT first) UNMODIFIED — no AI logic lives in the call controller
   |    - synthesizes the reply, plays it back, starts the next recording
   |
   |  On hangup (from either party) or call-timeout:
   |    - marks the `ai_sessions`/`calls` rows completed/failed with an
   |      end timestamp and cause, hangs up the channel if still up
   v
Caller hears the reply, loop repeats until hangup
```

**Call/session mapping**: reuses the existing `calls` + `ai_sessions`
tables exactly as originally designed (`ai_sessions.call_id` FK) — no
migration needed this phase. The controller keeps a small in-memory
`channel_id -> (call row id, session id)` map only for the duration of
each active call (`CallState`); the database remains the durable source
of truth, so state is inspectable and correct even if the map is empty
(e.g. right after a restart — in-flight calls from before a restart just
won't be found and will hang up cleanly if AI events reference them).

**Session isolation under concurrency**: every call gets its own `calls`
+ `ai_sessions` row and its own `CallState` keyed by Asterisk channel id
— never a shared/global variable. Verified two ways: a fully deterministic
automated test (`tests/test_call_controller.py::test_two_concurrent_calls_are_fully_isolated`,
using a fake ARI client) and a **live** test — two simultaneous
`channel originate` calls into extension 700 against the real Asterisk
instance, confirmed as two distinct `calls` rows with independent
histories and independent hangups.

**TEST MODE vs PRODUCTION MEDIA MODE**: when `TTS_PROVIDER=mock`, the
"synthesized audio" is placeholder bytes Asterisk can't play as speech —
the controller plays a short, genuinely bounded generated beep instead
(see Phase 7 below for why — an indications.conf tone was tried first and
found to have an impractical ~15s cadence), so the ARI Playback call
itself is still exercised, and logs the text that would have been
spoken. This is not a silent fallback from a *configured* real provider —
`TTS_PROVIDER` is never swapped; only how already-mock output is handled
downstream differs. With `TTS_PROVIDER=openai` configured, the controller
normalizes and writes the real synthesized audio to disk and plays it by
reference instead (see Phase 7's audio format work).

**ARI security**: HTTP server (`asterisk/etc/http.conf`) bound to
`127.0.0.1:8088` only — never exposed publicly. Dedicated `ai-agent` ARI
user (`asterisk/etc/ari.conf`, gitignored, generated by
`asterisk/scripts/generate_ari_secret.sh`) scoped to nothing but this one
Stasis app. No AMI was added — ARI alone was sufficient, as expected.

**Existing IVR untouched**: extension 700 was added as a new file
(`ai_agent.conf`) included into the existing `[internal]` context — the
1001/1002/1003 test extensions, the DTMF IVR at extension 600, and call
recording all remain exactly as Phase 3 left them (verified by rerunning
the Phase 3 checks after deploying this phase's config).

**Known limitations as of Phase 6** (updated in Phase 7 below — real
audio capture/format handling is now verified; real STT/LLM/TTS provider
calls still are not):

- **No barge-in / interruption support.** The caller cannot interrupt
  playback; if they hang up mid-playback, the controller detects it via
  the hangup event and cleans up rather than leaving a stuck recording,
  but doesn't stop the audio mid-sentence. A later phase, per the brief.
- **Real-time streaming is not implemented.** Each turn is a discrete
  record-then-process-then-play cycle (matches Phase 5's file/buffer-based
  design intentionally) — not continuous/low-latency audio.

## Real audio + real provider integration (Phase 7)

Status: **READY WITHOUT GATEWAY** for audio capture/format handling and
provider structural correctness — verified live with real Asterisk
recordings and real (non-mock) WAV audio. **NOT verified**: any actual
call to a real STT/LLM/TTS vendor — no working API credentials were
available (see the Phase 7 report). No SMG4004/GSM involvement.

```
SIP Test Endpoint
   |
   v
Asterisk
   |
   v
ARI
   |
   v
AI Call Controller
   |
   v
Audio Capture (ARI record() -> WAV file, read from ASTERISK_RECORDING_SPOOL_PATH)
   |
   v
Audio validation (app/services/audio.py):
   |  - is it a readable WAV? if not, skip the format check and let STT
   |    reject it directly (keeps the mock-STT/fake-ARI test convention working)
   |  - too short (< 0.3s) or effectively silent (RMS below threshold)?
   |    skip STT entirely, re-prompt instead of burning a provider call
   v
STT (unchanged from Phase 5/6 — audio bytes passed through as-is;
   |  Whisper-style providers resample server-side, no conversion needed)
   v
RAG / pgvector (unchanged from Phase 4/5)
   |
   v
LLM (unchanged from Phase 5/6)
   |
   v
TTS -> normalize_for_asterisk_playback() if the output isn't already
   |    8kHz/16kHz mono 16-bit PCM (OpenAI's 24kHz wav output isn't —
   |    resampled with stdlib `audioop`, no new dependency)
   v
Asterisk Playback (by file reference; controller now waits for
                    PlaybackFinished before starting the next recording,
                    so the AI's own voice is never captured back into
                    the caller's next turn)
```

### Audio format map (verified empirically, not assumed)

| Stage | Format | How verified |
|---|---|---|
| Asterisk ARI recording (caller audio) | WAV, 8kHz, mono, 16-bit signed PCM | Live: recorded a real ~21s clip via `channel originate ... application Milliwatt`, read it back with `app.services.audio.read_wav_info` — exactly 8000Hz/1ch/16-bit, confirmed non-silent |
| STT input | Same as above, unconverted | Whisper-family APIs accept arbitrary sample rates; no conversion applied |
| TTS output (OpenAI, `response_format="wav"`) | WAV, 24kHz, mono, 16-bit PCM | OpenAI's documented fixed rate for that response format |
| Asterisk playback | WAV, 8kHz **or** 16kHz mono 16-bit PCM only | `asterisk -rx "module show like format"` — `format_wav.so`'s own description: "Microsoft WAV/WAV16 format (8kHz/16kHz Signed Linear)"; 24kHz confirmed NOT playable |

So exactly one real conversion is needed in this whole pipeline: TTS
output → 8kHz mono 16-bit PCM before Asterisk can play it back
(`normalize_for_asterisk_playback`, stdlib `wave`+`audioop` only).

### What changed in the call controller (Section 12/13/14 hardening)

- **Playback now waits for completion.** Previously, `_synthesize_and_play`
  returned as soon as ARI accepted the Playback request, not when the
  audio actually finished — the next recording could start while the
  AI's own reply was still playing, risking Asterisk hearing/recording
  itself. Fixed with an `asyncio.Event` per playback id, resolved by the
  `PlaybackFinished` ARI event, with a `PROVIDER_TIMEOUT_SECONDS` timeout
  as a safety net.
- **Silence/too-short audio is now filtered before STT.** A turn whose
  recording is under 0.3s or below an RMS silence threshold skips the STT
  call entirely and just re-prompts — avoiding wasted (billed, for a real
  provider) calls on dead air.
- **A consecutive-failure cap (`AI_MAX_CONSECUTIVE_FAILURES`, default 3)
  ends a call gracefully** (a goodbye message, then hangup) instead of
  looping forever through silence/STT-failure/provider-failure turns.
  Resets to zero on any successful turn.
- **Explicit timeouts added where they were missing**: the embedding
  provider had none at all (fixed — reuses `PROVIDER_TIMEOUT_SECONDS`,
  same as STT/LLM/TTS already had); the RAG/pgvector query now has one
  too; and a new overall `AI_TURN_TIMEOUT_SECONDS` caps the whole
  embed+search+LLM sequence as a belt-and-suspenders on top of each
  individual provider's own timeout. A timeout raises
  `ConversationTimeoutError` (a `ConversationError` subclass, so existing
  handling still works) — the HTTP API maps it to `504`, the call
  controller plays the same safe-error message it uses for any other
  provider failure.
- **Concurrency: `run_forever` now dispatches each ARI event as its own
  task** instead of awaiting them one at a time. Without this, a slow
  provider call on one channel would block ARI event processing —
  therefore progress — on every *other* concurrent call, since all
  channels share one WebSocket event stream. Verified with a dedicated
  test using a deliberately slow mock LLM on one channel racing a fast
  one on another; per-channel state (`CallState`) was already isolated
  (Phase 6), so this only affects *when* events are processed, not
  correctness.

### A real bug found and fixed via live testing

The Phase 6 mock-TTS "tone" cue used `tone:record` (an indications.conf
tone). Testing it live for the first time with the new
wait-for-playback-completion logic revealed why that was a poor choice:
`record`'s actual cadence is defined as 1400Hz for 80ms, then **~15
seconds of silence**, per cycle — so ARI took ~15s to report
`PlaybackFinished` for what was meant to be a quick beep, effectively
stalling every mock-mode turn. Fixed by generating a real, short,
correctly-formatted WAV beep in code (`make_short_beep_wav`, 0.3s, stdlib
only) instead of relying on an indications.conf tone's timing.

### Provider verification approach (no live API calls made)

- **Structural tests** (`tests/test_openai_providers_structural.py`, 25
  tests): a fake `openai.AsyncOpenAI`-shaped client
  (`tests/fake_openai.py`) injected directly as `provider._client`, so
  every OpenAI provider class's parameter passing, response parsing,
  timeout handling, and error wrapping is verified — for valid input,
  empty/invalid input, timeouts, auth failures, generic provider
  failures, and malformed responses — with zero network calls and zero
  cost.
- **Real, non-cost audio fixtures** (`tests/audio_fixtures.py`): genuine
  WAV files (silence, a sine tone, noise) — not the Phase 5/6 convention
  of "audio bytes are just UTF-8 text" — for testing the audio
  normalization/validation logic itself.
- **Opt-in real-provider integration test**
  (`tests/test_real_provider_integration.py`): the minimal controlled
  round-trip from the brief (text → TTS → audio → STT → text → RAG →
  LLM → reply → TTS), gated behind `REAL_PROVIDER_TESTS=1` *and* all
  three of `STT_PROVIDER`/`LLM_PROVIDER`/`TTS_PROVIDER` being configured
  to `openai` with matching API keys — otherwise skipped. **Not run this
  phase**: no working API credentials were available anywhere in this
  environment (confirmed by inspecting `backend/.env` — every
  `*_API_KEY` is blank). No paid call was made.

### Known limitations (Phase 7)

- **No real STT/LLM/TTS vendor call has ever been made in this project.**
  Everything above is verified either structurally (fake SDK client) or
  against real Asterisk audio with mock providers. The moment real
  credentials are available, run
  `REAL_PROVIDER_TESTS=1 pytest tests/test_real_provider_integration.py -v`
  for the first controlled verification, with your explicit go-ahead.
- **Silence-based turn-taking (`maxSilenceSeconds`) still hasn't been
  observed triggering live.** The Milliwatt-based live test used a
  *continuous* tone (never silent), which is what proved the recording
  format/silence-detection logic works on real Asterisk output — but by
  construction it never lets `maxSilenceSeconds` fire either. This needs
  a real caller (or a SIPp scenario streaming real intermittent audio) to
  observe.
- Barge-in and real-time streaming remain out of scope, same as Phase 6.

## RAG / knowledge ingestion pipeline (Phase 4)

Status: **READY WITHOUT GATEWAY** — fully implemented and tested, no
telephony or SMG4004 involvement.

```
PDF (uploaded via POST /api/knowledge/upload)
   |
   v
Text extraction (pypdf) — per-page, validates it's actually a PDF,
   |                       detects empty/scanned PDFs and rejects clearly
   v
Whitespace normalization
   |
   v
Chunking (paragraph-aware, configurable size/overlap,
   |       page number preserved per chunk)
   v
Embedding provider (pluggable: "openai" | "mock" for dev/test —
   |                  never called with a real vendor unless configured)
   v
pgvector (knowledge_chunks.embedding, vector(1536))
   |
   v
Similarity search (POST /api/knowledge/search — cosine distance,
                    top-K, optional similarity threshold)
```

**Not yet built on top of this**: the LLM conversation layer that would
take search results and produce a spoken answer (STT → RAG → LLM → TTS,
per the original architecture) — this phase stops at retrieval.

Full details, API endpoints, and test results: see the Phase 4 report /
`backend/app/services/{pdf_extraction,chunking,knowledge_ingestion,knowledge_search}.py`.

**Known pgvector gotcha, fixed in this phase**: the `knowledge_chunks`
table's `ivfflat` index was originally created in the Phase 2 migration
while the table was empty. pgvector computes an ivfflat index's cluster
centroids from whatever data exists at `CREATE INDEX` time, so an index
built empty is degenerate and can silently return zero results for
`ORDER BY embedding <=> :query LIMIT :n` — confirmed directly against a
live Postgres instance (Postgres itself warns "ivfflat index created with
little data ... This will cause low recall" on creation). Fixed by
dropping the index (migration `5e5509c26b5e`) and relying on an exact
sequential scan, which is fast enough at the row counts this project
expects (tens to low hundreds of chunks per small-business knowledge
base). Re-add an ANN index later only if the corpus grows large enough
to need one.

## Infrastructure

Single Ubuntu/Debian VPS:

- **Asterisk (PJSIP)** — SIP signaling, IVR, dialplan, call recording (MixMonitor), call bridging.
- **FastAPI backend** — AI voice agent orchestration, knowledge ingestion API, admin/config API, internal auth for Asterisk-originated requests (AGI/ARI).
- **PostgreSQL + pgvector** — call logs, IVR selections, AI sessions/messages, recordings metadata, knowledge chunks + embeddings, staff routes, GSM channel config.
- **Nginx** — reverse proxy + TLS termination for the FastAPI backend's public-facing endpoints (knowledge upload, admin) only; internal AGI/ARI traffic stays local.
- **WireGuard** — VPN tunnel between the SMG4004 and the VPS so SIP/RTP is not exposed directly to the public internet.

Explicitly avoided: Kubernetes, multiple VPS nodes, message queues (Kafka/Redis clusters), managed enterprise PBX, custom SIP stack, unnecessary microservices.

## Data model

`calls`, `call_events`, `ivr_selections`, `ai_sessions`, `ai_messages`,
`recordings`, `staff_routes`, `gsm_channels`, `knowledge_documents`,
`knowledge_chunks`, `system_logs`. Implemented in Phase 2 as SQLAlchemy
models + an Alembic migration under `db/migrations/` (see
`backend/app/models/`), running against PostgreSQL + pgvector. Not yet
wired up to Asterisk (the Phase 3 dialplan logs to Asterisk's own
CDR/log files, not to these tables — that integration is a later phase).

## Concurrency model

- Every call gets a unique `call_id` (Asterisk channel-derived) correlated with a `session_id` for any AI interaction.
- No global/shared in-process call state — all state lives in the database or is passed explicitly between the dialplan and the FastAPI backend per request.
- The SMG4004's channel count (default assumed 4, confirmed via `SMG4004_CHANNEL_COUNT`) is the hard concurrency ceiling for GSM legs; the software must not assume more concurrent GSM calls than the gateway physically supports.

## SMG4004 items pending verification

The following must be confirmed against Synway's official SMG4004 documentation/firmware notes before the corresponding Asterisk configuration is finalized (see [docs/smg4004.md], added in Phase 19):

1. SIP registration model (gateway registers to Asterisk, or vice versa, or static peer).
2. Supported SIP transport(s) (UDP/TCP/TLS).
3. DTMF signaling method actually used end-to-end over GSM (RFC2833 vs SIP INFO vs in-band) — GSM-side DTMF delivery is carrier/hardware dependent and cannot be assumed.
4. Whether per-channel/per-SIM outbound routing is controlled from Asterisk (e.g. via SIP URI user-part = channel number) or must be configured on the gateway itself.
5. Codec support and any transcoding requirements.
6. NAT/keepalive requirements for the SIP trunk.

Until verified, these are implemented as configurable placeholders only — never hard-coded assumptions.

The following remain **explicitly NOT TESTED** until the physical
SMG4004 is available — none of this is claimed to work, regardless of
how complete the Asterisk/ARI/AI layers above are:

- GSM → SIP inbound call handling
- SIP → GSM outbound call handling
- SIM slot / GSM channel mapping
- Real GSM caller ID behavior
- GSM-path DTMF behavior
- GSM audio quality and echo behavior
- GSM codec behavior/transcoding
- Multi-SIM concurrency
- Gateway registration behavior
- Gateway-specific SIP headers/settings
