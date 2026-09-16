# Call Centre

Departments, staff and routing live in the database and are managed over the
API — **not** in the Asterisk dialplan. Staff numbers, priorities, backups,
ring timeouts and fallback behaviour can change while calls are running; no
dialplan edit and no Asterisk reload.

## How a call is routed

```
caller presses a digit in the IVR
        │
        ▼
[departments] context  (asterisk/etc/dialplan/departments.conf)
        │  AGI(route_agi.py,<digit>)
        ▼
GET /api/callcentre/route/<digit>        → ordered list of numbers
        │
        ▼
dial each destination in turn, ring_timeout_seconds each
        │  after each attempt: POST /api/callcentre/route/attempts  (call history)
        ├── answered  → bridged, call ends when either side hangs up
        └── nobody answered → department's no_answer_action
                               ai              → AI voice agent (extension 700)
                               fallback_number → the department's fallback number
                               hangup          → goodbye prompt, hang up
```

### Ordering rule

Agents are sorted by `priority` (lowest first), then name. **Each agent's own
number is tried first, then that agent's backup number**, before moving to the
next agent. Inactive agents are skipped entirely. A department whose
`no_answer_action` is `fallback_number` gets that number appended as the last
destination.

Example — Dr Singh (priority 10, backup) and Dr Kaur (priority 20):

| Order | Number | Kind |
|---|---|---|
| 1 | Dr Singh's mobile | `agent` |
| 2 | Dr Singh's backup | `agent_backup` |
| 3 | Dr Kaur's mobile | `agent` |

## Configuration

Everything below is behind the internal API key (`X-Internal-Api-Key`).

```bash
# Create a department on IVR digit 1
curl -X POST http://127.0.0.1:8000/api/callcentre/departments \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"dtmf_digit":"1","name":"Lumpy Disease Desk","ring_timeout_seconds":25,"no_answer_action":"ai"}'

# Add staff to it (priority: lowest rings first)
curl -X POST http://127.0.0.1:8000/api/callcentre/departments/<id>/agents \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"Dr Singh","phone_number":"+91XXXXXXXXXX","backup_number":"+91YYYYYYYYYY","priority":10}'

# Exactly what the dialplan will do with digit 1
curl -H "X-Internal-Api-Key: $INTERNAL_API_KEY" http://127.0.0.1:8000/api/callcentre/route/1
```

| Endpoint | Purpose |
|---|---|
| `GET/POST/PATCH/DELETE /api/callcentre/departments[/{id}]` | Departments (one per IVR digit) |
| `POST /api/callcentre/departments/{id}/agents`, `GET /agents`, `PATCH/DELETE /agents/{id}` | Staff |
| `GET /api/callcentre/route/{digit}` | Routing plan — used by the dialplan |
| `POST /api/callcentre/route/attempts` | Records one dial attempt |
| `GET /api/callcentre/calls`, `/calls/{id}/events` | Call history and per-call timeline |
| `GET /api/callcentre/stats?hours=24` | Dashboard counters |

**Taking someone off the rota:** `PATCH /agents/{id} {"active": false}` — they
stop receiving calls immediately, the rest of the team is unaffected.

## Dialling out

`SUNWAY_DIAL_PREFIX` in `asterisk/etc/dialplan/extensions.conf` decides how a
number is dialled:

- `PJSIP/` — softphone testing, dials local endpoints such as 1001.
- `PJSIP/smg-trunk/` — production, dials mobiles through the GSM gateway.

The digit-to-department mapping is data, so digits 1–8 are available; **9 is
reserved for the AI agent and 0 repeats the menu** (see `ivr.conf`).

## Deployment notes

`asterisk/scripts/deploy.sh` installs `route_agi.py` into Asterisk's AGI
directory. That is `<astdatadir>/agi-bin` — on Ubuntu
`/usr/share/asterisk/agi-bin`, **not** `/var/lib/asterisk/agi-bin`; the script
detects it from `core show settings`.

The AGI script needs the API URL and internal key. It reads them from the
environment (`SUNWAY_API_URL`, `INTERNAL_API_KEY`), falling back to the backend
env file (`SUNWAY_ENV_FILE`, default `/opt/sunway-gateway/backend/.env`).

## Verified behaviour

Live on Asterisk 18.10 (dev environment, 2026-09-16), a call into the
`departments` context with a two-agent department:

- the AGI fetched the plan (`GET /route/5` → 200);
- all three destinations were dialled in the configured order, each reporting
  its result (`POST /route/attempts` → 202, three times);
- with nobody answering, the call fell through to the AI agent as configured.

Routing rules (priority, backups, inactive agents, disabled departments,
fallback, attempt logging) are covered by `backend/tests/test_call_centre.py`.
