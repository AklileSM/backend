from functools import lru_cache

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_name: str = "A6 Stern API"
    app_env: str = "development"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 3001

    database_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "a6_stern"
    db_user: str = "postgres"
    db_password: str = ""

    minio_endpoint: str = "127.0.0.1"
    minio_api_port: int = 9000
    minio_console_port: int = 9001
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_use_ssl: bool = False
    minio_bucket_images: str = "construction-images"
    minio_bucket_thumbnails: str = "construction-thumbnails"
    minio_bucket_pointclouds: str = "construction-pointclouds"
    minio_bucket_reports: str = "construction-reports"

    hyperbolic_api_key: str = ""
    hyperbolic_api_url: str = "https://api.hyperbolic.xyz/v1/chat/completions"

    hyperbolic_model: str = "Qwen/Qwen2.5-VL-72B-Instruct"

    @field_validator("hyperbolic_api_key", mode="before")
    @classmethod
    def strip_hyperbolic_api_key(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    frontend_url: str = "http://localhost:5173"
    cors_extra_origins: str = ""
    presigned_url_expiry_seconds: int = 604800
    max_upload_size_bytes: int = 524288000
    thumbnail_width: int = 400
    thumbnail_height: int = 300
    thumbnail_quality: int = 82

    jwt_secret: str = "dev-only-change-me"  # override with JWT_SECRET in production
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minio_server(self) -> str:
        return f"{self.minio_endpoint}:{self.minio_api_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
