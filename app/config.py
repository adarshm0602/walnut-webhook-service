from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://walnut@127.0.0.1:55432/walnut"
    processing_delay_seconds: float = Field(default=30, gt=0)
    worker_concurrency: int = Field(default=20, ge=1, le=100)
    poll_interval_seconds: float = Field(default=0.2, gt=0)
    lease_seconds: float = Field(default=60, ge=0.3)
    db_pool_max_size: int = Field(default=10, ge=2, le=50)
    log_level: str = "INFO"
