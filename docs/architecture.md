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
Asterisk (PJSIP)                        [READY WITHOUT GATEWAY — testable via SIP softphones]
   |
   +--> IVR (DTMF menu)                 [READY WITHOUT GATEWAY]
   |       |
   |       +--> Staff routing (Sales / Support / Accounts)   [READY WITHOUT GATEWAY for SIP leg;
   |       |                                                   REQUIRES PHYSICAL SMG4004 for outbound GSM leg]
   |       v
   |    Staff mobile (via SMG4004 outbound GSM channel)       [REQUIRES PHYSICAL SMG4004]
   |
   +--> AI Voice Agent (FastAPI backend)  [READY WITHOUT GATEWAY]
           |
           +--> STT (provider abstraction)
           +--> RAG (PostgreSQL + pgvector, PDF knowledge base)
           +--> LLM (provider abstraction, grounded answers only)
           +--> TTS (provider abstraction)
           |
           +--> Escalate to staff routing if low-confidence / requested
```

## Infrastructure

Single Ubuntu/Debian VPS:

- **Asterisk (PJSIP)** — SIP signaling, IVR, dialplan, call recording (MixMonitor), call bridging.
- **FastAPI backend** — AI voice agent orchestration, knowledge ingestion API, admin/config API, internal auth for Asterisk-originated requests (AGI/ARI).
- **PostgreSQL + pgvector** — call logs, IVR selections, AI sessions/messages, recordings metadata, knowledge chunks + embeddings, staff routes, GSM channel config.
- **Nginx** — reverse proxy + TLS termination for the FastAPI backend's public-facing endpoints (knowledge upload, admin) only; internal AGI/ARI traffic stays local.
- **WireGuard** — VPN tunnel between the SMG4004 and the VPS so SIP/RTP is not exposed directly to the public internet.

Explicitly avoided: Kubernetes, multiple VPS nodes, message queues (Kafka/Redis clusters), managed enterprise PBX, custom SIP stack, unnecessary microservices.

## Data model (planned tables)

`calls`, `call_events`, `ivr_selections`, `ai_sessions`, `ai_messages`,
`recordings`, `staff_routes`, `gsm_channels`, `knowledge_documents`,
`knowledge_chunks`, `system_logs`. Defined in Phase 8–11 as Alembic
migrations under `db/migrations/`.

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
