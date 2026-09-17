# Client issues — current status

Date: 2026-09-17

One row per issue the client raised, with the cause that was actually found,
what changed in the code, the evidence behind the status, and what is still
outstanding.

**Evidence rule.** *Verified* means measured on a real system and
reproducible. *Local only* means proven on the development Asterisk with
stand-in providers. *Client required* means it cannot be established without
the client's gateway, SIM cards, Bhashini/Gemini keys or knowledge base — the
development machine has none of those.

---

## 1. The agent takes too long to answer

**Cause.** The worker waited 8 seconds of silence before it accepted that the
caller had finished, then synthesised the entire reply before playing any of
it, and re-synthesised the welcome message on every call.

**Changed.** End-of-speech silence 8 s → 2 s; the reply is split into
sentences and the first one starts playing while the rest is still being
synthesised; fixed phrases are rendered once and reused; per-stage timings are
logged for every turn.

**Evidence.** Caller stops speaking → hears the reply: **10.68 s → 3.41 s**,
measured on real Asterisk 18.10 with a stand-in TTS latency model.

**Outstanding.** Real Bhashini and Gemini round-trip times on the client's
network. *Client required.*

---

## 2. The voice sounds robotic

**Cause.** Flat 1.0× delivery, long silence padding at the start and end of
each clip, a record beep before every single turn, and a bookish written-Hindi
prompt.

**Changed.** Configurable tempo (`AI_TTS_SPEED=1.15`, pitch preserved), edge
silence trimmed, the turn beep is off by default (`AI_RECORD_BEEP=false`) and
never plays after an interruption, a conversational Hindi language policy, and
a helpline persona that keeps turns to one idea of about 25 words instead of
paragraph-length answers.

**Evidence.** Audio tests for tempo and trimming; beep behaviour covered by
two tests and confirmed on a live call. Naturalness itself is a human
judgement. *Local only → client required.*

---

## 3. The AI answers without reading the knowledge base

**Cause.** Two separate faults. First, documents indexed with one embedding
model were being queried with another, so the vectors were not comparable.
Second — found this round — **a follow-up question retrieved nothing at all**:
"iska ilaj kya hai?" names no topic, and the search ran on those words alone.

**Changed.** Hybrid retrieval (vector + lexical) with embedding-space metadata
so mismatched documents are excluded rather than silently mis-ranked; domain
aliases so "lampi", "lampy", "लम्पी" all reach the Lumpy Skin Disease source;
and a short follow-up is now prefixed with the caller's previous question
before retrieval, so the topic carries forward.

**Evidence.** 15 deterministic retrieval tests: seven farmer phrasings
(English, Devanagari, Hinglish, transliterated, single word) each reach the
LSD source and not an unrelated document; an unrelated question does not reach
the LSD source; and a two-turn conversation where the follow-up used to
retrieve nothing now returns the right document.

**Outstanding.** The client must **re-index the knowledge base** with the
configured embedding provider — retrieval stays weak on stale vectors no
matter what the code does. Accuracy against the real PDF is *client
required*.

---

## 4. The agent ignores the caller and talks over them

**Cause.** It had never actually been verified end to end; there was no
evidence Asterisk was delivering talk events to the worker at all.

**Changed.** `TALK_DETECT(set)=200,500` is applied before the call enters the
Stasis app; `ChannelTalkingStarted` stops playback, discards the queued
sentence chunks and starts recording the caller.

**Evidence.** **Verified on real Asterisk 18.10**: the interruption was
detected **116 ms** after the caller started speaking, playback stopped 2.3 s
into a 10.4 s clip, recording started 136 ms later, no errors. Re-run after
the beep fix with the same result.

**Outstanding.** How often GSM line noise or echo of the agent's own voice
falsely triggers it. *Client required.*

---

## 5. Noise and unclear audio

**Cause.** `audioop.ratecv` was resampling 48 kHz → 8 kHz **with no
anti-aliasing filter**, so 6 kHz energy folded back into the phone band at
full level.

**Changed.** Band-limited resampler with a windowed-sinc low-pass, peak
normalisation to −3 dBFS, short fades at clip edges.

**Evidence.** Alias energy **0.0 dB → −79.8 dB**, in-band 3 kHz preserved to
within 0.01 dB, no clipping.

**Outstanding.** Noise contributed by the GSM path itself, echo and one-way
audio. *Client required.*

---

## 6. Call Centre and admin GUI were never tested

**Cause.** Both existed only as unit-tested code.

**Changed.** Nothing structural this round. A live routing call was placed on
the development Asterisk: it dialled three destinations in the configured
priority order and fell back to the AI agent. The admin panel was served and
its health view read real Asterisk state.

**Outstanding.** A person clicking through the panel in a browser, and
department routing to real staff mobiles over GSM. *Client required.*

---

## 7. Conversation behaviour vs the helpline reference document

**Cause.** The deployed system prompt was a generic "helpful telephone
assistant for this business". The welcome message was the client's approved
LUVAS wording, but nothing after it told the agent it was a Lumpy Skin Disease
helpline, what it must never say, or when to escalate.

**Changed.** A helpline persona is now part of the prompt by default
(`AI_PERSONA=lsd_helpline`): farmer-friendly spoken Hindi, one idea per turn,
never diagnoses an animal, never names a medicine or a dose, never discusses
price, compensation or scheme eligibility, escalates a suspected active case to
the nearest government veterinary hospital, and attributes ethnoveterinary
preparations to Sampurna Nand Yadav and colleagues (NDDB). District diagnostic
centres are loaded from an operator-controlled file and injected verbatim, and
the agent is explicitly forbidden from naming any centre that is not in it.
See [helpline behaviour](HELPLINE_BEHAVIOUR.md).

**Evidence.** 22 tests covering the guardrails, the prompt assembly order, and
the directory — including that an unlisted district produces a refusal rather
than the wrong centre, and that a broken directory file degrades to naming
nothing.

**Outstanding.** Two things. **The Haryana district → diagnostic centre list
still has to be supplied**; it ships empty on purpose. And the persona
constrains the model, it does not guarantee it — the actual Hindi wording has
to be reviewed on real calls with the client's Gemini key. *Client required.*

---

## Test run

```
pytest -q  →  320 passed, 1 skipped, 0 failed
```

The skip is the opt-in real-provider test, which needs live API keys.

## What the client has to do

1. Supply the district diagnostic centre list, and set
   `REFERRAL_DIRECTORY_PATH`.
2. Re-index the knowledge base with the configured embedding provider.
3. `pip install -r requirements.txt`, `alembic upgrade head`, restart the
   worker and backend.
4. Make real GSM calls and review: Hindi answer quality, interruption
   handling, department routing to staff mobiles, and voice naturalness.
5. Click through the admin panel in a browser.
