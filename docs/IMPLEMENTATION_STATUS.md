# Implementation Status

Status of every functional area in the architecture document, as of
2026-09-16. Honest by rule: **DONE** means built *and* tested; **PARTIAL**
means usable but incomplete; **NOT BUILT** means not started; **BLOCKED**
means it needs hardware, credentials or client input that is not available.

The development environment (this repository's WSL Asterisk) has **no GSM
gateway, no Bhashini key and no Gemini key**, so anything requiring them is
tested on the client machine, not here.

## Requirement matrix

| # | Architecture requirement | Implementation | Key files | Status |
|---|---|---|---|---|
| 3 | Complete call flow (GSM → SIP → IVR → routing/AI) | End-to-end path works on the client box | `asterisk/etc/dialplan/`, `backend/app/services/` | PARTIAL — IVR/AI live; staff forwarding needs real numbers |
| 4 | GSM gateway architecture | SMG4008-8G, 8 ports (document assumes SMG4004/4 ports) | — | BLOCKED — channel/SIM layout to be read from the live gateway |
| 5 | IVR flow (welcome, menu, retries, timeout, goodbye) | Dialplan IVR with retries, invalid and timeout handling | `asterisk/etc/dialplan/ivr.conf` | DONE (prompt audio still to be recorded) |
| 6 | DTMF selection | Digits 1–8 → departments, 9 → AI, 0 → repeat | `ivr.conf`, `departments.conf` | DONE (DTMF mode on the real gateway still to be confirmed) |
| 7 | Call forwarding to staff mobiles | Department routing: priority order, per-agent backup, fallback | `departments.conf`, `app/services/call_routing.py` | DONE in software — dialling real mobiles is BLOCKED on the trunk + numbers |
| 8 | AI voice agent | ARI worker: ASR → RAG → LLM → TTS → playback | `app/services/call_controller.py` | DONE |
| 9 | PDF knowledge base → RAG | Upload, chunking, embeddings, pgvector search | `app/services/knowledge_*.py`, `app/api/knowledge.py` | PARTIAL — no versioning/activate-deactivate/re-index yet |
| 10 | Concurrent calls | Per-channel state, isolated AI sessions | `call_controller.py` | PARTIAL — isolation tested; real multi-channel GSM load not tested |
| 12 | Server/PBX components | Ubuntu, Asterisk 18.10, Python/FastAPI, PostgreSQL + pgvector | `docs/production-deployment.md` | DONE |
| 13 | SMG ↔ server SIP/RTP | Working on the client LAN | — | DONE (client machine) |
| 14 | Security | Internal API key, firewall script, no secrets in git | `scripts/setup_firewall.sh`, `app/core/security.py` | PARTIAL — no Fail2Ban, TLS, outbound whitelist or roles yet |
| 15 | Development phases | Phases 1–8 delivered | — | PARTIAL |
| 16 | Testing T-01..T-18 | See table below | `backend/tests/` | PARTIAL |
| 17 | Deployment plan | Production runbook + systemd units | `docs/production-deployment.md`, `scripts/systemd/` | DONE |
| 18 | Client information required | Outstanding items tracked | `docs/client-information-required.md` | BLOCKED on client |
| — | Call centre (departments, agents, fallback, history) | Full model, API, routing, dialplan, call history | `app/api/callcentre.py`, `app/models/routing.py` | DONE (admin GUI still to build) |
| — | Admin GUI | — | — | NOT BUILT |
| — | AI → human transfer | — | — | NOT BUILT |
| — | Monitoring/health endpoints | `/health`, `/ready` only | `app/api/health.py` | PARTIAL |
| — | Hindi voice quality/expressiveness | Anti-aliased audio, configurable speed, Hindi-only prompt | `app/services/audio.py`, `app/core/config.py` | PARTIAL — needs live tuning with the real voice |

## T-01 – T-18

`PASS` = actually run and passed. `BLOCKED` = needs hardware/credentials.
`NOT RUN` = not yet executed.

| ID | Test | Status | Evidence |
|---|---|---|---|
| T-01 | SIP registration (gateway ↔ Asterisk) | BLOCKED (dev) | Working on the client machine; not re-verified here |
| T-02 | Incoming GSM call | BLOCKED | No gateway in the dev environment |
| T-03 | Welcome message | PASS (dev) | Played on a real extension-700 call; 8 kHz mono verified |
| T-04 | DTMF digits 1,2,3,9,0 | PARTIAL | Routing target for each digit verified in the dialplan and live via the `departments` context; DTMF detection itself needs the gateway |
| T-05 | Forward digit 1 | PARTIAL | Routing + dial + fallback proven live; real mobile numbers BLOCKED |
| T-06 | Forward digit 2 | PARTIAL | Same routing code path as T-05 |
| T-07 | Forward digit 3 | PARTIAL | Same routing code path as T-05 |
| T-08 | AI route (digit 9) | PASS | Live calls into the AI agent on extension 700 |
| T-09 | STT accuracy | NOT RUN | Needs the Bhashini key + recorded Hindi samples |
| T-10 | RAG retrieval | PARTIAL | Unit-tested; 20-question accuracy set NOT RUN |
| T-11 | AI answer quality | NOT RUN | Needs a human review pass on the client box |
| T-12 | TTS quality | PARTIAL | Format/level verified; naturalness review outstanding |
| T-13 | Timeout, no DTMF | PASS | IVR retry/timeout path in `ivr.conf`, exercised in dev |
| T-14 | Invalid digit | PASS | Invalid branch plays the prompt and re-offers the menu |
| T-15 | Concurrent calls | PARTIAL | Session isolation tested in code; real 4+ channel GSM test BLOCKED |
| T-16 | 30-minute stability | NOT RUN | — |
| T-17 | SIP security / Fail2Ban | NOT RUN | Fail2Ban not configured yet |
| T-18 | PDF update | PARTIAL | Upload/delete tested; re-index and activation NOT BUILT |

## Next, in priority order

1. **Admin GUI** — dashboard, departments/staff, knowledge base, AI settings.
2. **AI → human transfer** — a real ARI bridge to a staff member, not just a
   spoken promise.
3. **Knowledge base management** — versions, activate/deactivate, re-index,
   and the 20-question accuracy set (T-10).
4. **Security** — Fail2Ban, outbound number whitelist, call duration cap.
5. **Monitoring** — worker/gateway health, latency and error counters.
6. **On the client machine** — live Hindi voice tuning (T-09, T-11, T-12),
   GSM tests (T-01/T-02/T-05–T-07), concurrency (T-15), stability (T-16).
