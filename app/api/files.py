import logging
import mimetypes
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from datetime import date
from email.utils import format_datetime as _fmt_datetime

from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)
from fastapi.responses import Response as PlainResponse, StreamingResponse
from sqlalchemy import case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, require_user_can_upload
from app.services.pointcloud import submit_conversion
from app.database import get_db
from app.models import FileAsset, Project, ProjectMember, Room, User
from app.schemas import (
    BulkActionResponse,
    BulkFileIdsRequest,
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
    project_slug: str | None = None,
    media_type: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MyUploadItemResponse]:
    """Assets uploaded by the current user.

    Optional `project_slug` scopes the result to a single project; the caller
    must be a member (or a global admin). Optional `media_type` filters to one
    of image / video / pointcloud / pdf — used by the profile page to power
    its Images / Videos / PDFs side-rail.

    The uploader id is stored in `file_assets.metadata_json.uploaded_by_user_id`.
    Comparing via `cast(JSONB)['key'].astext` avoids the JSON-quoting pitfall
    that a naive `cast(String)` would hit.
    """
    if media_type is not None and media_type not in {"image", "video", "pointcloud", "pdf"}:
        raise HTTPException(status_code=400, detail="Invalid media_type")

    stmt = (
        select(FileAsset, Room)
        .join(Room, FileAsset.room_id == Room.id)
        .where(cast(FileAsset.metadata_json, JSONB)["uploaded_by_user_id"].astext == current_user.id)
        .order_by(FileAsset.created_at.desc())
    )

    if project_slug:
        project = db.scalar(select(Project).where(Project.slug == project_slug))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if not current_user.is_admin:
            membership = db.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == current_user.id,
                )
            )
            if membership is None:
                raise HTTPException(status_code=403, detail="Not a member of this project")
        stmt = stmt.where(Room.project_id == project.id)

    if media_type:
        stmt = stmt.where(FileAsset.media_type == media_type)

    rows = db.execute(stmt).all()
    return [_serialize_my_upload(asset, room) for asset, room in rows]


@router.get("/search", response_model=list[MyUploadItemResponse])
def search_files(
    q: str,
    project_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MyUploadItemResponse]:
    """Fuzzy, project-scoped file search.

    Powers the header search box. Matches on display_name, original_name, and
    room.name with trigram similarity + ILIKE (for short prefix queries that
    trigram alone misses). If `q` parses as an ISO date, capture_date matches
    exactly too. Results are ordered by max similarity, then created_at.

    Membership-gated the same way as /my-uploads.
    """
    query = (q or "").strip()
    if not query:
        return []
    # Cap pathological queries early — Postgres handles the rest cheaply with
    # the trigram indexes.
    if len(query) > 100:
        query = query[:100]

    project = db.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not current_user.is_admin:
        membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == current_user.id,
            )
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="Not a member of this project")

    # Detect "YYYY-MM-DD" — gives users a way to jump to a date directly.
    parsed_date = None
    try:
        parsed_date = date.fromisoformat(query)
    except ValueError:
        pass

    like_pattern = f"%{query}%"
    similarity = func.greatest(
        func.similarity(FileAsset.display_name, query),
        func.similarity(FileAsset.original_name, query),
        func.similarity(Room.name, query),
    ).label("score")

    # `op('%')` is the pg_trgm similarity operator — uses the GIN index built
    # in ensure_search_trigram_indexes(). The ILIKE clauses widen the net for
    # short prefixes that fall below the default 0.3 similarity threshold.
    match_clauses = [
        FileAsset.display_name.ilike(like_pattern),
        FileAsset.original_name.ilike(like_pattern),
        Room.name.ilike(like_pattern),
        FileAsset.display_name.op("%")(query),
        FileAsset.original_name.op("%")(query),
        Room.name.op("%")(query),
    ]
    if parsed_date is not None:
        match_clauses.append(FileAsset.capture_date == parsed_date)

    stmt = (
        select(FileAsset, Room, similarity)
        .join(Room, FileAsset.room_id == Room.id)
        .where(Room.project_id == project.id)
        .where(or_(*match_clauses))
        .order_by(similarity.desc(), FileAsset.created_at.desc())
        .limit(20)
    )
    rows = db.execute(stmt).all()
    return [_serialize_my_upload(asset, room) for asset, room, _score in rows]


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
    if not _can_delete_file(current_user, asset, db):
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


def _drop_asset_storage(asset: FileAsset) -> None:
    """Remove an asset's blobs from MinIO. Best-effort; ignores 404s.

    Extracted so the bulk endpoint and the single-file endpoint share the
    same teardown semantics.
    """
    if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
        storage_service.remove_object_best_effort(
            asset.thumbnail_bucket_name, asset.thumbnail_object_name
        )
    if asset.media_type == "pointcloud":
        storage_service.remove_pointcloud_asset_best_effort(
            asset.bucket_name, asset.object_name, asset.metadata_json
        )
    else:
        storage_service.remove_object_best_effort(asset.bucket_name, asset.object_name)


@router.post("/bulk-delete", response_model=BulkActionResponse)
def bulk_delete_files(
    payload: BulkFileIdsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BulkActionResponse:
    """Delete many file assets in one call.

    Per-asset permission is checked the same way as the single-file delete;
    failures (missing rows, no permission) are silently counted as `skipped`
    so a partially-allowed batch still gets cleaned up rather than 403'ing
    the whole thing.
    """
    if not payload.ids:
        return BulkActionResponse(affected=0, skipped=0)
    # Dedupe + cap to avoid pathologically large requests.
    unique_ids = list(dict.fromkeys(payload.ids))[:500]

    assets = db.scalars(select(FileAsset).where(FileAsset.id.in_(unique_ids))).all()
    found_ids = {a.id for a in assets}
    affected = 0
    for asset in assets:
        if not _can_delete_file(current_user, asset, db):
            continue
        _drop_asset_storage(asset)
        db.delete(asset)
        affected += 1
    if affected:
        db.commit()
    skipped = len(unique_ids) - affected
    # Rows the user no longer has access to + rows that weren't found are
    # both counted as skipped; the frontend just shows the totals.
    skipped = max(skipped, len(unique_ids) - len(found_ids))
    return BulkActionResponse(affected=affected, skipped=skipped)


@router.post("/bulk-download")
def bulk_download_files(
    payload: BulkFileIdsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream a ZIP of the original objects for the requested files.

    Skips:
      - PCDs whose original LAZ was deleted after conversion (no usable
        single-file representation left in storage)
      - rows the caller can't view in the first place
      - rows whose object is missing in MinIO

    The ZIP is assembled on disk in a temp file, then streamed back. For
    typical "30-file accidental upload" batches this is well under 1 GB.
    """
    if not payload.ids:
        raise HTTPException(status_code=400, detail="No file ids supplied")
    unique_ids = list(dict.fromkeys(payload.ids))[:500]

    assets = db.scalars(
        select(FileAsset)
        .where(FileAsset.id.in_(unique_ids))
        .options(selectinload(FileAsset.room))
    ).all()

    # Build the ZIP in a temp file. Closing the file is the caller's job —
    # FastAPI's BackgroundTask runs the cleanup after the response is sent.
    tmp = tempfile.NamedTemporaryFile(prefix="bulk-", suffix=".zip", delete=False)
    tmp.close()
    skipped = 0
    written = 0
    seen_names: dict[str, int] = {}

    try:
        with zipfile.ZipFile(tmp.name, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for asset in assets:
                if not _can_delete_file(current_user, asset, db):
                    # Same gate as delete — viewers shouldn't be able to bulk
                    # exfiltrate either. Single-file download endpoint stays
                    # open for them.
                    skipped += 1
                    continue
                meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
                if asset.media_type == "pointcloud" and meta.get("original_removed_after_conversion"):
                    # Potree output is a directory of files, not the LAZ —
                    # not meaningful to include in a "download originals" zip.
                    skipped += 1
                    continue
                # Avoid name collisions when two assets have the same display
                # name (e.g. moved files retaining their old room prefix).
                base = asset.display_name or asset.id
                name = base
                if name in seen_names:
                    seen_names[name] += 1
                    stem, _, ext = base.partition(".")
                    name = f"{stem}-{seen_names[base]}{'.' + ext if ext else ''}"
                else:
                    seen_names[name] = 0

                try:
                    stream = storage_service.stream_object(asset.bucket_name, asset.object_name)
                except Exception:
                    skipped += 1
                    continue
                try:
                    with zf.open(name, mode="w") as out:
                        for chunk in stream.stream(amt=1024 * 1024):
                            out.write(chunk)
                    written += 1
                except Exception:
                    skipped += 1
                finally:
                    stream.close()
                    stream.release_conn()

        if written == 0:
            os.unlink(tmp.name)
            raise HTTPException(
                status_code=404,
                detail="None of the selected files could be downloaded",
            )

        # Stream the assembled zip back; clean up the temp file once the
        # client has finished reading.
        from starlette.background import BackgroundTask
        zip_size = os.path.getsize(tmp.name)
        filename = f"files-{written}.zip"
        return StreamingResponse(
            _iter_file(tmp.name),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(zip_size),
                "X-Bulk-Affected": str(written),
                "X-Bulk-Skipped": str(skipped),
            },
            background=BackgroundTask(_unlink_quietly, tmp.name),
        )
    except HTTPException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _iter_file(path: str, chunk_size: int = 1024 * 1024):
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


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
