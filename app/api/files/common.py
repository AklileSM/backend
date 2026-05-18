"""Shared helpers for the files endpoints.

Everything that more than one files submodule needs lives here: HTTP Range
parsing, cache header construction, the delete-permission gate, the response
serialisers, and the small constants.
"""

from __future__ import annotations

import mimetypes
import re
from email.utils import format_datetime as _fmt_datetime

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FileAsset, ProjectMember, Room, User
from app.schemas import MediaFileResponse, MyUploadItemResponse, RoomMediaGroup


# Potree's three octree output files. The pointcloud proxy in `serve.py`
# rejects any other filename to keep MinIO behind the proxy.
_POTREE_FILES = frozenset({"metadata.json", "hierarchy.bin", "octree.bin"})


# ---------------------------------------------------------------------------
# Cache + Range helpers
# ---------------------------------------------------------------------------


def _make_cache_headers(stat) -> dict[str, str]:
    """Build ETag, Last-Modified, and Cache-Control headers from a MinIO stat object."""
    headers: dict[str, str] = {"Cache-Control": "public, max-age=86400"}
    if stat.etag:
        headers["ETag"] = f'"{stat.etag.strip(chr(34))}"'
    if stat.last_modified is not None:
        try:
            headers["Last-Modified"] = _fmt_datetime(stat.last_modified, usegmt=True)
        except Exception:
            pass
    return headers


def _is_not_modified(request: Request, etag_header: str | None) -> bool:
    """Return True if If-None-Match matches the given ETag (304 eligible)."""
    if not etag_header:
        return False
    inm = request.headers.get("if-none-match", "").strip()
    if not inm:
        return False
    return inm == "*" or etag_header in {x.strip() for x in inm.split(",")}


def _parse_http_range(range_header: str | None, total: int) -> tuple[int, int] | None:
    """
    Parse a single Range: bytes=… header. Returns (first, last) inclusive for a 206
    partial response, or None to serve the full object (200).

    Supports bytes=a-b, bytes=a-, and suffix bytes=-b. Multiple ranges are not
    supported (first spec wins); malformed headers yield None (full file).
    """
    if not range_header or total <= 0:
        return None
    m = re.match(r"^\s*bytes\s*=\s*(\S+)\s*$", range_header, re.I)
    if not m:
        return None
    spec = m.group(1).split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    left, right = spec.split("-", 1)
    try:
        if left == "":
            # suffix: last N bytes
            suffix_len = int(right)
            if suffix_len <= 0:
                return None
            first = max(0, total - suffix_len)
            last = total - 1
            return (first, last)
        first = int(left)
        if right == "":
            last = total - 1
        else:
            last = int(right)
    except ValueError:
        return None
    if first < 0 or first > last:
        return None
    if first >= total:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{total}"},
        )
    last = min(last, total - 1)
    return (first, last)


# ---------------------------------------------------------------------------
# Permission gate (delete)
# ---------------------------------------------------------------------------


def _can_delete_file(user: User, asset: FileAsset, db: Session) -> bool:
    if user.is_admin:
        return True
    room = db.scalar(select(Room).where(Room.id == asset.room_id))
    if room is None:
        return False
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == room.project_id,
            ProjectMember.user_id == user.id,
        )
    )
    return member is not None and member.role in ("owner", "editor")


# ---------------------------------------------------------------------------
# Response serialisers and small helpers
# ---------------------------------------------------------------------------


def _media_key(media_type: str) -> str:
    if media_type == "pointcloud":
        return "pointclouds"
    if media_type == "video":
        return "videos"
    if media_type == "pdf":
        return "pdfs"
    return "images"


def _content_type_for_asset(asset: FileAsset) -> str:
    if asset.content_type:
        return asset.content_type
    guessed, _ = mimetypes.guess_type(asset.display_name)
    return guessed or "application/octet-stream"


def _empty_group() -> RoomMediaGroup:
    return RoomMediaGroup(images=[], videos=[], pointclouds=[], pdfs=[])


def _serialize_asset(asset: FileAsset) -> MediaFileResponse:
    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    conversion_status = meta.get("conversion_status") if asset.media_type == "pointcloud" else None

    if asset.media_type == "pointcloud" and conversion_status == "ready":
        # Serve via the backend proxy so Potree can fetch all sibling files
        # (hierarchy.bin, octree.bin) on the same origin without CORS issues.
        full_src = f"/api/files/{asset.id}/pointcloud/metadata.json"
        src = full_src
    else:
        # Same-origin URLs via backend proxy so browsers never call private MinIO IPs
        # (required for HTTPS + public deployments; presigned URLs embed LAN endpoints).
        full_src = f"/api/files/{asset.id}/content"
        src = full_src
        if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
            src = f"/api/files/{asset.id}/thumbnail"

    uploaded_by = meta.get("uploaded_by_user_id")
    uploaded_by_str = str(uploaded_by) if uploaded_by is not None else None
    conversion_error = meta.get("conversion_error") if asset.media_type == "pointcloud" else None
    return MediaFileResponse(
        id=asset.id,
        src=src,
        type=asset.media_type,
        file_name=asset.display_name,
        full_src=full_src,
        capture_date=asset.capture_date,
        uploaded_by_user_id=uploaded_by_str,
        conversion_status=conversion_status,
        conversion_error=conversion_error,
    )


def _serialize_my_upload(asset: FileAsset, room: Room) -> MyUploadItemResponse:
    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    conversion_status = meta.get("conversion_status") if asset.media_type == "pointcloud" else None

    if asset.media_type == "pointcloud" and conversion_status == "ready":
        full_src = f"/api/files/{asset.id}/pointcloud/metadata.json"
        src = full_src
    else:
        full_src = f"/api/files/{asset.id}/content"
        src = full_src
        if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
            src = f"/api/files/{asset.id}/thumbnail"

    return MyUploadItemResponse(
        id=asset.id,
        room_slug=room.slug,
        room_name=room.name,
        media_type=asset.media_type,
        file_name=asset.display_name,
        capture_date=asset.capture_date,
        created_at=asset.created_at,
        src=src,
        full_src=full_src,
        conversion_status=conversion_status,
    )
