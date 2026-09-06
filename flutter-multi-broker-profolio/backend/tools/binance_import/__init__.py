"""One-off Binance historical import (spec §5).

NOT a production adapter and NOT registered with the scheduler (§5.10
item 1). §5.0: the owner is not continuing to use Binance — Futu is the
ongoing crypto venue. This exists for exactly one purpose: recover the
cost basis of coins already bought here, so the holdings now sitting on
the Ledger are not orphaned.

Run once, verify against §5.11, then revoke the API key and delete the
credential from the encrypted store.
"""
