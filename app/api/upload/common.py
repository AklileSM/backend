"""Shared helpers for the upload routes.

Everything that more than one upload kind needs lives here: constants,
display-name generation, MinIO bucket selection, the SHA-256 duplicate
check, manifest read/write, the membership/permission gate, and the
shared activity-log entry shape.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
from datetime import date
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import FileAsset, ProjectMember, Room, User
from app.services.activity import log_activity

settings = get_settings()

# Frontend dispatches on these four, any other value is rejected at the API
# boundary with 400.
_ALLOWED_MEDIA = frozenset({"image", "video", "pointcloud", "pdf"})

# Chunk sizes when streaming an upload to disk.  32 MB for pointclouds (large
# files, fewer request boundaries); 1 MB for the smaller media types.
_POINTCLOUD_CHUNK = 32 * 1024 * 1024
_UPLOAD_CHUNK = 1 * 1024 * 1024

# Where chunked-upload sessions stage their `.part` files.  Sub-dir per
# `upload_id` keeps cleanup trivial.
_POINTCLOUD_UPLOAD_DIR = Path(tempfile.gettempdir()) / "a6_pointcloud_uploads"

_CANONICAL_EXTENSION: dict[str, str] = {
    "image": ".jpg",
    "video": ".mp4",
    "pointcloud": ".laz",
    "pdf": ".pdf",
}


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


def _require_can_upload(user: User, room: Room, db: Session) -> None:
    """Admins bypass.  Otherwise the user must be an `owner` or `editor`
    member of the project that owns this room."""
    if user.is_admin:
        return
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == room.project_id,
            ProjectMember.user_id == user.id,
        )
    )
    if member is None or member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only project owners and editors can upload files")


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


def _log_upload_activity(
    db: Session,
    *,
    room: Room,
    asset: FileAsset,
    current_user: User,
    source: str | None = None,
) -> None:
    """Record an `upload.<media_type>` row on the project activity feed.

    Centralised here so every upload path (small, chunked, direct-MinIO,
    point-cloud) records the same shape of metadata. ``source`` tags the
    origin of the upload (e.g. "robot") so automated captures are
    distinguishable from human uploads in the feed; omitted for human uploads.
    """
    metadata = {
        "file_name": asset.display_name,
        "room_name": room.name,
        "room_slug": room.slug,
        "capture_date": asset.capture_date.isoformat(),
    }
    if source:
        metadata["source"] = source
    log_activity(
        db,
        project_id=room.project_id,
        actor=current_user,
        action=f"upload.{asset.media_type}",
        target_type="file_asset",
        target_id=asset.id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Display-name + storage-path generation
# ---------------------------------------------------------------------------


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
    room: Room,
    capture_date: date,
    media_type: str,
    content_type: str,
    original_filename: str,
    db: Session,
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


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _check_duplicate(db: Session, room_id: str, capture_date: date, sha256_hash: str) -> None:
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


# ---------------------------------------------------------------------------
# Chunked-upload manifest
# ---------------------------------------------------------------------------


def _read_upload_manifest(manifest_path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    for raw in manifest_path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        meta[k] = v
    return meta
