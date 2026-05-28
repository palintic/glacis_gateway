from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "postgresql+asyncpg://glacis:glacispassword@localhost:5432/glacis_gateway"
    REDIS_URL: str = "redis://localhost:6379/0"
    OPENAI_API_KEY: str | None = None
    LLM_MODEL: str = "gpt-4o-mini"

    # Target response time for logging/observability
    INGESTION_TIMEOUT_LIMIT_MS: int = 200

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
