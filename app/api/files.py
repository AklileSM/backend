from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import FileAsset, Room
from app.schemas import ExplorerByDateResponse, ExplorerByRoomResponse, MediaFileResponse, RoomMediaGroup
from app.services.storage import storage_service

router = APIRouter()


def _media_key(media_type: str) -> str:
    if media_type == "pointcloud":
        return "pointclouds"
    if media_type == "video":
        return "videos"
    return "images"


def _serialize_asset(asset: FileAsset) -> MediaFileResponse:
    full_src = storage_service.get_presigned_url(asset.bucket_name, asset.object_name)
    src = full_src
    if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
        src = storage_service.get_presigned_url(asset.thumbnail_bucket_name, asset.thumbnail_object_name)
    return MediaFileResponse(
        id=asset.id,
        src=src,
        type=asset.media_type,
        file_name=asset.display_name,
        full_src=full_src,
        capture_date=asset.capture_date,
    )


def _empty_group() -> RoomMediaGroup:
    return RoomMediaGroup(images=[], videos=[], pointclouds=[])


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


@router.get("/{file_id}/url")
def get_file_url(file_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"url": storage_service.get_presigned_url(asset.bucket_name, asset.object_name)}
