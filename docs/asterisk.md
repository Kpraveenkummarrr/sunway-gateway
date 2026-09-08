# Asterisk / PJSIP Setup and Testing (Phase 3)

Status: **READY WITHOUT GATEWAY**. Everything in this document works with
SIP softphones only — no SMG4004 hardware involved. GSM-specific behavior
is out of scope here and called out separately in
[docs/architecture.md](architecture.md).

## What's in `asterisk/`

```
asterisk/
  etc/
    pjsip/
      pjsip.conf                 master file — deployed as /etc/asterisk/pjsip.conf
      pjsip_transport.conf       one UDP transport, port 5060
      pjsip_extensions.conf      endpoints/AORs for test extensions 1001-1003
      pjsip_auth.conf.example    credential template (safe to commit)
      pjsip_auth.conf            real credentials (gitignored, generated locally)
    dialplan/
      extensions.conf            master file — deployed as /etc/asterisk/extensions.conf
      internal.conf               extension-to-extension calls + IVR test entry point
      ivr.conf                   the IVR menu
      recording.conf              shared MixMonitor subroutine
    rtp.conf                     RTP port range
  scripts/
    generate_sip_secrets.sh      generates pjsip_auth.conf with random passwords
    deploy.sh                    backs up /etc/asterisk, then deploys the above
```

## Installing Asterisk

Native install, no Docker:

```bash
sudo apt-get update
sudo apt-get install -y asterisk
```

This installs Asterisk 18 (the version currently in Ubuntu 22.04's
`universe` repo) with the full PJSIP module set (`chan_pjsip`, `res_pjsip*`).
The package starts Asterisk automatically as a systemd service with its
stock default configuration.

Verify:

```bash
asterisk -V
sudo systemctl status asterisk
sudo asterisk -rx "module show like pjsip"
```

## Deploying this repo's config

```bash
cd asterisk/scripts
./generate_sip_secrets.sh     # only once — creates pjsip_auth.conf, gitignored
sudo ./deploy.sh
```

`deploy.sh`:

1. Refuses to run unless `pjsip_auth.conf` already exists (forces you to
   generate real credentials instead of deploying a template with no
   passwords).
2. Backs up the **entire existing** `/etc/asterisk/` to
   `/var/backups/asterisk-config-<timestamp>/` before changing anything —
   safe to run even against a pre-existing installation.
3. Copies the PJSIP config, dialplan, and `rtp.conf` into `/etc/asterisk/`.
4. Disables the legacy `chan_sip` driver (`noload => chan_sip.so` in
   `modules.conf`) because it ships enabled by default and binds UDP 5060,
   conflicting with our PJSIP transport. This project uses PJSIP exclusively
   — chan_sip is deprecated upstream and was never needed.
5. Creates the recording directory (`/var/spool/asterisk/recordings`)
   owned by the `asterisk` user.
6. Reloads PJSIP and the dialplan.

To roll back, restore any file from the timestamped backup directory and
reload.

## PJSIP configuration

- **Transport**: one UDP transport on port 5060, all interfaces. No NAT
  settings are configured — this is a local/LAN dev config. NAT/WireGuard
  settings for a VPS deployment are added later (see `docs/network.md`,
  not yet written).
- **Endpoints**: 1001, 1002, 1003. `disallow=all` / `allow=ulaw,alaw` (the
  common denominator for GSM gateway trunks and SIP softphones alike).
  `dtmf_mode=rfc4733` — this is PJSIP's name for what's commonly called
  "RFC2833" DTMF (out-of-band `telephone-event` RTP packets); the same
  concept, different config keyword than legacy `chan_sip` used.
- **AORs**: `max_contacts=1`, `qualify_frequency=30` (keepalive/OPTIONS
  ping every 30s so `pjsip show endpoints` reflects real reachability).
- **Auth**: digest username/password per extension, defined in
  `pjsip_auth.conf` (gitignored — see "SIP credentials" below).

## Dialplan

- **`[internal]`**: direct extension-to-extension calling (dial 1001,
  1002, or 1003 from any registered extension), plus a `600` test entry
  point that jumps into the IVR — standing in for the real inbound trunk
  context that will exist once the SMG4004 is connected.
- **`[ivr-menu]`**: "Press 1 for Staff 1, 2 for Staff 2, 3 for Staff 3."
  Routes to extensions 1001/1002/1003 (test extensions, **not** GSM/mobile
  numbers — that wiring happens once the gateway and staff numbers exist).
  Handles invalid input (`i`) and no-input timeout (`t`) by replaying the
  menu, capped at `IVR_MAX_RETRIES` (default 3) attempts before playing a
  goodbye message and hanging up.
- **`[sub-recording]`**: a `Gosub` subroutine that starts `MixMonitor` on
  any call that reaches it — used by both `[internal]` and `[ivr-menu]`.

### IVR audio

`Background()`/`Playback()` reference `custom/ivr-*` sound files that are
**not recorded yet**. Asterisk logs a WARNING and continues silently when a
referenced sound file is missing — so the IVR's routing/DTMF/retry/timeout
logic is fully testable by ear-and-keypad (or scripted DTMF) even without
real prompts. Record actual audio, or wire up TTS (a later phase), before
this goes anywhere near production.

## Call recording

`MixMonitor` starts as soon as a call is answered/bridged (from
`[sub-recording]`). Files are written to:

```
${RECORDING_BASE_DIR}/YYYY/MM/DD/<call-uniqueid>_<HHMMSS>_<caller>_<destination>.wav
```

`RECORDING_BASE_DIR` defaults to `/var/spool/asterisk/recordings`, created
by `deploy.sh` and owned by the `asterisk` user (mode `750` — not
world-readable, since recordings can contain sensitive conversations).
`CDR(userfield)` is also set to the recording path so it shows up in the
CDR alongside the call.

This is the Phase 3 baseline only — associating recordings with the
Phase 2 `recordings` table (call ID, caller, destination, retention) is a
later integration, not implemented yet.

## Logging

Default Asterisk logging (`/var/log/asterisk/messages`, `/var/log/asterisk/full`
if enabled) plus explicit `NoOp()` lines in the dialplan at each key
decision point (call start, IVR selection, dial result, invalid input,
timeout) — enough to trace a call's path from the CLI/log without any
external tooling. Wiring these into the Phase 2 `call_events`/`system_logs`
tables is a later integration.

## SIP credentials

`pjsip_auth.conf` is generated locally by `generate_sip_secrets.sh` (random
24-character passwords) and is **gitignored** — never commit it.
`pjsip_auth.conf.example` (committed) documents the expected format with
placeholder passwords only.

## Security notes (dev environment)

- Only UDP 5060 (SIP) and the RTP range (10000-20000/udp) need to be
  reachable by softphones on the local network; nothing else in this repo
  opens ports.
- Asterisk's AMI (`manager.conf`) and ARI (`ari.conf`) are left at their
  package defaults, which are **not** enabled for external/anonymous
  access out of the box. Phase 3 does not enable or configure either —
  they'll be scoped and secured explicitly when the AI backend needs them.
- No default/blank SIP passwords are used; each extension gets a random
  generated credential.
- On a real deployment, restrict UDP 5060 and the RTP range at the
  firewall to only the expected sources (softphone subnet, and — once
  connected — the SMG4004's address over WireGuard). Not applicable to
  this local dev setup.

## Testing

No GUI softphone is available in this (headless/WSL) environment, so Phase 3
was verified with **real SIP protocol exchanges** via two tools instead of
manual clicking:

- **SIPp** (`sip-tester` package) — real REGISTER (with digest challenge/
  response) and real INVITE/ACK/BYE call flows over actual UDP sockets to
  the live Asterisk instance. This is genuine SIP client behavior, not a
  simulation of one.
- **Asterisk CLI `channel originate ... application SendDTMF <digit>`** —
  used specifically to drive the IVR's digit/invalid/timeout logic, since
  scripting real RTP DTMF (RFC4733 telephone-events) in SIPp is a heavier
  lift than the IVR routing logic itself warranted. This exercises the
  exact same dialplan code path a real DTMF digit would (`WaitExten()`
  receiving a digit), just injected without an RTP round-trip.

All results below are from actual runs against the deployed config, not
predicted/assumed. See the Phase 3 report for the full command-by-command
log. Summary:

| # | Check | Result |
|---|-------|--------|
| 1-3 | 1001/1002/1003 register | PASS — SIPp REGISTER w/ digest auth, 200 OK, confirmed via `pjsip show contacts` |
| 4 | 1001 → 1002 call | PASS — full INVITE/ACK/BYE exchange, recording file created |
| 5 | 1002 → 1003 call | PASS — same |
| 6-9 | IVR answers, 1/2/3 route correctly | PASS — confirmed via CDR `NoOp` entries ("IVR selection N -> extension 100N") |
| 10 | Invalid DTMF handled | PASS — digit 9 correctly hit the `i` extension |
| 11 | Timeout handled | PASS — no-input call correctly reached the max-retries goodbye path instead of hanging |
| 12 | Recording works | PASS — `.wav` files created per call, correctly named and permissioned |
| 13 | Clean hangup | PASS — `core show channels count` returned to 0 active after every test |
| 14 | Multiple simultaneous calls | PASS — 3 concurrent IVR calls all routed correctly with no errors |

One real bug was caught and fixed during this testing: `dtmf_mode=rfc2833`
(the old chan_sip keyword) is rejected by PJSIP, which needs `rfc4733` —
fixed in `pjsip_extensions.conf`. A second issue — legacy `chan_sip`
binding UDP 5060 and conflicting with our PJSIP transport — was fixed by
disabling `chan_sip` in `deploy.sh` (see above).

**Caveat**: recordings from these tests are near-silent placeholder audio
(SIPp doesn't send real voice RTP) — they confirm the recording
*mechanism* works (file created, correctly named, correct permissions),
not audio quality. Real audio content will only be verified with a real
softphone or the physical SMG4004.

Manual testing steps with a real softphone (e.g. Zoiper, Linphone) — for
whoever picks this up next without needing SIPp/CLI tricks — are:

1. Install a SIP softphone on any machine that can reach the Asterisk
   host on UDP 5060.
2. Register three profiles as `1001`, `1002`, `1003` against the
   Asterisk host IP, using the passwords in your locally generated
   `pjsip_auth.conf`.
3. From 1001, dial `1002` — should ring and connect.
4. From any registered extension, dial `600` — should hear (silence,
   until prompts are recorded) the IVR; press `1`/`2`/`3` to route to
   1001/1002/1003; press an unmapped digit (e.g. `9`) to hear the
   invalid-input path loop back to the menu; wait 8s with no input to
   hear the timeout path loop back to the menu; do this `IVR_MAX_RETRIES`
   times to reach the goodbye message and hangup.
5. Check `/var/spool/asterisk/recordings/YYYY/MM/DD/` for a `.wav` file
   per call after hangup.

## REQUIRES PHYSICAL SMG4004

Not implemented or tested in Phase 3, and not claimed to work:

- GSM → SIP inbound calls
- SIP → GSM outbound calls (staff/mobile routing)
- Real GSM caller ID
- GSM-path DTMF behavior (carrier/hardware dependent)
- Per-SIM/per-channel routing behavior
- Multi-SIM concurrent call testing
- GSM audio quality/echo behavior
