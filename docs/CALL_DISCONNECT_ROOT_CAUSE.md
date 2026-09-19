# Calls dropping at about 2:45 — root cause and fix

**Status: FIXED (reproduced and verified on real Asterisk 18.10). NOT PROVEN — requires live
GSM/Synway test** that a physical call also stays up (the SIP session timers and the gateway were
not part of the reproduction).

## Root cause

`AI_CALL_TIMEOUT_SECONDS` was **120** (copied from the example `.env` into the client's `.env`, so it
also overrode any better code default). It was evaluated **only when a caller turn finished**, and
the hangup was abrupt — no goodbye. With ~55 s per exchange (15–20 s wait + answer + speaking) the
checks landed at about 55 s, 110 s and 165 s; the first one past 120 s was 165 s = **2:45**.

Reproduced before the fix (comfort-noise gateway, 15 s of provider latency per answer): the call
was cut at **143.2 s** with `Call timeout reached` after 2 answers.

## Fix

| Change | Where |
|---|---|
| Default limit 120 s → **900 s** (0 = no limit); editable in the admin panel (0–7200) | `config.py`, `system_config.py`, `.env.example` |
| Enforced by a **timer**, not at turn boundaries: if the AI is listening the turn is ended at once; if it is thinking or speaking the answer finishes and the call closes when it would listen next | `call_controller._call_deadline` |
| A polite **closing message** is played, then the call ends with cause `max_duration` (never an abrupt cut) | `_end_if_over_duration`, `AI_CLOSING_MESSAGE` (Hindi default built in) |
| One line per call: `Call finished: … duration= turns= barge_ins= ended_by=` | `_finish_call` |

**The client's `.env` must be edited** — a copied `AI_CALL_TIMEOUT_SECONDS=120` overrides the new
default: set it to `900` (or use the panel). See [the execution sheet](CLIENT_UAT_UPDATE_20260919.md#82-the-env-lines-that-must-change).

## Evidence

| | Before | After |
|---|---:|---:|
| Call length (same scenario, 15 s provider latency per answer) | **143.2 s**, cut by `Call timeout reached` | @@LONG@@ |
| Answers received | 2 | @@LONGN@@ |

`tests/test_call_duration_limit.py` (8 tests): the default is 900; a call past its limit gets a
goodbye and cause `max_duration`; a call at 170 s with the default is left alone; 0 disables the
limit; the timer ends a *listening* call without waiting for a turn boundary; the timer does nothing
if the call ends first; a call that is thinking is not interrupted.

## Not proven

Anything that ends a real call other than this timer: SIP session timers, the Synway, the GSM network
or a caller hanging up. `Call finished … ended_by=` now says who ended each call
(`this worker` vs `caller/gateway/Asterisk`), which is the evidence needed if calls still drop.
