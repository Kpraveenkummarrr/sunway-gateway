# Client execution sheet — 2026-09-19 update

Covers the four problems reported after the last pass: the 15–20 s wait after every
question, calls dropping at about 2:45, noisy / robotic replies, and interruptions
not being heard. These are **operator steps**; nothing here has been run on the
client's machine or the Synway. Use a maintenance window and a Hindi-speaking
tester. Do not paste credentials, raw calls or pcaps into public issues.

Client paths used below: repository `/home/admin1/sunway-gateway`, services
`sunway-backend` and `sunway-ai-worker`, logs `journalctl -u sunway-ai-worker`.

## 8.1 Back up first

Follow section 1 of the [final-pass sheet](CLIENT_UAT_CHECKLIST.md#1-preserve-the-actual-client-baseline)
(code revision, `backend/.env`, `/etc/asterisk`, database dump). Then record the
current values of the settings this update changes — non-secret keys only:

```bash
cd /home/admin1/sunway-gateway/backend
grep -E '^(AI_CALL_TIMEOUT_SECONDS|AI_TTS_SPEED|AI_END_OF_SPEECH_SILENCE_SECONDS|AI_MAX_TURN_SECONDS|AI_MAX_CONTEXT_CHARS|AI_TALK_DETECT_[A-Z_]+|AI_ENDPOINT_[A-Z_]+|AI_AUDIO_PROFILE|RAG_TOP_K|RAG_SIMILARITY_THRESHOLD)=' .env \
  | tee "$HOME/sunway-backup-latest/settings-before.txt"
```

## 8.2 The `.env` lines that must change

A value copied from an older `.env.example` **overrides** the new code default, so
these are not optional if the file still holds the old value:

| Key | Old value in an older `.env` | Set to | Why |
|---|---|---|---|
| `AI_CALL_TIMEOUT_SECONDS` | `120` | `900` | 120 s, checked only at turn ends, cut calls at ~2:45 (`docs/CALL_DISCONNECT_ROOT_CAUSE.md`) |
| `AI_TTS_SPEED` | `1.15` | `1.0` | 1.0 means the audio is not tempo-processed at all |
| `AI_MAX_CONTEXT_CHARS` | `2000` | `3600` | 2000 cut the third and fourth retrieved chunk off mid-sentence |

Everything else in `.env.example` that this update added is a *default* — leave it
out of `.env` unless you are deliberately tuning. Also confirm
`RAG_EMBEDDING_PROVIDER` is **not** `mock` in production (the guard rejects it);
see `docs/RAG_ROOT_CAUSE.md`.

Then, after the operator approves the maintenance window:

```bash
sudo systemctl restart sunway-backend sunway-ai-worker
sudo systemctl is-active sunway-backend sunway-ai-worker asterisk postgresql
sudo journalctl -u sunway-ai-worker --since '2 minutes ago' --no-pager | grep -E 'Stasis|registered|ready|Error'
```

The admin panel now also shows: maximum call length, caller-silence wait,
barge-in sensitivity, audio profile, speech loudness and peak ceiling (values
there override `.env` for the next call, no restart needed).

## 8.3 One question — read what the worker logged

Place a call, ask one LSD question in Hindi, and stay silent while the answer plays.

```bash
sudo journalctl -u sunway-ai-worker --since '3 minutes ago' --no-pager \
  | grep -E 'Barge-in sensitivity|Caller finished|Recording finished|Turn timing|TTS clip|Call finished'
```

What to expect and what each line proves:

| Log line | Good | Bad — and what it means |
|---|---|---|
| `Barge-in sensitivity applied: … talk_detect=200,350` | present once per call | absent: `AI_TALK_DETECT_OVERRIDE=false` or ARI variable set failed; barge-in then uses the dialplan's `200,500` |
| `Caller finished speaking: … signal=…` | present, `speech=` about the length of your question | absent, and `Recording finished … ended_by=MAX DURATION (silence was never detected)`: neither signal worked — see below |
| `signal=TALK_DETECT finished + worker timer` | the gateway sends voice frames in silence, or a short hangover of them | — |
| `signal=no voice frames (RTP statistics)` | the gateway stops sending voice frames in silence (comfort noise / VAD) — the client's case | — |
| `Recording finished: … end_of_speech_wait_ms=` | about **1100–1500** | 5,000+ , or `ended_by=MAX DURATION`: silence was not detected; the turn ran to `AI_MAX_TURN_SECONDS` |
| `Turn timing … end_wait_ms=… heard_delay_ms=…` | `heard_delay_ms` = end wait + ASR + retrieval + LLM + first TTS chunk + ~0.2 s | one stage dominating: that is now the thing to fix, and the line names it |
| `TTS clip: … tempo_applied=False … clipped=0.0000` | on every clip | `tempo_applied=True` means `AI_TTS_SPEED` is still not 1.0 |
| `Call finished: … duration= turns= barge_ins= ended_by=` | your call's real length | — |

The old fingerprint of the problem in `Stage … stage=talk_finished_event_to_recording
duration_ms=` was 15,000–19,000. After the update that stage should be under a
few seconds, or absent.

**If `MAX DURATION` still appears** with `signal=` absent: send the log lines and one
capture (8.7). It means the gateway sends neither continuous voice frames nor a
hangover the worker can see, *and* the RTP counters were unavailable
(`ARI rtp_statistics`), or the line noise sits above the barge-in threshold. The
knobs are `AI_TALK_DETECT_THRESHOLD` and, for very short hangovers,
`AI_TALK_DETECT_SILENCE_MS=100`.

## 8.4 Three consecutive questions

Ask three different questions in one call. Each `Recording finished` must show a
worker-detected end and `end_of_speech_wait_ms` in the same 1100–1500 range. Record
the three `heard_delay_ms` values.

## 8.5 Interruption

Each of these, on the real GSM path, several times:

1. interrupt during the first sentence of an answer;
2. interrupt in the pause **between** two sentences of one answer (the case that used to fail);
3. interrupt twice in a row;
4. do not interrupt (line noise must not cut the reply short).

```bash
sudo journalctl -u sunway-ai-worker --since '10 minutes ago' --no-pager | grep -E 'Barge-in|barge_in_stop'
```

Pass: `Barge-in detected …` within a fraction of a second, `stage=barge_in_stop`
duration in the tens of milliseconds, the reply stops. Also write down: was the
first word of the interruption lost, and did any reply get cut short by noise.

## 8.6 A call longer than 3 minutes

Stay on the line for at least 5 minutes, asking a question every minute or so.
`Call finished` must show the duration you actually held and `ended_by=caller/gateway/Asterisk`;
there must be no `Call reached its maximum duration` before 900 s. To see the
closing message, temporarily set `AI_CALL_TIMEOUT_SECONDS=60` in the admin panel:
after 60 s of call time the caller hears a polite closing sentence, then the call
ends with `cause=max_duration`. Put the value back afterwards.

## 8.7 Capture one call (evidence for the noise question)

```bash
sudo tcpdump -i any -n -s 0 -w /tmp/call.pcap udp and host <SYNWAY_IP>     # start, then place the call
python scripts/rtp_forensics.py /tmp/call.pcap --out /tmp/call-forensics \
    --reference /var/spool/asterisk/sounds/ai-agent/<a reply WAV>.wav
```

Read `payload_types` (is payload type 13 present?), `sequence.loss_percent`,
`timestamps`, `pacing_ms`, and `integrity_vs_reference.excess_distortion_db`.
A value near 0 dB means the AI's audio reached the wire as clean as G.711 allows,
so any remaining noise is added after Asterisk. Then follow
[the Synway A/B plan](SYNWAY_GSM_AB_PLAN.md) — one gateway change at a time.

## 8.8 Hindi audio A/B (the noisy / robotic question)

The default is now the clean chain at speed 1.0. To compare it with the previous
chain on identical text, without a code change:

1. `AI_AUDIO_PROFILE=legacy` and `AI_TTS_SPEED=1.15` in the admin panel → call, ask the same question, save the reply;
2. set `clean` and `1.0` → same call, same question;
3. compare by ear on the handset, blind, several people; write down clarity 1–5 and disturbance 1–5.

The worker logs the same clip's measurements for both (`TTS clip: … rms= peak= clipped= noise_floor=`).
No result here claims one sounds better on a handset — that needs a Hindi listener.

## 8.9 Knowledge base (Lumpy Skin Disease)

```bash
cd /home/admin1/sunway-gateway/backend
.venv/bin/python scripts/reindex_knowledge.py --help
.venv/bin/python scripts/rag_uat_probe.py --out /tmp/rag-probe-$(date +%F).json
```

Ask, by voice: "लम्पी रोग क्या है", "इसके लक्षण क्या हैं", "यह कैसे फैलता है",
"इससे बचाव कैसे करें", "इसका टीका कब लगवाएं", "क्या इसका दूध पी सकते हैं",
and a follow-up with no topic ("इसका इलाज क्या है"). A wrong or missing answer is
a bug to report with the question, the `RAG result document=… chunk=…` log lines
and the chunk text. Retrieval was verified here only against a synthetic Hindi
knowledge base; the client's real document has not been seen.

## 8.10 Send back

For every failed row: the exact question, the log lines above, and — for audio —
the capture and a short recording of the handset. Redact caller numbers.

## 8.11 Rollback of this update only

```bash
cd /home/admin1/sunway-gateway
git switch <the previous branch or commit recorded in 8.1>
cp "$HOME/sunway-backup-latest/backend.env" backend/.env     # restores the old settings
sudo systemctl restart sunway-backend sunway-ai-worker
```

Individual switches, without a code rollback: `AI_ENDPOINT_MONITOR=false` (back to
Asterisk's own end-of-speech detection only), `AI_AUDIO_PROFILE=legacy` with
`AI_TTS_SPEED=1.15` (the previous audio chain, byte-identical),
`AI_TALK_DETECT_OVERRIDE=false` (barge-in sensitivity back to the dialplan's
`200,500`, and no per-turn detector reset). Restoring `AI_CALL_TIMEOUT_SECONDS=120`
brings the ~2:45 cut-off back — it is listed only so the rollback is complete.
