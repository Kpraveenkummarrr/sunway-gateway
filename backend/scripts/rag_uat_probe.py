"""Read-only mandatory-query benchmark against the REAL deployed index.

Rejects mock embeddings. Records chunks and timings, never calls an LLM or
automatically claims that an unlabelled hit is relevant. Output contains KB
excerpts: keep it private and ask the KB owner to score relevance.
"""
import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import Settings
from app.core.db import AsyncSessionLocal, engine
from app.providers.embeddings.factory import get_embedding_provider
from app.services.knowledge_search import build_retrieval_query, search_chunks
from app.services.knowledge_reindex import index_status

QUERIES = [
    ("लम्पी स्किन डिजीज क्या है?", []),
    ("इसके लक्षण क्या हैं?", ["लम्पी स्किन डिजीज क्या है?"]),
    ("यह कैसे फैलता है?", ["लम्पी स्किन डिजीज क्या है?", "इसके लक्षण क्या हैं?"]),
    ("इससे बचाव कैसे करें?", ["लम्पी स्किन डिजीज क्या है?"]),
    ("iska ilaj kya hai?", ["lumpy skin disease"]),
    ("lumpy disease symptoms", []),
    ("gai ko lumpy ho gaya kya karna chahiye", []),
    ("haanji", ["lumpy skin disease", "iska ilaj kya hai?"]),
]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    if settings.rag_embedding_provider.strip().lower() == "mock":
        parser.error("Mock embeddings cannot establish semantic retrieval readiness")
    if args.out.exists():
        parser.error("Output already exists; choose a new evidence path")
    provider = get_embedding_provider(settings)
    report = {"embedding_space": provider.embedding_space, "evidence": "real configured provider; human relevance review required", "queries": []}
    try:
        async with AsyncSessionLocal() as db:
            report["index_status"] = asdict(await index_status(db, embedding_provider=provider))
            for text, previous in QUERIES:
                query = build_retrieval_query(text, previous)
                started = time.perf_counter()
                vector = await asyncio.wait_for(provider.embed_one(query), timeout=settings.provider_timeout_seconds)
                embedding_ms = round((time.perf_counter() - started) * 1000, 2)
                timings = {}
                hits = await asyncio.wait_for(search_chunks(db, query_embedding=vector, query_text=query,
                    top_k=settings.rag_top_k, similarity_threshold=settings.rag_similarity_threshold,
                    embedding_space=provider.embedding_space, timings=timings), timeout=settings.provider_timeout_seconds)
                report["queries"].append({"text": text, "retrieval_query": query, "embedding_ms": embedding_ms,
                    "timings": timings, "hits": [asdict(hit) for hit in hits], "human_relevance": "PENDING"})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2, default=str)
        print(f"Recorded {len(report['queries'])} queries; human relevance labels still required")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
