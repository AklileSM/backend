from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import Project, ProjectMember, Room, User
from app.schemas import (
    ProjectCreateRequest,
    ProjectMemberAddRequest,
    ProjectMemberResponse,
    ProjectMemberUpdateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
    RoomCreateRequest,
    RoomResponse,
    RoomUpdateRequest,
)
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()

_ALLOWED_FLOORPLAN_TYPES = {"image/jpeg", "image/png", "image/webp"}
_FLOORPLAN_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _project_to_response(p: Project) -> ProjectResponse:
    floorplan_url: str | None = None
    if p.floorplan_url:
        try:
            floorplan_url = storage_service.get_presigned_url(
                settings.minio_bucket_floorplans, p.floorplan_url
            )
        except Exception:
            floorplan_url = None
    return ProjectResponse(
        id=p.id,
        name=p.name,
        slug=p.slug,
        description=p.description,
        location=p.location,
        status=p.status,
        owner_id=p.owner_id,
        floorplan_url=floorplan_url,
        created_at=p.created_at,
        updated_at=p.updated_at or p.created_at,
    )


def _room_to_response(r: Room) -> RoomResponse:
    return RoomResponse(
        id=r.id,
        name=r.name,
        slug=r.slug,
        project_id=r.project_id,
        floor_plan_coordinates=r.floor_plan_coordinates,
        sort_order=r.sort_order,
    )


def _member_to_response(m: ProjectMember) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        user_id=m.user_id,
        username=m.user.username,
        email=m.user.email,
        role=m.role,
        joined_at=m.joined_at,
    )


def _get_project_or_404(project_id: str, db: Session) -> Project:
    p = db.scalar(select(Project).where(Project.id == project_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def _get_member_or_403(project_id: str, user: User, db: Session) -> ProjectMember | None:
    """Return membership record, or None if user is admin (admins bypass membership)."""
    if user.is_admin:
        return None
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    if member is None:
        raise HTTPException(status_code=403, detail="Not a member of this project")
    return member


# ---------------------------------------------------------------------------
# Project list & create
# ---------------------------------------------------------------------------

@router.get("", response_model=list[ProjectResponse])
@router.get("/", response_model=list[ProjectResponse])
def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ProjectResponse]:
    if current_user.is_admin:
        projects = db.scalars(select(Project).order_by(Project.name.asc())).all()
    else:
        stmt = (
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == current_user.id)
            .order_by(Project.name.asc())
        )
        projects = db.scalars(stmt).all()
    return [_project_to_response(p) for p in projects]


@router.post("/", response_model=ProjectResponse, status_code=201)
def create_project(
    payload: ProjectCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = Project(
        name=payload.name.strip(),
        slug=payload.slug.strip(),
        description=payload.description,
        location=payload.location,
        owner_id=current_user.id,
        status="active",
        updated_at=datetime.utcnow(),
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="A project with that slug already exists") from None

    db.add(ProjectMember(project_id=project.id, user_id=current_user.id, role="owner"))
    db.commit()
    db.refresh(project)
    return _project_to_response(project)


# ---------------------------------------------------------------------------
# Slug-based lookup — must be defined before /{project_id}
# ---------------------------------------------------------------------------

@router.get("/by-slug/{slug}", response_model=ProjectResponse)
def get_project_by_slug(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = db.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _get_member_or_403(project.id, current_user, db)
    return _project_to_response(project)


# ---------------------------------------------------------------------------
# Single project
# ---------------------------------------------------------------------------

@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = _get_project_or_404(project_id, db)
    _get_member_or_403(project_id, current_user, db)
    return _project_to_response(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role != "owner":
        raise HTTPException(status_code=403, detail="Only project owners can update the project")

    if payload.name is not None:
        project.name = payload.name.strip()
    if payload.description is not None:
        project.description = payload.description
    if payload.location is not None:
        project.location = payload.location
    if payload.status is not None:
        if payload.status not in ("active", "on_hold", "completed", "archived"):
            raise HTTPException(status_code=400, detail="Invalid status value")
        project.status = payload.status
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return _project_to_response(project)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    project = _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role != "owner":
        raise HTTPException(status_code=403, detail="Only project owners can delete projects")
    db.delete(project)
    db.commit()


# ---------------------------------------------------------------------------
# Floorplan
# ---------------------------------------------------------------------------

@router.post("/{project_id}/floorplan", response_model=ProjectResponse)
async def upload_floorplan(
    project_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only project owners and editors can upload a floorplan")

    content_type = file.content_type or "image/jpeg"
    if content_type not in _ALLOWED_FLOORPLAN_TYPES:
        raise HTTPException(status_code=400, detail="Floorplan must be JPEG, PNG, or WebP")

    ext = _FLOORPLAN_EXT.get(content_type, ".jpg")
    object_name = f"{project_id}/floorplan{ext}"

    data = await file.read()
    storage_service.upload_bytes(
        bucket_name=settings.minio_bucket_floorplans,
        object_name=object_name,
        data=data,
        content_type=content_type,
    )

    if project.floorplan_url and project.floorplan_url != object_name:
        storage_service.remove_object_best_effort(settings.minio_bucket_floorplans, project.floorplan_url)

    project.floorplan_url = object_name
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return _project_to_response(project)


@router.delete("/{project_id}/floorplan", response_model=ProjectResponse)
def delete_floorplan(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only project owners and editors can remove the floorplan")

    if project.floorplan_url:
        storage_service.remove_object_best_effort(settings.minio_bucket_floorplans, project.floorplan_url)
        project.floorplan_url = None
        project.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(project)
    return _project_to_response(project)


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

@router.get("/{project_id}/rooms", response_model=list[RoomResponse])
def list_project_rooms(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RoomResponse]:
    _get_project_or_404(project_id, db)
    _get_member_or_403(project_id, current_user, db)
    rooms = db.scalars(
        select(Room)
        .where(Room.project_id == project_id)
        .order_by(Room.sort_order.asc(), Room.name.asc())
    ).all()
    return [_room_to_response(r) for r in rooms]


@router.post("/{project_id}/rooms", response_model=RoomResponse, status_code=201)
def create_room(
    project_id: str,
    payload: RoomCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RoomResponse:
    _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only owners and editors can create rooms")

    room = Room(
        project_id=project_id,
        name=payload.name.strip(),
        slug=payload.slug.strip(),
        sort_order=payload.sort_order,
    )
    db.add(room)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="A room with that slug already exists") from None
    db.commit()
    db.refresh(room)
    return _room_to_response(room)


@router.patch("/{project_id}/rooms/{room_id}", response_model=RoomResponse)
def update_room(
    project_id: str,
    room_id: str,
    payload: RoomUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RoomResponse:
    _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only owners and editors can update rooms")

    room = db.scalar(
        select(Room).where(Room.id == room_id, Room.project_id == project_id)
    )
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    if payload.name is not None:
        room.name = payload.name.strip()
    if payload.slug is not None:
        room.slug = payload.slug.strip()
    if payload.floor_plan_coordinates is not None:
        room.floor_plan_coordinates = payload.floor_plan_coordinates
    if payload.sort_order is not None:
        room.sort_order = payload.sort_order
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="A room with that slug already exists") from None
    db.refresh(room)
    return _room_to_response(room)


@router.delete("/{project_id}/rooms/{room_id}", status_code=204)
def delete_room(
    project_id: str,
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only project owners and editors can delete rooms")

    room = db.scalar(
        select(Room).where(Room.id == room_id, Room.project_id == project_id)
    )
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    db.delete(room)
    db.commit()


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

@router.get("/{project_id}/members", response_model=list[ProjectMemberResponse])
def list_members(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ProjectMemberResponse]:
    _get_project_or_404(project_id, db)
    _get_member_or_403(project_id, current_user, db)
    members = db.scalars(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .options(selectinload(ProjectMember.user))
    ).all()
    return [_member_to_response(m) for m in members]


@router.post("/{project_id}/members", response_model=ProjectMemberResponse, status_code=201)
def add_member(
    project_id: str,
    payload: ProjectMemberAddRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectMemberResponse:
    _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role != "owner":
        raise HTTPException(status_code=403, detail="Only project owners can add members")

    target_user = db.scalar(select(User).where(User.id == payload.user_id))
    if target_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    existing = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == payload.user_id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=400, detail="User is already a member of this project")

    new_member = ProjectMember(project_id=project_id, user_id=payload.user_id, role=payload.role)
    db.add(new_member)
    db.commit()
    db.refresh(new_member)
    new_member.user = target_user
    return _member_to_response(new_member)


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberResponse)
def update_member(
    project_id: str,
    user_id: str,
    payload: ProjectMemberUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectMemberResponse:
    _get_project_or_404(project_id, db)
    caller = _get_member_or_403(project_id, current_user, db)
    if caller is not None and caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only project owners can change member roles")

    member = db.scalar(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .options(selectinload(ProjectMember.user))
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    member.role = payload.role
    db.commit()
    db.refresh(member)
    return _member_to_response(member)


@router.delete("/{project_id}/members/{user_id}", status_code=204)
def remove_member(
    project_id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    _get_project_or_404(project_id, db)
    caller = _get_member_or_403(project_id, current_user, db)
    if caller is not None and caller.role != "owner" and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Only project owners can remove other members")

    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    db.delete(member)
    db.commit()
