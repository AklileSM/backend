from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.database import get_db
from app.models import Project, User
from app.schemas import (
    AdminUserResponse,
    AdminUserUpdateRequest,
    ProjectResponse,
)

router = APIRouter()


def _user_to_admin_response(u: User) -> AdminUserResponse:
    return AdminUserResponse(
        id=u.id,
        username=u.username,
        email=u.email,
        is_admin=u.is_admin,
        is_active=u.is_active,
        created_at=u.created_at,
    )


def _project_to_response(p: Project) -> ProjectResponse:
    return ProjectResponse(
        id=p.id,
        name=p.name,
        slug=p.slug,
        description=p.description,
        location=p.location,
        status=p.status,
        owner_id=p.owner_id,
        created_at=p.created_at,
        updated_at=p.updated_at or p.created_at,
    )


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[AdminUserResponse]:
    users = db.scalars(select(User).order_by(User.created_at.asc())).all()
    return [_user_to_admin_response(u) for u in users]


@router.get("/user-search", response_model=list[AdminUserResponse])
def search_users(
    q: str = Query(..., min_length=1),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AdminUserResponse]:
    like = f"%{q}%"
    users = db.scalars(
        select(User)
        .where(
            User.is_active == True,  # noqa: E712
            or_(User.username.ilike(like), User.email.ilike(like)),
        )
        .order_by(User.username.asc())
        .limit(10)
    ).all()
    return [_user_to_admin_response(u) for u in users]


@router.get("/users/{user_id}", response_model=AdminUserResponse)
def get_user(
    user_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUserResponse:
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_to_admin_response(user)


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
def update_user(
    user_id: str,
    payload: AdminUserUpdateRequest,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUserResponse:
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.is_admin is not None:
        if user.id == current_admin.id and not payload.is_admin:
            raise HTTPException(status_code=400, detail="Cannot remove your own admin privileges")
        user.is_admin = payload.is_admin
    if payload.is_active is not None:
        if user.id == current_admin.id and not payload.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
        user.is_active = payload.is_active
    if payload.email is not None:
        user.email = payload.email or None
    db.commit()
    db.refresh(user)
    return _user_to_admin_response(user)


@router.get("/projects", response_model=list[ProjectResponse])
def list_all_projects(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ProjectResponse]:
    projects = db.scalars(select(Project).order_by(Project.name.asc())).all()
    return [_project_to_response(p) for p in projects]


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    project = db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    db.delete(project)
    db.commit()
