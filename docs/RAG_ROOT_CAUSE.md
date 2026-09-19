# Retrieval correctness and migration

Status: **PARTIALLY FIXED**; client semantic relevance is not yet established.
Update 2026-09-19 (below): retrieval no longer depends on the embedding being
meaningful, verified on a synthetic Hindi knowledge base — **not** on the
client's real 123-chunk document, which has not been seen.

## 2026-09-19 — a full-corpus lexical channel

### Root cause (proven from the code, not from the client's data)

`search_chunks` ranked by vector similarity. Its "lexical fallback" was a SQL
`LIKE` over the chunks containing *any* query word, **ordered by that same vector
distance and cut to 50 rows**. Two consequences:

1. With placeholder (`mock`) or weak embeddings the ordering carries no meaning,
   so on a ~123-chunk knowledge base a given chunk sits inside the 50-row window
   only about 40% of the time (50 of 123) — an answering chunk is more often
   missing than present. (The client ran `mock:1536`; the earlier pass now
   rejects it in production, but a *weak* real embedding degrades the same way
   for Hindi questions against Hindi text.)
2. What counted as a "term" was wrong for Hindi: `क्या`, `है`, `के` were content
   words (so nearly every chunk matched), and follow-up detection counted them too
   (so "इसका इलाज क्या है?" was not seen as a follow-up).

Then, because the helpline persona answers only from context, every miss became
the fixed sentence "information unavailable, consult a veterinarian": the caller
hears a refusal for a question the document answers. Two smaller causes: the
context cap (2,000 characters) cut the third and fourth retrieved 800-character
chunks off mid-sentence, and a caller's spelling (लंपी / लम्पि / lampi), an ASR
misspelling, or a synonym (गांठदार त्वचा रोग) matched nothing.

### What changed

`app/services/lexical_retrieval.py` — an in-memory BM25 index over **every** ready
chunk, independent of how or whether it was embedded, so a bad vector cannot hide
the knowledge base. `search_chunks` merges its hits with the vector channel:

| Piece | Behaviour |
|---|---|
| Canonical spelling | nukta and chandrabindu folded; nasal conjunct = anusvara (लम्पी = लंपी); ी/ू folded to ि/ु (लम्पी = लम्पि); Devanagari digits read as digits; zero-width joiners removed — on both the question and the passages |
| Function words | Hindi, Romanised Hindi and English function words dropped (क्या, है, के, kya, hai, the …) |
| Inflection | light suffix stripping (फैलता / फैलती / फैलना meet); Latin words untouched; never below 3 characters |
| Synonyms | built-in groups (LSD / symptoms / spread / prevention / vaccine / treatment / milk / meat / fever / lumps / flies / disease / skin / cow / buffalo / animal / vet), plus an optional editable JSON file (`RAG_SYNONYMS_PATH`, example in `config/retrieval_synonyms.example.json`) — names only, never answers |
| Misspellings | a question word absent from the corpus is matched to its nearest spelling by character-trigram similarity |
| "What should I do" | advice phrasing ("क्या करूं", "kya karein") adds the intent words उपचार/चिकित्सक/अलग/सलाह/सूचना to *rank* a chunk (they are not required for it to qualify) |
| Coverage gate | a chunk is returned only if it covers ≥ 34% of the question (IDF-weighted), so an unanswerable question still returns **nothing** and the caller is told honestly |
| Follow-ups | a question of ≤ 2 content words that names no subject carries the **subject** (in the caller's own spelling) of the last question that had one, across acknowledgements ("जी हाँ") in between — the earlier question's *angle* is deliberately not carried, or "and prevention?" would be dragged to the symptoms passage |
| Cache | one index per process, rebuilt when the chunk count or any `updated_at` changes |
| Context size | `AI_MAX_CONTEXT_CHARS` 2000 → 3600 so four whole chunks fit |

### Evidence

`tests/kb_fixtures.py` is a 12-passage Hindi knowledge base written for testing
(LSD introduction, symptoms, spread, prevention, vaccination, milk and meat, what
to do, a traditional-preparation passage, and four look-alike passages on mastitis,
foot-and-mouth disease, feeding and deworming — chosen because they share words
such as दूध, टीका and रोग with the LSD passages). It is **not** the client's
document and claims no authority.

| Check | Result |
|---|---|
| 22 farmer questions (Hindi, Hinglish, Romanised, one word, misspelt, synonym, follow-ups with a pronoun only, and a topic carried across an acknowledgement) — the answering passage is among the top 4 | **22 / 22** |
| …and ranked first | 6 / 22 — ranking *within* the relevant passages is the embedding model's job; recall is the contract here |
| 4 questions the knowledge base does not answer (weather, electricity bill, cricket score, wheat sowing) — chunks returned | **0 / 4** |

The same 22 cases also run against PostgreSQL through `search_chunks` and the whole
conversation path (`tests/test_lsd_hindi_retrieval.py`), and the module has its own
unit tests (`tests/test_lexical_retrieval.py`).

One limit found while testing: "मेरी गाय को गांठें हो गई हैं क्या करूं" retrieves the
*symptoms* passage, not the "keep it apart, call the veterinary hospital" passage.
Connecting a description to the right advice needs semantic understanding; the
persona's own escalation rule covers a suspected case. Also, a one-vowel-sign
misspelling of a three-letter stem (फैल / फेल) is not matched: folding ऐ/ए would
merge बैल (bullock) with बेल (bael leaf), which this knowledge base may contain.

### Not proven — requires the client's data

- **The client's real 123 chunks.** Re-index, then run `scripts/rag_uat_probe.py` and
  have the knowledge owner label the top hits. Numbers above are for a
  synthetic knowledge base of 12 passages: recall on 123 chunks, where dozens
  mention the disease, will be lower, and top-4 may need `RAG_TOP_K` 5–6.
- Real-embedding behaviour (the probe rejects `mock`; the lexical channel needs
  neither).
- Whether the built-in synonym groups cover how the client's callers actually
  talk. Add groups to the JSON file as the call log shows misses; no code change.


Mock embeddings are deterministic hashes, not meaning-aware vectors. They can
exercise database plumbing and lexical aliases but cannot validate multilingual
semantic retrieval. Production mode now rejects them. Unknown/mismatched index
provenance is excluded and visible as stale; equal vector dimensions do not
make two embedding spaces compatible.

## Practical provider decision

Preferred **candidate**, subject to client benchmark: FastEmbed 0.7.3 CPU ONNX
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, supplied as the
Qdrant quantized artifact `qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`.
Its supported-model listing specifies 384 dimensions and approximately 0.22 GB
model size; this is not its full runtime memory footprint.
[FastEmbed model registry](https://raw.githubusercontent.com/qdrant/fastembed/main/fastembed/text/pooled_embedding.py),
[model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2).

Implementation uses two CPU threads by default, batch size eight, one cached
model per process, serial inference and no runtime download. At first load it
requires 1.5 GB available memory and checks the configured ONNX SHA256. The API
and worker are separate processes: budget for **two model copies**, PostgreSQL,
Asterisk and WSL overhead. Do not enable it with insufficient WSL memory.
Install/benchmark in staging first; it was not loaded on the low-memory laptop.

384 values are normalized and padded with 1152 zeros to preserve the existing
vector(1536) schema and rollback. Padding preserves dot products/norms/cosine;
it does not manufacture semantic information or compatibility. A new space ID
includes model, artifact hash and padding version. Full reindex is mandatory.
No schema migration or automatic provider switch was performed.

Fallback if client RAM/latency is unsuitable: existing OpenAI embedding integration,
`RAG_EMBEDDING_PROVIDER=openai`, `RAG_EMBEDDING_MODEL=text-embedding-3-small`,
`RAG_EMBEDDING_DIMENSIONS=1536`, and a client-approved `RAG_EMBEDDING_API_KEY`.
This is an external API/cost/privacy requirement, not a free local solution.
No API account was provisioned and no client document was transmitted here.

## Index and conversation fixes

- Strip PostgreSQL NUL extraction artifacts without damaging Hindi marks.
- Validate full embedding count/dimensions/finite values before inserts/replacements.
- Exclude unknown space metadata instead of silently ranking incompatible vectors.
- Mark stale documents visibly; reindex validates all vectors before mutating any.
- Carry explicit topics across multiple short follow-up/acknowledgement turns.
- Without supporting chunks, real-provider LSD mode returns a deterministic referral fallback instead of asking the model to supply facts from memory.

Lexical alias matching is still a fallback. Existing mock tests primarily prove
document recall, not that the correct symptom/prevention/treatment paragraph
ranks first. Subject-change ambiguity, transliteration and one-word queries
must be assessed with real embeddings and the **client's actual 123 chunks**.
No claim that all such cases are solved is made.

## Verification

`scripts/rag_uat_probe.py` rejects mock providers, executes the seven mandatory
questions plus an acknowledgement follow-up against the deployed database,
and writes embedding time, retrieval time, chunk IDs/text/scores and index
health. Have the KB owner label the top hits; absence of a hit is not a pass.
The output contains private KB excerpts: keep it restricted.

Exact full reindex: `.venv/bin/python scripts/reindex_knowledge.py` (no flag means all documents).
Inspect `--help` and take the backup described in [client checklist](CLIENT_UAT_CHECKLIST.md)
first. Run in maintenance mode: simultaneous edits/reindex jobs are not supported.
Do not accept stale documents or “mock:1536” as production readiness.
