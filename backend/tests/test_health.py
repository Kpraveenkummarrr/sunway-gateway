import httpx
import pytest
from httpx import ASGITransport

from app.core.db import get_db
from app.main import app


@pytest.mark.asyncio
async def test_health_liveness() -> None:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class _FakeResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        return self._row


class _FakeSession:
    """Minimal stand-in for AsyncSession — no real DB needed for this test."""

    def __init__(self, pgvector_installed: bool = True) -> None:
        self._pgvector_installed = pgvector_installed

    async def execute(self, statement) -> _FakeResult:  # noqa: ANN001
        sql = str(statement)
        if "pg_extension" in sql:
            return _FakeResult(("vector",) if self._pgvector_installed else None)
        return _FakeResult((1,))


@pytest.mark.asyncio
async def test_ready_ok_when_db_and_pgvector_available() -> None:
    async def _fake_get_db():
        yield _FakeSession(pgvector_installed=True)

    app.dependency_overrides[get_db] = _fake_get_db
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["pgvector"]["ok"] is True


@pytest.mark.asyncio
async def test_ready_degraded_when_pgvector_missing() -> None:
    async def _fake_get_db():
        yield _FakeSession(pgvector_installed=False)

    app.dependency_overrides[get_db] = _fake_get_db
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["pgvector"]["ok"] is False


@pytest.mark.asyncio
async def test_ready_failure_does_not_expose_database_credentials() -> None:
    class UnavailableSession:
        async def execute(self, statement):
            raise RuntimeError("postgresql://user:private-password@host/db")

    async def unavailable_db():
        yield UnavailableSession()

    app.dependency_overrides[get_db] = unavailable_db
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["ok"] is False
    assert "private-password" not in response.text
