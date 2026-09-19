# Voice forensic result

Status: **CLIENT UAT REQUIRED**. The source of actual noise/robotic speech is
unproven. Synthetic tones cannot establish human naturalness, intelligibility,
ASR accuracy, packet loss, echo or GSM quality.

## Update 2026-09-19 — what changed, and what is still unknown

**Finding.** The welcome message and the AI's replies go through the *same* processing, so the
processing chain alone does not explain "welcome clear, answers noisy". What differs is the
**text**: the welcome is pure Devanagari; LLM replies carry digits, units, percentages,
brackets and Latin acronyms that an Indic voice skips or mangles. That is a plausible contributor
(not proven audible without a listener), and it is now removed at the source.

| Change | Detail |
|---|---|
| Tempo default 1.15 → **1.0** | At 1.0 the time-stretch code is **not run** (tests fail if it is). WSOLA at 1.15 on real speech (Windows SAPI, English, 48 kHz — *not* Bhashini Hindi) moved median F0 jitter 1.83% → 1.94% and mean HNR by −0.2 dB: barely measurable, so it was **not proven** to be the robotic sound; it is off because it is unnecessary work and makes replies sound rushed |
| `AI_AUDIO_PROFILE=clean` (default) | One band-limited resample (none if the source is already 8 kHz); gentle trim; loudness matched by the *speech* (−18 dBFS) not the peak, boost ≤ 9 dB, hard peak ceiling −3 dBFS (no clipping possible); 10 ms cosine edges. `legacy` = the previous chain, byte-identical, one setting away |
| Spoken-text normalisation (`hindi_tts_text.py`) | Digits/ranges/%/₹/units (incl. °C, °F) → Hindi words; LSD, NDDB, LUVAS, ICAR… spoken as a farmer says them; other capitals spelled; brackets → pauses. Pure Devanagari is returned unchanged (the approved welcome is never altered). The prompt also tells Gemini to write every word in Devanagari |
| Per-clip diagnostics | One log line per clip: source rate/duration, resampled?, tempo applied?, trim, gain, peak/RMS, clipping, noise floor, prep ms |

**A/B (one switch, same source).** `scripts/telephony_voice_ab.py` now writes
`D1_A_previous_production_legacy_1_15x` (A) and `D2_B_native_clean_1_0x` (B) plus µ-law previews and prints
duration, peak, RMS, clipping, noise floor, spectral centroid/roll-off, F0 jitter, HNR and WPM for source, A and B.
Run it on the client's raw Bhashini WAV: `--input-wav <raw.wav> --text "<exact transcript>"`. The comparison
of a handset's *sound* still needs a Hindi listener — nothing here scores naturalness.

**Still unknown — NOT PROVEN, requires live GSM/Synway test:** whether the residual "noise" is added
after Asterisk (gateway DSP, GSM radio, echo). `scripts/rtp_forensics.py --reference <played WAV>` turns that
into a number (`excess_distortion_db` ≈ 0 ⇒ the AI's audio reached the wire clean); the plan for the gateway
side is [SYNWAY_GSM_AB_PLAN.md](SYNWAY_GSM_AB_PLAN.md).

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
