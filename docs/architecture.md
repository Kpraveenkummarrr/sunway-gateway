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
the controller plays a short `tone:record` cue (an indications.conf tone,
no sound file needed) instead, so the ARI Playback call itself is still
exercised, and logs the text that would have been spoken. This is not a
silent fallback from a *configured* real provider — `TTS_PROVIDER` is
never swapped; only how already-mock output is handled downstream
differs. With `TTS_PROVIDER=openai` configured, the controller writes
the real synthesized audio to disk and plays it by reference instead.

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

**Known limitations** (all explicitly NOT claimed as working):

- **No real bidirectional audio tested end-to-end.** The full
  record → STT → RAG → LLM → TTS → playback loop's *mechanics* are
  verified (live: answer/play/record/hangup all succeed against real
  Asterisk; automated: the full loop with a fake ARI client and real
  audio-shaped bytes). But no *real* audio (a softphone or SIPp actually
  speaking) was fed through it, and no real STT/LLM/TTS provider was
  called — no working credentials were available (see the Phase 6
  report). `Local` test channels (used for the live verification) don't
  generate real RTP audio, so Asterisk's silence-based turn-taking
  (`maxSilenceSeconds`) couldn't be observed triggering naturally in this
  environment — it's configured correctly and ARI accepted it, but only
  hangup/timeout-driven turn completion was actually observed live.
- **No barge-in / interruption support.** The caller cannot interrupt
  playback; if they hang up mid-playback, the controller detects it via
  the hangup event and cleans up rather than leaving a stuck recording,
  but doesn't stop the audio mid-sentence. A later phase, per the brief.
- **Real-time streaming is not implemented.** Each turn is a discrete
  record-then-process-then-play cycle (matches Phase 5's file/buffer-based
  design intentionally) — not continuous/low-latency audio.

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
