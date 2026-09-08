# Client Information Required

This information is needed from the client before the corresponding phases
can be completed. Development proceeds without it wherever possible
(everything marked READY WITHOUT GATEWAY in [architecture.md](architecture.md)),
using placeholders in `.env` until real values are supplied.

## Gateway

- [ ] Exact SMG4004 model/variant and hardware revision
- [ ] Firmware version currently installed
- [ ] Gateway management IP address (LAN or via VPN)
- [ ] Gateway admin credentials (for configuration, not stored in this repo)
- [ ] Confirmed SIP port and transport used by the gateway

## SIM / carrier

- [ ] SIM carrier(s) per channel
- [ ] Number of SIMs actively installed (up to 4 on this unit)
- [ ] SIM PIN(s), if enabled
- [ ] Incoming phone numbers per SIM
- [ ] Any carrier-specific DTMF or call-forwarding quirks known to the client

## Staff routing

- [ ] Staff mobile numbers for: Sales, Support, Accounts (or actual departments)
- [ ] Preferred fallback number(s) if staff don't answer
- [ ] Business hours (for after-hours routing/fallback)

## IVR

- [ ] Exact welcome message wording
- [ ] Final IVR menu options and wording (department names, order)
- [ ] Desired retry count and timeout behavior
- [ ] Preferred hold/fallback message text

## AI Voice Agent

- [ ] PDF(s) or documents to use as the knowledge base
- [ ] Preferred AI response language(s) (English / Tamil / Hindi / other)
- [ ] Any specific tone/behavior requirements for the AI agent
- [ ] Whether AI should be enabled by default or staff-routing only initially
- [ ] Preferred LLM/STT/TTS providers, if the client has existing accounts/keys

## Recording & compliance

- [ ] Whether call recording is legally required/permitted in the client's jurisdiction
- [ ] Desired recording retention period
- [ ] Any consent announcement required before recording

## Infrastructure

- [ ] VPS provider and access (or whether we provision one)
- [ ] Domain name for the admin panel / API, if any
- [ ] Whether the gateway can reach the VPS via WireGuard, or requires a different network path (site has existing router/firewall constraints?)

## Notes

Do not invent or assume any of the above. Where a value is unknown, leave
the corresponding `.env` variable blank and document the gap here rather
than guessing a default.
