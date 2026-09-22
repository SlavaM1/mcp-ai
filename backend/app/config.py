from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://mcp_ai:mcp_ai@db:5432/mcp_ai"
    mcp_server_url: str = "http://weather-mcp:8001/mcp"
    mcp_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_tool_calls: int = Field(default=4, ge=1, le=20)
    cors_origins: str = "http://localhost:4201"

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
