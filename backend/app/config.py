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

    # MCP / OAuth
    public_base_url: str = "http://localhost:8000"
    mcp_allowed_origins: str = ""  # comma-separated extra origins
    mcp_rate_limit_per_minute: int = 60

    oauth_enable_dcr: bool = False   # Dynamic Client Registration (RFC 7591)
    oauth_enable_cimd: bool = False  # Client ID Metadata Documents
    oauth_cimd_allowed_hosts: str = "" # comma-separated host allowlist

    oauth_access_ttl_seconds: int = 3600        # 1 hour
    oauth_refresh_ttl_days: int = 30
    oauth_code_ttl_seconds: int = 300           # 5 minutes

    @property
    def mcp_allowed_origins_set(self) -> set[str]:
        base = {self.frontend_origin, self.public_base_url.rstrip("/")}
        extras = {o.strip() for o in self.mcp_allowed_origins.split(",") if o.strip()}
        return base | extras

    @property
    def oauth_cimd_allowed_hosts_set(self) -> set[str]:
        return {h.strip() for h in self.oauth_cimd_allowed_hosts.split(",") if h.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
