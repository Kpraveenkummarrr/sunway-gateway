# Synway SMG4004 GSM Gateway + Asterisk + IVR + AI Voice Agent

Small-business telephone automation system built on a Synway SMG4004 GSM
gateway, Asterisk (PJSIP), an IVR with staff call routing, and an optional
AI voice agent that answers caller questions using a PDF/knowledge base
(RAG over PostgreSQL + pgvector).

## Status

Project is at **Phase 6 (Asterisk AI call control via ARI)**. See
[docs/architecture.md](docs/architecture.md) for the target architecture,
[docs/asterisk.md](docs/asterisk.md) for the Asterisk/PJSIP/IVR setup and
test results, and
[docs/client-information-required.md](docs/client-information-required.md)
for what's needed from the client before later phases can proceed.

## Repository layout

```
asterisk/           Asterisk configuration (PJSIP, dialplan, IVR) — not installed system config
  etc/pjsip/         PJSIP trunk/endpoint templates
  etc/dialplan/       extensions.conf fragments (IVR, routing)
  scripts/            AGI/helper scripts called from the dialplan

backend/             FastAPI AI voice agent + admin/config API
  app/api/            HTTP route handlers
  app/services/        Business logic (call sessions, RAG, escalation, etc.)
  app/models/          SQLAlchemy models / Pydantic schemas
  app/providers/        STT / TTS / LLM provider abstractions
    stt/
    tts/
    llm/
  app/core/            Config, logging, security, DB session
  tests/               Automated tests

db/
  migrations/          Alembic migrations (calls, recordings, knowledge base, etc.)

docs/                  Architecture, installation, security, troubleshooting, etc.
scripts/               Deployment / ops scripts (backups, firewall setup, etc.)
recordings/            Local call recordings (gitignored; not committed)
```

## Requirements (target deployment)

- Ubuntu/Debian VPS
- Asterisk (PJSIP)
- Python 3.11+ / FastAPI
- PostgreSQL 15+ with `pgvector`
- Nginx (reverse proxy / TLS)
- WireGuard (VPN link to the SMG4004)

## Getting started

1. Copy `.env.example` to `backend/.env` and fill in real values (never commit `.env`).
2. See `docs/installation.md` (added in a later phase) for Asterisk setup.
3. For the backend + database, see below.

## Backend development setup (Phase 2)

Tested on Windows with PostgreSQL + pgvector running under WSL2 (Ubuntu
22.04); the same commands apply on a native Ubuntu/Debian host with `wsl -d
Ubuntu -u root -- ...` dropped.

### 1. PostgreSQL + pgvector

```bash
# Ubuntu/Debian (or inside WSL as root)
apt-get install -y postgresql postgresql-contrib postgresql-server-dev-all build-essential git

# Build pgvector from source (no Windows binary; Ubuntu has no prebuilt package
# for all versions either, so building from source is the reliable path)
cd /usr/src
git clone --branch v0.7.4 --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector
make
make install

# Create the app role, database, and enable the extension
sudo -u postgres psql <<'SQL'
CREATE ROLE sunway_user LOGIN PASSWORD '<choose a strong password>';
CREATE DATABASE sunway_gateway OWNER sunway_user;
SQL
sudo -u postgres psql -d sunway_gateway -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Put the resulting connection string in `backend/.env` as `DATABASE_URL`
(`postgresql+asyncpg://sunway_user:<password>@<host>:5432/sunway_gateway`).
Never commit `backend/.env` — it's gitignored; `.env.example` at the repo
root only holds placeholders.

> On Windows + WSL2, PostgreSQL listening on `127.0.0.1:5432` inside WSL is
> reachable from Windows as `127.0.0.1:5432` via WSL2's automatic localhost
> forwarding — no extra networking config needed for local development.

### 2. Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt           # Linux/macOS

# Apply database migrations
./.venv/Scripts/python.exe -m alembic upgrade head

# Run the API
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Verify:

```bash
curl http://127.0.0.1:8000/health   # liveness — process is up
curl http://127.0.0.1:8000/ready    # readiness — DB + pgvector reachable
```

### 3. Tests

```bash
cd backend
./.venv/Scripts/python.exe -m pytest -v
```

## Knowledge base / PDF ingestion (Phase 4)

No API key needed for local dev/testing — set `RAG_EMBEDDING_PROVIDER=mock`
in `backend/.env` (already the default in this repo's local setup). This
produces deterministic but semantically meaningless vectors; never use it
for a real knowledge base. For real ingestion, set
`RAG_EMBEDDING_PROVIDER=openai` and `RAG_EMBEDDING_API_KEY` (and
`pip install openai`, not installed by default).

With the backend running (`uvicorn app.main:app --reload`):

```bash
# Upload a PDF (add -H "X-Internal-Api-Key: <key>" if INTERNAL_API_KEY is set)
curl -X POST http://127.0.0.1:8000/api/knowledge/upload \
  -F "file=@/path/to/document.pdf;type=application/pdf"

# List documents
curl http://127.0.0.1:8000/api/knowledge

# Get one document's status
curl http://127.0.0.1:8000/api/knowledge/<document_id>

# Search
curl -X POST http://127.0.0.1:8000/api/knowledge/search \
  -H "Content-Type: application/json" \
  -d '{"query": "your question here"}'

# Delete
curl -X DELETE http://127.0.0.1:8000/api/knowledge/<document_id>
```

Tests (`backend/tests/test_pdf_extraction.py`, `test_chunking.py`,
`test_embeddings.py`, `test_knowledge_ingestion.py`,
`test_knowledge_search.py`, `test_knowledge_api.py`) run against the real
dev database with the mock embedding provider — no network calls, no cost:

```bash
cd backend
./.venv/Scripts/python.exe -m pytest -v
```

## AI conversation orchestration (Phase 5)

Same mock-provider approach as Phase 4 — set `LLM_PROVIDER=mock`,
`STT_PROVIDER=mock`, `TTS_PROVIDER=mock` in `backend/.env` (already the
default in this repo's local setup) for local dev/testing with no API
key and no cost. For real responses, set the provider to `openai` and the
matching `*_API_KEY` (and `pip install openai`, not installed by
default) — **no real provider has been tested in this project**; no
working API credentials were available during development.

With the backend running and at least one knowledge document uploaded
(see above):

```bash
# Create a session
curl -X POST http://127.0.0.1:8000/api/conversation/sessions \
  -H "Content-Type: application/json" -d '{}'

# Send a text message (primary dev/test path)
curl -X POST http://127.0.0.1:8000/api/conversation/sessions/<session_id>/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "your question here"}'

# Get session + full history
curl http://127.0.0.1:8000/api/conversation/sessions/<session_id>

# Send audio (file/buffer based — not a real-time stream; with the mock
# STT provider, the "audio" file's bytes are just UTF-8 text)
curl -X POST http://127.0.0.1:8000/api/conversation/sessions/<session_id>/audio \
  -F "file=@question.txt;type=application/octet-stream"

# End the session
curl -X POST http://127.0.0.1:8000/api/conversation/sessions/<session_id>/end
```

Tests (`test_stt.py`, `test_llm.py`, `test_tts.py`, `test_rag_context.py`,
`test_conversation.py`, `test_audio_orchestration.py`,
`test_conversation_api.py`) run against the real dev database with mock
providers — no network calls, no cost:

```bash
cd backend
./.venv/Scripts/python.exe -m pytest -v
```

## Asterisk / PJSIP setup (Phase 3)

Native install (no Docker), SIP softphone testing only — see
[docs/asterisk.md](docs/asterisk.md) for full details and test results.

```bash
sudo apt-get install -y asterisk
cd asterisk/scripts
./generate_sip_secrets.sh   # generates test-extension passwords, gitignored
sudo ./deploy.sh            # backs up existing config, deploys ours, reloads
```

Verify:

```bash
sudo asterisk -rx "pjsip show endpoints"
sudo asterisk -rx "dialplan show internal"
```

## Asterisk AI call control (Phase 6)

ARI-based call control connecting Asterisk to the Phase 5 conversation
service — no SMG4004/GSM involved yet, tested via a dedicated extension
(`700`) and Asterisk CLI/SIP test calls. Full details, live test results,
and known limitations: [docs/asterisk.md](docs/asterisk.md#ari--ai-call-control-phase-6).

```bash
# Enable ARI (needs a full restart, not just a reload, for http.conf changes)
sudo systemctl restart asterisk

cd asterisk/scripts
./generate_ari_secret.sh    # generates the ARI user's password, gitignored
sudo ./deploy.sh            # deploys ari.conf, http.conf, the ai_agent.conf dialplan

# Run the call controller (separate long-lived process, not part of uvicorn)
cd backend
python -m app.ai_call_worker
```

Test without a phone: `asterisk -rx "channel originate Local/700@internal application Wait 15"`
while the worker above is running, then check the `calls`/`ai_sessions`
tables for the resulting row.

Tests (`test_call_controller.py`, using a fake ARI client — no live
Asterisk needed):

```bash
cd backend
./.venv/Scripts/python.exe -m pytest tests/test_call_controller.py -v
```

## Important note on the gateway

This project **never assumes undocumented Synway SMG4004 behavior**. Any
capability that cannot be verified from official documentation or requires
the physical device is explicitly marked `REQUIRES PHYSICAL SMG4004` in the
docs and code comments, rather than implemented speculatively.
