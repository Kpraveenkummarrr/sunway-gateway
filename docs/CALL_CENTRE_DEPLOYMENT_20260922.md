# Call-centre / IVR — what was fixed live on the client server (2026-09-22)

This documents changes made **directly on the client's Asterisk server** during a live
troubleshooting session, so they survive a server rebuild and are not silently lost. Two of
the three files below are **not tracked in this repository** (they are host-specific,
generated, or contain the client's live IP) — copy the exact content here back onto the
server if it is ever rebuilt.

## 1. `/etc/asterisk/ivr.conf` — already correct in the repo

`asterisk/etc/dialplan/ivr.conf` in this repository already routes digits 1–8 to the
`departments` context (pattern `_[1-8]`), 9 to the AI agent, 0 to repeat the menu. The
client's live server was running an **older, stale copy** of this file (hardcoded digits 1–3
routing to test SIP extensions 1001–1003, no 9/0 handling) — `deploy.sh` had evidently not
been re-run since this file was last updated. It was hand-patched live to match; **the repo
version is the source of truth going forward** — redeploy this file from the repo (via
`asterisk/scripts/deploy.sh`, reviewed, not blindly overwritten over live trunk config) rather
than trusting whatever is currently on the server.

## 2. `/etc/asterisk/extensions_from_smg.conf` — NOT in this repo, client-specific

This file decides where an inbound GSM call from the Synway goes. It is not templated in the
repository (it is generated per deployment). **Found on the client server sending every call
straight to the AI agent, bypassing the IVR menu entirely** (`Goto(internal,700,1)`). Fixed to:

```ini
[from-smg]
exten => _X.,1,NoOp(Incoming GSM call from SMG4008: ${CALLERID(num)} to ${EXTEN})
 same => n,Goto(ivr-menu,s,1)
 same => n,Hangup()
```

If this file is ever regenerated or the server rebuilt, **recreate it with exactly this
content** (adjust the extension pattern only if the client's numbering plan changes).

## 3. `/etc/asterisk/pjsip_smg4008.conf` — NOT in this repo, contains the client's IP

Client's Synway trunk endpoint definition, keyed to `192.168.0.106`. Added one line:

```ini
[smg4008]
type=endpoint
dtmf_mode=inband
...
```

**Why `inband` and not `rfc4733`:** the Synway's own settings correctly advertise
`DTMF Transmit Mode: RFC2833`, but a packet capture of a live call proved **no RFC2833
(payload type 101) packets are ever sent** — the inbound RTP flow from the Synway carries only
payload type 0 (voice) for the whole call. `dtmf_mode=inband` makes Asterisk look for DTMF
tones inside the voice audio itself instead of relying on a signalling event that never
arrives. **Root cause status: still open.** Even with `inband`, no key press was detected in
a live test call, and the decoded audio itself showed no audible tone (RMS ~-50 dBFS, close to
the noise floor) — meaning the key press signal is not reaching the gateway's SIP/RTP output
at all, in any form. Confirmed:
- Asterisk `smg4008` endpoint config correct (checked both `rfc4733` and `inband`)
- Synway `VoIP → Media → DTMF Transmit Mode` correctly set to RFC2833
- Synway `Port` pages (checked ports 4, 6, 7, 8) have no separate GSM-side DTMF/tone-detection
  toggle — the Media page setting is the only DTMF-related option this firmware exposes

**Next diagnostic step (needs physical access, not yet done):** swap the SIM card for one from
a different mobile operator and repeat the packet-capture test
(`backend/scripts/rtp_forensics.py`, see below). If the digit still doesn't arrive with a
different SIM, this is a Synway SMG4008 hardware/firmware limitation and needs the vendor's
support with the capture evidence; if it works with a different SIM, the original SIM's
operator does not relay in-call DTMF for this call type.

## 4. IVR prompt audio — now in the repo

`custom/ivr-welcome-menu`, `custom/ivr-invalid`, `custom/ivr-goodbye`, `custom/ivr-timeout`
were referenced by the dialplan but never recorded — Asterisk logged a WARNING and played
nothing (a caller heard silence, an answered call with no audio). Generated with the
project's own Bhashini TTS pipeline and installed. Regenerate with:

```bash
cd backend
.venv/bin/python scripts/gen_ivr_prompts.py --out /tmp/ivr_prompts
sudo asterisk -rx "core show settings" | grep -i sound   # confirm the real sounds dir first
sudo cp /tmp/ivr_prompts/*.wav <that sounds dir>/custom/
sudo chown asterisk:asterisk <that sounds dir>/custom/*.wav
sudo asterisk -rx "dialplan reload"
```

On this client's install the sounds directory is `/usr/share/asterisk/sounds` (`astdatadir`
in `asterisk.conf`), **not** `/var/lib/asterisk/sounds` — confirm with the command above before
assuming either path; both existed on disk but only one is actually read by the dialplan.

## 5. Operational notes learned this session

- **Never type `exit` at the `asterisk -r`/`-rvvv` console.** On this install it does not just
  disconnect the console — it stops the whole Asterisk daemon (`Asterisk cleanly ending (0)`),
  dropping every active call. systemd restarts it automatically (`Restart=on-failure`), but any
  call in progress at that moment is killed. Use `Ctrl+C`, or open a new terminal instead.
- Packet-capture diagnosis: capture on the **specific interface** (`eth0` here), not `-i any`.
  Modern tcpdump's `-i any` writes the `LINUX_SLL2` link-layer format, which
  `backend/scripts/rtp_forensics.py`'s pcap reader does not parse (it reads Ethernet,
  classic Linux-cooked SLL, and raw IP) — it silently reports `"udp_datagrams": 0` even when
  tcpdump itself captured real traffic. Capturing on the real interface avoids this; the
  reader could also be extended to support SLL2 (linktype 276) if `-i any` is ever required
  again.
