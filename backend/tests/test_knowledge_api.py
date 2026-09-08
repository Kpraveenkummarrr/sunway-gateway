import tempfile
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.main import app
from app.models.knowledge import KnowledgeDocument
from tests.pdf_fixtures import make_simple_pdf


def _override_settings(tmp_dir: str):
    base = get_settings()
    overridden = base.model_copy(
        update={
            "rag_embedding_provider": "mock",
            "internal_api_key": "",  # unauthenticated in tests, same as local dev default
            "knowledge_storage_path": tmp_dir,
        }
    )

    def _get() -> Settings:
        return overridden

    return _get


@pytest.mark.asyncio
async def test_health_still_works() -> None:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ready_still_works() -> None:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] in ("ok", "degraded")


@pytest.mark.asyncio
async def test_upload_list_get_search_and_delete_document(db_session) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app.dependency_overrides[get_settings] = _override_settings(tmp)
        transport = ASGITransport(app=app)
        document_id = None
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                files = {"file": ("upload_test.pdf", make_simple_pdf("Uploaded via API test."), "application/pdf")}
                upload_response = await client.post("/api/knowledge/upload", files=files)
                assert upload_response.status_code == 201
                body = upload_response.json()
                assert body["status"] == "ready"
                assert body["filename"] == "upload_test.pdf"
                document_id = body["id"]

                list_response = await client.get("/api/knowledge")
                assert list_response.status_code == 200
                assert any(d["id"] == document_id for d in list_response.json())

                get_response = await client.get(f"/api/knowledge/{document_id}")
                assert get_response.status_code == 200
                assert get_response.json()["id"] == document_id

                search_response = await client.post(
                    "/api/knowledge/search", json={"query": "Uploaded via API test."}
                )
                assert search_response.status_code == 200
                results = search_response.json()
                assert any(r["document_id"] == document_id for r in results)

                delete_response = await client.delete(f"/api/knowledge/{document_id}")
                assert delete_response.status_code == 204

                get_after_delete = await client.get(f"/api/knowledge/{document_id}")
                assert get_after_delete.status_code == 404
        finally:
            app.dependency_overrides.pop(get_settings, None)
            if document_id is not None:
                doc = await db_session.get(KnowledgeDocument, document_id)
                if doc is not None:
                    await db_session.delete(doc)
                    await db_session.commit()


@pytest.mark.asyncio
async def test_upload_rejects_non_pdf_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app.dependency_overrides[get_settings] = _override_settings(tmp)
        try:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                files = {"file": ("not_a_pdf.pdf", b"plain text content", "application/pdf")}
                response = await client.post("/api/knowledge/upload", files=files)
                assert response.status_code == 400
        finally:
            app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_get_missing_document_returns_404() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app.dependency_overrides[get_settings] = _override_settings(tmp)
        try:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/knowledge/00000000-0000-0000-0000-000000000000")
            assert response.status_code == 404
        finally:
            app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_upload_requires_internal_api_key_when_configured() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = get_settings()
        overridden = base.model_copy(
            update={
                "rag_embedding_provider": "mock",
                "internal_api_key": "test-secret-key",
                "knowledge_storage_path": tmp,
            }
        )
        app.dependency_overrides[get_settings] = lambda: overridden
        try:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                files = {"file": ("secured.pdf", make_simple_pdf("secret"), "application/pdf")}
                no_key_response = await client.post("/api/knowledge/upload", files=files)
                assert no_key_response.status_code == 401

                with_key_response = await client.post(
                    "/api/knowledge/upload",
                    files=files,
                    headers={"X-Internal-Api-Key": "test-secret-key"},
                )
                assert with_key_response.status_code == 201
                document_id = with_key_response.json()["id"]

                cleanup = await client.delete(
                    f"/api/knowledge/{document_id}", headers={"X-Internal-Api-Key": "test-secret-key"}
                )
                assert cleanup.status_code == 204
        finally:
            app.dependency_overrides.pop(get_settings, None)
