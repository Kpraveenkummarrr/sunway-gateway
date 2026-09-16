"""Admin authentication.

Two ways in, both accepted by `require_admin`:
  * a signed session cookie, issued by POST /api/admin/login against
    ADMIN_PASSWORD — this is what the browser panel uses;
  * the X-Internal-Api-Key header — what the dialplan AGI and scripts use.

The cookie carries only an expiry and is signed with APP_SECRET_KEY; it is
never a credential itself. Passwords and keys are compared in constant time
and are never logged.
"""

import base64
import hmac
import json
import time
from hashlib import sha256

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SESSION_COOKIE = "sunway_admin"
SESSION_TTL_SECONDS = 12 * 3600


def _sign(payload: bytes, secret: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), payload, sha256).digest()).decode().rstrip("=")


def issue_session(settings: Settings) -> str:
    payload = json.dumps({"exp": int(time.time()) + SESSION_TTL_SECONDS}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{encoded}.{_sign(payload, settings.session_signing_key)}"


def session_is_valid(token: str | None, settings: Settings) -> bool:
    if not token or "." not in token:
        return False
    encoded, _, signature = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        expiry = json.loads(payload).get("exp", 0)
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(signature, _sign(payload, settings.session_signing_key)):
        return False
    return time.time() < expiry


def password_matches(candidate: str, settings: Settings) -> bool:
    return bool(settings.admin_password) and hmac.compare_digest(candidate, settings.admin_password)


async def require_admin(
    request: Request,
    x_internal_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Returns the actor name for audit entries."""
    if settings.internal_api_key and x_internal_api_key and hmac.compare_digest(
        x_internal_api_key, settings.internal_api_key
    ):
        return "api-key"

    if session_is_valid(request.cookies.get(SESSION_COOKIE), settings):
        return "admin"

    if not settings.internal_api_key and not settings.admin_password:
        # Local dev default: nothing configured, so nothing to enforce.
        logger.warning("Admin endpoint is unauthenticated — set ADMIN_PASSWORD and INTERNAL_API_KEY")
        return "anonymous"

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")
