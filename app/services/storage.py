import logging
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlparse, urlunparse

from minio import Minio
from minio.error import S3Error
from PIL import Image, ImageOps

from app.config import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)

_POTREE_SUFFIXES = ("metadata.json", "hierarchy.bin", "octree.bin")


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

    def stat_object(self, bucket_name: str, object_name: str):
        """Return the full MinIO stat object (.size, .etag, .last_modified)."""
        return self.client.stat_object(bucket_name, object_name)

    def stat_object_size(self, bucket_name: str, object_name: str) -> int:
        return int(self.stat_object(bucket_name, object_name).size)

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

    def get_presigned_put_url(self, bucket_name: str, object_name: str) -> str:
        raw = self.client.get_presigned_url(
            method="PUT",
            bucket_name=bucket_name,
            object_name=object_name,
            expires=timedelta(seconds=settings.presigned_url_expiry_seconds),
        )
        public_base = (settings.minio_public_upload_base_url or "").strip()
        if not public_base:
            return raw
        try:
            src = urlparse(raw)
            dst = urlparse(public_base if "://" in public_base else f"https://{public_base}")
            return urlunparse((dst.scheme, dst.netloc, src.path, src.params, src.query, src.fragment))
        except Exception:
            return raw

    def download_object_to_path(self, bucket_name: str, object_name: str, file_path: str) -> None:
        self.client.fget_object(bucket_name, object_name, file_path)

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
        image = ImageOps.fit(image, (settings.thumbnail_width, settings.thumbnail_height), Image.LANCZOS)
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

    def remove_pointcloud_asset_best_effort(
        self,
        bucket_name: str,
        object_name: str,
        metadata_json: dict | None,
    ) -> None:
        """
        Remove a point-cloud upload: Potree outputs live under ``{stem}_potree/`` beside
        the original LAS/LAZ key. Listing may fail in edge cases; we always try the three
        known converter outputs as a fallback.
        """
        meta = metadata_json if isinstance(metadata_json, dict) else {}
        base = meta.get("potree_base_object")
        if not isinstance(base, str) or not base.strip():
            base = object_name.rsplit(".", 1)[0] + "_potree/"
        else:
            base = base.strip()
        if not base.endswith("/"):
            base = base + "/"

        try:
            for obj in self.client.list_objects(bucket_name, prefix=base, recursive=True):
                on = getattr(obj, "object_name", None)
                if on:
                    self.remove_object_best_effort(bucket_name, on)
        except S3Error as e:
            logger.warning(
                "list_objects failed for pointcloud cleanup bucket=%s prefix=%s: %s",
                bucket_name,
                base,
                e,
            )
            for suffix in _POTREE_SUFFIXES:
                self.remove_object_best_effort(bucket_name, base + suffix)

        self.remove_object_best_effort(bucket_name, object_name)


storage_service = StorageService()
