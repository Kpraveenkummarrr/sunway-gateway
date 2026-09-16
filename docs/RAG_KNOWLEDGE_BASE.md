# Knowledge base and RAG

## Runtime pipeline

```text
PDF -> text extraction -> Unicode normalization -> page-aware chunks
    -> configured embedding provider -> pgvector
    -> query embedding + lexical candidates -> ranked context
    -> Gemini grounding prompt -> spoken answer
```

Only documents with `status=ready` and non-null vectors are searchable.
Ingestion records the content hash, embedding space, dimensions, and chunk
count in `knowledge_documents.metadata_json`.

## Retrieval behavior

The primary ranking signal remains pgvector cosine similarity. A bounded
lexical pass is merged into the vector candidates so exact business terms and
speech-recognition variants can be recovered when semantic similarity is
weak. Lexical evidence boosts ordering but does not replace the vector score
returned by the API. A strong multi-word lexical match can pass the similarity
threshold because it is direct evidence from the source text.

Searches normalize Unicode compatibility forms, case, punctuation, and word
boundaries while preserving Hindi letters and combining marks. The current
alias set includes common forms of Lumpy Skin Disease and is intentionally
small; it is not an answer dictionary.

## Embedding compatibility

Ingestion and query both use the configured provider factory. OpenAI spaces
are identified by provider, model, and dimensions; the local mock space is
identified separately. A document with explicit metadata from another space
is excluded rather than silently mixed. Documents created before this
metadata existed remain available for backward compatibility, but should be
re-indexed after changing provider/model/dimensions.

The production knowledge base must never be ingested with the mock provider.
The mock provider exists only for deterministic local tests.

## Lumpy Skin Disease acceptance test

The regression suite covers these query forms against a fixture source that
contains the disease name and facts:

- English: `What is Lumpy Skin Disease?`
- Hindi: `लम्पी स्किन डिजीज क्या है?`
- Hinglish: `Lumpy skin disease ke symptoms kya hain?`
- ASR-style: `Lumpy skin disease ke lakshan batao`

The expected assertion is source-chunk retrieval, not a hardcoded answer.
The PostgreSQL-backed integration test requires pgvector and was not runnable
in the local Windows session because PostgreSQL was unavailable.
