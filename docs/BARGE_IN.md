# AI barge-in

## Local architecture

```text
AI playback starts
  -> Asterisk TALK_DETECT emits ChannelTalkingStarted
  -> controller cancels the active ARI playback
  -> prefetched TTS chunk is discarded
  -> caller recording starts after playback stop
  -> existing ASR -> RAG -> LLM -> TTS loop continues
```

The controller tracks the active playback per channel and uses a per-call
generation counter. A talking event without active playback is ignored, so
normal caller speech during the recording phase does not create a second
interruption. Playback cancellation is issued through ARI
`DELETE /playbacks/{id}`.

The AI dialplan enables `TALK_DETECT(set)=200,500` before entering Stasis.
The `func_talkdetect` module and ARI talking events must be available in the
deployed Asterisk installation. These thresholds are local defaults, not a
claim that they are tuned for the client's GSM echo/noise conditions.

## Local verification

Tests cover:

- caller speech stopping an active playback;
- prefetched reply chunks being discarded;
- a talking event with no active playback being ignored.

## Required client verification

On the real GSM path, test no interruption, early interruption, mid-sentence
interruption, immediate speech, and background noise. Record playback-stop
delay and check for false triggers from GSM noise or acoustic echo. A local
event/unit test cannot prove echo cancellation or voice detection quality.
