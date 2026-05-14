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
    minio_bucket_pdfs: str = "construction-pdfs"
    minio_bucket_reports: str = "construction-reports"
    minio_bucket_floorplans: str = "construction-floorplans"
    # Optional public URL for browser direct uploads (example: https://minio.example.com).
    # If empty, presigned URLs use minio_server as-is.
    minio_public_upload_base_url: str = ""

    vision_api_key: str = ""
    vision_api_url: str = "http://192.168.50.103:11434/v1/chat/completions"
    vision_model: str = "qwen3-vl:8b"

    @field_validator("vision_api_key", mode="before")
    @classmethod
    def strip_vision_api_key(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    frontend_url: str = "http://localhost:5173"
    cors_extra_origins: str = ""
    presigned_url_expiry_seconds: int = 604800
    max_upload_size_bytes: int = 5368709120  # 5 GB — large enough for LAZ point clouds
    #: After Potree conversion succeeds, delete the uploaded LAZ/LAS from MinIO (viewer uses _potree/ only).
    #: Set DELETE_ORIGINAL_POINTCLOUD_AFTER_CONVERSION=false to keep originals for re-convert / archive.
    delete_original_pointcloud_after_conversion: bool = True
    thumbnail_width: int = 400
    thumbnail_height: int = 300
    thumbnail_quality: int = 82

    jwt_secret: str = ""  # override with JWT_SECRET in production
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@example.com"
    smtp_from_name: str = "A6 Stern"
    smtp_use_tls: bool = True

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