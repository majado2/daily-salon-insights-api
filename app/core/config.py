from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite+pysqlite:///./salon.db"
    jwt_secret: str = "development-only-change-this-secret"
    cookie_secure: bool = False
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:4173",
            "http://localhost:4173",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    access_token_minutes: int = 15
    session_idle_minutes: int = 30
    session_absolute_hours: int = 8
    api_prefix: str = "/api/v1"
    timezone: str = "Asia/Riyadh"

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment == "production":
            if len(self.jwt_secret) < 32 or self.jwt_secret.startswith("development"):
                raise ValueError("JWT_SECRET must be a strong production secret")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
