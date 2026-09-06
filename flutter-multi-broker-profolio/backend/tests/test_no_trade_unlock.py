"""Guard: the backend must never be able to unlock Futu trading (§4.3 rule 2).

This is the CI check the spec asks for. It is a *structural* guarantee,
not a policy someone has to remember: if `unlock_trade` is never called,
the OpenD session cannot place, modify or cancel an order even if this
host is fully compromised.

Two things are banned:

1. Any call to `unlock_trade` from application code.
2. Any hardcoded trade-password-shaped literal anywhere in the repo.

Why this is enforceable at all: reads do not require unlock. Verified
three ways on 2026-09-06 — Futu's docs scope unlock to "Place Order or
Modify or Cancel Orders"; the SDK's query methods contain no unlock
gate; and empirically, against real OpenD 10.6.6608 with a session that
never unlocked, accinfo_query / position_list_query /
history_deal_list_query all returned data.

The repo previously asserted the opposite in
BROKER_INTEGRATION_DETAILS §C.5, and that one wrong claim is the entire
reason a trade password ever existed in settings and .env.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_REPO = _BACKEND.parent

#: Call sites, not the mere word — the string appears legitimately in
#: docstrings explaining why we do not call it, and in test tripwires.
_UNLOCK_CALL = re.compile(r"\.unlock_trade\s*\(|\bawait\s+unlock_trade\s*\(")

#: Settings/env keys that would reintroduce a stored trade password.
_PASSWORD_SETTING = re.compile(
    r"futu_trade_unlock_password|MBP_FUTU_TRADE_UNLOCK_PASSWORD",
    re.IGNORECASE,
)


def _app_sources() -> list[Path]:
    return sorted(p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts)


class TestApplicationCodeNeverUnlocks:
    def test_no_unlock_trade_call_in_app_code(self) -> None:
        offenders: list[str] = []
        for path in _app_sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if _UNLOCK_CALL.search(line):
                    offenders.append(f"{path.relative_to(_BACKEND)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "application code calls unlock_trade — this removes the structural "
            "guarantee that a compromised host cannot trade (§4.3 rule 2). "
            "Reads do not need it; see this module's docstring.\n"
            + "\n".join(offenders)
        )

    def test_futu_adapter_exposes_no_unlock_method(self) -> None:
        from app.adapters.futu.adapter import FutuAdapter

        assert not [a for a in dir(FutuAdapter) if "unlock" in a.lower()]

    def test_futu_client_exposes_no_unlock_method(self) -> None:
        from app.adapters.futu.client import FutuOpenDClient

        assert not [a for a in dir(FutuOpenDClient) if "unlock" in a.lower()]

    def test_settings_has_no_trade_password_field(self) -> None:
        from app.core.settings import Settings

        fields = Settings.model_fields
        assert not [f for f in fields if "unlock" in f or "trade_password" in f]

    def test_adapter_takes_no_password_argument(self) -> None:
        import inspect

        from app.adapters.futu.adapter import FutuAdapter

        params = inspect.signature(FutuAdapter.__init__).parameters
        assert not [p for p in params if "password" in p or "unlock" in p]


class TestNoStoredTradePassword:
    def test_no_trade_password_setting_in_app_code(self) -> None:
        offenders = [
            str(p.relative_to(_BACKEND))
            for p in _app_sources()
            if _PASSWORD_SETTING.search(p.read_text(encoding="utf-8", errors="replace"))
            and "no Futu trade-unlock password" not in p.read_text(encoding="utf-8", errors="replace")
        ]
        assert not offenders, (
            "a Futu trade-password setting has come back: " + ", ".join(offenders)
        )

    @pytest.mark.parametrize("example", [".env.example", "backend/.env.example"])
    def test_env_examples_do_not_ship_a_trade_password(self, example: str) -> None:
        path = _REPO / example
        if not path.exists():
            pytest.skip(f"{example} not present")
        body = path.read_text(encoding="utf-8", errors="replace")
        offending = [
            line
            for line in body.splitlines()
            if _PASSWORD_SETTING.search(line) and not line.lstrip().startswith("#")
        ]
        assert not offending, (
            f"{example} still defines a trade-unlock password: {offending}"
        )
