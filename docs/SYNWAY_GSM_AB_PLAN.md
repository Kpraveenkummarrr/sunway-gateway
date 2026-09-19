# Synway SMG4008 / GSM — controlled A/B plan

Status: **NOT PROVEN — requires live GSM/Synway test.** Nothing on the gateway was
changed, and nothing here was measured on the physical unit. This plan exists so
that the gateway is only touched with evidence, one variable at a time, with a
way back.

## What is known, and how well

| Fact | Source | Strength |
|---|---|---|
| The Synway sends RTP payload type 0 (PCMU), 160-byte packets every 20 ms, 0% loss | Client packet capture | Client-provided evidence |
| The Synway also sends payload type 13 (RFC 3389 comfort noise) | Client packet capture; Asterisk logs "Comfort noise support incomplete … turn off on client if possible" | Client-provided evidence |
| With comfort noise, Asterisk's own end-of-speech detector never fires, so a turn runs to the 20 s cap | Reproduced on Asterisk 18.10 with a simulated gateway (`docs/LATENCY_ROOT_CAUSE.md`) | Proven for Asterisk; **not** proven on the physical unit |
| The software no longer depends on the gateway's silence behaviour to end a turn | Same measurements, after the fix | Proven against a simulated gateway |
| Which gateway option produces payload type 13, and what its name is on this firmware | — | **Unknown.** Do not guess it |
| Whether the gateway's audio path (gain, AGC, echo cancellation, noise suppression) adds the "noisy / disturbed" quality | — | **Unknown.** The AI-side audio measured clean (`docs/VOICE_ROOT_CAUSE.md`) |

Consequence: the software fix stands on its own. Changing the gateway is now
optional, and worth doing only because comfort-noise switching can itself be
audible and because the fewer fallbacks the system leans on, the better.

## Rules

1. **Record before you touch.** Export the gateway configuration if the firmware
   offers it; otherwise write down (or photograph) every value on every page you
   are about to change, with the date. That record is the rollback.
2. **One change per experiment.** Never change gain, AGC, echo cancellation and
   silence suppression together; the result would explain nothing.
3. **Use only options that exist on this firmware.** This plan names *categories*
   of setting. Look up the exact names in the SMG4008 manual or the vendor's
   support; do not assume a name from another model.
4. **Same call script every time**, same SIM, same handset, same time of day, at
   least 5 calls per arm. The reference call asks the same three Hindi questions.
5. **Do not run the experiment on live farmer traffic.**
6. A change is kept only if the capture and the ratings both improve. "Sounds
   different" is not a result.

## Before any change: the baseline (arm A)

For each of 5 calls with today's settings:

```bash
# on the Asterisk host, while the call is up
sudo tcpdump -i any -n -s 0 -w /tmp/A-call-N.pcap udp and host <SYNWAY_IP>
# afterwards
python scripts/rtp_forensics.py /tmp/A-call-N.pcap --out /tmp/A-call-N-forensics
```

Record for each call:

| What | Where it comes from |
|---|---|
| Payload types seen, and when (0 = voice, 13 = comfort noise) | `rtp_forensics.py`: `payload_types` |
| Packet loss, duplicates, reordering, cadence, jitter | `rtp_forensics.py`: `sequence`, `timestamps`, `pacing_ms` |
| Level, clipping, DC offset of what the caller's side delivered | `rtp_forensics.py`: `decoded_audio` |
| Which signal ended each caller turn | Worker log: `Caller finished speaking: … signal=…` |
| End-of-speech wait, and the whole delay the caller felt | Worker log: `Recording finished: … end_of_speech_wait_ms=…` and `Turn timing … heard_delay_ms=…` |
| Whether the AI's own audio was intact on the wire | `rtp_forensics.py --reference <the WAV Asterisk played>` → `integrity_vs_reference.excess_distortion_db` |
| A 1–5 rating for clarity and for "disturbance", by a Hindi speaker on the handset | Human |

`excess_distortion_db` near 0 means the AI's audio reached the wire as clean as
G.711 allows, so any remaining noise was added after Asterisk — that is the
question the client cares about, and this is how it becomes a number.

## Experiment B1 — the comfort-noise / silence-suppression behaviour (payload type 13)

**Hypothesis.** Turning off the option that makes the gateway stop sending voice
frames in silence and send comfort noise instead will remove payload type 13
from the capture, and callers will hear no on/off switching of background noise.

| | |
|---|---|
| **Category to look for** | On the gateway's IP/SIP-side voice settings: silence suppression, VAD, comfort-noise generation (CNG) — however this firmware words it |
| **Old value** | Record it (the capture shows it is currently *on* in effect) |
| **New value** | The "off" position of that single option |
| **Nothing else changes** | Codec, gain, echo cancellation, AGC stay as they are |
| **Expected in the capture** | Payload type 13 count = 0; voice packets continue through silence |
| **Expected in the worker log** | `signal=TALK_DETECT finished + worker timer` (continuous frames) instead of `signal=no voice frames (RTP statistics)`; `end_of_speech_wait_ms` about 1.2–1.3 s either way |
| **Keep the change if** | PT 13 gone **and** clarity/disturbance ratings are not worse |
| **Roll back** | Restore the recorded old value; re-run one call to confirm the baseline capture returns |

If the option cannot be found, or turning it off changes nothing in the capture,
stop: the software copes either way.

## Experiment B2 — echo and level, only if callers still report noise after B1

Only start B2 if B1 is finished and callers still describe noise or an echo of the
AI's own voice.

1. **Characterise first, without changing anything.** Capture a call in which the
   caller stays silent while the AI speaks: `rtp_forensics.py` on the
   caller→Asterisk flow shows what returns from the handset side while the AI is
   talking. Energy that follows the AI's speech is echo; steady hiss is line or
   radio noise. They have different remedies.
2. **Then change exactly one** of these categories, if the firmware has it, in
   this order, each as its own arm, recording the old value first:
   - echo cancellation (on/off, or its tail length);
   - the gain towards the GSM side (AI → handset) — the AI-side level is already
     controlled (speech loudness −18 dBFS, peak ceiling −3 dBFS), so a large gain
     here amplifies the GSM path's own noise;
   - the gain from the GSM side (handset → AI);
   - automatic gain control / noise suppression on the caller side. These can
     also shave the first syllable of an interruption; test barge-in
     (`docs/BARGE_IN.md`) after any such change.
3. Judge each arm exactly as arm A was judged.

Do **not** touch the codec or jitter buffer unless the capture shows loss,
reordering or cadence errors: the client's own capture shows 0% loss and a clean
20 ms cadence, so there is no evidence either is at fault.

## What each outcome tells you

| Capture / ratings | Meaning | Next step |
|---|---|---|
| `excess_distortion_db` ≈ 0 on the AI's flow, handset still noisy | The noise is added after Asterisk: gateway DSP, GSM radio/network or handset | B2, then a second SIM/channel and handset to separate radio from gateway |
| `excess_distortion_db` clearly > 0 | Something between Asterisk's playback and the wire altered the audio | Investigate Asterisk transcoding/playback; not the gateway |
| Noise present in the caller→Asterisk flow while the AI is silent | Line/radio noise or caller-side echo | B2 step 1, then the echo/gain arms |
| PT 13 present and B1 changed nothing | The option is not the one that produces it, or the firmware ignores it | Ask the vendor; the software already copes |

## Rollback for everything

Restore the recorded values (or re-import the exported configuration), then run
one baseline call and compare its capture with arm A. The software side rolls
back independently: see `docs/LATENCY_ROOT_CAUSE.md`, "Rollback".
