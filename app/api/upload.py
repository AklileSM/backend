import mimetypes
import os
import tempfile
import uuid
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_user_can_upload
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Room, User
from app.schemas import UploadResponse
from app.services.pointcloud import convert_pointcloud_background
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()

_ALLOWED_MEDIA = frozenset({"image", "video", "pointcloud", "pdf"})
_POINTCLOUD_CHUNK = 8 * 1024 * 1024  # 8 MB read chunks for streaming

_CANONICAL_EXTENSION: dict[str, str] = {
    "image": ".jpg",
    "video": ".mp4",
    "pointcloud": ".laz",
    "pdf": ".pdf",
}


def _ext_from_content_type(content_type: str, fallback: str) -> str:
    """Derive a clean extension from MIME type, falling back to the provided value."""
    ct = content_type.lower()
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "mp4" in ct:
        return ".mp4"
    if "quicktime" in ct:
        return ".mov"
    if "pdf" in ct:
        return ".pdf"
    if "octet-stream" in ct or not ct:
        return fallback
    guessed = mimetypes.guess_extension(ct)
    return guessed if guessed else fallback


def _generate_display_name(
    *,
    room: "Room",
    capture_date: date,
    media_type: str,
    content_type: str,
    original_filename: str,
    db: "Session",
) -> str:
    """Return a name like ``2026-03-29_room3_001.jpg``."""
    orig_ext = os.path.splitext(original_filename)[1]
    canonical_fallback = _CANONICAL_EXTENSION.get(media_type, ".bin")
    ext = _ext_from_content_type(content_type, orig_ext or canonical_fallback)

    # Count existing assets for this room + date so the sequence is always correct.
    seq: int = db.scalar(
        select(func.count()).where(
            FileAsset.room_id == room.id,
            FileAsset.capture_date == capture_date,
        )
    ) or 0
    seq += 1

    return f"{capture_date.isoformat()}_{room.slug}_{seq:03d}{ext}"


def _bucket_for_media_type(media_type: str) -> str:
    if media_type == "pointcloud":
        return settings.minio_bucket_pointclouds
    if media_type == "pdf":
        return settings.minio_bucket_pdfs
    return settings.minio_bucket_images


@router.post("/single", response_model=UploadResponse)
async def upload_single(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    room_slug: str = Form(...),
    media_type: str = Form(...),
    capture_date: date = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_can_upload),
) -> UploadResponse:
    if media_type not in _ALLOWED_MEDIA:
        raise HTTPException(status_code=400, detail="Invalid media_type")

    room = db.scalar(select(Room).where(Room.slug == room_slug))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    if media_type == "pdf":
        fn = (file.filename or "").lower()
        ct = (file.content_type or "").lower()
        if "pdf" not in ct and not fn.endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Expected a PDF file")

    extension = os.path.splitext(file.filename or "")[1]
    object_name = f"{room.slug}/{capture_date.isoformat()}/{uuid.uuid4().hex}{extension}"
    bucket_name = _bucket_for_media_type(media_type)
    content_type = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    )
    if media_type == "pdf" and "pdf" not in content_type.lower():
        content_type = "application/pdf"

    # --- Point clouds: stream to a temp file to avoid loading GBs into RAM ---
    if media_type == "pointcloud":
        return await _upload_pointcloud(
            file=file,
            room=room,
            capture_date=capture_date,
            object_name=object_name,
            bucket_name=bucket_name,
            content_type=content_type,
            extension=extension,
            current_user=current_user,
            db=db,
            background_tasks=background_tasks,
        )

    # --- All other types: read into memory (images, videos, PDFs) ---
    raw = await file.read()
    if len(raw) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="File too large")

    storage_service.upload_bytes(
        bucket_name=bucket_name,
        object_name=object_name,
        data=raw,
        content_type=content_type,
    )

    thumbnail_bucket_name = None
    thumbnail_object_name = None
    if media_type == "image" and content_type.startswith("image/"):
        thumbnail_bucket_name = settings.minio_bucket_thumbnails
        thumbnail_object_name = (
            f"{room.slug}/{capture_date.isoformat()}/thumb-{uuid.uuid4().hex}.jpg"
        )
        thumbnail = storage_service.generate_thumbnail(raw)
        storage_service.upload_bytes(
            bucket_name=thumbnail_bucket_name,
            object_name=thumbnail_object_name,
            data=thumbnail,
            content_type="image/jpeg",
        )

    display_name = _generate_display_name(
        room=room,
        capture_date=capture_date,
        media_type=media_type,
        content_type=content_type,
        original_filename=file.filename or "",
        db=db,
    )

    asset = FileAsset(
        room_id=room.id,
        media_type=media_type,
        capture_date=capture_date,
        original_name=file.filename or "upload",
        display_name=display_name,
        bucket_name=bucket_name,
        object_name=object_name,
        thumbnail_bucket_name=thumbnail_bucket_name,
        thumbnail_object_name=thumbnail_object_name,
        content_type=content_type,
        file_size=len(raw),
        metadata_json={
            "uploaded_by_user_id": current_user.id,
            "uploaded_by_username": current_user.username,
        },
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )


async def _upload_pointcloud(
    *,
    file: UploadFile,
    room: Room,
    capture_date: date,
    object_name: str,
    bucket_name: str,
    content_type: str,
    extension: str,
    current_user: User,
    db: Session,
    background_tasks: BackgroundTasks,
) -> UploadResponse:
    """Stream a LAZ/point-cloud file to a temp file, upload to MinIO, then
    trigger PotreeConverter as a background task."""

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=extension)
    try:
        file_size = 0
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            while True:
                chunk = await file.read(_POINTCLOUD_CHUNK)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > settings.max_upload_size_bytes:
                    raise HTTPException(status_code=413, detail="File too large")
                tmp_file.write(chunk)

        storage_service.upload_file_path(
            bucket_name=bucket_name,
            object_name=object_name,
            file_path=tmp_path,
            content_type=content_type,
        )
    except HTTPException:
        os.unlink(tmp_path)
        raise
    except Exception:
        os.unlink(tmp_path)
        raise

    pc_display_name = _generate_display_name(
        room=room,
        capture_date=capture_date,
        media_type="pointcloud",
        content_type=content_type,
        original_filename=file.filename or "",
        db=db,
    )

    asset = FileAsset(
        room_id=room.id,
        media_type="pointcloud",
        capture_date=capture_date,
        original_name=file.filename or "upload",
        display_name=pc_display_name,
        bucket_name=bucket_name,
        object_name=object_name,
        content_type=content_type,
        file_size=file_size,
        metadata_json={
            "uploaded_by_user_id": current_user.id,
            "uploaded_by_username": current_user.username,
            "conversion_status": "pending",
        },
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    # Schedule conversion — tmp_path is cleaned up by the task when done.
    background_tasks.add_task(convert_pointcloud_background, asset.id, tmp_path)

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )
