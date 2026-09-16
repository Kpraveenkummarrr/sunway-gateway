# Pre-UAT release report

Date: 2026-09-16

## Scope of this local release

This release addresses local AI voice, RAG grounding, latency diagnostics,
audio diagnostics, and barge-in control. It does not include new call-centre
or admin-GUI features and was not deployed remotely.

## Changes

- `f3f1c5d`: hybrid knowledge retrieval and embedding-space metadata.
- `fbffc24`: local latency diagnostics, conversational Hindi policy,
  grounding prompt, audio measurements, and ARI barge-in control.
- `f7c252b`: timing-name compatibility and Hindi-policy regression cleanup.

## Local evidence

- Focused local tests: 67 passed (audio, TTS, embeddings, RAG helpers, AGI
  routing, controller barge-in, provider structure, and Hindi-policy checks).
- Full DB-backed regression: not runnable because PostgreSQL refused the local
  connection.
- No API keys, passwords, SIP secrets, or client data were printed or added.
- Working tree was clean after the implementation commits.

## Not established locally

- Bhashini ASR/TTS latency or naturalness.
- Gemini live latency or answer quality.
- GSM codec, RTP, echo, noise, or one-way audio behavior.
- Actual client database contents and Lumpy Skin Disease retrieval against the
  client's PDF.
- Barge-in detection delay and false-trigger rate on the GSM path.

## UAT decision

Not ready to declare client UAT complete. The code path is locally prepared,
but the client must re-index the knowledge base with the configured embedding
provider and run real Hindi/GSM acceptance calls before voice quality or
barge-in can be accepted.
