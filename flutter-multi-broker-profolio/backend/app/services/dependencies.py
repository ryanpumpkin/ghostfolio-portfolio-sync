"""Service dependency factories for API routes."""

from __future__ import annotations

from functools import lru_cache

from app.adapters.base import SourceAdapter
from app.core.settings import Settings, get_settings
from app.models.domain import Connection
from app.services.adapter_factory import AdapterFactory
from app.services.aggregator import ConnectionRepository, PortfolioAggregator
from app.services.connection_status import (
    FirestoreConnectionStatusWriter,
    InMemoryConnectionStatusPublisher,
    listener_from_writer,
)
from app.services.fx import (
    ExchangerateHostProvider,
    FrankfurterProvider,
    FxProvider,
    FxService,
    NullFxCacheStore,
    OpenExchangeRatesProvider,
)
from app.services.portfolio_cache import PortfolioSnapshotCache
from app.services.quote_hub import QuoteHub, QuoteSourceRegistry
from app.services.vault import (
    ConnectionVaultStore,
    CredentialMode,
    CredentialVaultService,
    FirestoreConnectionVaultStore,
    KmsProvider,
    build_kms_provider,
)


class StaticAdapterRegistry(QuoteSourceRegistry):
    """Static mapping-based adapter registry used until vault/connection wiring lands."""

    def __init__(self, adapters: dict[str, SourceAdapter] | None = None) -> None:
        self._adapters = {k.lower(): v for k, v in (adapters or {}).items()}

    def for_connection(self, connection: Connection) -> SourceAdapter | None:
        return self.for_source(connection.source)

    def for_source(self, source: str) -> SourceAdapter | None:
        return self._adapters.get(source.lower())


class VaultBackedConnectionRepository(ConnectionRepository):
    """Maps vault connection records into aggregator Connection models."""

    def __init__(self, store: ConnectionVaultStore) -> None:
        self._store = store

    async def list_connections(self, user_id: str) -> list[Connection]:
        rows = await self._store.list_for_user(user_id=user_id)
        out: list[Connection] = []
        for row in rows:
            out.append(
                Connection(
                    source=row.source,
                    connection_id=row.connection_id,
                    display_name=row.display_name,
                    server_key_mode=row.credential_mode is CredentialMode.SERVER_KEY,
                    enabled=row.enabled,
                )
            )
        return out


@lru_cache(maxsize=1)
def get_adapter_registry() -> StaticAdapterRegistry:
    # Stub: backend-vault / backend-adapters wiring will inject real per-user adapters.
    return StaticAdapterRegistry()


@lru_cache(maxsize=1)
def get_connection_repository() -> ConnectionRepository:
    return VaultBackedConnectionRepository(get_connection_vault_store())


@lru_cache(maxsize=1)
def get_fx_service(settings: Settings | None = None) -> FxService:
    cfg = settings or get_settings()
    provider_name = cfg.fx_provider.strip().lower()
    provider: FxProvider
    if provider_name in {"openexchangerates", "openexchangerates.org"}:
        api_key = cfg.fx_provider_api_key
        if not api_key:
            msg = "fx_provider_api_key is required for openexchangerates"
            raise ValueError(msg)
        provider = OpenExchangeRatesProvider(api_key=api_key)
    elif provider_name in {"exchangerate.host", "exchangeratehost"}:
        # Requires an API key as of late 2024; if missing, fall through to
        # Frankfurter so the dashboard still works for personal use.
        if cfg.fx_provider_api_key:
            provider = ExchangerateHostProvider(api_key=cfg.fx_provider_api_key)
        else:
            provider = FrankfurterProvider()
    else:
        # Default: Frankfurter (ECB rates, no API key required).
        provider = FrankfurterProvider()
    return FxService(provider=provider, firestore_cache=NullFxCacheStore())


@lru_cache(maxsize=1)
def get_portfolio_aggregator() -> PortfolioAggregator:
    return PortfolioAggregator(
        connections=get_connection_repository(),
        adapters=get_adapter_registry(),
        fx=get_fx_service(),
        adapter_factory=get_adapter_factory(),
        vault_service=get_vault_service(),
        status_publisher=get_connection_status_publisher(),
    )


@lru_cache(maxsize=1)
def get_portfolio_snapshot_cache() -> PortfolioSnapshotCache:
    return PortfolioSnapshotCache()


@lru_cache(maxsize=1)
def get_quote_hub() -> QuoteHub:
    return QuoteHub(get_adapter_registry())


@lru_cache(maxsize=1)
def get_kms_provider(settings: Settings | None = None) -> KmsProvider:
    cfg = settings or get_settings()
    return build_kms_provider(provider_name=cfg.kms_provider, key_id=cfg.kms_key_id)


@lru_cache(maxsize=1)
def get_connection_vault_store() -> ConnectionVaultStore:
    return FirestoreConnectionVaultStore()


@lru_cache(maxsize=1)
def get_vault_service() -> CredentialVaultService:
    return CredentialVaultService(
        store=get_connection_vault_store(),
        kms=get_kms_provider(),
    )


@lru_cache(maxsize=1)
def get_adapter_factory() -> AdapterFactory:
    return AdapterFactory()


@lru_cache(maxsize=1)
def get_connection_status_writer() -> FirestoreConnectionStatusWriter:
    return FirestoreConnectionStatusWriter()


@lru_cache(maxsize=1)
def get_connection_status_publisher() -> InMemoryConnectionStatusPublisher:
    writer = get_connection_status_writer()
    publisher = InMemoryConnectionStatusPublisher()
    publisher.subscribe(listener_from_writer(writer))
    return publisher


@lru_cache(maxsize=1)
def get_analyst_service() -> "AnalystService":
    # Lazy import: AnalystService pulls in the longbridge SDK at module
    # load via its data-fetcher helpers, and we want this dependency
    # module to remain importable even when the SDK isn't installed
    # (test environments without `longbridge`).
    from app.services.analyst.service import AnalystService

    return AnalystService()


__all__ = [
    "StaticAdapterRegistry",
    "get_adapter_registry",
    "get_analyst_service",
    "get_connection_repository",
    "get_adapter_factory",
    "get_connection_status_publisher",
    "get_connection_status_writer",
    "get_fx_service",
    "get_kms_provider",
    "get_portfolio_aggregator",
    "get_quote_hub",
    "get_connection_vault_store",
    "get_vault_service",
]
