from datetime import datetime

import ast
import io
import mimetypes
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import Project, ProjectActivity, ProjectMember, Room, User
from app.schemas import (
    ProjectActivityEntry,
    ProjectCreateRequest,
    ProjectMemberAddRequest,
    ProjectMemberResponse,
    ProjectMemberUpdateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
    RoomCreateRequest,
    RobotMapResponse,
    RoomResponse,
    RoomUpdateRequest,
)
from app.services.activity import log_activity
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()

_ALLOWED_FLOORPLAN_TYPES = {"image/jpeg", "image/png", "image/webp"}
_FLOORPLAN_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_SLUG_MAX_LEN = 100
_SLUG_SUFFIX_LIMIT = 1000


def _slugify(name: str) -> str:
    """Lowercase, hyphenate, strip non-[a-z0-9-] — matches frontend autoSlug."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    return s.strip("-")[:_SLUG_MAX_LEN]


def _unique_project_slug(db: Session, base: str) -> str:
    """Return base, or base-2, base-3... until globally free in projects.slug."""
    if not base:
        base = "project"
    candidate = base
    for n in range(2, _SLUG_SUFFIX_LIMIT + 1):
        exists = db.scalar(select(Project.id).where(Project.slug == candidate))
        if exists is None:
            return candidate
        suffix = f"-{n}"
        candidate = f"{base[: _SLUG_MAX_LEN - len(suffix)]}{suffix}"
    raise HTTPException(status_code=400, detail="Could not allocate a unique slug")


def _unique_room_slug(db: Session, project_id: str, base: str) -> str:
    """Return base, or base-2... until free within a single project."""
    if not base:
        base = "room"
    candidate = base
    for n in range(2, _SLUG_SUFFIX_LIMIT + 1):
        exists = db.scalar(
            select(Room.id).where(Room.project_id == project_id, Room.slug == candidate)
        )
        if exists is None:
            return candidate
        suffix = f"-{n}"
        candidate = f"{base[: _SLUG_MAX_LEN - len(suffix)]}{suffix}"
    raise HTTPException(status_code=400, detail="Could not allocate a unique room slug")


def _project_to_response(p: Project) -> ProjectResponse:
    floorplan_url: str | None = None
    if p.floorplan_url:
        floorplan_url = f"/api/projects/{p.id}/floorplan"
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


def _parse_robot_map_yaml(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Robot map YAML must be UTF-8 text") from exc
    data: dict[str, object] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if key in {"resolution", "occupied_thresh", "free_thresh"}:
            data[key] = float(raw_value)
        elif key == "negate":
            data[key] = int(raw_value)
        elif key == "origin":
            data[key] = ast.literal_eval(raw_value)
        else:
            data[key] = raw_value.strip("\"'")

    resolution = data.get("resolution")
    origin = data.get("origin")
    if not isinstance(resolution, float) or not isinstance(origin, list) or len(origin) < 2:
        raise HTTPException(status_code=400, detail="Robot map YAML must include resolution and origin")
    return {
        "resolution": resolution,
        "origin_x": float(origin[0]),
        "origin_y": float(origin[1]),
        "origin_yaw": float(origin[2]) if len(origin) > 2 else 0.0,
    }


def _robot_map_response(project: Project) -> RobotMapResponse:
    meta = project.robot_map_json if isinstance(project.robot_map_json, dict) else None
    if not meta:
        raise HTTPException(status_code=404, detail="No robot map uploaded")
    version = str(meta.get("uploaded_at") or project.updated_at or "")
    return RobotMapResponse(
        image_url=f"/api/projects/{project.id}/robot-map/image?v={version}",
        width=int(meta["width"]),
        height=int(meta["height"]),
        resolution=float(meta["resolution"]),
        origin_x=float(meta["origin_x"]),
        origin_y=float(meta["origin_y"]),
        origin_yaw=float(meta.get("origin_yaw") or 0.0),
        frame=str(meta.get("frame") or "map"),
        yaml_object_name=meta.get("yaml_object_name"),
        image_object_name=meta.get("image_object_name"),
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


@router.post("", response_model=ProjectResponse, status_code=201)
@router.post("/", response_model=ProjectResponse, status_code=201)
def create_project(
    payload: ProjectCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    name = payload.name.strip()

    name_taken = db.scalar(
        select(Project.id).where(
            Project.owner_id == current_user.id,
            func.lower(Project.name) == name.lower(),
        )
    )
    if name_taken is not None:
        raise HTTPException(
            status_code=400,
            detail=f"You already have a project named '{name}'",
        )

    slug = _unique_project_slug(db, _slugify(name))

    project = Project(
        name=name,
        slug=slug,
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
        raise HTTPException(status_code=400, detail="Project name conflicts with an existing one") from None

    db.add(ProjectMember(project_id=project.id, user_id=current_user.id, role="owner"))
    db.commit()
    db.refresh(project)
    return _project_to_response(project)


# ---------------------------------------------------------------------------
# Slug-based lookup, must be defined before /{project_id}
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
        new_name = payload.name.strip()
        if new_name.lower() != project.name.lower():
            clash = db.scalar(
                select(Project.id).where(
                    Project.owner_id == project.owner_id,
                    func.lower(Project.name) == new_name.lower(),
                    Project.id != project.id,
                )
            )
            if clash is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"You already have a project named '{new_name}'",
                )
        project.name = new_name
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

@router.get("/{project_id}/floorplan", response_model=None)
def get_floorplan(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    project = _get_project_or_404(project_id, db)
    if not project.floorplan_url:
        raise HTTPException(status_code=404, detail="No floorplan uploaded")
    try:
        stat = storage_service.stat_object(settings.minio_bucket_floorplans, project.floorplan_url)
    except Exception:
        raise HTTPException(status_code=404, detail="Floorplan not found in storage")

    etag = f'"{stat.etag}"' if stat.etag else None
    if etag and request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    try:
        data = storage_service.get_object_bytes(settings.minio_bucket_floorplans, project.floorplan_url)
    except Exception:
        raise HTTPException(status_code=404, detail="Floorplan not found in storage")

    content_type = mimetypes.guess_type(project.floorplan_url)[0] or "image/jpeg"
    headers = {"Content-Length": str(len(data)), "Cache-Control": "public, max-age=86400"}
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type=content_type, headers=headers)


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
# Robot map
# ---------------------------------------------------------------------------

@router.get("/{project_id}/robot-map", response_model=RobotMapResponse)
def get_robot_map(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMapResponse:
    project = _get_project_or_404(project_id, db)
    _get_member_or_403(project_id, current_user, db)
    return _robot_map_response(project)


@router.get("/{project_id}/robot-map/image", response_model=None)
def get_robot_map_image(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    project = _get_project_or_404(project_id, db)
    meta = project.robot_map_json if isinstance(project.robot_map_json, dict) else None
    if not meta or not meta.get("image_object_name"):
        raise HTTPException(status_code=404, detail="No robot map uploaded")
    object_name = str(meta["image_object_name"])

    try:
        stat = storage_service.stat_object(settings.minio_bucket_floorplans, object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="Robot map image not found in storage")

    etag = f'"{stat.etag}"' if stat.etag else None
    if etag and request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    data = storage_service.get_object_bytes(settings.minio_bucket_floorplans, object_name)
    content_type = mimetypes.guess_type(object_name)[0] or "image/png"
    headers = {"Content-Length": str(len(data)), "Cache-Control": "public, max-age=86400"}
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type=content_type, headers=headers)


@router.post("/{project_id}/robot-map", response_model=RobotMapResponse)
async def upload_robot_map(
    project_id: str,
    yaml_file: UploadFile = File(...),
    image_file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMapResponse:
    project = _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only project owners and editors can upload a robot map")

    yaml_bytes = await yaml_file.read()
    image_bytes = await image_file.read()
    parsed = _parse_robot_map_yaml(yaml_bytes)

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
            out = io.BytesIO()
            image.convert("L").save(out, format="PNG")
            png_bytes = out.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Robot map image must be a readable image/PGM file") from exc

    yaml_object_name = f"{project_id}/robot-map/map.yaml"
    image_object_name = f"{project_id}/robot-map/map.png"
    storage_service.upload_bytes(
        bucket_name=settings.minio_bucket_floorplans,
        object_name=yaml_object_name,
        data=yaml_bytes,
        content_type="text/yaml",
    )
    storage_service.upload_bytes(
        bucket_name=settings.minio_bucket_floorplans,
        object_name=image_object_name,
        data=png_bytes,
        content_type="image/png",
    )

    old_meta = project.robot_map_json if isinstance(project.robot_map_json, dict) else {}
    for old_name in (old_meta.get("yaml_object_name"), old_meta.get("image_object_name")):
        if old_name and old_name not in {yaml_object_name, image_object_name}:
            storage_service.remove_object_best_effort(settings.minio_bucket_floorplans, str(old_name))

    project.robot_map_json = {
        **parsed,
        "width": width,
        "height": height,
        "frame": "map",
        "yaml_object_name": yaml_object_name,
        "image_object_name": image_object_name,
        "source_yaml_name": yaml_file.filename,
        "source_image_name": image_file.filename,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return _robot_map_response(project)


@router.delete("/{project_id}/robot-map", response_model=ProjectResponse)
def delete_robot_map(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectResponse:
    project = _get_project_or_404(project_id, db)
    member = _get_member_or_403(project_id, current_user, db)
    if member is not None and member.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Only project owners and editors can remove the robot map")

    meta = project.robot_map_json if isinstance(project.robot_map_json, dict) else {}
    for object_name in (meta.get("yaml_object_name"), meta.get("image_object_name")):
        if object_name:
            storage_service.remove_object_best_effort(settings.minio_bucket_floorplans, str(object_name))
    project.robot_map_json = None
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

    name = payload.name.strip()
    name_taken = db.scalar(
        select(Room.id).where(
            Room.project_id == project_id,
            func.lower(Room.name) == name.lower(),
        )
    )
    if name_taken is not None:
        raise HTTPException(
            status_code=400,
            detail=f"A room named '{name}' already exists in this project",
        )

    slug = _unique_room_slug(db, project_id, _slugify(name))

    room = Room(
        project_id=project_id,
        name=name,
        slug=slug,
        sort_order=payload.sort_order,
    )
    db.add(room)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Room name conflicts with an existing one") from None
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
        new_name = payload.name.strip()
        if new_name.lower() != room.name.lower():
            clash = db.scalar(
                select(Room.id).where(
                    Room.project_id == project_id,
                    func.lower(Room.name) == new_name.lower(),
                    Room.id != room.id,
                )
            )
            if clash is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"A room named '{new_name}' already exists in this project",
                )
        room.name = new_name
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
    log_activity(
        db,
        project_id=project_id,
        actor=current_user,
        action="member.add",
        target_type="project_member",
        target_id=target_user.id,
        metadata={"added_username": target_user.username, "role": new_member.role},
    )
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
        select(ProjectMember)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .options(selectinload(ProjectMember.user))
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    removed_username = member.user.username if member.user else user_id
    db.delete(member)
    db.commit()
    log_activity(
        db,
        project_id=project_id,
        actor=current_user,
        action="member.remove",
        target_type="project_member",
        target_id=user_id,
        metadata={"removed_username": removed_username},
    )


@router.get("/by-slug/{slug}/activity", response_model=list[ProjectActivityEntry])
def get_project_activity(
    slug: str,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ProjectActivityEntry]:
    """Latest N activity entries for a project, newest first.

    Restricted to project owners and editors (plus global admins). Viewers
    don't see the audit log, it leaks information about who else is
    active in the project, which a read-only collaborator doesn't need.
    Caps `limit` at 200 so a bad client can't pull the whole table.
    """
    project = db.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not current_user.is_admin:
        member = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == current_user.id,
            )
        )
        if member is None or member.role not in ("owner", "editor"):
            raise HTTPException(
                status_code=403,
                detail="Only project owners and editors can view activity",
            )

    capped = max(1, min(limit or 50, 200))
    rows = db.scalars(
        select(ProjectActivity)
        .where(ProjectActivity.project_id == project.id)
        .order_by(ProjectActivity.created_at.desc())
        .limit(capped)
    ).all()
    return [
        ProjectActivityEntry(
            id=r.id,
            project_id=r.project_id,
            user_id=r.user_id,
            username=r.username,
            action=r.action,
            target_type=r.target_type,
            target_id=r.target_id,
            metadata=r.metadata_json,
            created_at=r.created_at,
        )
        for r in rows
    ]
