# Synway SMG4004 GSM Gateway + Asterisk + IVR + AI Voice Agent

Small-business telephone automation system built on a Synway SMG4004 GSM
gateway, Asterisk (PJSIP), an IVR with staff call routing, and an optional
AI voice agent that answers caller questions using a PDF/knowledge base
(RAG over PostgreSQL + pgvector).

## Status

Project is at **Phase 3 (Asterisk/PJSIP telephony foundation)**. See
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

## Important note on the gateway

This project **never assumes undocumented Synway SMG4004 behavior**. Any
capability that cannot be verified from official documentation or requires
the physical device is explicitly marked `REQUIRES PHYSICAL SMG4004` in the
docs and code comments, rather than implemented speculatively.
