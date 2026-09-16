# Admin Panel

A browser panel served by the backend itself at **`/api/admin/panel`** — one
static page, no build step and no extra runtime dependency. Every screen
reads and writes the real backend; nothing on it is mocked or hard-coded.

## Access

```
http://<server>:8000/api/admin/panel
```

Sign in with `ADMIN_PASSWORD` (set it in `backend/.env`). The login creates a
signed, HttpOnly session cookie valid for 12 hours, signed with
`APP_SECRET_KEY`. Scripts and the dialplan AGI keep using
`X-Internal-Api-Key`; both are accepted.

If neither `ADMIN_PASSWORD` nor `INTERNAL_API_KEY` is set (the local dev
default) the panel is open and the backend logs a warning on every request.
**Set both on the client machine**, and put the panel behind HTTPS.

## Screens

| Screen | What it does | Backend |
|---|---|---|
| Dashboard | Active/total/completed/failed calls, AI vs staff-forwarded, average duration, service status | `/api/callcentre/stats`, `/api/admin/health` |
| Departments | Create, enable/disable, delete; ring timeout, no-answer action, fallback number, DTMF digit | `/api/callcentre/departments` |
| Staff | Add/remove, on/off duty, priority, mobile, backup mobile, per-agent ring timeout | `/api/callcentre/agents` |
| Routing | What every key does right now, including ring order and the reserved 9 (AI) and 0 (repeat) | `/api/admin/routing-overview` |
| Call history | Recent calls with caller, status, who handled it, duration, plus a per-call timeline of dial attempts | `/api/callcentre/calls`, `/calls/{id}/events` |
| Knowledge base | Upload a PDF, see processing status, page/chunk counts, delete | `/api/knowledge` |
| AI settings | Language, persona/system prompt, caller phrases, speech speed, turn timeouts, retrieval size | `/api/admin/settings` |
| System health | Asterisk, AI worker, SIP endpoints, load/memory/disk | `/api/admin/health` |
| Audit | Who changed what, and when | `/api/admin/audit` |

## AI settings take effect without a restart

Saved settings go to the `system_config` table, and the AI worker re-reads
them **at the start of each call**, so a change applies to the next caller.
Cached welcome/error/goodbye audio is dropped when settings change, so no
stale phrase is played.

Only operator-tunable, non-secret values are editable — language, persona,
caller phrases, speech speed, timeouts, retrieval size and answer length.
API keys, database URLs and provider selection stay in the environment; the
panel API refuses to store them and never returns them.

## Health readings are measured, never guessed

- **Asterisk** — ARI `/asterisk/info` (reports the real version).
- **AI worker** — whether its Stasis app is registered on ARI.
- **Gateway/SIP** — Asterisk's endpoint list with each endpoint's state.
- **Resources** — load average, memory and disk from the OS.

Anything that cannot be measured is shown as **Unknown** with the reason
(for example, ARI unreachable), not as a made-up value.

## Verified

Against a live backend and real Asterisk 18.10 (2026-09-16): the panel
served (HTTP 200, 23.7 KB, 22 API calls), health reported the real Asterisk
version, correctly reported the AI worker as not running, and listed the
actual SIP endpoint states. Covered by `backend/tests/test_admin_panel.py`
(15 tests): login/session/tampering, API-key access, settings validation and
persistence, secret exposure, audit entries, health shape, routing overview,
per-agent ring timeout, panel wiring, and a change made in the panel
reaching the next call.

## Not built yet

Knowledge-base versioning, activate/deactivate and re-index; role-based
users (there is one admin password); HTTPS termination is left to the
reverse proxy.
