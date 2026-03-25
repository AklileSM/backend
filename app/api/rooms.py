from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Room
from app.schemas import RoomResponse

router = APIRouter()


def _list_rooms(db: Session) -> list[RoomResponse]:
    rooms = db.scalars(select(Room).order_by(Room.sort_order.asc(), Room.name.asc())).all()
    return [RoomResponse(id=r.id, name=r.name, slug=r.slug, project_id=r.project_id) for r in rooms]


# Both paths: some proxies/clients call /api/rooms without a trailing slash.
@router.get("", response_model=list[RoomResponse])
@router.get("/", response_model=list[RoomResponse])
def list_rooms(db: Session = Depends(get_db)) -> list[RoomResponse]:
    return _list_rooms(db)


@router.get("/{room_slug}", response_model=RoomResponse)
def get_room(room_slug: str, db: Session = Depends(get_db)) -> RoomResponse:
    room = db.scalar(select(Room).where(Room.slug == room_slug))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return RoomResponse(id=room.id, name=room.name, slug=room.slug, project_id=room.project_id)
