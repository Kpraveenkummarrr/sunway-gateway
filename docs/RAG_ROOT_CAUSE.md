# Retrieval correctness and migration

Status: **PARTIALLY FIXED**; client semantic relevance is not yet established.

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
