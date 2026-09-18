"""Admin panel API: login, system health, runtime AI settings, audit trail,
and the panel page itself.

Departments, agents, call history and knowledge documents are served by the
existing call-centre and knowledge APIs; the panel calls those directly.
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import SESSION_COOKIE, issue_session, password_matches, require_admin
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.models.system import AuditLog
from app.services.system_config import EDITABLE_KEYS, ConfigError, effective_settings, set_values
from app.services.system_health import collect

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = get_logger(__name__)

PANEL_FILE = Path(__file__).resolve().parents[1] / "static" / "admin.html"


class LoginRequest(BaseModel):
    password: str


class SettingsUpdate(BaseModel):
    values: dict[str, str | int | float]


@router.post("/login")
async def login(
    body: LoginRequest, response: Response, settings: Settings = Depends(get_settings)
) -> dict:
    if not settings.admin_password:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password login is disabled — ADMIN_PASSWORD is not set",
        )
    if not password_matches(body.password, settings):
        logger.warning("Failed admin login attempt")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Incorrect password")

    response.set_cookie(
        SESSION_COOKIE,
        issue_session(settings),
        httponly=True,
        samesite="strict",
        secure=settings.app_url.startswith("https"),
        max_age=12 * 3600,
    )
    logger.info("Admin logged in")
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/session")
async def session_state(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Whether the browser is already signed in, and whether a password is
    even configured (so the panel can explain itself)."""
    from app.core.admin_auth import session_is_valid

    return {
        "authenticated": session_is_valid(request.cookies.get(SESSION_COOKIE), settings)
        or (settings.app_env.lower() in ("development", "test") and not (settings.admin_password or settings.internal_api_key)),
        "password_login_enabled": bool(settings.admin_password),
    }


@router.get("/health")
async def system_health(
    _: str = Depends(require_admin), settings: Settings = Depends(get_settings)
) -> dict:
    return await collect(settings)


@router.get("/settings")
async def read_settings(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Effective values (environment defaults with stored overrides applied)
    plus each field's constraints, so the panel can validate before saving.
    Only non-secret, editable settings are ever returned."""
    effective = await effective_settings(db, settings)
    return {
        "values": {key: getattr(effective, key) for key in EDITABLE_KEYS},
        "fields": [
            {
                "key": spec.key,
                "label": spec.label,
                "type": spec.kind.__name__,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "choices": list(spec.choices) if spec.choices else None,
            }
            for spec in EDITABLE_KEYS.values()
        ],
        "providers": {
            "stt": settings.stt_provider,
            "llm": settings.llm_provider,
            "tts": settings.tts_provider,
            "embeddings": settings.rag_embedding_provider,
            "llm_model": settings.gemini_model if settings.llm_provider == "gemini" else settings.llm_model,
        },
    }


@router.put("/settings")
async def update_settings(
    body: SettingsUpdate,
    actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        applied = await set_values(db, body.values, actor=actor)
    except ConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"applied": {k: str(v) for k, v in applied.items()}}


@router.get("/audit")
async def audit_trail(
    limit: int = Query(default=50, ge=1, le=500),
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (
        (await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))).scalars().all()
    )
    return [
        {
            "at": row.created_at,
            "actor": row.actor,
            "action": row.action,
            "entity": row.entity,
            "entity_id": row.entity_id,
            "detail": row.detail,
        }
        for row in rows
    ]


@router.get("/routing-overview")
async def routing_overview(
    _: str = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """What each IVR digit does right now, including the reserved ones."""
    from app.models.routing import Department
    from app.services.call_routing import build_plan
    from sqlalchemy.orm import selectinload

    departments = (
        (await db.execute(select(Department).options(selectinload(Department.agents)).order_by(Department.dtmf_digit)))
        .scalars()
        .all()
    )
    mapping = {
        d.dtmf_digit: {
            "kind": "department",
            "name": d.name,
            "enabled": d.enabled,
            "no_answer_action": d.no_answer_action,
            "destinations": [
                {"number": t.number, "label": t.label, "kind": t.kind} for t in build_plan(d).targets
            ],
        }
        for d in departments
    }
    mapping.setdefault("9", {"kind": "reserved", "name": "AI voice agent", "enabled": True, "destinations": []})
    mapping.setdefault("0", {"kind": "reserved", "name": "Repeat menu", "enabled": True, "destinations": []})
    return {"digits": dict(sorted(mapping.items()))}


@router.get("/panel", include_in_schema=False)
async def panel() -> FileResponse:
    if not PANEL_FILE.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Admin panel asset is missing")
    return FileResponse(PANEL_FILE, media_type="text/html")
