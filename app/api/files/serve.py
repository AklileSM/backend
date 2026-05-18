"""File serving, proxy streams from MinIO to the browser.

All four endpoints share the same model: pull the object's metadata, honour
`Range:` headers for partial responses, honour `If-None-Match` for 304s, and
either inline (<= 5 MB) or stream the body.

The Potree pointcloud proxy is required because Potree 2.x issues byte-range
requests to read specific chunks of `hierarchy.bin` and `octree.bin`. Serving
more bytes than requested would corrupt its node-count arithmetic.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response as PlainResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FileAsset
from app.services.storage import storage_service

from .common import (
    _POTREE_FILES,
    _content_type_for_asset,
    _is_not_modified,
    _make_cache_headers,
    _parse_http_range,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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
def proxy_file_thumbnail(asset_id: str, request: Request, db: Session = Depends(get_db)) -> PlainResponse:
    asset = db.scalar(select(FileAsset).where(FileAsset.id == asset_id))
    if asset is None or not (asset.thumbnail_bucket_name and asset.thumbnail_object_name):
        raise HTTPException(status_code=404, detail="Not found")
    media_type = "image/jpeg"
    try:
        stat = storage_service.stat_object(asset.thumbnail_bucket_name, asset.thumbnail_object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")
    cache_headers = _make_cache_headers(stat)
    if _is_not_modified(request, cache_headers.get("ETag")):
        return PlainResponse(status_code=304, headers=cache_headers)
    try:
        data = storage_service.get_object_bytes(asset.thumbnail_bucket_name, asset.thumbnail_object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")
    return PlainResponse(
        content=data,
        media_type=media_type,
        headers={"Content-Length": str(len(data)), **cache_headers},
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
        stat = storage_service.stat_object(asset.bucket_name, asset.object_name)
        total = int(stat.size)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")
    cache_headers = _make_cache_headers(stat)

    range_header = request.headers.get("range")
    try:
        parsed = _parse_http_range(range_header, total)
    except HTTPException:
        raise

    # 304 only for full-file requests (range requests must get 206 with the actual bytes)
    if parsed is None and _is_not_modified(request, cache_headers.get("ETag")):
        return PlainResponse(status_code=304, headers={"Accept-Ranges": "bytes", **cache_headers})

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
                **cache_headers,
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
            headers={"Accept-Ranges": "bytes", "Content-Length": str(len(data)), **cache_headers},
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
        headers={"Accept-Ranges": "bytes", "Content-Length": str(total), **cache_headers},
    )


@router.get("/{asset_id}/pointcloud/{path:path}")
def proxy_pointcloud_file(
    asset_id: str,
    path: str,
    request: Request,
    db: Session = Depends(get_db),
) -> PlainResponse:
    """
    Proxy individual Potree octree files (metadata.json, hierarchy.bin, octree.bin)
    from MinIO so the browser fetches them on the same origin, no CORS config needed.

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
        stat = storage_service.stat_object(asset.bucket_name, object_name)
        total = int(stat.size)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")
    cache_headers = _make_cache_headers(stat)

    range_header = request.headers.get("range")
    logger.debug("pointcloud proxy: %s total=%d range=%s", filename, total, range_header)

    try:
        parsed = _parse_http_range(range_header, total)
    except HTTPException:
        raise

    if parsed is None and _is_not_modified(request, cache_headers.get("ETag")):
        return PlainResponse(status_code=304, headers={"Accept-Ranges": "bytes", **cache_headers})

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
                **cache_headers,
            },
        )

    try:
        data = storage_service.get_object_bytes(asset.bucket_name, object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in storage")

    return PlainResponse(
        content=data,
        media_type=content_type,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(len(data)), **cache_headers},
    )
