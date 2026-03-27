from app.providers.base import BankProvider, AccountData, TransactionData, ConnectionData, ConnectTokenData

# Registry of available providers.
_PROVIDERS: dict[str, type[BankProvider]] = {}

# All known providers the system supports (extensible for future connectors).
KNOWN_PROVIDERS = [
    {
        "name": "pluggy",
        "display_name": "Pluggy",
        "description": "Open finance provider for Brazilian banks",
        "flow_type": "widget",
    },
]


def register_provider(name: str, cls: type[BankProvider]) -> None:
    """Register a bank provider implementation."""
    _PROVIDERS[name] = cls


def get_provider(name: str) -> BankProvider:
    """Get an instance of a registered bank provider by name."""
    provider_class = _PROVIDERS.get(name)
    if not provider_class:
        available = ", ".join(_PROVIDERS.keys()) or "(none)"
        raise ValueError(f"Unknown provider: {name}. Available: {available}")
    return provider_class()


def list_providers() -> list[dict[str, str]]:
    """Return info about all registered providers."""
    return [
        {"name": name, "flow_type": cls().flow_type}
        for name, cls in _PROVIDERS.items()
    ]


def all_known_providers() -> list[dict]:
    """Return all known providers with a configured flag."""
    return [
        {**p, "configured": p["name"] in _PROVIDERS}
        for p in KNOWN_PROVIDERS
    ]


def _auto_register_providers() -> None:
    """Auto-register providers when credentials are configured."""
    from app.core.config import get_settings
    settings = get_settings()

    if settings.pluggy_client_id and settings.pluggy_client_secret:
        from app.providers.pluggy import PluggyProvider
        register_provider("pluggy", PluggyProvider)


_auto_register_providers()


_storage_provider = None


def get_storage_provider():
    """Get the configured storage provider (singleton)."""
    global _storage_provider
    if _storage_provider is None:
        from app.core.config import get_settings

        settings = get_settings()
        if settings.storage_provider == "local":
            from app.providers.local_storage import LocalStorageProvider

            _storage_provider = LocalStorageProvider()
        else:
            raise NotImplementedError(
                f"Storage provider '{settings.storage_provider}' is not yet implemented. "
                "Supported: 'local'"
            )
    return _storage_provider


__all__ = [
    "BankProvider",
    "AccountData",
    "TransactionData",
    "ConnectionData",
    "ConnectTokenData",
    "register_provider",
    "get_provider",
    "list_providers",
    "all_known_providers",
    "get_storage_provider",
]
