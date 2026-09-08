"""IBKR adapters.

Two paths exist, and they are not equivalent:

* `IbkrFlexAdapter` — Flex Web Service. Read-only token, no session, no
  2FA, no Gateway process. This is what §4.1 prefers and what scheduled
  syncs should use.
* `IbkrAdapter` — Client Portal / TWS Gateway. Needs a live GUI process,
  the account login password and a daily second factor. Kept for the
  interactive case; not suitable for unattended runs.
"""

from app.adapters.ibkr.adapter import (
    IbkrAdapter,
    IBKRClient,
    IbkrClient,
)
from app.adapters.ibkr.flex import (
    FlexAuthError,
    FlexConfig,
    FlexError,
    FlexStatement,
    FlexWebServiceClient,
    IbkrFlexAdapter,
)

__all__ = [
    "FlexAuthError",
    "FlexConfig",
    "FlexError",
    "FlexStatement",
    "FlexWebServiceClient",
    "IBKRClient",
    "IbkrAdapter",
    "IbkrClient",
    "IbkrFlexAdapter",
]
