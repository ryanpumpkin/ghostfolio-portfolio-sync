"""Load the verified Ghostfolio symbol mappings (spec §7.1).

The mappings live in ``config/ghostfolio_symbols.yaml`` rather than in code
because they are *observations about a running instance*, not logic. They
are re-verified after a Ghostfolio upgrade; code is not.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_LOG = logging.getLogger("mbp.ghostfolio.config")

# backend/app/services/ghostfolio/config.py -> repo/config/
_DEFAULT_PATH = Path(__file__).resolve().parents[4] / "config" / "ghostfolio_symbols.yaml"


class SymbolConfigError(RuntimeError):
    """The symbol config is missing or malformed."""


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SymbolConfigError(
            f"cannot read Ghostfolio symbol config at {path}: {exc}. "
            "Crypto activities cannot be pushed without it (§7.1 forbids "
            "guessing the symbol)."
        ) from exc
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise SymbolConfigError(f"{path} did not parse to a mapping")
    return parsed


@lru_cache(maxsize=4)
def load_crypto_overrides(path: str | None = None) -> dict[str, str]:
    """Return ``{canonical_code: "symbol" | "DATASOURCE:symbol"}``.

    Cached, because it is read once per sync and never changes at runtime.
    Pass an explicit path in tests to bypass the cache key.
    """
    target = Path(path) if path else _DEFAULT_PATH
    document = _load(target)
    crypto = document.get("crypto") or {}
    if not isinstance(crypto, dict):
        raise SymbolConfigError(f"{target}: 'crypto' must be a mapping")

    overrides = {str(k).strip().upper(): str(v).strip() for k, v in crypto.items()}
    if not overrides:
        _LOG.warning(
            "no crypto symbol overrides in %s — every crypto activity will be "
            "skipped rather than guessed (§7.1)",
            target,
        )
    return overrides


__all__ = ["SymbolConfigError", "load_crypto_overrides"]
