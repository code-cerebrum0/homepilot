from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str = Field(min_length=32)

    frontend_origin: str = "http://localhost:3000"
    cookie_secure: bool = True
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 30

    app_timezone: str = "Asia/Kolkata"
    default_currency: str = "INR"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ollama_timeout_seconds: float = 90
    ollama_max_tool_rounds: int = 5

    auto_create_tables: bool = True
    simulator_enabled: bool = False
    simulator_interval_seconds: int = 50

    login_max_failures: int = 5
    login_window_seconds: int = 900
    device_rate_limit_per_minute: int = 120
    chat_rate_limit_per_minute: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
