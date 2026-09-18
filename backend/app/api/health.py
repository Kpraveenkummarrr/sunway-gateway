from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health")
async def health() -> dict:
    """Liveness check — process is up. Does not touch the database."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    """Readiness check — verifies the database is reachable and the
    pgvector extension is installed.

    AI provider / Asterisk connectivity checks are added once those
    integrations exist (later phases).
    """
    checks: dict[str, dict] = {}
    overall_ok = True

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - readiness probe must not crash
        logger.error("readiness db check failed: %s", type(exc).__name__)
        checks["database"] = {"ok": False, "error": "Database unavailable"}
        overall_ok = False

    try:
        result = await db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
        has_pgvector = result.first() is not None
        checks["pgvector"] = {"ok": has_pgvector}
        overall_ok = overall_ok and has_pgvector
    except Exception as exc:  # noqa: BLE001
        logger.error("readiness pgvector check failed: %s", type(exc).__name__)
        checks["pgvector"] = {"ok": False, "error": "Extension check unavailable"}
        overall_ok = False

    response.status_code = 200 if overall_ok else 503
    return {"status": "ok" if overall_ok else "degraded", "checks": checks}
