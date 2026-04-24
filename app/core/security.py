"""
Security utilities: API key authentication for service-to-service calls.

Design decision: API keys (not JWT) because:
- This is a backend service, not a user-facing app
- API keys are simpler to rotate (just update env var, no token invalidation)
- Keys are hashed at startup — raw values never stored in memory post-init
- Rate limiting is handled at the Redis layer, not here
"""

from __future__ import annotations

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str | None = Security(API_KEY_HEADER)) -> str:
    """
    FastAPI dependency. Inject into any route that requires authentication.

    Usage:
        @router.get("/protected", dependencies=[Depends(verify_api_key)])

    Returns the raw key on success (for logging/audit without storing).
    Raises 401 on missing key, 403 on invalid key.
    """
    settings = get_settings()

    if api_key is None:
        log.warning("api_key_missing", path="unknown")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide via X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not settings.verify_api_key(api_key):
        # Log partial key for debugging without exposing full key
        masked = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
        log.warning("api_key_invalid", masked_key=masked)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return api_key
