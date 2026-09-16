# Real telephony voice-quality diagnosis

Status: instrumentation and controlled A/B prepared; client measurements and
Hindi-listener verdict are still required. No codec, jitter-buffer, gain,
tempo, or Bhashini model change is approved by this document alone.

## Baseline preserved

- The pre-diagnostic code/configuration baseline is Git commit `81cc948`.
- The production path remains unchanged by the diagnostic instrumentation.
- Each A/B run writes `safe_current_voice_config.json`, containing only the
  effective non-secret voice settings used for that run.
- Before changing the deployed `.env`, make a root-readable backup outside
  the repository and keep the current `system_config.ai_tts_speed` value.
  Never commit either the `.env` or a database dump.

## What the source audit establishes

The current integration requests Hindi TTS with:

- service ID `Bhashini/IITM/TTS` by default;
- `gender=female`;
- no speaker/voice ID, SSML, pitch, prosody, or provider-side rate parameter.

Therefore “female” is the most specific voice selection made by this code;
the exact speaker behind that model is selected by Bhashini and is not
identified by the request. The worker logs the base service ID, gender,
language, and tempo at startup; per-call logs contain the effective tempo
after any admin-panel override is loaded.

The provider WAV is decoded unchanged. The call controller then:

1. reads its actual rate/channels/bit depth;
2. low-pass filters and resamples it to 8 kHz mono 16-bit PCM;
3. trims leading/trailing silence to about 60 ms padding;
4. applies the effective local `ai_tts_speed` using WSOLA;
5. peak-normalizes to -3 dBFS, with at most +9 dB gain;
6. writes a WAV which Asterisk translates to the negotiated endpoint codec.

The code default is `ai_tts_speed=1.15`, but the admin-panel database can
override it. This is a local post-synthesis tempo change, not a Bhashini
speaking-rate setting. It is the leading testable artifact hypothesis, not a
confirmed root cause.

The repository's test endpoints offer `ulaw,alaw`. The deployed SMG4008
endpoint configuration is not in this repository, so the real negotiated
codec cannot be declared from source code.

## Controlled A/B: stages A and B

Run on the deployed host from the `backend` directory. This makes one real,
potentially billed Bhashini TTS request and reuses that exact response for
both tempo variants:

```bash
sudo -u sunway ./.venv/bin/python scripts/telephony_voice_ab.py \
  --out /var/tmp/sunway-voice-ab
```

The script reads the admin-panel override when PostgreSQL is available. Use
`--env-only` only when intentionally testing the `.env` value alone. To
re-analyse a previously captured provider WAV without a network call:

```bash
./.venv/bin/python scripts/telephony_voice_ab.py \
  --input-wav /path/to/original-bhashini.wav \
  --current-speed 1.15 \
  --out /var/tmp/sunway-voice-ab
```

Artifacts:

| File | What it isolates |
|---|---|
| `A_original_bhashini.wav` | Bhashini output before local processing |
| `B0_resample_only_8k.wav` | Narrow-band resampling only |
| `B1_current_8k.wav` | Current trim, tempo, normalization, and resampling |
| `B2_candidate_native_cadence_8k.wav` | Same source and processing at native 1.00x cadence |
| `C1`–`C4` codec previews | Offline G.711 u-law/A-law quantisation previews only |
| `voice_ab_report.json` | TTS time, rates, durations, levels, silence, clipping, hashes, and WPM |

The G.711 previews do not prove which codec was negotiated and do not model
RTP loss, jitter, the SMG4008, GSM radio, or the handset speaker.

An alternate Bhashini service can be compared without changing production:

```bash
./.venv/bin/python scripts/telephony_voice_ab.py \
  --candidate-service-id '<service-id-confirmed-for-this-key>' \
  --out /var/tmp/sunway-voice-model-ab
```

Do not guess a service ID or call one “best” from a model list. First confirm
that the client key/pipeline authorizes it, then let the Hindi-speaking tester
select it. The script sends only the request fields already supported by this
integration.

## Stages C and D: Asterisk, RTP, SMG4008, and GSM

After deploying the instrumentation, each real call logs:

- `Telephony media formats`: negotiated native codec plus Asterisk read/write
  translation formats;
- `TTS source` and `TTS prepared`: model/gender, source/target rate, duration,
  WPM, peak delta, peak/RMS/noise floor, silence, clipping, TTS time, and prep
  time;
- `Playback timing`: ARI request time, wall time, expected WAV duration, drift,
  timeout, or barge-in;
- `RTP statistics`: packet counts, loss, jitter, and RTT as reported by
  Asterisk/RTCP;
- `Turn timing`: VAD/capture, ASR, embedding, retrieval, LLM, TTS, preparation,
  first-audio, speaking, and total response latency.

Confirm the live channel independently while the test call is active:

```bash
asterisk -rx "core show channels concise"
asterisk -rx "core show channel <exact-channel-name>"
asterisk -rx "pjsip show channelstats"
```

Capture the RTP leg for stage C without changing codec or enabling a jitter
buffer:

```bash
sudo tcpdump -i any -s 0 -w /var/tmp/sunway-voice-ab.pcap \
  'udp portrange 10000-20000'
```

In Wireshark, inspect the SIP SDP for the selected payload/codec, then use
Telephony -> RTP -> RTP Streams to record packet loss, sequence errors,
jitter, and timing and to export the Asterisk-to-SMG audio. That exported
outbound stream is stage C. A consented handset recording of the same fixed
phrase is stage D.

For echo/disturbance, repeat with the handset earpiece and loudspeaker in a
quiet room, then with representative background noise. Note whether the
disturbance exists in exported stage C audio. If it appears only at stage D,
investigate SMG gain/echo cancellation, GSM coverage, and handset acoustics;
do not alter server DSP first.

## Root-cause decision table

| First degraded stage | Evidence | Action |
|---|---|---|
| A | Original provider WAV is already robotic/unclear | Compare only authorized Hindi Bhashini service IDs and text phrasing |
| B0 | A is clear; resample-only file is unclear | Review the 8 kHz filter/resampler and unavoidable narrow-band loss |
| B1 only | B0/B2 are clear; current file is not | Native cadence is the fix; set effective speed to 1.00 after approval |
| B1 and B2 | Both processed files distort while B0 is clear | Isolate trim/normalization; inspect reported gain and clipping before changing it |
| C | Local WAVs are clear; exported outbound RTP is not | Verify negotiated codec and Asterisk translation path |
| D | Exported RTP is clear; handset is not | Diagnose packet loss/jitter, SMG4008 gain/echo settings, GSM radio, and handset |

Do not combine model, tempo, gain, codec, and jitter changes in one trial. One
variable per A/B is required to identify the bottleneck.

## Hindi listener result sheet

Use the same phrase, handset, SIM/channel, location, and volume. The tester
must not be told which file/call is the candidate until scoring is complete.

| Sample | Intelligibility (1–5) | Naturalness (1–5) | Too fast/slow | Harsh/clipped | Echo/dropouts | Notes |
|---|---:|---:|---|---|---|---|
| A original | | | | | | |
| B current | | | | | | |
| B native cadence | | | | | | |
| C exported RTP | | | | | | |
| D GSM handset | | | | | | |

Before result: client reports the current female Hindi GSM call as
unnatural/unclear. After result: **pending this measured A/B and a verdict from
an actual Hindi-speaking tester**. Code tests alone must not be described as
human-like voice acceptance.
