"""Chunked LAZ/LAS upload through the backend proxy.

Three-step protocol:

1. ``POST /pointcloud/init``     — frontend declares the file; backend stages
                                   a working directory and returns
                                   ``{upload_id, chunk_size}``.
2. ``POST /pointcloud/chunk``    — repeated, one call per 32 MB slice.
                                   Chunks land as ``00000000.part`` files in
                                   the staging directory.
3. ``POST /pointcloud/complete`` — backend creates the `FileAsset` row in
                                   ``conversion_status="uploading"`` and
                                   spawns a daemon thread to assemble, hash,
                                   upload to MinIO, and queue conversion.
                                   Returns immediately so Cloudflare and the
                                   Next.js proxy don't time out the request.

The fallback path when the direct/presigned PUT route is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import FileAsset, Room, User
from app.schemas import UploadResponse
from app.services.pointcloud import submit_conversion
from app.services.storage import storage_service

from .common import (
    _POINTCLOUD_CHUNK,
    _bucket_for_media_type,
    _build_object_name,
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


@router.post("/pointcloud/init")
def init_pointcloud_upload(
    room_id: str = Form(...),
    capture_date: date = Form(...),
    filename: str = Form(...),
    file_size: int = Form(...),
    content_type: str = Form("application/octet-stream"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str | int]:
    room = db.scalar(select(Room).where(Room.id == room_id))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    _require_can_upload(current_user, room, db)
    if file_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid file size")
    if file_size > settings.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="File too large")

    upload_id = uuid.uuid4().hex
    upload_dir = _pointcloud_upload_path(upload_id)
    upload_dir.mkdir(parents=True, exist_ok=False)
    safe_name = _safe_filename(filename)
    manifest = upload_dir / "manifest.txt"
    manifest.write_text(
        "\n".join(
            [
                f"room_id={room_id}",
                f"capture_date={capture_date.isoformat()}",
                f"filename={safe_name}",
                f"file_size={file_size}",
                f"content_type={content_type or 'application/octet-stream'}",
                f"display_name={_generate_display_name(room=room, capture_date=capture_date, media_type='pointcloud', content_type=content_type or 'application/octet-stream', original_filename=safe_name, db=db)}",
            ]
        ),
        encoding="utf-8",
    )
    return {"upload_id": upload_id, "chunk_size": _POINTCLOUD_CHUNK}


@router.post("/pointcloud/chunk")
async def upload_pointcloud_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    _: User = Depends(get_current_user),
) -> dict[str, bool | int]:
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="Invalid chunk index")
    upload_dir = _pointcloud_upload_path(upload_id)
    if not upload_dir.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    chunk_path = upload_dir / f"{chunk_index:08d}.part"
    total = 0
    with open(chunk_path, "wb") as out:
        while True:
            buf = await chunk.read(_POINTCLOUD_CHUNK)
            if not buf:
                break
            total += len(buf)
            out.write(buf)
    return {"ok": True, "chunk_index": chunk_index, "chunk_size": total}


@router.post("/pointcloud/complete", response_model=UploadResponse)
def complete_pointcloud_upload(
    upload_id: str = Form(...),
    total_chunks: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadResponse:
    # Assembling chunks + SHA-256 + the MinIO upload for a multi-GB LAS takes
    # 1–2 minutes, which is longer than Cloudflare's free-tier (~100s) and
    # Next.js's rewrite proxy (~30s) will hold a request open. We create the
    # asset row up front with status="uploading" and do the heavy work in a
    # daemon thread, so this handler returns in well under a second.
    if total_chunks <= 0:
        raise HTTPException(status_code=400, detail="Invalid total chunk count")
    upload_dir = _pointcloud_upload_path(upload_id)
    manifest = upload_dir / "manifest.txt"
    if not upload_dir.exists() or not manifest.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    # Verify every chunk arrived before promising the client a success.
    for i in range(total_chunks):
        if not (upload_dir / f"{i:08d}.part").exists():
            raise HTTPException(status_code=400, detail=f"Missing chunk {i}")

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

    display_name = meta.get("display_name") or _generate_display_name(
        room=room,
        capture_date=capture_date,
        media_type="pointcloud",
        content_type=content_type,
        original_filename=original_filename,
        db=db,
    )
    object_name = _build_object_name(room.id, capture_date, display_name)
    bucket_name = _bucket_for_media_type("pointcloud")
    extension = os.path.splitext(original_filename)[1]

    asset = FileAsset(
        room_id=room.id,
        media_type="pointcloud",
        capture_date=capture_date,
        original_name=original_filename or "upload",
        display_name=display_name,
        bucket_name=bucket_name,
        object_name=object_name,
        content_type=content_type,
        file_size=None,
        sha256_hash=None,
        metadata_json={
            "uploaded_by_user_id": current_user.id,
            "uploaded_by_username": current_user.username,
            "conversion_status": "uploading",
        },
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    threading.Thread(
        target=_finalize_pointcloud_upload_in_background,
        kwargs=dict(
            asset_id=asset.id,
            upload_dir=upload_dir,
            total_chunks=total_chunks,
            bucket_name=bucket_name,
            object_name=object_name,
            content_type=content_type,
            extension=extension,
        ),
        daemon=True,
    ).start()

    _log_upload_activity(db, room=room, asset=asset, current_user=current_user)

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )


def _finalize_pointcloud_upload_in_background(
    *,
    asset_id: str,
    upload_dir: Path,
    total_chunks: int,
    bucket_name: str,
    object_name: str,
    content_type: str,
    extension: str,
) -> None:
    """Background worker for `/pointcloud/complete`.

    The asset row already exists with metadata_json["conversion_status"] ==
    "uploading". We assemble chunks → SHA-256 → duplicate check → MinIO upload
    → hand the temp file to the converter pool. The asset progresses through
    uploading → processing → ready (or → failed on error). Runs in a daemon
    thread so the originating HTTP request can return immediately.
    """
    db = SessionLocal()
    tmp_fd, assembled_path = tempfile.mkstemp(suffix=extension or ".laz")
    file_size = 0
    hasher = hashlib.sha256()
    minio_uploaded = False
    handed_off_to_converter = False
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            for i in range(total_chunks):
                part = upload_dir / f"{i:08d}.part"
                if not part.exists():
                    raise RuntimeError(f"Missing chunk {i}")
                with open(part, "rb") as src:
                    while True:
                        buf = src.read(_POINTCLOUD_CHUNK)
                        if not buf:
                            break
                        file_size += len(buf)
                        if file_size > settings.max_upload_size_bytes:
                            raise RuntimeError(
                                f"File exceeds {settings.max_upload_size_bytes}-byte limit"
                            )
                        hasher.update(buf)
                        out.write(buf)

        sha256_hash = hasher.hexdigest()

        # Duplicate check — exclude the placeholder row we just inserted.
        existing = db.scalar(
            select(FileAsset)
            .where(FileAsset.sha256_hash == sha256_hash, FileAsset.id != asset_id)
            .options(joinedload(FileAsset.room))
        )
        if existing is not None:
            room_name = existing.room.name if existing.room else "an unknown room"
            raise RuntimeError(
                f'Already uploaded to {room_name} on {existing.capture_date} as "{existing.display_name}"'
            )

        storage_service.upload_file_path(
            bucket_name=bucket_name,
            object_name=object_name,
            file_path=assembled_path,
            content_type=content_type,
        )
        minio_uploaded = True

        asset = db.get(FileAsset, asset_id)
        if asset is None:
            # Row was deleted from the UI while we were uploading — clean up MinIO.
            storage_service.remove_object_best_effort(bucket_name, object_name)
            return
        asset.file_size = file_size
        asset.sha256_hash = sha256_hash
        meta = dict(asset.metadata_json or {})
        meta["conversion_status"] = "pending"
        meta.pop("conversion_error", None)
        asset.metadata_json = meta
        db.commit()

        submit_conversion(asset_id, assembled_path)
        handed_off_to_converter = True
    except Exception as exc:
        logger.exception(
            "Background finalisation failed for pointcloud asset %s", asset_id
        )
        try:
            asset = db.get(FileAsset, asset_id)
            if asset is not None:
                meta = dict(asset.metadata_json or {})
                meta["conversion_status"] = "failed"
                meta["conversion_error"] = str(exc)[:600]
                asset.metadata_json = meta
                db.commit()
        except Exception:
            logger.exception("Could not mark asset %s as failed", asset_id)
        if minio_uploaded:
            storage_service.remove_object_best_effort(bucket_name, object_name)
    finally:
        db.close()
        if not handed_off_to_converter:
            try:
                os.unlink(assembled_path)
            except OSError:
                pass
        for p in upload_dir.glob("*.part"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            (upload_dir / "manifest.txt").unlink()
        except OSError:
            pass
        try:
            upload_dir.rmdir()
        except OSError:
            pass
