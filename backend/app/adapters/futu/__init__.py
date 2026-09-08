"""Futu adapter.

Read-only by construction: nothing here can unlock trading (spec §4.3
rule 2). See `adapter.py`'s module docstring for the verification.
"""

from app.adapters.futu.adapter import FutuAdapter, FutuClient
from app.adapters.futu.client import FutuOpenDClient

__all__ = [
    "FutuAdapter",
    "FutuClient",
    "FutuOpenDClient",
]
