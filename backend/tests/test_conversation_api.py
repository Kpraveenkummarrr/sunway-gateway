import httpx
import pytest
from httpx import ASGITransport

from app.core.config import Settings, get_settings
from app.main import app
from app.models.ai import AISession


def _override_settings():
    base = get_settings()
    overridden = base.model_copy(
        update={
            "rag_embedding_provider": "mock",
            "llm_provider": "mock",
            "stt_provider": "mock",
            "tts_provider": "mock",
            "internal_api_key": "",
            "rag_similarity_threshold": -1.0,
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


@pytest.mark.asyncio
async def test_create_session_send_message_get_history_end_session(db_session) -> None:
    app.dependency_overrides[get_settings] = _override_settings()
    session_id = None
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            create_response = await client.post("/api/conversation/sessions", json={"language": "en"})
            assert create_response.status_code == 201
            body = create_response.json()
            session_id = body["id"]
            assert body["status"] == "active"

            message_response = await client.post(
                f"/api/conversation/sessions/{session_id}/messages",
                json={"text": "What are your business hours?"},
            )
            assert message_response.status_code == 200
            turn = message_response.json()
            assert turn["user_message"]["text"] == "What are your business hours?"
            assert turn["assistant_message"]["text"]
            assert "retrieved_chunks" in turn

            get_response = await client.get(f"/api/conversation/sessions/{session_id}")
            assert get_response.status_code == 200
            session_data = get_response.json()
            assert len(session_data["messages"]) == 2

            end_response = await client.post(f"/api/conversation/sessions/{session_id}/end")
            assert end_response.status_code == 200
            assert end_response.json()["status"] == "completed"

            # Sending another message on an ended session should fail cleanly.
            after_end_response = await client.post(
                f"/api/conversation/sessions/{session_id}/messages", json={"text": "hello again"}
            )
            assert after_end_response.status_code == 400
    finally:
        app.dependency_overrides.pop(get_settings, None)
        if session_id is not None:
            session = await db_session.get(AISession, session_id)
            if session is not None:
                await db_session.delete(session)
                await db_session.commit()


@pytest.mark.asyncio
async def test_send_message_to_missing_session_returns_404() -> None:
    app.dependency_overrides[get_settings] = _override_settings()
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/conversation/sessions/00000000-0000-0000-0000-000000000000/messages",
                json={"text": "hello"},
            )
            assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_send_audio_message_end_to_end(db_session) -> None:
    app.dependency_overrides[get_settings] = _override_settings()
    session_id = None
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            create_response = await client.post("/api/conversation/sessions", json={})
            session_id = create_response.json()["id"]

            files = {"file": ("audio.raw", b"what are your business hours?", "application/octet-stream")}
            audio_response = await client.post(f"/api/conversation/sessions/{session_id}/audio", files=files)
            assert audio_response.status_code == 200
            body = audio_response.json()
            assert body["transcribed_text"] == "what are your business hours?"
            assert body["assistant_audio_base64"]
            assert body["assistant_audio_format"]
    finally:
        app.dependency_overrides.pop(get_settings, None)
        if session_id is not None:
            session = await db_session.get(AISession, session_id)
            if session is not None:
                await db_session.delete(session)
                await db_session.commit()


@pytest.mark.asyncio
async def test_sessions_require_internal_api_key_when_configured(db_session) -> None:
    base = get_settings()
    overridden = base.model_copy(
        update={
            "rag_embedding_provider": "mock",
            "llm_provider": "mock",
            "internal_api_key": "test-secret-key",
        }
    )
    app.dependency_overrides[get_settings] = lambda: overridden
    session_id = None
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            no_key_response = await client.post("/api/conversation/sessions", json={})
            assert no_key_response.status_code == 401

            with_key_response = await client.post(
                "/api/conversation/sessions", json={}, headers={"X-Internal-Api-Key": "test-secret-key"}
            )
            assert with_key_response.status_code == 201
            session_id = with_key_response.json()["id"]
    finally:
        app.dependency_overrides.pop(get_settings, None)
        if session_id is not None:
            session = await db_session.get(AISession, session_id)
            if session is not None:
                await db_session.delete(session)
                await db_session.commit()
