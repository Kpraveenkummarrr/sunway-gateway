# AI barge-in

## Architecture

```text
AI reply starts (one or more chunks)
  -> Asterisk TALK_DETECT emits ChannelTalkingStarted
  -> controller:
       - a chunk is playing      -> cancel that ARI playback
       - between two chunks      -> nothing is playing, but the reply is still in progress:
                                    bump the generation so the chunk being prepared and every
                                    later one is discarded
  -> caller recording starts after the reply is abandoned
  -> existing ASR -> RAG -> LLM -> TTS loop continues
```

The controller tracks, per call, the active playback, a barge-in generation
counter and a `reply_in_progress` flag that stays true from the first reply chunk
being requested until the reply is finished or discarded. Playback cancellation is
issued through ARI `DELETE /playbacks/{id}`. A talking event while the AI is only
*listening* is not a barge-in: it feeds the end-of-speech tracker instead
(`docs/LATENCY_ROOT_CAUSE.md`).

## What was found and fixed (2026-09-19)

All of this was measured on a real Asterisk 18.10 with a simulated gateway
(`scripts/dev_call_harness.py`). Physical GSM behaviour is a separate question
(see "Not proven").

### 1. Interruptions in the gap between reply chunks were ignored — fixed

A reply is spoken as several chunks, and while the next chunk is still being
synthesised nothing is playing. The controller only acted on a talking event if a
playback was active, so an interruption in that gap did nothing and the AI then
played the remaining chunks over the caller.

| | Before | After |
|---|---:|---:|
| Interruption during the gap between chunks: AI audio heard while the caller was speaking | **2.72 s** | **0.0 s** |
| Was a stop issued | never (no playback to stop) | remaining chunks discarded |

### 2. A "stuck talking" detector made barge-in impossible on some gateways — mitigated

TALK_DETECT's silence clock only advances on voice frames. If the gateway goes
from speech straight to comfort noise or silence, the detector still believes the
caller is talking and never reports the *next* burst of speech. Measured with a
gateway that stops sending voice frames the instant speech stops:

| Gateway behaviour after speech | Second burst reported? |
|---|---|
| Continuous voice frames | Yes |
| Comfort noise / silence, ≥ 300 ms of voice-frame hangover | Yes |
| Comfort noise / silence, 0 ms hangover | **No — no event at all** |
| Comfort noise / silence, 100 ms hangover | Late and unreliable |
| Any of the above, after `TALK_DETECT(remove)` + `TALK_DETECT(set)` | **Yes, in ~120 ms** |

The controller therefore resets the detector at the end of every caller turn,
in the background, before the reply is even generated. The reset is skipped when
`AI_TALK_DETECT_OVERRIDE=false`, because then the dialplan owns TALK_DETECT and
its parameters are not the worker's to recreate.

### 3. Sensitivity is now a setting — soft-spoken callers were never detected

The dialplan hard-coded `TALK_DETECT(set)=200,500`. The second value is a
*magnitude* threshold (mean |sample| of a 20 ms frame, 16-bit scale), not a
duration. A soft-spoken caller at a mean magnitude of 350 never triggered it.
`AI_TALK_DETECT_THRESHOLD` (default 350) is now applied to every call over ARI —
no dialplan edit — and is adjustable in the admin panel. Asterisk's own default
is 256. Raise it if line noise or the AI's echo cuts replies short; set it near
2.5× the line's noise level (`scripts/audio_level_report.py` shows it).
`AI_TALK_DETECT_SILENCE_MS` (default 200) is how long Asterisk waits before
reporting "finished talking"; keep it short (see the latency document: it is also
the voice-frame hangover a comfort-noise gateway must provide for that event).

## Local verification

Automated (all run in the suite):

- caller speech stopping an active playback; prefetched reply chunks discarded;
- a talking event while idle not interrupting anything;
- the gap-between-chunks case, at unit level (`tests/test_barge_in_matrix.py`);
- talk-detect events reaching the end-of-speech tracker before the barge-in
  early-return, the reset at the end of each turn, and the reset being skipped
  when the dialplan owns TALK_DETECT (`tests/test_endpointing.py`).

Real Asterisk, simulated gateway (raw numbers in the latency document's tables):

- in-flight interruption: TALK_DETECT event and stop request at 140 ms, 0.0 s of overlap;
- interruption between chunks: 0.0 s of overlap (was 2.72 s);
- interruption with a comfort-noise / DTX gateway with 0 ms hangover: detected at 131 / 130 ms, playback stopped, 0.0 s overlap.

## Not proven — requires live GSM/Synway test

- **The first syllable of an interruption.** Recording starts after the reply is
  abandoned, so the first word or two of what the caller says while interrupting
  can be lost. Not measurable here.
- **False triggers.** Line noise or acoustic echo of the AI's own voice above the
  threshold will cut replies short; only the real GSM path shows it. The threshold
  is the knob.
- **Half-duplex behaviour of the gateway/GSM path.** If the far end cannot hear or
  send while the AI is talking, no software can detect the interruption.
- **Whether the Synway's hangover is long enough** for the "finished talking"
  event; the RTP-statistics signal covers the case where it is not, for
  end-of-speech, and the reset above covers it for barge-in.

Test on the real path: no interruption, early interruption, mid-sentence
interruption, interruption in the pause between two sentences, immediate speech,
and background noise. Record the playback-stop delay (`Stage … stage=barge_in_stop`
in the worker log) and any false triggers.
