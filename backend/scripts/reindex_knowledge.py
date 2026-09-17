"""Re-embed the knowledge base with the embedding provider in use.

The AI can only search a document whose stored vectors came from the same
embedding model the query uses. Change RAG_EMBEDDING_PROVIDER or
RAG_EMBEDDING_MODEL and every older document silently drops out of search —
which looks exactly like "the AI is not reading the knowledge base". This
prints what is searchable and fixes what is not.

    cd backend && .venv/bin/python scripts/reindex_knowledge.py --status
    cd backend && .venv/bin/python scripts/reindex_knowledge.py            # re-index everything
    cd backend && .venv/bin/python scripts/reindex_knowledge.py --stale    # only what search ignores
    cd backend && .venv/bin/python scripts/reindex_knowledge.py --document <uuid>

Exit code 0 means everything the AI needs is searchable; 1 means something
still is not, and the reason is printed next to it.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.db import AsyncSessionLocal, engine  # noqa: E402
from app.models.knowledge import KnowledgeDocument  # noqa: E402
from app.providers.embeddings.base import EmbeddingProviderError  # noqa: E402
from app.providers.embeddings.factory import get_embedding_provider  # noqa: E402
from app.services.knowledge_reindex import (  # noqa: E402
    ReindexError,
    index_status,
    reindex_all,
    reindex_document,
)
from app.services.pdf_extraction import PyPDFTextExtractor  # noqa: E402


async def _print_status(db, provider) -> bool:
    report = await index_status(db, embedding_provider=provider)
    summary = report.summary()
    print(f"embedding space in use : {summary['current_embedding_space']}")
    print(f"documents              : {summary['documents']}")
    print(f"searchable by the AI   : {summary['searchable_documents']}")
    print(f"stale / unsearchable   : {summary['stale_documents']}")
    if report.documents:
        print()
        print(f"{'document':<40} {'chunks':>7} {'status':>10}  reason")
        for d in report.documents:
            flag = "STALE" if d.stale else "ok"
            print(f"{d.filename[:40]:<40} {d.chunk_count:>7} {flag:>10}  {d.reason}")
    return report.is_healthy


async def main_async(args) -> int:
    settings = get_settings()
    try:
        provider = get_embedding_provider(settings)
    except EmbeddingProviderError as exc:
        print(f"Embedding provider unavailable: {exc}")
        print("Set RAG_EMBEDDING_PROVIDER (and its API key) in backend/.env first.")
        return 1

    extractor = PyPDFTextExtractor()
    async with AsyncSessionLocal() as db:
        if args.status:
            return 0 if await _print_status(db, provider) else 1

        if args.document:
            document = await db.get(KnowledgeDocument, args.document)
            if document is None:
                print(f"No document with id {args.document}")
                return 1
            try:
                chunks = await reindex_document(
                    db,
                    document,
                    embedding_provider=provider,
                    extractor=extractor,
                    chunk_size=settings.rag_chunk_size,
                    chunk_overlap=settings.rag_chunk_overlap,
                )
            except ReindexError as exc:
                print(f"FAILED {document.filename}: {exc}")
                return 1
            print(f"re-indexed {document.filename}: {chunks} chunks")
        else:
            report = await reindex_all(
                db,
                embedding_provider=provider,
                extractor=extractor,
                chunk_size=settings.rag_chunk_size,
                chunk_overlap=settings.rag_chunk_overlap,
                only_stale=args.stale,
            )
            print(f"embedding space : {report.embedding_space}")
            print(f"re-indexed      : {len(report.reindexed)} documents, {report.chunk_count} chunks")
            for name in report.reindexed:
                print(f"  ok      {name}")
            for name, error in report.failures.items():
                print(f"  FAILED  {name}: {error}")

        print()
        healthy = await _print_status(db, provider)
        return 0 if healthy else 1


async def _run(args) -> int:
    try:
        return await main_async(args)
    finally:
        # Without this the asyncpg/SSL connections are torn down by the
        # interpreter after the loop has closed, which prints an alarming
        # traceback over a run that actually succeeded.
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="report only, change nothing")
    parser.add_argument("--stale", action="store_true", help="re-index only unsearchable documents")
    parser.add_argument("--document", help="re-index a single document id")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
