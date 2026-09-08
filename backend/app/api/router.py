"""Top-level /v1 router. Feature routers are mounted by their owning modules."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.analyst import router as analyst_router
from app.api.connections import router as connections_router
from app.api.fx import router as fx_router
from app.api.portfolio import router as portfolio_router
from app.api.quotes import router as quotes_router
from app.api.watchlist import router as watchlist_router
from app.middleware.auth import AuthenticatedUser, current_user

api_router = APIRouter(prefix="/v1")


@api_router.get("/whoami", summary="Echo the authenticated user (smoke test)")
def whoami(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> dict[str, str | None]:
    """Return the authenticated user; used by integration tests and uptime probes."""
    return {"user_id": user.user_id, "email": user.email}


api_router.include_router(portfolio_router)
api_router.include_router(quotes_router)
api_router.include_router(fx_router)
api_router.include_router(connections_router)
api_router.include_router(analyst_router)
api_router.include_router(watchlist_router)
