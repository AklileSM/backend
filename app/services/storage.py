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
            settings.minio_bucket_pdfs,
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

    def upload_file_path(
        self,
        *,
        bucket_name: str,
        object_name: str,
        file_path: str,
        content_type: str,
    ) -> None:
        """Upload a large file from disk without loading it into memory."""
        self.client.fput_object(
            bucket_name,
            object_name,
            file_path,
            content_type=content_type,
        )

    def stream_object(self, bucket_name: str, object_name: str):
        """Return a streaming MinIO response. Caller must close it."""
        return self.client.get_object(bucket_name, object_name)

    def stat_object_size(self, bucket_name: str, object_name: str) -> int:
        st = self.client.stat_object(bucket_name, object_name)
        return int(st.size)

    def get_object_range_bytes(
        self,
        bucket_name: str,
        object_name: str,
        start: int,
        end_inclusive: int,
    ) -> bytes:
        """Read only [start, end_inclusive] from object (S3 Range), without loading the whole file."""
        length = end_inclusive - start + 1
        response = self.client.get_object(
            bucket_name,
            object_name,
            offset=start,
            length=length,
        )
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def get_presigned_url(self, bucket_name: str, object_name: str) -> str:
        return self.client.presigned_get_object(
            bucket_name,
            object_name,
            expires=timedelta(seconds=settings.presigned_url_expiry_seconds),
        )

    def get_object_bytes(self, bucket_name: str, object_name: str) -> bytes:
        response = self.client.get_object(bucket_name, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

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

    def remove_object_best_effort(self, bucket_name: str, object_name: str) -> None:
        try:
            self.client.remove_object(bucket_name, object_name)
        except S3Error as e:
            code = getattr(e, "code", "") or ""
            if code in ("NoSuchKey", "ResourceNotFound"):
                return
            raise


storage_service = StorageService()
