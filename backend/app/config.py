from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .mcp_client import validate_server_name


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://mcp_ai:mcp_ai@db:5432/mcp_ai"
    mcp_server_url: str = "http://weather-mcp:8001/mcp"
    mcp_servers: dict[str, str] | None = None
    mcp_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_tool_calls: int = Field(default=12, ge=1, le=20)
    cors_origins: str = "http://localhost:4201"

    @field_validator("mcp_servers")
    @classmethod
    def validate_mcp_servers(cls, servers: dict[str, str] | None) -> dict[str, str] | None:
        if servers is None:
            return None
        if not servers:
            raise ValueError("MCP_SERVERS не может быть пустым JSON-объектом.")
        for name, url in servers.items():
            validate_server_name(name)
            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"Для MCP-сервера '{name}' должен быть задан URL.")
        return {name: url.strip() for name, url in servers.items()}

    @property
    def configured_mcp_servers(self) -> dict[str, str]:
        return self.mcp_servers or {"weather": self.mcp_server_url}

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
