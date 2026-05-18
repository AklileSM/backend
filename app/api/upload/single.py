"""POST /single, single-request upload for images, videos, and PDFs.

Pointcloud uploads also enter through this route (the frontend sends
``media_type=pointcloud``) but delegate to `_upload_pointcloud` below, which
streams to a temp file and submits the asset to the conversion pool.  The
chunked / direct upload paths under `/pointcloud/...` are the preferred
flows for multi-GB scans; this is the fallback for small ones.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import tempfile
import uuid
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Room, User
from app.schemas import UploadResponse
from app.services.ai import generate_and_cache_ai_description
from app.services.pointcloud import submit_conversion
from app.services.storage import storage_service

from .common import (
    _ALLOWED_MEDIA,
    _POINTCLOUD_CHUNK,
    _UPLOAD_CHUNK,
    _bucket_for_media_type,
    _build_object_name,
    _check_duplicate,
    _generate_display_name,
    _log_upload_activity,
    _require_can_upload,
)

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


@router.post("/single", response_model=UploadResponse)
async def upload_single(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    room_id: str = Form(...),
    media_type: str = Form(...),
    capture_date: date = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadResponse:
    if media_type not in _ALLOWED_MEDIA:
        raise HTTPException(status_code=400, detail="Invalid media_type")

    room = db.scalar(select(Room).where(Room.id == room_id))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    _require_can_upload(current_user, room, db)

    if media_type == "pdf":
        fn = (file.filename or "").lower()
        ct = (file.content_type or "").lower()
        if "pdf" not in ct and not fn.endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Expected a PDF file")

    content_type = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    )
    if media_type == "pdf" and "pdf" not in content_type.lower():
        content_type = "application/pdf"

    display_name = _generate_display_name(
        room=room,
        capture_date=capture_date,
        media_type=media_type,
        content_type=content_type,
        original_filename=file.filename or "",
        db=db,
    )
    object_name = _build_object_name(room.id, capture_date, display_name)
    bucket_name = _bucket_for_media_type(media_type)
    extension = os.path.splitext(display_name)[1]

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
        )

    # --- All other types: stream to a temp file to avoid loading large files into RAM ---
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=extension)
    file_size = 0
    hasher = hashlib.sha256()
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > settings.max_upload_size_bytes:
                    raise HTTPException(status_code=413, detail="File too large")
                hasher.update(chunk)
                tmp_file.write(chunk)

        sha256_hash = hasher.hexdigest()
        _check_duplicate(db, room.id, capture_date, sha256_hash)

        storage_service.upload_file_path(
            bucket_name=bucket_name,
            object_name=object_name,
            file_path=tmp_path,
            content_type=content_type,
        )

        thumbnail_bucket_name = None
        thumbnail_object_name = None
        if media_type == "image" and content_type.startswith("image/"):
            thumbnail_bucket_name = settings.minio_bucket_thumbnails
            thumbnail_object_name = (
                f"{room.id}/{capture_date.isoformat()}/thumb-{uuid.uuid4().hex}.jpg"
            )
            with open(tmp_path, "rb") as f:
                raw = f.read()
            thumbnail = await asyncio.get_event_loop().run_in_executor(
                None, storage_service.generate_thumbnail, raw
            )
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
            display_name=display_name,
            bucket_name=bucket_name,
            object_name=object_name,
            thumbnail_bucket_name=thumbnail_bucket_name,
            thumbnail_object_name=thumbnail_object_name,
            content_type=content_type,
            file_size=file_size,
            sha256_hash=sha256_hash,
            metadata_json={
                "uploaded_by_user_id": current_user.id,
                "uploaded_by_username": current_user.username,
            },
            ai_description_status="generating" if media_type == "image" else None,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)

        if media_type == "image":
            background_tasks.add_task(generate_and_cache_ai_description, asset.id)

        _log_upload_activity(db, room=room, asset=asset, current_user=current_user)

        return UploadResponse(
            id=asset.id,
            room=room.slug,
            media_type=asset.media_type,
            file_name=asset.display_name,
            capture_date=asset.capture_date,
        )
    except HTTPException:
        raise
    except Exception:
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
) -> UploadResponse:
    """Stream a LAZ/point-cloud file to a temp file, upload to MinIO, then
    submit conversion to the process pool."""

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=extension)
    try:
        file_size = 0
        hasher = hashlib.sha256()
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            while True:
                chunk = await file.read(_POINTCLOUD_CHUNK)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > settings.max_upload_size_bytes:
                    raise HTTPException(status_code=413, detail="File too large")
                hasher.update(chunk)
                tmp_file.write(chunk)

        sha256_hash = hasher.hexdigest()
        _check_duplicate(db, room.id, capture_date, sha256_hash)

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

    asset = FileAsset(
        room_id=room.id,
        media_type="pointcloud",
        capture_date=capture_date,
        original_name=file.filename or "upload",
        display_name=os.path.basename(object_name),
        bucket_name=bucket_name,
        object_name=object_name,
        content_type=content_type,
        file_size=file_size,
        sha256_hash=sha256_hash,
        metadata_json={
            "uploaded_by_user_id": current_user.id,
            "uploaded_by_username": current_user.username,
            "conversion_status": "pending",
        },
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    # Submit to the process pool, runs in a separate process, does not block
    # the web server. Temp file is cleaned up by convert_pointcloud_background.
    submit_conversion(asset.id, tmp_path)

    _log_upload_activity(db, room=room, asset=asset, current_user=current_user)

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )
