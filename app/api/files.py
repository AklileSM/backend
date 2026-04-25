import logging
import mimetypes
import os
import re
import tempfile
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)
from fastapi.responses import Response as PlainResponse, StreamingResponse
from sqlalchemy import case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, require_user_can_upload
from app.services.pointcloud import submit_conversion
from app.database import get_db
from app.models import FileAsset, Room, User
from app.schemas import (
    DateMediaCounts,
    ExplorerByDateResponse,
    ExplorerByRoomResponse,
    ExplorerDatesSummaryResponse,
    MediaFileResponse,
    MyUploadItemResponse,
    RoomMediaGroup,
)
from app.services.storage import storage_service

router = APIRouter()

_POTREE_FILES = frozenset({"metadata.json", "hierarchy.bin", "octree.bin"})


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


def _can_delete_file(user: User, _asset: FileAsset) -> bool:
    return user.role in ("admin", "manager")


def _empty_group() -> RoomMediaGroup:
    return RoomMediaGroup(images=[], videos=[], pointclouds=[], pdfs=[])


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


@router.get("/my-uploads", response_model=list[MyUploadItemResponse])
def list_my_uploads(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MyUploadItemResponse]:
    """Assets whose upload metadata records this user (admin/manager uploads)."""
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Upload history is only available for admin and manager accounts")

    # Compare as text: cast(json)->>'uploaded_by_user_id' must match user id (plain cast(String) on JSON
    # values can include JSON quoting and fail to match).
    stmt = (
        select(FileAsset, Room)
        .join(Room, FileAsset.room_id == Room.id)
        .where(cast(FileAsset.metadata_json, JSONB)["uploaded_by_user_id"].astext == current_user.id)
        .order_by(FileAsset.created_at.desc())
    )
    rows = db.execute(stmt).all()
    return [_serialize_my_upload(asset, room) for asset, room in rows]


@router.get("/explorer/dates", response_model=ExplorerDatesSummaryResponse)
def explorer_dates_summary(db: Session = Depends(get_db)) -> ExplorerDatesSummaryResponse:
    image_sum = func.sum(case((FileAsset.media_type == "image", 1), else_=0))
    video_sum = func.sum(case((FileAsset.media_type == "video", 1), else_=0))
    pointcloud_sum = func.sum(case((FileAsset.media_type == "pointcloud", 1), else_=0))
    pdf_sum = func.sum(case((FileAsset.media_type == "pdf", 1), else_=0))
    stmt = (
        select(FileAsset.capture_date, image_sum, video_sum, pointcloud_sum, pdf_sum)
        .group_by(FileAsset.capture_date)
        .order_by(FileAsset.capture_date.asc())
    )
    dates: dict[str, DateMediaCounts] = {}
    for capture_date, images, videos, pointclouds, pdfs in db.execute(stmt):
        dates[capture_date.isoformat()] = DateMediaCounts(
            images=int(images or 0),
            videos=int(videos or 0),
            pointclouds=int(pointclouds or 0),
            pdfs=int(pdfs or 0),
        )
    return ExplorerDatesSummaryResponse(dates=dates)


@router.get("/explorer/date/{capture_date}", response_model=ExplorerByDateResponse)
def explorer_by_date(capture_date: date, db: Session = Depends(get_db)) -> ExplorerByDateResponse:
    rooms = db.scalars(select(Room).order_by(Room.sort_order.asc())).all()
    room_map: dict[str, RoomMediaGroup] = {room.name: _empty_group() for room in rooms}

    stmt = (
        select(FileAsset)
        .join(Room)
        .options(selectinload(FileAsset.room))
        .where(FileAsset.capture_date == capture_date)
        .order_by(Room.sort_order.asc(), FileAsset.display_name.asc())
    )
    assets = db.scalars(stmt).all()
    for asset in assets:
        group = room_map.setdefault(asset.room.name, _empty_group())
        getattr(group, _media_key(asset.media_type)).append(_serialize_asset(asset))

    return ExplorerByDateResponse(date=capture_date.isoformat(), rooms=room_map)


@router.get("/explorer/room/{room_slug}", response_model=ExplorerByRoomResponse)
def explorer_by_room(room_slug: str, db: Session = Depends(get_db)) -> ExplorerByRoomResponse:
    room = db.scalar(select(Room).where(Room.slug == room_slug))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    stmt = (
        select(FileAsset)
        .where(FileAsset.room_id == room.id)
        .order_by(FileAsset.capture_date.asc(), FileAsset.display_name.asc())
    )
    assets = db.scalars(stmt).all()
    dates_map: dict[str, RoomMediaGroup] = defaultdict(_empty_group)
    for asset in assets:
        day = asset.capture_date.isoformat()
        getattr(dates_map[day], _media_key(asset.media_type)).append(_serialize_asset(asset))

    return ExplorerByRoomResponse(room=room.slug, room_name=room.name, dates=dict(dates_map))


@router.delete("/{file_id}", status_code=204)
def delete_file_asset(
    file_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    if not _can_delete_file(current_user, asset):
        raise HTTPException(status_code=403, detail="Not allowed to delete this file")

    if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
        storage_service.remove_object_best_effort(asset.thumbnail_bucket_name, asset.thumbnail_object_name)
    if asset.media_type == "pointcloud":
        storage_service.remove_pointcloud_asset_best_effort(
            asset.bucket_name,
            asset.object_name,
            asset.metadata_json,
        )
    else:
        storage_service.remove_object_best_effort(asset.bucket_name, asset.object_name)

    db.delete(asset)
    db.commit()


@router.get("/{file_id}/url")
def get_file_url(file_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    conversion_status = meta.get("conversion_status") if asset.media_type == "pointcloud" else None
    if asset.media_type == "pointcloud" and conversion_status == "ready":
        return {"url": f"/api/files/{asset.id}/pointcloud/metadata.json"}
    return {"url": f"/api/files/{asset.id}/content"}


@router.get("/{asset_id}/thumbnail", response_model=None)
def proxy_file_thumbnail(asset_id: str, db: Session = Depends(get_db)) -> PlainResponse:
    asset = db.scalar(select(FileAsset).where(FileAsset.id == asset_id))
    if asset is None or not (asset.thumbnail_bucket_name and asset.thumbnail_object_name):
        raise HTTPException(status_code=404, detail="Not found")
    media_type = "image/jpeg"
    try:
        data = storage_service.get_object_bytes(asset.thumbnail_bucket_name, asset.thumbnail_object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")
    return PlainResponse(
        content=data,
        media_type=media_type,
        headers={
            "Content-Length": str(len(data)),
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get("/{asset_id}/content", response_model=None)
def proxy_file_content(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.scalar(select(FileAsset).where(FileAsset.id == asset_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Not found")
    if asset.media_type == "pointcloud":
        meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
        if meta.get("conversion_status") == "ready":
            raise HTTPException(status_code=404, detail="Use pointcloud routes")

    media_type = _content_type_for_asset(asset)
    try:
        total = storage_service.stat_object_size(asset.bucket_name, asset.object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")

    range_header = request.headers.get("range")
    try:
        parsed = _parse_http_range(range_header, total)
    except HTTPException:
        raise

    if parsed is not None:
        first, last = parsed
        try:
            chunk = storage_service.get_object_range_bytes(
                asset.bucket_name, asset.object_name, first, last
            )
        except Exception:
            raise HTTPException(status_code=404, detail="File not found in storage")
        return PlainResponse(
            content=chunk,
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {first}-{last}/{total}",
                "Content-Length": str(len(chunk)),
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
            },
        )

    # Whole-file: prefer one buffer + Content-Length for small files (avoids chunked encoding
    # overhead and proxy 502s). Stream large files so TTFB is immediate for panoramas.
    _INLINE_MAX = 5 * 1024 * 1024
    if total <= _INLINE_MAX:
        try:
            data = storage_service.get_object_bytes(asset.bucket_name, asset.object_name)
        except Exception:
            raise HTTPException(status_code=404, detail="File not found in storage")
        return PlainResponse(
            content=data,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(data)),
                "Cache-Control": "public, max-age=86400",
            },
        )

    stream = storage_service.stream_object(asset.bucket_name, asset.object_name)

    def body():
        try:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                yield chunk
        finally:
            stream.close()
            stream.release_conn()

    return StreamingResponse(
        body(),
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(total),
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.post("/{file_id}/retry-conversion", status_code=202)
def retry_pointcloud_conversion(
    file_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_can_upload),
) -> dict[str, str]:
    """Re-queue a failed point cloud conversion. Admin only.

    Downloads the original LAZ from MinIO and resubmits it to the converter
    pool. Returns 409 if the asset is not in 'failed' state or if the original
    file was already deleted after a previous successful conversion.
    """
    asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    if asset.media_type != "pointcloud":
        raise HTTPException(status_code=400, detail="Only point cloud assets can be retried")

    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    status = meta.get("conversion_status")
    if status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry: conversion status is '{status}', not 'failed'",
        )
    if meta.get("original_removed_after_conversion"):
        raise HTTPException(
            status_code=409,
            detail="Cannot retry: the original LAZ was deleted after a previous successful conversion. Re-upload the file.",
        )

    extension = os.path.splitext(asset.object_name)[1] or ".laz"
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=extension)
    os.close(tmp_fd)

    try:
        storage_service.download_object_to_path(asset.bucket_name, asset.object_name, tmp_path)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=502, detail=f"Could not retrieve original file from storage: {exc}")

    new_meta = dict(meta)
    new_meta["conversion_status"] = "pending"
    new_meta.pop("conversion_error", None)
    asset.metadata_json = new_meta
    db.commit()

    try:
        submit_conversion(asset.id, tmp_path)
    except Exception as exc:
        new_meta["conversion_status"] = "failed"
        new_meta["conversion_error"] = f"Failed to queue retry: {exc}"
        asset.metadata_json = new_meta
        db.commit()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=503, detail=f"Could not queue conversion: {exc}")

    return {"status": "queued", "asset_id": asset.id}


@router.get("/{asset_id}/conversion-status")
def get_conversion_status(
    asset_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Poll the conversion status of a point cloud asset."""
    asset = db.scalar(select(FileAsset).where(FileAsset.id == asset_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    return {
        "status": meta.get("conversion_status", "unknown"),
        "error": meta.get("conversion_error"),
    }


@router.get("/{asset_id}/pointcloud/{path:path}")
def proxy_pointcloud_file(
    asset_id: str,
    path: str,
    request: Request,
    db: Session = Depends(get_db),
) -> PlainResponse:
    """
    Proxy individual Potree octree files (metadata.json, hierarchy.bin, octree.bin)
    from MinIO so the browser fetches them on the same origin — no CORS config needed.

    Potree 2.x issues byte-range requests (Range: bytes=X-Y) to read specific
    chunks of hierarchy.bin and octree.bin.  We must honour these or Potree will
    receive more bytes than it expects and corrupt its node-count arithmetic.
    """
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    asset = db.scalar(select(FileAsset).where(FileAsset.id == asset_id))
    if asset is None or asset.media_type != "pointcloud":
        raise HTTPException(status_code=404, detail="Not found")

    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    base_object = meta.get("potree_base_object")
    if not base_object:
        raise HTTPException(status_code=404, detail="Point cloud not yet converted")

    filename = path.split("/")[-1]
    if filename not in _POTREE_FILES:
        raise HTTPException(status_code=404, detail="Not found")

    object_name = base_object + filename
    content_type = "application/json" if filename.endswith(".json") else "application/octet-stream"

    try:
        total = storage_service.stat_object_size(asset.bucket_name, object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")

    range_header = request.headers.get("range")
    logger.debug("pointcloud proxy: %s total=%d range=%s", filename, total, range_header)

    try:
        parsed = _parse_http_range(range_header, total)
    except HTTPException:
        raise

    if parsed is not None:
        first, last = parsed
        try:
            chunk = storage_service.get_object_range_bytes(
                asset.bucket_name, object_name, first, last
            )
        except Exception:
            raise HTTPException(status_code=404, detail="File not found in storage")
        return PlainResponse(
            content=chunk,
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {first}-{last}/{total}",
                "Content-Length": str(len(chunk)),
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
            },
        )

    try:
        data = storage_service.get_object_bytes(asset.bucket_name, object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")

    return PlainResponse(
        content=data,
        media_type=content_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
            "Cache-Control": "public, max-age=86400",
        },
    )
