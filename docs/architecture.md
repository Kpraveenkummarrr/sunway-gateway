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
   +--> AI Voice Agent (FastAPI backend)  [READY WITHOUT GATEWAY]
           |
           +--> STT (provider abstraction)              [NOT YET IMPLEMENTED]
           +--> RAG (PostgreSQL + pgvector, PDF knowledge base)   [READY WITHOUT GATEWAY —
           |                                                       implemented and tested, see below]
           +--> LLM (provider abstraction, grounded answers only) [NOT YET IMPLEMENTED]
           +--> TTS (provider abstraction)              [NOT YET IMPLEMENTED]
           |
           +--> Escalate to staff routing if low-confidence / requested
```

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
