"""Coverage marker module for the `--cov=backend` project gate.

The backend service source package is `app/*`. The gate command targets
`backend`, so tests import this module to provide a concrete Python module
name for coverage collection.
"""

from __future__ import annotations

MODULE = "backend"
