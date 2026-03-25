import mimetypes
import os
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_user_can_upload
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Room, User
from app.schemas import UploadResponse
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()

_ALLOWED_MEDIA = frozenset({"image", "video", "pointcloud"})


def _bucket_for_media_type(media_type: str) -> str:
    if media_type == "pointcloud":
        return settings.minio_bucket_pointclouds
    return settings.minio_bucket_images


@router.post("/single", response_model=UploadResponse)
async def upload_single(
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

    raw = await file.read()
    if len(raw) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="File too large")

    extension = os.path.splitext(file.filename or "")[1]
    object_name = f"{room.slug}/{capture_date.isoformat()}/{uuid.uuid4().hex}{extension}"
    bucket_name = _bucket_for_media_type(media_type)
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"

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
        thumbnail_object_name = f"{room.slug}/{capture_date.isoformat()}/thumb-{uuid.uuid4().hex}.jpg"
        thumbnail = storage_service.generate_thumbnail(raw)
        storage_service.upload_bytes(
            bucket_name=thumbnail_bucket_name,
            object_name=thumbnail_object_name,
            data=thumbnail,
            content_type="image/jpeg",
        )

    asset = FileAsset(
        room_id=room.id,
        media_type=media_type,
        capture_date=capture_date,
        original_name=file.filename or "upload",
        display_name=file.filename or "upload",
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
