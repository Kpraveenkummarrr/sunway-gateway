# Model/provider decision — no invented benchmark winner

| Component | Decision | Measured locally | Client gate |
|---|---|---|---|
| LLM | Retain configured Gemini, default model `gemini-2.5-flash`; not proven best | Executed existing A/B runner; Gemini unavailable (no key), 0/12 responses | Actual configured model, p50/p95 complete/first-useful output, grounded Hindi score and cost |
| Alternative LLM | Sarvam-M 24B local not practical on reported 8 GB CPU-only client | No model path/runtime artifact; no inference attempted | Do not install a 24B model on this machine |
| Existing API alternative | OpenAI-compatible supported provider remains an optional paid comparison | No approved credential/model run | Client account, privacy/cost approval, same context/phrase benchmark |
| ASR | Retain Bhashini Hindi conformer integration | Payload/format/error tests only | Human transcription vs clean/noisy/fast/Hinglish/interrupted speech |
| TTS | Retain Bhashini configured female service pending A/B | Native response unavailable; synthetic DSP is not TTS | Raw WAV and handset listening, generation time, authorized service alternatives |
| Embeddings | Opt-in quantized multilingual MiniLM candidate; API fallback if unsuitable | Structural/cosine-padding tests; no model loaded | Memory headroom, actual Hindi/Hinglish retrieval accuracy, reindex |
| DSP | Retain current filter/gain/tempo defaults until listening | -78.27 dB 6 kHz alias probe; no clipping on harmonic fixtures | Randomized 1.00/1.10/1.15x speech and GSM comparison |

The benchmark was actually executed: `scripts/llm_ab_benchmark.py --provider-a
gemini --provider-b sarvam_m --out data/final-uat-llm-20260917`. Both candidates
were unavailable, hence no latency, quality, hallucination or cost ranking.
Its retrieval used an empty local index with mock embeddings: not client RAG
evidence. The script's existing “grounded” count measures nonempty retrieval,
**not factual entailment**. Human source checking is mandatory.

Covered runner cases include greeting, symptoms, spread, prevention, vaccination,
milk safety, suspected case, follow-up, Hinglish, transliteration, short follow-up
and unclear input. The client must additionally test separate meat safety,
"iska ilaj kya hai?", and yes/no continuation. Neither existing LLM implementation
streams tokens: first available response and full completion coincide; do not
report this as a measured time-to-first-token improvement.

No new provider purchase, local model download, live synthesis or component
replacement was performed. Raw voice quality, GSM quality and operator
preferences cannot be inferred from model reputation or code tests.
