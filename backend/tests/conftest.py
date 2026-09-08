from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, engine


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_after_test() -> AsyncGenerator[None, None]:
    """pytest-asyncio gives each test function its own event loop, but the
    app's engine (and its asyncpg connection pool) is a module-level
    singleton created once. Pooled connections bound to a prior test's
    (now-closed) loop break on the next test — dispose the pool after
    every test so the next one starts with fresh connections."""
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """A real session against the live dev database (same one /ready
    checks). ingest_pdf() commits progressively by design (so a partial
    failure still leaves a visible document row), which rules out a
    rollback-only transaction wrapper here — tests are responsible for
    deleting whatever rows they create."""
    async with AsyncSessionLocal() as session:
        yield session
