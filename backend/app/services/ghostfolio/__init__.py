"""Ghostfolio integration — the display layer (spec §7).

Ghostfolio owns display, history and cost basis. It does **not** own target
allocation, drift or rebalancing (§7.2, §8), and it never sees ``TRANSFER``
records (§6.3).
"""

from app.services.ghostfolio.client import (
    GhostfolioAuthError,
    GhostfolioClient,
    GhostfolioError,
)

__all__ = ["GhostfolioAuthError", "GhostfolioClient", "GhostfolioError"]
