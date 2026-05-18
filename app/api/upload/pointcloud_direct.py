"""Direct browser-to-MinIO LAZ/LAS upload via a presigned PUT URL.

Three-step protocol:

1. ``POST /pointcloud/direct-init``     — the backend mints a presigned PUT
                                          URL and returns it alongside an
                                          ``upload_id``.  The browser then
                                          PUTs the file directly to MinIO,
                                          skipping the Next.js proxy.
2. *(browser PUTs the file to MinIO)*
3. ``POST /pointcloud/direct-complete`` — the backend validates the uploaded
                                          object, hashes it for dedupe,
                                          downloads a temp copy for
                                          PotreeConverter, and creates the
                                          `FileAsset` row.

Requires ``MINIO_PUBLIC_UPLOAD_BASE_URL`` to be set to a URL the browser can
reach.  When unset, the init endpoint returns 400 and the frontend falls back
to the chunked path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Room, User
from app.schemas import UploadResponse
from app.services.pointcloud import submit_conversion
from app.services.storage import storage_service

from .common import (
    _POINTCLOUD_CHUNK,
    _bucket_for_media_type,
    _build_object_name,
    _check_duplicate,
    _generate_display_name,
    _log_upload_activity,
    _pointcloud_upload_path,
    _read_upload_manifest,
    _require_can_upload,
    _safe_filename,
)

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


@router.post("/pointcloud/direct-init")
def init_pointcloud_direct_upload(
    room_id: str = Form(...),
    capture_date: date = Form(...),
    filename: str = Form(...),
    file_size: int = Form(...),
    content_type: str = Form("application/octet-stream"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    if not settings.minio_public_upload_base_url.strip():
        raise HTTPException(
            status_code=400,
            detail="Direct upload not available: MINIO_PUBLIC_UPLOAD_BASE_URL is not configured.",
        )

    room = db.scalar(select(Room).where(Room.id == room_id))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    _require_can_upload(current_user, room, db)
    if file_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid file size")
    if file_size > settings.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="File too large")

    upload_id = uuid.uuid4().hex
    safe_name = _safe_filename(filename)
    display_name = _generate_display_name(
        room=room,
        capture_date=capture_date,
        media_type="pointcloud",
        content_type=content_type or "application/octet-stream",
        original_filename=safe_name,
        db=db,
    )
    object_name = _build_object_name(room.id, capture_date, display_name)
    bucket_name = _bucket_for_media_type("pointcloud")
    upload_dir = _pointcloud_upload_path(upload_id)
    upload_dir.mkdir(parents=True, exist_ok=False)
    (upload_dir / "manifest.txt").write_text(
        "\n".join(
            [
                f"room_id={room_id}",
                f"capture_date={capture_date.isoformat()}",
                f"filename={safe_name}",
                f"file_size={file_size}",
                f"content_type={content_type or 'application/octet-stream'}",
                f"bucket_name={bucket_name}",
                f"object_name={object_name}",
                f"display_name={display_name}",
            ]
        ),
        encoding="utf-8",
    )
    upload_url = storage_service.get_presigned_put_url(bucket_name, object_name)
    return {"upload_id": upload_id, "upload_url": upload_url, "method": "PUT"}


@router.post("/pointcloud/direct-complete", response_model=UploadResponse)
def complete_pointcloud_direct_upload(
    upload_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadResponse:
    upload_dir = _pointcloud_upload_path(upload_id)
    manifest = upload_dir / "manifest.txt"
    if not upload_dir.exists() or not manifest.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    try:
        meta = _read_upload_manifest(manifest)
        room_id = meta.get("room_id", "")
        room = db.scalar(select(Room).where(Room.id == room_id))
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        capture_date_raw = meta.get("capture_date")
        if not capture_date_raw:
            raise HTTPException(status_code=400, detail="Missing capture_date")
        capture_date = date.fromisoformat(capture_date_raw)
        original_filename = _safe_filename(meta.get("filename", "upload.laz"))
        content_type = meta.get("content_type", "application/octet-stream")
        bucket_name = meta.get("bucket_name", _bucket_for_media_type("pointcloud"))
        object_name = meta.get("object_name", "")
        display_name = meta.get("display_name") or _generate_display_name(
            room=room,
            capture_date=capture_date,
            media_type="pointcloud",
            content_type=content_type,
            original_filename=original_filename,
            db=db,
        )
        if not object_name:
            raise HTTPException(status_code=400, detail="Missing uploaded object")

        declared_size = int(meta.get("file_size", "0"))
        stored_size = storage_service.stat_object_size(bucket_name, object_name)
        if stored_size <= 0:
            raise HTTPException(status_code=400, detail="Uploaded object is empty")
        if declared_size > 0 and stored_size != declared_size:
            raise HTTPException(status_code=400, detail="Uploaded size mismatch")

        extension = os.path.splitext(original_filename)[1]
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=extension or ".laz")
        os.close(tmp_fd)
        try:
            storage_service.download_object_to_path(bucket_name, object_name, tmp_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=400, detail="Failed to prepare uploaded pointcloud")

        hasher = hashlib.sha256()
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    buf = f.read(_POINTCLOUD_CHUNK)
                    if not buf:
                        break
                    hasher.update(buf)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail="Failed to read uploaded pointcloud")

        sha256_hash = hasher.hexdigest()
        try:
            _check_duplicate(db, room.id, capture_date, sha256_hash)
        except HTTPException:
            storage_service.remove_object_best_effort(bucket_name, object_name)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return _save_pointcloud_asset_and_queue_conversion(
            db=db,
            room=room,
            capture_date=capture_date,
            original_filename=original_filename,
            object_name=object_name,
            bucket_name=bucket_name,
            content_type=content_type,
            file_size=stored_size,
            current_user=current_user,
            local_path_for_conversion=tmp_path,
            display_name=display_name,
            sha256_hash=sha256_hash,
        )
    finally:
        try:
            manifest.unlink()
        except OSError:
            pass
        try:
            upload_dir.rmdir()
        except OSError:
            pass


def _save_pointcloud_asset_and_queue_conversion(
    *,
    db: Session,
    room: Room,
    capture_date: date,
    original_filename: str,
    object_name: str,
    bucket_name: str,
    content_type: str,
    file_size: int,
    current_user: User,
    local_path_for_conversion: str,
    display_name: str,
    sha256_hash: str | None = None,
) -> UploadResponse:
    """Synchronous variant — still used by the small/direct upload paths.

    The chunked `/complete` endpoint uses the streaming background variant
    in `pointcloud_chunked.py` so the HTTP response doesn't outlast
    Cloudflare's 100s / Next.js's 30s proxy timeouts on multi-GB uploads.
    """
    asset = FileAsset(
        room_id=room.id,
        media_type="pointcloud",
        capture_date=capture_date,
        original_name=original_filename or "upload",
        display_name=display_name,
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

    try:
        # Runs in a separate process — does not block the web server.
        # Temp file is cleaned up by convert_pointcloud_background.
        submit_conversion(asset.id, local_path_for_conversion)
    except Exception as exc:
        # Pool not ready or other submission error — remove the orphaned asset
        # so the user can retry rather than being stuck at "pending" forever.
        try:
            db.delete(asset)
            db.commit()
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue conversion: {exc}",
        ) from exc

    _log_upload_activity(db, room=room, asset=asset, current_user=current_user)

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )
