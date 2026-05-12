import asyncio
import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from datetime import date

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import require_user_can_upload
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Room, User
from app.schemas import UploadResponse
from app.services.ai import generate_and_cache_ai_description
from app.services.pointcloud import submit_conversion
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()

_STALE_UPLOAD_AGE_SECONDS = 4 * 3600  # 4 hours


def cleanup_stale_uploads() -> None:
    """Remove abandoned chunked-upload temp directories older than 4 hours.

    Called once at server startup. Safe to call multiple times — errors on
    individual directories are logged and skipped.
    """
    if not _POINTCLOUD_UPLOAD_DIR.exists():
        return
    now = time.time()
    for d in _POINTCLOUD_UPLOAD_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            age = now - d.stat().st_mtime
            if age > _STALE_UPLOAD_AGE_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                logger.info("Removed stale upload directory %s (age %.0f s)", d, age)
        except OSError as exc:
            logger.warning("Could not check/remove stale upload dir %s: %s", d, exc)

_ALLOWED_MEDIA = frozenset({"image", "video", "pointcloud", "pdf"})
_POINTCLOUD_CHUNK = 32 * 1024 * 1024  # 32 MB chunks to reduce request overhead on large uploads
_UPLOAD_CHUNK = 1 * 1024 * 1024       # 1 MB chunks for images, PDFs, and videos
_POINTCLOUD_UPLOAD_DIR = Path(tempfile.gettempdir()) / "a6_pointcloud_uploads"

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
    """Return a name like ``room3-20260329-001.jpg``."""
    orig_ext = os.path.splitext(original_filename)[1]
    canonical_fallback = _CANONICAL_EXTENSION.get(media_type, ".bin")
    ext = _ext_from_content_type(content_type, orig_ext or canonical_fallback)

    # Count existing assets for this room + date + media_type so each media
    # category has an independent sequence.
    seq: int = db.scalar(
        select(func.count()).where(
            FileAsset.room_id == room.id,
            FileAsset.capture_date == capture_date,
            FileAsset.media_type == media_type,
        )
    ) or 0
    seq += 1

    compact_date = capture_date.strftime("%Y%m%d")
    return f"{room.slug}-{compact_date}-{seq:03d}{ext}"


def _build_object_name(room_id: str, capture_date: date, display_name: str) -> str:
    return f"{room_id}/{capture_date.isoformat()}/{display_name}"


def _bucket_for_media_type(media_type: str) -> str:
    if media_type == "pointcloud":
        return settings.minio_bucket_pointclouds
    if media_type == "pdf":
        return settings.minio_bucket_pdfs
    return settings.minio_bucket_images


def _pointcloud_upload_path(upload_id: str) -> Path:
    return _POINTCLOUD_UPLOAD_DIR / upload_id


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "").strip()
    if not base:
        return "upload.laz"
    return base


def _check_duplicate(db: "Session", room_id: str, capture_date: date, sha256_hash: str) -> None:
    """Raise 409 if an identical file already exists anywhere in the system."""
    existing = db.scalar(
        select(FileAsset)
        .where(FileAsset.sha256_hash == sha256_hash)
        .options(joinedload(FileAsset.room))
    )
    if existing is not None:
        room_name = existing.room.name if existing.room else "an unknown room"
        raise HTTPException(
            status_code=409,
            detail=f"This file has already been uploaded to {room_name} on {existing.capture_date} as \"{existing.display_name}\".",
        )


def _read_upload_manifest(manifest_path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    for raw in manifest_path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        meta[k] = v
    return meta


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

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )


@router.post("/pointcloud/init")
def init_pointcloud_upload(
    room_id: str = Form(...),
    capture_date: date = Form(...),
    filename: str = Form(...),
    file_size: int = Form(...),
    content_type: str = Form("application/octet-stream"),
    db: Session = Depends(get_db),
    _: User = Depends(require_user_can_upload),
) -> dict[str, str | int]:
    room = db.scalar(select(Room).where(Room.id == room_id))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
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


@router.post("/pointcloud/direct-init")
def init_pointcloud_direct_upload(
    room_id: str = Form(...),
    capture_date: date = Form(...),
    filename: str = Form(...),
    file_size: int = Form(...),
    content_type: str = Form("application/octet-stream"),
    db: Session = Depends(get_db),
    _: User = Depends(require_user_can_upload),
) -> dict[str, str]:
    if not settings.minio_public_upload_base_url.strip():
        raise HTTPException(
            status_code=400,
            detail="Direct upload not available: MINIO_PUBLIC_UPLOAD_BASE_URL is not configured.",
        )

    room = db.scalar(select(Room).where(Room.id == room_id))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
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


@router.post("/pointcloud/chunk")
async def upload_pointcloud_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    _: User = Depends(require_user_can_upload),
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
    current_user: User = Depends(require_user_can_upload),
) -> UploadResponse:
    if total_chunks <= 0:
        raise HTTPException(status_code=400, detail="Invalid total chunk count")
    upload_dir = _pointcloud_upload_path(upload_id)
    manifest = upload_dir / "manifest.txt"
    if not upload_dir.exists() or not manifest.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

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

    tmp_fd, assembled_path = tempfile.mkstemp(suffix=extension or ".laz")
    file_size = 0
    hasher = hashlib.sha256()
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            for i in range(total_chunks):
                part = upload_dir / f"{i:08d}.part"
                if not part.exists():
                    raise HTTPException(status_code=400, detail=f"Missing chunk {i}")
                with open(part, "rb") as src:
                    while True:
                        buf = src.read(_POINTCLOUD_CHUNK)
                        if not buf:
                            break
                        file_size += len(buf)
                        if file_size > settings.max_upload_size_bytes:
                            raise HTTPException(status_code=413, detail="File too large")
                        hasher.update(buf)
                        out.write(buf)

        sha256_hash = hasher.hexdigest()
        _check_duplicate(db, room.id, capture_date, sha256_hash)

        storage_service.upload_file_path(
            bucket_name=bucket_name,
            object_name=object_name,
            file_path=assembled_path,
            content_type=content_type,
        )
    except HTTPException:
        try:
            os.unlink(assembled_path)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.unlink(assembled_path)
        except OSError:
            pass
        raise
    finally:
        for p in upload_dir.glob("*.part"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            manifest.unlink()
        except OSError:
            pass
        try:
            upload_dir.rmdir()
        except OSError:
            pass

    return _save_pointcloud_asset_and_queue_conversion(
        db=db,
        room=room,
        capture_date=capture_date,
        original_filename=original_filename,
        object_name=object_name,
        bucket_name=bucket_name,
        content_type=content_type,
        file_size=file_size,
        current_user=current_user,
        local_path_for_conversion=assembled_path,
        display_name=display_name,
        sha256_hash=sha256_hash,
    )


@router.post("/pointcloud/direct-complete", response_model=UploadResponse)
def complete_pointcloud_direct_upload(
    upload_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_can_upload),
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


@router.post("/single", response_model=UploadResponse)
async def upload_single(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    room_id: str = Form(...),
    media_type: str = Form(...),
    capture_date: date = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_can_upload),
) -> UploadResponse:
    if media_type not in _ALLOWED_MEDIA:
        raise HTTPException(status_code=400, detail="Invalid media_type")

    room = db.scalar(select(Room).where(Room.id == room_id))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

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

    # Submit to the process pool — runs in a separate process, does not block
    # the web server. Temp file is cleaned up by convert_pointcloud_background.
    submit_conversion(asset.id, tmp_path)

    return UploadResponse(
        id=asset.id,
        room=room.slug,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
    )
