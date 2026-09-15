# Production Deployment (Client Server)

Step-by-step setup for a **real Ubuntu server/VPS** — everything here has
already been verified working (in an equivalent native-Ubuntu environment,
via WSL2 during development; the commands are identical on a real Ubuntu
box). No Docker anywhere in this project.

This gets you to: PostgreSQL + pgvector running, the FastAPI backend and
AI call worker running as system services, Asterisk with PJSIP/IVR/ARI
configured, and three test SIP extensions you can register a softphone
against. **The physical SMG4004 gateway is not covered here** — see
[Step 11](#step-11--when-the-smg4004-arrives) and
[client-information-required.md](client-information-required.md).

## Prerequisites

- A fresh **Ubuntu 22.04 LTS** server (a small VPS is enough — 1-2 vCPU,
  2GB RAM comfortably covers Postgres + Asterisk + the backend for a
  handful of concurrent calls).
- Root or sudo SSH access.
- This repo's contents available on the server (git clone, or copy the
  files up — see Step 1).
- Filled-in values from
  [client-information-required.md](client-information-required.md)
  (staff numbers, IVR wording, PDFs, etc.) — not required to get the
  system *running*, but needed before it's actually useful to the client.

Everything below assumes you're SSH'd into the server as a sudo-capable
user, and installs things under `/opt/sunway-gateway`.

---

## Step 1 — Base server setup

```bash
sudo apt-get update && sudo apt-get upgrade -y

# A dedicated, unprivileged service user — the systemd units in
# scripts/systemd/ run as this user, not root.
sudo useradd --system --create-home --shell /usr/sbin/nologin sunway

sudo mkdir -p /opt/sunway-gateway
sudo chown sunway:sunway /opt/sunway-gateway
```

Get the code onto the server (either works):

```bash
# Option A: clone directly (if the server can reach GitHub and the repo is accessible)
sudo -u sunway git clone https://github.com/<your-org>/<your-repo>.git /opt/sunway-gateway

# Option B: push from your machine
#   scp -r . youruser@server:/tmp/sunway-gateway && sudo mv /tmp/sunway-gateway/* /opt/sunway-gateway/ && sudo chown -R sunway:sunway /opt/sunway-gateway
```

## Step 2 — PostgreSQL + pgvector

```bash
sudo apt-get install -y postgresql postgresql-contrib postgresql-server-dev-all build-essential git

# pgvector has no Ubuntu package for every version — build from source
# (this is exactly what was done in development; see docs/architecture.md)
cd /usr/src
sudo git clone --branch v0.7.4 --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector
sudo make
sudo make install
```

Create the database and a dedicated role (generate a real password — **do
not** reuse the example below):

```bash
openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 32   # copy this, you'll need it in Step 3

sudo -u postgres psql <<'SQL'
CREATE ROLE sunway_user LOGIN PASSWORD 'PASTE_THE_GENERATED_PASSWORD_HERE';
CREATE DATABASE sunway_gateway OWNER sunway_user;
SQL
sudo -u postgres psql -d sunway_gateway -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Verify:

```bash
sudo -u postgres psql -d sunway_gateway -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
# should show: vector | 0.7.4
```

PostgreSQL listens on `127.0.0.1:5432` by default — leave it that way,
never expose it publicly (the firewall script in Step 7 denies it
explicitly too, as defense in depth).

## Step 3 — Backend (FastAPI)

```bash
sudo apt-get install -y python3-venv python3-pip

cd /opt/sunway-gateway/backend
sudo -u sunway python3 -m venv .venv
sudo -u sunway ./.venv/bin/pip install --upgrade pip
sudo -u sunway ./.venv/bin/pip install -r requirements.txt
```

Create `backend/.env` from the template and fill in real values:

```bash
sudo -u sunway cp /opt/sunway-gateway/.env.example /opt/sunway-gateway/backend/.env
sudo -u sunway nano /opt/sunway-gateway/backend/.env
```

At minimum, set:

- `DATABASE_URL` — `postgresql+asyncpg://sunway_user:<password from Step 2>@127.0.0.1:5432/sunway_gateway`
- `APP_SECRET_KEY` — generate one: `openssl rand -base64 32`
- `INTERNAL_API_KEY` — generate one the same way (protects the knowledge/conversation APIs)
- `KNOWLEDGE_STORAGE_PATH` — e.g. `/opt/sunway-gateway/backend/data/knowledge_documents`
- `LLM_PROVIDER`, `STT_PROVIDER`, `TTS_PROVIDER`, `RAG_EMBEDDING_PROVIDER` — leave as `mock` for
  now (see [Step 9](#step-9--enabling-real-ai-providers-optional-costs-money) to go live later)
- `ASTERISK_ARI_USERNAME`/`ASTERISK_ARI_PASSWORD` — set in Step 5, come back to this file after

Never commit this file — it's already gitignored.

Run migrations:

```bash
cd /opt/sunway-gateway/backend
sudo -u sunway ./.venv/bin/python -m alembic upgrade head
```

## Step 4 — Asterisk + PJSIP

```bash
sudo apt-get install -y asterisk
```

Generate SIP credentials for the three test extensions (1001-1003) and
deploy this repo's config:

```bash
cd /opt/sunway-gateway/asterisk/scripts
sudo -u sunway ./generate_sip_secrets.sh
sudo ./deploy.sh
```

`deploy.sh` backs up whatever config already exists to
`/var/backups/asterisk-config-<timestamp>/` before touching anything, and
disables the legacy `chan_sip` module (this project uses PJSIP only —
see [docs/asterisk.md](asterisk.md) for why).

Verify:

```bash
sudo asterisk -rx "pjsip show endpoints"   # should list 1001, 1002, 1003
sudo asterisk -rx "dialplan show internal" # should show 600 (IVR) and 700 (AI)
```

## Step 5 — Asterisk ARI (for the AI call worker)

```bash
cd /opt/sunway-gateway/asterisk/scripts
sudo ./generate_ari_secret.sh
```

This prints the `ai-agent` ARI password **once** — copy it into
`backend/.env` as `ASTERISK_ARI_PASSWORD` now.

`http.conf`'s `bindaddr`/`enabled` change needs a full restart, not just
a reload:

```bash
sudo systemctl restart asterisk
sudo asterisk -rx "http show status"   # should show "Server Enabled and Bound to 127.0.0.1:8088"
```

Give the `sunway` service user access to Asterisk's recording/sounds
directories (the AI worker reads caller recordings and writes TTS output
there):

```bash
sudo usermod -aG asterisk sunway
```

## Step 6 — systemd services

```bash
sudo cp /opt/sunway-gateway/scripts/systemd/sunway-backend.service /etc/systemd/system/
sudo cp /opt/sunway-gateway/scripts/systemd/sunway-ai-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sunway-backend
sudo systemctl enable --now sunway-ai-worker
```

Verify both came up clean:

```bash
sudo systemctl status sunway-backend --no-pager
sudo systemctl status sunway-ai-worker --no-pager
sudo asterisk -rx "ari show apps"   # should list "ai-agent" once the worker connects
```

## Step 7 — Firewall

```bash
cd /opt/sunway-gateway/scripts
# If you already know the SMG4004's IP, pass it to restrict SIP/RTP to just that source:
sudo ./setup_firewall.sh <smg4004_ip>
# Otherwise, for initial softphone testing:
sudo ./setup_firewall.sh
```

This opens SSH, SIP (5060/udp), and the RTP range (10000-20000/udp) only
— PostgreSQL, ARI, and AMI stay denied to the outside world (they're
already bound to `127.0.0.1`; this is belt-and-suspenders). Re-run once
the SMG4004's IP is known, if you ran it open initially.

## Step 8 — End-to-end verification

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
# both should return {"status":"ok", ...}

# Register a softphone (Zoiper, Linphone, etc.) as extension 1001, 1002,
# or 1003 against the server's IP, using the password from
# asterisk/etc/pjsip/pjsip_auth.conf (generated in Step 4, not committed
# to git — read it directly on the server if you didn't save it elsewhere).

# From a registered extension:
#   dial 600 -> hear the IVR, press 1/2/3 to route to the other test extensions
#   dial 700 -> AI test call (mock providers until Step 9)
```

See [docs/asterisk.md](asterisk.md) for the full manual test checklist
and [docs/architecture.md](architecture.md) for what's marked READY vs
NOT YET vs REQUIRES PHYSICAL SMG4004.

## Step 9 — Enabling real AI providers (optional, costs money)

The system runs entirely on `mock` providers out of the box — no API key
needed, no cost, but responses are canned/deterministic, not real AI.

**Hindi production (client requirement):** Bhashini for speech, OpenAI for
the LLM.

1. Install dependencies (includes `openai` and `httpx`):
   `sudo -u sunway /opt/sunway-gateway/backend/.venv/bin/pip install -r requirements.txt`
2. In `backend/.env` set:
   ```
   AI_LANGUAGE=hi
   STT_PROVIDER=bhashini
   TTS_PROVIDER=bhashini
   LLM_PROVIDER=openai
   LLM_MODEL=gpt-4o-mini
   LLM_API_KEY=<secret>
   BHASHINI_INFERENCE_URL=https://dhruva-api.bhashini.gov.in/services/inference/pipeline
   BHASHINI_INFERENCE_API_KEY=<secret>
   BHASHINI_ASR_SERVICE_ID=ai4bharat/conformer-hi-gpu--t4
   BHASHINI_TTS_SERVICE_ID=Bhashini/IITM/TTS
   BHASHINI_TTS_GENDER=female
   ```
   and **remove** any English `AI_WELCOME_MESSAGE` line so the built-in
   Hindi phrases are used (the worker logs a warning if one remains).
   Keep `RAG_EMBEDDING_PROVIDER` as the knowledge base was ingested with —
   changing embedding provider requires re-ingesting every document.
3. Verify with real (billed, minimal) calls before restarting:
   `cd /opt/sunway-gateway/backend && sudo -u sunway ./.venv/bin/python scripts/hindi_pipeline_smoke_test.py`
   — every line must be `PASS`; WAVs are written to `/tmp/sunway-smoke`.
4. Restart: `sudo systemctl restart sunway-ai-worker sunway-backend`

`mock` remains a valid value for any provider as a rollback. The older
all-OpenAI option (`STT_PROVIDER=openai`, `TTS_PROVIDER=openai`) is still
supported; its opt-in check is
`REAL_PROVIDER_TESTS=1 ./.venv/bin/python -m pytest tests/test_real_provider_integration.py -v`.

## Step 10 — Upload the knowledge base

```bash
curl -X POST http://127.0.0.1:8000/api/knowledge/upload \
  -H "X-Internal-Api-Key: <your INTERNAL_API_KEY>" \
  -F "file=@/path/to/document.pdf;type=application/pdf"
```

See [README.md](../README.md#knowledge-base--pdf-ingestion-phase-4) for
the rest of the knowledge management API (list/get/delete/search).

## Step 11 — When the SMG4004 arrives

Nothing in this repo talks to the physical gateway yet — that integration
hasn't been built (see docs/architecture.md's "REQUIRES PHYSICAL
SMG4004" sections for the exact list). Once the hardware is on-site:

1. Fill in the gateway section of
   [client-information-required.md](client-information-required.md)
   (IP, firmware, SIP registration model, etc.) — do not guess these.
2. That information drives the next phase of work: an Asterisk PJSIP
   trunk to the gateway, verified against its actual documented
   behavior, then GSM inbound/outbound routing.

## Maintenance

- **Logs**: `sudo journalctl -u sunway-backend -f` /
  `sudo journalctl -u sunway-ai-worker -f` / `/var/log/asterisk/messages`
- **Database backup**: `sudo -u postgres pg_dump sunway_gateway | gzip > backup-$(date +%F).sql.gz`
  (automate via cron; store off-server)
- **Call recordings**: `/var/spool/asterisk/recordings/` (Phase 3
  MixMonitor) and `/var/spool/asterisk/recording/` (ARI, AI calls) —
  apply `RECORDING_RETENTION_DAYS` cleanup manually or via cron; nothing
  in this repo deletes recordings automatically yet.
- **Updating the code**: `cd /opt/sunway-gateway && sudo -u sunway git pull`,
  then re-run the pip install / alembic upgrade / `deploy.sh` steps above
  for whatever changed, then restart the two systemd services.
