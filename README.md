# Synway SMG4004 GSM Gateway + Asterisk + IVR + AI Voice Agent

Small-business telephone automation system built on a Synway SMG4004 GSM
gateway, Asterisk (PJSIP), an IVR with staff call routing, and an optional
AI voice agent that answers caller questions using a PDF/knowledge base
(RAG over PostgreSQL + pgvector).

## Status

Project is at **Phase 1 (repository scaffold)**. See
[docs/architecture.md](docs/architecture.md) for the target architecture and
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

1. Copy `.env.example` to `.env` and fill in real values (never commit `.env`).
2. See `docs/installation.md` (added in a later phase) for Asterisk and
   backend setup steps.

## Important note on the gateway

This project **never assumes undocumented Synway SMG4004 behavior**. Any
capability that cannot be verified from official documentation or requires
the physical device is explicitly marked `REQUIRES PHYSICAL SMG4004` in the
docs and code comments, rather than implemented speculatively.
