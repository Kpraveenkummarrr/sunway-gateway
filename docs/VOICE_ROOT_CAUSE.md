# Voice forensic result

Status: **CLIENT UAT REQUIRED**. The source of actual noise/robotic speech is
unproven. Synthetic tones cannot establish human naturalness, intelligibility,
ASR accuracy, packet loss, echo or GSM quality.

## Stage map

| Stage | Available code evidence | Required client evidence |
|---|---|---|
| Gemini text | Buffered completion, persona/history/retrieved context | Exact reply, latency and factual review |
| Spoken text | Markdown cleanup, sentence chunks, bare URL removal | Exact cleaned text, chunk boundaries and gaps |
| Bhashini raw | Existing serviceId + female config; no invented speed/SSML parameter | Original WAV, actual service ID/voice, generation time, Hindi listening |
| Conversion | Band-limited mono PCM conversion to 8 kHz, WSOLA, capped gain, trimming, fades | Compare same source at 1.00/1.10/1.15x |
| Asterisk | Pre-registered playback ID; duration/request/outcome logging | Actual installed version/modules, sent-audio recording, playback events |
| SIP/RTP | Channel formats and raw RTP statistics logged | Negotiated SDP payload, actual RTP payload, packet sequence/timestamps/RTCP |
| SMG4008/GSM | No runtime capture available | Gateway gain/echo settings, radio/SIM/channel, handset recording |

Configured codecs in repository test endpoints are not proof of production codec
negotiation. Channel native formats are not packet-capture evidence. RTCP may be
absent: missing remote loss/jitter is **unknown**, not zero. Jitter units must be
verified against the installed Asterisk version and RTP clock; do not label raw
values milliseconds without conversion.

## Reproducible local measurements

Command run: `.venv/Scripts/python.exe scripts/dsp_benchmark.py --out data/final-uat-dsp-20260917`

Input: synthetic 180 Hz harmonic probe at 48 kHz, 0.2 s leading and 0.3 s trailing silence.
This is **not Hindi or Bhashini audio**. All comparison files are listenable WAVs.

| Variant | Duration | Peak dBFS | Clipped fraction | Estimated F0 | Median centroid |
|---|---:|---:|---:|---:|---:|
| Raw probe | 3.500 s | -9.274 | 0 | 179.78 Hz | 651.72 Hz |
| Resample only | 3.500 s | -9.154 | 0 | 181.82 Hz | 648.21 Hz |
| Processed 1.00x | 3.130 s | -3.000 | 0 | 181.82 Hz | 648.14 Hz |
| Processed 1.10x | 2.820 s | -3.000 | 0 | 181.82 Hz | 650.53 Hz |
| Processed 1.15x | 2.700 s | -3.000 | 0 | 181.82 Hz | 648.29 Hz |

6 kHz input tone → 8 kHz conversion: relative aliased RMS **-78.27 dB**
(interior samples, edges excluded). Bundle generation and analysis: **2602 ms**,
not TTS time or call latency. F0 is an autocorrelation estimate with quantized
lag resolution; octave errors on real speech are possible. The reported noise
floor is the 10th percentile frame RMS, not isolated environmental noise.

The DSP implementation was retained; this is a baseline comparison across
tempo settings, not an invented before/after GSM improvement.

## Controlled Hindi/GSM comparison

Run `telephony_voice_ab.py` on the client with the same approved Hindi phrase.
The script now includes 1.10x and 1.15x explicitly, refuses to overwrite an
existing bundle, refuses silent DB-override fallback, and only computes WPM
for an offline recording when its exact transcript is supplied. Offline input
provider identity is unverified; local settings are not proof of file provenance.

Compare raw → resample-only → current → native cadence in randomized order,
first through headphones, then softphone/Asterisk, then the same GSM channel.
Use an actual Hindi-speaking tester. Record intelligibility (word errors),
naturalness 1–5, disturbance 1–5, sentence-boundary gaps, and comments.
Repeat with a second SIM/channel and handset without changing multiple factors.

- Poor raw audio: test an authorized alternate Bhashini service ID using `--candidate-service-id`; keep phrase/gender constant. No model is declared “best” without listening.
- Clean raw, poor conversion: identify the first degraded processing variant; do not hide it by speeding speech.
- Clean local files, poor sent RTP: investigate Asterisk transcoding/playback and timing.
- Clean transmitted RTP but poor handset: investigate Synway, GSM radio/network, channel and handset.
- Echo requires near/far-end comparison; a spectral/noise metric alone cannot diagnose it.

Playback gap diagnostics are not end-to-end audible latency. `capture_ms`
includes caller speech, not just VAD. `first_audio_ready_ms` means local WAV
ready, not handset sound. `talk_finished_event_to_recording` starts at the
received TALK_DETECT-finished event, not the exact final phoneme.

Primary API evidence: [Asterisk playback ordering guidance](https://docs.asterisk.org/Configuration/Interfaces/Asterisk-REST-Interface-ARI/Introduction-to-ARI-and-Channels/ARI-and-Channels-Simple-Media-Manipulation/)
and [ARI channels API](https://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/Channels_REST_API/).
The preallocated playback ID is supported before Asterisk 18; no latest-only
feature is required by that change.
