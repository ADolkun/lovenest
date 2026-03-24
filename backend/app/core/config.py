from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    # App
    app_name: str = "Securo"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/securo"

    # Auth
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Pluggy
    pluggy_client_id: str = ""
    pluggy_client_secret: str = ""
    pluggy_oauth_redirect_uri: str = "http://localhost:5173/oauth/callback"

    # Frontend
    frontend_url: str = "http://localhost:5173"

    # Defaults
    default_currency: str = "USD"  # fallback currency when user preference is unavailable

    # FX Rates
    openexchangerates_app_id: str = ""
    supported_currencies: str = "USD,EUR,GBP,BRL,CAD,AUD,CHF,ARS,JPY,MXN,INR,SEK"  # comma-separated list
    fx_sync_mode: str = "on_demand"  # "on_demand" or "scheduled"

    # Celery
    redis_url: str = "redis://localhost:6379/0"

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()
