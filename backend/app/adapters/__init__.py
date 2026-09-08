"""Source adapters package.

Each subpackage holds one concrete `SourceAdapter` implementation plus a
thin SDK-wrapper Protocol so tests can inject fake clients without pulling
the real broker SDK as a runtime dependency.
"""

from app.adapters._common import (
    HealthTracker,
    PermanentError,
    RetryPolicy,
    SessionCache,
    TransientError,
    retry_async,
)
from app.adapters.base import SourceAdapter

__all__ = [
    "HealthTracker",
    "PermanentError",
    "RetryPolicy",
    "SessionCache",
    "SourceAdapter",
    "TransientError",
    "retry_async",
]
