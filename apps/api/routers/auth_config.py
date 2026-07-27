"""Public authentication bootstrap metadata; never returns tokens or identities."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from packages.common.config import Settings

from apps.api.deps import settings_dep

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/config")
def auth_config(settings: Settings = Depends(settings_dep)) -> dict[str, str]:
    """Tell the browser whether an interactive Bearer token is required."""
    return {"mode": settings.auth_mode}
