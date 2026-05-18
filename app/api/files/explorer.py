"""Explorer endpoints, user-facing file listing.

* `/my-uploads`        — assets uploaded by the current user, project-
                          scoped optionally.
* `/search`             — fuzzy trigram + ILIKE search across display name,
                          original name, room name. Optional ISO-date exact
                          match.
* `/explorer/dates`     — per-date media counts, used by the calendar
                          highlighter.
* `/explorer/date/{d}`  — all files captured on a single date, grouped by
                          room name.
* `/explorer/room/{slug}` — all files in a single room, grouped by date.

All routes are auth-gated except `/my-uploads` and `/search`, which are
gated by project membership (admins bypass).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.database import get_db
from app.models import FileAsset, Project, ProjectMember, Room, User
from app.schemas import (
    DateMediaCounts,
    ExplorerByDateResponse,
    ExplorerByRoomResponse,
    ExplorerDatesSummaryResponse,
    MyUploadItemResponse,
    RoomMediaGroup,
)

from .common import (
    _empty_group,
    _media_key,
    _serialize_asset,
    _serialize_my_upload,
)

router = APIRouter()


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
    of image / video / pointcloud / pdf, used by the profile page to power
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
    # Cap pathological queries early, Postgres handles the rest cheaply with
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

    # Detect "YYYY-MM-DD", gives users a way to jump to a date directly.
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

    # `op('%')` is the pg_trgm similarity operator, uses the GIN index built
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
