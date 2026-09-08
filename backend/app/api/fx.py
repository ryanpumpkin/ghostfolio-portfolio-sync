"""FX endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models.domain import FxRate
from app.services.dependencies import get_fx_service
from app.services.fx import FxService

router = APIRouter(tags=["fx"])


@router.get("/fx", response_model=list[FxRate])
async def get_fx_rates(
    pairs: Annotated[str, Query(min_length=3)],
    fx_service: Annotated[FxService, Depends(get_fx_service)],
) -> list[FxRate]:
    parsed: list[tuple[str, str]] = []
    for token in pairs.split(","):
        if ":" not in token:
            raise HTTPException(status_code=422, detail=f"Invalid pair '{token}'")
        base, quote = token.split(":", 1)
        base_u = base.strip().upper()
        quote_u = quote.strip().upper()
        if len(base_u) != 3 or len(quote_u) != 3:
            raise HTTPException(status_code=422, detail=f"Invalid pair '{token}'")
        parsed.append((base_u, quote_u))

    rates = await fx_service.get_rates_for(parsed)
    return [rates[pair] for pair in sorted(rates)]
