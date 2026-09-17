# Client issues — current status

Date: 2026-09-17 · Third pass — Sarvam-M local-LLM feasibility and noise root-cause

**Status meanings.** FIXED = closed with evidence on this machine, nothing
outstanding. PARTIALLY FIXED = the cause was found and fixed, but part of the
work remains. CLIENT UAT REQUIRED = the code work is done and only a live run
on the client's box, gateway or provider keys can confirm it. BLOCKED = it
cannot proceed without something the client has to supply.

The development machine has **no SMG4008-8G, no SIM, no Bhashini key and no
Gemini key**. Nothing here claims a GSM, browser or live-provider result.

## Matrix

| Client Issue | Root Cause | Fix | Automated Evidence | Live Evidence | Remaining Action | Status |
|---|---|---|---|---|---|---|
| 1. Time lag | 8 s end-of-speech wait; whole reply synthesized before any audio; welcome re-synthesized per call; a short closing sentence was merged into the first chunk, so the caller waited for the whole reply's synthesis | End-of-speech 8 s → 2 s; sentence chunking with the next chunk synthesized during playback; fixed phrases pre-rendered once; first chunk allowed to be shorter (24 chars) than later ones (40) | `test_latency.py` (6): first audio arrives before whole-reply synthesis; chunk N+1 overlaps chunk N; second call needs no welcome TTS. First-chunk text down 17% on a 5-reply sample (87→50, 66→36 chars on two of them) | Caller-stops-to-reply **10.68 s → 3.41 s** on real Asterisk 18.10 (stand-in TTS) | AI-01, AI-02: time a real GSM call and read the `Turn timing` line | CLIENT UAT REQUIRED |
| 2. Robotic / non-expressive voice | Flat 1.0× delivery, silence padding, a beep every turn, bookish prompt, markdown/list markers/emoji reaching the TTS engine. **Confirmed this pass: the LLM provider (Gemini) is not a contributor** — `spoken_text()` cleanup output is proven identical regardless of which provider's formatting habits produced the reply | Tempo 1.15× pitch-preserved, silence trimmed, beep off by default, conversational Hindi policy, helpline persona (one idea, ~25 words), `spoken_text()` strips headings/bullets/numbering/emphasis/links/tables/emoji before synthesis | `test_spoken_text.py` (20), `test_audio.py` (26), `test_llm_noise_independence.py` (3). 1.15× keeps F0 at **148.1 Hz vs 148.1 Hz**; `LLMResponse` proven to carry no audio-relevant fields | A/B bundle produced offline: A 48 kHz source → B1 current 8 kHz, duration 3.00→2.58 s, peak −3.0 dBFS, no clipping | AI-13: listen to `B1` vs `B2`; AI-11 on a real call. Switching to Sarvam-M would **not** fix this on its own — see issue 10 | CLIENT UAT REQUIRED |
| 3. Database / KB retrieval | Documents embedded with one model were being queried with another, so search excluded them silently; a pronoun-only follow-up retrieved nothing; **and there was no way to re-index**, so the operator could not fix any of it | Hybrid vector+lexical retrieval with embedding-space metadata; domain aliases; follow-up carries the previous question's topic; new `knowledge_reindex` service, `/api/knowledge/index/status`, `/api/knowledge/index/reindex` and `scripts/reindex_knowledge.py` | `test_lsd_knowledge_retrieval.py` (22) — all seven mandatory questions reach the answering passage, in Hindi, Hinglish, transliteration and one word; `test_knowledge_reindex.py` (11) — a stale document is reported, re-indexed and searchable again | Full cycle run against real PostgreSQL: a document indexed with `openai:text-embedding-3-small:1536` reported STALE and exit code 1 → re-index → "searchable by the AI: 1", exit code 0 | KB-01…KB-05: upload the real PDF, re-index, run the mandatory questions | CLIENT UAT REQUIRED |
| 4. AI not listening when interrupted | Never verified end to end; no evidence Asterisk was delivering talk events | `TALK_DETECT(set)=200,500` before Stasis; talk event stops playback, discards queued chunks, starts recording; the turn after an interruption never beeps | `test_barge_in_matrix.py` (9) — interruption on the first, middle and last chunk; two in a row; stale audio never plays; line noise with nothing playing changes nothing; events for an unknown or ended call are ignored | **Verified on real Asterisk 18.10**: detected **116 ms** after the caller spoke, playback stopped 2.3 s into a 10.4 s clip, recording started 136 ms later, 0 errors | AI-07…AI-10, especially the false-trigger rate on a noisy GSM line | CLIENT UAT REQUIRED |
| 5. Voice noise / disturbance | `audioop.ratecv` resampled 48 kHz → 8 kHz **with no anti-alias filter** (fixed, prior pass). **This pass: proved the LLM cannot be the source** (structurally — `LLMResponse` carries no audio data — and empirically — identical processed audio for two very different provider-style texts through the same source WAV) **and narrowed the remaining candidates to Asterisk/RTP/Synway/GSM**, none measurable without the physical setup | Band-limited windowed-sinc resampler, peak normalisation to −3 dBFS, edge fades; stage-by-stage isolation procedure written (`docs/NOISE_ROOT_CAUSE.md`) so the client can pin down exactly which of Bhashini / our processing / Asterisk / Synway / GSM the residual noise is in | `test_audio.py` (26), `test_llm_noise_independence.py` (3): alias **0.0 dB → −79.8 dB** (re-measured fresh this pass, matches the prior figure exactly); processed audio byte-identical regardless of provider style | G.711 µ-law/A-law round-trip previews generated and measured: no clipping, ≤0.4 dB RMS change | Run `docs/NOISE_ROOT_CAUSE.md`'s stage-by-stage procedure on the client machine: raw Bhashini capture → our processing → G.711 preview → real-call recordings (AI-11, AI-12) | CLIENT UAT REQUIRED |
| 6. Call Centre / Admin GUI not UAT tested | Only unit-tested | No structural change this pass; a full live checklist now exists | `test_call_centre.py`, `test_route_agi.py`, `test_admin_panel.py` | A live routing call dialled three destinations in the configured order then fell back to the AI; the panel was served and its health view read real Asterisk state | The whole of `docs/CLIENT_UAT_CHECKLIST.md` §1 and §2 — browser clicks and GSM calls | CLIENT UAT REQUIRED |
| 7. Helpline behaviour (persona, guardrails, escalation) | The system prompt was a generic business assistant: no helpline role, no guardrails, no escalation, no source attribution | `AI_PERSONA=lsd_helpline`: farmer vocabulary, one idea per turn, never diagnoses, never names a medicine or dose, never discusses price/compensation/schemes, escalates to a government veterinary hospital, attributes preparations to Sampurna Nand Yadav and colleagues (NDDB) | `test_helpline_persona.py` (22) — guardrails present, prompt assembly order, persona switchable from the admin panel | — | AI-14, AI-15, AI-17 on real calls; review the Hindi wording the model actually produces | CLIENT UAT REQUIRED |
| 8. District diagnostic centres | A language model asked to recall district centres invents them | Centres are loaded from an operator JSON file and injected verbatim; naming anything else is forbidden; no file means the agent names none | `test_helpline_persona.py` — an unlisted district produces a refusal, a broken file degrades to naming nothing | — | **The client must supply the Haryana district → centre list**, then set `REFERRAL_DIRECTORY_PATH` | BLOCKED |
| 9. Behaviour vs the client reference PDF | The reference document ("Lumpy lsd helpline details.pdf") is **not in the workspace** | Persona built from the requirements as stated in the written brief | — | — | Add the PDF to the repository (or `docs/`), then a line-by-line comparison can be completed and signed off | BLOCKED |
| 10. Local LLM alternative (Sarvam-M) requested | Client asked whether a paid Gemini API could be replaced by a locally-run Sarvam-M (24B params) | `LLM_PROVIDER=sarvam_m` provider built (`app/providers/llm/sarvam_m_provider.py`), gated by a real hardware check (`app/services/hardware_check.py`, `scripts/check_sarvam_hardware.py`) that never lets it attempt to load on unsuitable hardware; identical RAG-context construction to Gemini (`prompt_context.py`); A/B harness (`scripts/llm_ab_benchmark.py`) built and run | `test_hardware_check.py` (9), `test_sarvam_m_provider.py` (16), `test_llm_provider_parity.py` (3) | **Hardware check run for real on the dev machine: UNSUPPORTED HARDWARE** (4 threads, 8 GB RAM, <1 GB free disk, integrated GPU — see `docs/LLM_AB_TEST.md`). A/B harness run with mock providers (proves the harness works) and with `gemini`/`sarvam_m` (both correctly report "provider unavailable": no key, no model path) | Run `scripts/check_sarvam_hardware.py` on the client's actual server. If SUPPORTED/MARGINAL, download a GGUF and run `scripts/llm_ab_benchmark.py` for real numbers before considering production use | CLIENT UAT REQUIRED |
| 11. Noise root-cause isolation | Client noise complaint persisted after the resampler fix; needed to rule the LLM in or out before chasing the wrong layer | `docs/NOISE_ROOT_CAUSE.md`: full stage table (LLM → text cleanup → Bhashini → resample → normalize → playback → Asterisk → Synway → GSM), each stage's evidence or explicit "CLIENT REQUIRED" | `test_llm_noise_independence.py` (3) | Alias re-measured fresh this session: −79.8 dB, matching the original fix exactly | The stage-by-stage capture procedure in `docs/NOISE_ROOT_CAUSE.md` — raw Bhashini vs. our processing vs. G.711 preview vs. real-call recordings | CLIENT UAT REQUIRED |

## Reference document

The client-provided PDF is **not present in this workspace** — a search of the
whole repository for `*.pdf` returns nothing. The helpline persona was
therefore built from the requirements as they were written out in the brief
(role, tone, turn length, guardrails, escalation, NDDB attribution, district
routing). **The comparison against the reference document is not complete and
is not claimed to be.** Add the PDF to the repository and it can be finished.

## Test run

```
pytest -q  →  408 passed, 1 skipped, 0 failed
```

The skip is the opt-in real-provider test, which needs live API keys. 34 of
those tests were added in this pass (hardware check, Sarvam-M provider,
LLM-provider RAG parity, barge-in across providers, LLM/audio-noise
independence).

## What the client has to do

1. Supply the district → diagnostic centre list and set `REFERRAL_DIRECTORY_PATH`.
2. Add the reference PDF to the repository.
3. Re-index the knowledge base (`scripts/reindex_knowledge.py`) and confirm
   `stale / unsearchable : 0`.
4. Run `scripts/check_sarvam_hardware.py` on the real server if a local LLM
   is still wanted — do not install `llama-cpp-python` or download a GGUF
   before it reports SUPPORTED or MARGINAL.
5. Work through `docs/NOISE_ROOT_CAUSE.md`'s capture procedure to isolate
   the remaining GSM-path noise.
6. Work through `docs/CLIENT_UAT_CHECKLIST.md` and send back the failed rows
   with their log lines or recordings.
