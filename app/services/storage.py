from datetime import timedelta
from io import BytesIO

from minio import Minio
from minio.error import S3Error
from PIL import Image

from app.config import get_settings

settings = get_settings()


class StorageService:
    def __init__(self) -> None:
        self.client = Minio(
            settings.minio_server,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_use_ssl,
        )

    def ensure_buckets(self) -> None:
        for bucket in (
            settings.minio_bucket_images,
            settings.minio_bucket_thumbnails,
            settings.minio_bucket_pointclouds,
            settings.minio_bucket_reports,
        ):
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

    def upload_bytes(
        self,
        *,
        bucket_name: str,
        object_name: str,
        data: bytes,
        content_type: str,
    ) -> None:
        self.client.put_object(
            bucket_name,
            object_name,
            BytesIO(data),
            len(data),
            content_type=content_type,
        )

    def get_presigned_url(self, bucket_name: str, object_name: str) -> str:
        return self.client.presigned_get_object(
            bucket_name,
            object_name,
            expires=timedelta(seconds=settings.presigned_url_expiry_seconds),
        )

    def generate_thumbnail(self, raw_bytes: bytes) -> bytes:
        image = Image.open(BytesIO(raw_bytes))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((settings.thumbnail_width, settings.thumbnail_height))
        output = BytesIO()
        image.save(output, format="JPEG", quality=settings.thumbnail_quality, optimize=True)
        return output.getvalue()

    def healthcheck(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except S3Error:
            return False
        except Exception:
            return False


storage_service = StorageService()
