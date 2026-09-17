from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.admin_auth import require_admin
from app.models.knowledge import KnowledgeDocument
from app.providers.embeddings.base import EmbeddingProviderError
from app.providers.embeddings.factory import get_embedding_provider
from app.services.knowledge_ingestion import IngestionError, ingest_pdf
from app.services.knowledge_reindex import (
    ReindexError,
    index_status,
    reindex_all,
    reindex_document,
)
from app.services.knowledge_search import SearchError, search_chunks
from app.services.pdf_extraction import PyPDFTextExtractor

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], dependencies=[Depends(require_admin)])
# Separate router, included ahead of `router` in main.py: the GET
# "/api/knowledge/{document_id}" route is declared first and would otherwise
# match "/api/knowledge/index/status" and reject it as a bad UUID.
index_router = APIRouter(
    prefix="/api/knowledge/index", tags=["knowledge"], dependencies=[Depends(require_admin)]
)
logger = get_logger(__name__)

_extractor = PyPDFTextExtractor()


class DocumentOut(BaseModel):
    id: UUID
    filename: str
    status: str
    page_count: int | None
    chunk_count: int | None = None
    error: str | None = None

    @classmethod
    def from_model(cls, doc: KnowledgeDocument) -> "DocumentOut":
        meta = doc.metadata_json or {}
        return cls(
            id=doc.id,
            filename=doc.filename,
            status=doc.status,
            page_count=doc.page_count,
            chunk_count=meta.get("chunk_count"),
            error=meta.get("error"),
        )


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None
    similarity_threshold: float | None = None


class SearchResultOut(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_filename: str
    page_number: int | None
    chunk_index: int
    chunk_text: str
    similarity: float


@router.post("/upload", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DocumentOut:
    if file.content_type not in ("application/pdf", "application/octet-stream", None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Only PDF files are accepted")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    try:
        embedding_provider = get_embedding_provider(settings)
    except EmbeddingProviderError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        document = await ingest_pdf(
            db,
            file_bytes=file_bytes,
            original_filename=file.filename or "upload.pdf",
            storage_dir=Path(settings.knowledge_storage_path),
            extractor=_extractor,
            embedding_provider=embedding_provider,
            chunk_size=settings.rag_chunk_size,
            chunk_overlap=settings.rag_chunk_overlap,
        )
    except IngestionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return DocumentOut.from_model(document)


@router.get("", response_model=list[DocumentOut])
async def list_documents(db: AsyncSession = Depends(get_db)) -> list[DocumentOut]:
    result = await db.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc()))
    return [DocumentOut.from_model(doc) for doc in result.scalars().all()]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: UUID, db: AsyncSession = Depends(get_db)) -> DocumentOut:
    document = await db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentOut.from_model(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    document = await db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")

    storage_path = Path(document.storage_path)
    await db.delete(document)  # cascades to knowledge_chunks
    await db.commit()

    try:
        storage_path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 - best-effort file cleanup
        logger.warning("Could not remove stored file %s: %s", storage_path, exc)


@router.post("/search", response_model=list[SearchResultOut])
async def search(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[SearchResultOut]:
    try:
        embedding_provider = get_embedding_provider(settings)
    except EmbeddingProviderError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    query_embedding = await embedding_provider.embed_one(body.query)

    try:
        results = await search_chunks(
            db,
            query_embedding=query_embedding,
            query_text=body.query,
            top_k=body.top_k or settings.rag_top_k,
            similarity_threshold=body.similarity_threshold
            if body.similarity_threshold is not None
            else settings.rag_similarity_threshold,
            embedding_space=embedding_provider.embedding_space,
        )
    except SearchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return [
        SearchResultOut(
            chunk_id=r.chunk_id,
            document_id=r.document_id,
            document_filename=r.document_filename,
            page_number=r.page_number,
            chunk_index=r.chunk_index,
            chunk_text=r.chunk_text,
            similarity=r.similarity,
        )
        for r in results
    ]


class IndexDocumentOut(BaseModel):
    document_id: str
    filename: str
    status: str
    chunk_count: int
    embedded_chunk_count: int
    embedding_space: str | None
    stale: bool
    reason: str
    last_indexed_at: str | None


class IndexStatusOut(BaseModel):
    current_embedding_space: str
    documents: int
    stale_documents: int
    searchable_documents: int
    healthy: bool
    items: list[IndexDocumentOut]


class ReindexRequest(BaseModel):
    document_id: UUID | None = None
    only_stale: bool = False


class ReindexOut(BaseModel):
    embedding_space: str
    reindexed: list[str]
    chunk_count: int
    failures: dict[str, str]
    ok: bool


def _embedding_provider_or_503(settings: Settings):
    try:
        return get_embedding_provider(settings)
    except EmbeddingProviderError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@index_router.get("/status", response_model=IndexStatusOut)
async def get_index_status(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IndexStatusOut:
    """Which documents the AI can actually search, and why the rest cannot."""
    provider = _embedding_provider_or_503(settings)
    report = await index_status(db, embedding_provider=provider)
    return IndexStatusOut(
        **report.summary(),
        items=[
            IndexDocumentOut(
                document_id=d.document_id,
                filename=d.filename,
                status=d.status,
                chunk_count=d.chunk_count,
                embedded_chunk_count=d.embedded_chunk_count,
                embedding_space=d.embedding_space,
                stale=d.stale,
                reason=d.reason,
                last_indexed_at=d.last_indexed_at.isoformat() if d.last_indexed_at else None,
            )
            for d in report.documents
        ],
    )


@index_router.post("/reindex", response_model=ReindexOut)
async def reindex(
    body: ReindexRequest | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReindexOut:
    """Re-embed documents with the embedding provider queries currently use."""
    body = body or ReindexRequest()
    provider = _embedding_provider_or_503(settings)

    if body.document_id is not None:
        document = await db.get(KnowledgeDocument, body.document_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
        try:
            chunks = await reindex_document(
                db,
                document,
                embedding_provider=provider,
                extractor=_extractor,
                chunk_size=settings.rag_chunk_size,
                chunk_overlap=settings.rag_chunk_overlap,
            )
        except ReindexError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return ReindexOut(
            embedding_space=provider.embedding_space,
            reindexed=[document.filename],
            chunk_count=chunks,
            failures={},
            ok=True,
        )

    report = await reindex_all(
        db,
        embedding_provider=provider,
        extractor=_extractor,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        only_stale=body.only_stale,
    )
    return ReindexOut(
        embedding_space=report.embedding_space,
        reindexed=report.reindexed,
        chunk_count=report.chunk_count,
        failures=report.failures,
        ok=report.ok,
    )
