from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


async def require_internal_api_key(
    x_internal_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Gates internal-only endpoints (knowledge management, etc.) behind a
    shared secret so they aren't wide open if this service ends up
    reachable beyond localhost.

    If INTERNAL_API_KEY isn't set (local dev default), this is a no-op —
    but logs a warning once per call so the gap isn't silent.
    """
    if not settings.internal_api_key:
        logger.warning("INTERNAL_API_KEY not set — internal endpoint is unauthenticated")
        return

    if x_internal_api_key != settings.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
