# GSM call noise/disturbance — root-cause analysis

Date: 2026-09-17

The client still hears noise/disturbance on real GSM calls, and separately
asked whether Sarvam-M (as a Gemini replacement) could be part of the
problem. It cannot be, and this document proves why — then does what can
actually be done from here on the parts of the audio chain that remain
genuinely open.

## Stage-by-stage

| Audio stage | Measurement | Evidence | Conclusion |
|---|---|---|---|
| LLM (Gemini or Sarvam-M) | `LLMResponse` dataclass fields | `tests/test_llm_noise_independence.py::test_llmresponse_carries_no_audio_relevant_data` — exactly `{text, finish_reason}` | **Cannot be the source.** No audio bytes, sample rate, or gain parameter ever crosses this boundary. |
| Text cleanup (`spoken_text`) | Two very different reply "styles" (long plain vs. markdown-laden) pushed through the pipeline | `test_the_processed_audio_is_identical_regardless_of_which_llm_style_produced_the_text` — processed output is **byte-identical** for the same source WAV regardless of which text drove the chunking | Chunk *count* can differ between providers; the *audio content of each chunk* cannot be affected by which provider produced the text. |
| Bhashini TTS raw output | Not capturable here — no Bhashini key in this environment | `docs/TELEPHONY_VOICE_QUALITY.md` (prior session): request has no SSML/prosody/rate parameter — "female" gender is the only voice selection made | **Unknown from here.** Needs a live Bhashini capture on the client machine. |
| Resampling (48kHz→8kHz) | 1kHz + 6kHz test tone through `normalize_for_asterisk_playback()`, re-measured this session | `test_downsampling_does_not_alias_high_frequencies_into_the_phone_band` — **-79.8 dB** alias suppression (re-measured fresh just now, matches the figure from the original fix) | **Not a noise source** — was, before the windowed-sinc resampler replaced `audioop.ratecv` (which passed the same test at 0.0 dB). |
| Normalization/tempo | Peak-normalize to -3 dBFS, WSOLA tempo at 1.15x | `test_prepare_tts_never_clips_hot_audio`, `test_the_tempo_stage_keeps_the_speaker_pitch_of_voice_like_audio` — 0% clipping, F0 unchanged (148.1→148.1 Hz) at 1.15x | **Not a noise source** on the evidence available; pitch is preserved, no clipping introduced. |
| Playback (`_write_and_play_wav` → ARI `/play`) | Absolute `sound:` path, WAV written under the Asterisk spool root | Live-verified in a prior session (silent-playback bug found and fixed) | Working as intended; no further diagnostic capability without a live channel. |
| Asterisk media / RTP | — | Not measurable without a live call | **CLIENT REQUIRED.** |
| Synway gateway (transcoding) | — | Not measurable without the physical SMG4008-8G | **CLIENT REQUIRED.** |
| GSM radio/line | — | Not measurable without a SIM and a real call | **CLIENT REQUIRED.** |

## Answering Part 12 directly

**1. Is Gemini causing the audio noise?**
No. `LLMResponse` carries no audio data (proven, not assumed — see the
first table row). The LLM's only effect on audio is indirect, through the
*length and formatting* of the text it returns, and that effect is fully
absorbed by `spoken_text()`/`speech_chunks()` before synthesis — proven
identical regardless of provider style.

**2. Is Sarvam-M changing the audio noise when all downstream audio
settings remain identical?**
Cannot be measured with real Sarvam-M output — no machine available to this
session can run the 24B model (see `docs/LLM_AB_TEST.md`). **Structurally**,
by the same proof as question 1, it cannot: whatever text Sarvam-M returns
goes through the identical `spoken_text()` → TTS → `prepare_tts_for_playback()`
pipeline, which is provably indifferent to which provider supplied the text.

**3. Is the noise present in raw Bhashini audio?**
Unknown from this environment — no Bhashini key here. This is exactly what
`scripts/telephony_voice_ab.py`'s `A_original_bhashini.wav` output (stage
A, unprocessed) is for: it is the raw provider response, captured before
any of our processing runs. **CLIENT REQUIRED.**

**4. Is the noise introduced during local audio processing?**
No new noise source was found this pass, and one was fixed in a prior
session (the aliasing resampler — 0.0 dB → -79.8 dB, re-confirmed today).
Peak normalization introduces zero clipping and preserves pitch under
tempo change. This cannot be ruled out with absolute certainty without a
live before/after capture (stage B0 vs. B1 in `telephony_voice_ab.py`), but
every measurable property of this stage is currently clean.

**5. Is the noise introduced only after Asterisk/Synway/GSM?**
This is where the actual client-reported noise most plausibly originates,
given (3) and (4) show no problem up to the point audio leaves this
codebase — but it is stated as the *most likely remaining candidate*, not
as a proven conclusion, because it has not been measured. **CLIENT
REQUIRED.**

**6. What evidence supports each conclusion?**
Listed in the stage table above: two fresh test runs this session
(`test_llm_noise_independence.py`, re-run alias measurement), plus the
existing `test_audio.py` suite (26 tests) and `docs/TELEPHONY_VOICE_QUALITY.md`
from the prior audio-quality session, all passing / consistent.

## What isolates stages 3, 5, 6, 7, 8, 9 (client machine only)

This cannot be done from a laptop with no Bhashini key, no Asterisk-facing
GSM trunk, and no SIM. On the client machine, in order:

```bash
# Stage A: raw Bhashini output, before any of our processing.
python scripts/telephony_voice_ab.py --out /var/tmp/sunway-noise-check
# -> A_original_bhashini.wav: listen to this alone. If the noise is
#    already here, it is Bhashini's, not ours or Asterisk's.

# Stage B: our processing only (no Asterisk/GSM yet).
# -> B0_resample_only_8k.wav (just resampled) vs B1_current_8k.wav (full
#    pipeline). Compare both to A. If B sounds worse than A, the defect is
#    in our processing — file it against app/services/audio.py with the
#    specific B file.

# Stage C: G.711 codec preview (what Asterisk negotiates for the trunk).
# -> C1..C4 in the same output dir. Compare to B. If C sounds worse than
#    B, the codec/transcoding step is implicated.

# Stage D-G: only obtainable with a real call.
python scripts/audio_level_report.py --latest 20
# -> run against real ai-agent__*.wav caller-turn recordings after a real
#    GSM call. These are the caller's audio as Asterisk decoded it, so
#    NOISY/CLIPPING flags here point at the GSM/SMG leg specifically, not
#    at our TTS output at all (caller audio never passes through our TTS
#    pipeline).
```

If the client can also capture the Synway gateway's own RTP stream (mirror
port or `tcpdump` on the Asterisk host, `wireshark`/`rtpbreak` to extract
the audio) before and after Asterisk, that isolates stage F from stage
G/H/I directly. That capability was not assumed to exist and is offered
as the next concrete step, not run here.

## What this document does not claim

It does not claim the Asterisk/Synway/GSM path is the noise source — only
that it is the only remaining unmeasured candidate once the LLM and our own
processing are ruled out or shown clean. It does not claim Sarvam-M sounds
better or worse than Gemini on audio grounds, because neither can currently
produce real audio in this environment for Sarvam-M's case (no hardware)
and Gemini's case (no key) — see `docs/LLM_AB_TEST.md`.
