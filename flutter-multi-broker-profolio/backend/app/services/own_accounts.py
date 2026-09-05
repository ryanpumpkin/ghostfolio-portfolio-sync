"""Registry of the owner's own addresses and accounts (spec §6.3, §4.5).

§6.3 requires this by name: *"Maintain a list of own addresses and own
account identifiers so transfers are recognised."* Without it, a
withdrawal from Binance to the owner's own Ledger is indistinguishable
from a withdrawal to a third party, and the safe default (never a SELL)
costs information the reconciliation in §6.4 needs.

What this module decides
------------------------
Only one thing, and it is the thing §6.3 calls the most important rule in
the document: **is the counterparty of this movement the owner?** If yes,
the movement is a custody change (``TRANSFER``); if no or unknown, it
stays a ``DEPOSIT``/``WITHDRAWAL``. Both are excluded from the Ghostfolio
push, so a wrong answer here never fabricates a trade — it only changes
how honestly the movement is labelled.

Security rules this module enforces rather than merely documenting (§4.5)
------------------------------------------------------------------------
* **An xpub is rejected outright.** An extended public key derives every
  address in an account and reveals the complete balance and transaction
  history. §4.5: *"Never store an xpub in this repo... it is a credential
  and belongs in the encrypted store."* Loading one raises.
* **Anything resembling a seed phrase is rejected.** §4.5: *"The seed
  phrase is never entered anywhere, ever."* A config file that contains
  twelve or more lowercase words in one value is refused on sight.
* Plain receive addresses are lower-risk than an xpub but still
  deanonymising, so the real config file is gitignored and only an
  example is committed. Keep the real one in the private config repo.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.models.domain import Transaction, TransactionType

_LOG = logging.getLogger("mbp.own_accounts")

# backend/app/services/own_accounts.py -> repo/config/
_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "own_accounts.yaml"

# Extended public keys across the common encodings. Checked case-sensitively
# on the raw value because these prefixes are fixed by the encoding.
_XPUB_PREFIXES = (
    "xpub", "ypub", "zpub", "tpub", "upub", "vpub",
    "Ltub", "Mtub", "dgub", "xprv", "yprv", "zprv",
)

# A seed phrase is 12/15/18/21/24 lowercase words. Anything with 12+
# space-separated lowercase alphabetic words is refused rather than
# guessed at.
_WORDLIKE = re.compile(r"^[a-z]+$")
_MIN_SEED_WORDS = 12


class OwnAccountsConfigError(RuntimeError):
    """The own-accounts config is missing, malformed, or contains a secret."""


def _looks_like_seed_phrase(value: str) -> bool:
    words = value.strip().split()
    if len(words) < _MIN_SEED_WORDS:
        return False
    return all(_WORDLIKE.match(word) for word in words)


def _looks_like_xpub(value: str) -> bool:
    stripped = value.strip()
    return any(stripped.startswith(prefix) for prefix in _XPUB_PREFIXES)


def _reject_forbidden_material(value: str, *, where: str) -> None:
    """Refuse credentials that §4.5 says must never be in this repo.

    Deliberately raises rather than warning. A warning in a log nobody
    reads is not a control, and both of these leak the entire history of
    an account.
    """
    if _looks_like_xpub(value):
        raise OwnAccountsConfigError(
            f"{where}: value looks like an extended public key (xpub/zpub/...). "
            "An xpub derives every address in the account and reveals its "
            "complete balance and transaction history. §4.5 forbids storing "
            "one here — it is a credential and belongs in the encrypted "
            "store. Use individual receive addresses instead."
        )
    if _looks_like_seed_phrase(value):
        raise OwnAccountsConfigError(
            f"{where}: value looks like a seed phrase. §4.5: the seed phrase "
            "is never entered anywhere, ever. Remove it from this file and "
            "treat the wallet as compromised if it was ever committed."
        )


@dataclass(slots=True)
class OwnAccountsRegistry:
    """Addresses and account identifiers belonging to the owner."""

    addresses: frozenset[str] = field(default_factory=frozenset)
    account_ids: frozenset[str] = field(default_factory=frozenset)
    labels: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _normalize(value: str) -> str:
        # Case-folded because EVM addresses appear in both checksummed and
        # lowercase form depending on the source, and bech32 is
        # case-insensitive by specification. Comparing raw strings would
        # silently miss matches and turn a transfer back into a withdrawal.
        return value.strip().casefold()

    def is_own(self, counterparty: str | None) -> bool:
        if not counterparty:
            return False
        candidate = self._normalize(counterparty)
        return candidate in self.addresses or candidate in self.account_ids

    def label_for(self, counterparty: str | None) -> str | None:
        if not counterparty:
            return None
        return self.labels.get(self._normalize(counterparty))

    @classmethod
    def empty(cls) -> OwnAccountsRegistry:
        """A registry that recognises nothing.

        The safe default: every movement stays a DEPOSIT/WITHDRAWAL, which
        is still excluded from the Ghostfolio push. Nothing is ever
        mislabelled as a trade for want of this file.
        """
        return cls()

    @classmethod
    def load(cls, path: str | Path | None = None) -> OwnAccountsRegistry:
        target = Path(path) if path else _DEFAULT_PATH
        if not target.exists():
            _LOG.info(
                "no own-accounts config at %s — transfers between your own "
                "accounts will stay labelled as deposits/withdrawals. They "
                "are still never pushed as trades (§6.3).",
                target,
            )
            return cls.empty()

        try:
            document = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise OwnAccountsConfigError(f"cannot read {target}: {exc}") from exc

        if document is None:
            return cls.empty()
        if not isinstance(document, dict):
            raise OwnAccountsConfigError(f"{target} did not parse to a mapping")

        addresses: set[str] = set()
        account_ids: set[str] = set()
        labels: dict[str, str] = {}

        for section, sink in (("addresses", addresses), ("account_ids", account_ids)):
            entries = document.get(section) or []
            if not isinstance(entries, list):
                raise OwnAccountsConfigError(
                    f"{target}: '{section}' must be a list"
                )
            for index, entry in enumerate(entries):
                where = f"{target}:{section}[{index}]"
                if isinstance(entry, str):
                    value, label = entry, None
                elif isinstance(entry, dict):
                    raw = entry.get("value") or entry.get("address") or entry.get("id")
                    if not isinstance(raw, str):
                        raise OwnAccountsConfigError(
                            f"{where}: entry needs a string 'value'"
                        )
                    value = raw
                    label_raw = entry.get("label")
                    label = label_raw if isinstance(label_raw, str) else None
                elif isinstance(entry, int):
                    # An EVM address is `0x` followed by 40 hex digits, so
                    # an unquoted one is valid YAML for an integer and
                    # PyYAML silently returns a number — the address is
                    # gone before we ever see it. Say exactly that,
                    # because "entry must be a string" sends people
                    # looking in the wrong place.
                    raise OwnAccountsConfigError(
                        f"{where}: got the number {entry!r}, not a string. An "
                        "address made only of hex digits (any 0x... EVM "
                        "address) parses as an integer unless it is quoted. "
                        'Wrap it in quotes: - "0xabc..."'
                    )
                else:
                    raise OwnAccountsConfigError(
                        f"{where}: entry must be a string or a mapping"
                    )

                _reject_forbidden_material(value, where=where)
                normalized = cls._normalize(value)
                if not normalized:
                    continue
                sink.add(normalized)
                if label:
                    labels[normalized] = label

        registry = cls(
            addresses=frozenset(addresses),
            account_ids=frozenset(account_ids),
            labels=labels,
        )
        _LOG.info(
            "own-accounts registry loaded: %d addresses, %d account ids",
            len(registry.addresses),
            len(registry.account_ids),
        )
        return registry


def resolve_transfers(
    transactions: list[Transaction], registry: OwnAccountsRegistry
) -> list[Transaction]:
    """Promote own-account movements to ``TRANSFER`` (§6.3).

    A ``DEPOSIT`` or ``WITHDRAWAL`` whose counterparty is the owner is a
    custody change. Everything else is left exactly as the adapter
    reported it.

    Note what this never does: it never turns anything into a BUY or a
    SELL. Both the input and output types are excluded from the Ghostfolio
    push, so the worst case of an incomplete registry is a movement
    labelled less precisely — never a fabricated trade, and never a
    destroyed cost basis.
    """
    resolved: list[Transaction] = []
    promoted = 0
    for transaction in transactions:
        if (
            transaction.type in (TransactionType.DEPOSIT, TransactionType.WITHDRAWAL)
            and registry.is_own(transaction.counterparty)
        ):
            resolved.append(transaction.model_copy(update={"type": TransactionType.TRANSFER}))
            promoted += 1
        else:
            resolved.append(transaction)
    if promoted:
        _LOG.info(
            "recognised %d movement(s) as transfers between your own accounts",
            promoted,
        )
    return resolved


__all__ = [
    "OwnAccountsConfigError",
    "OwnAccountsRegistry",
    "resolve_transfers",
]
