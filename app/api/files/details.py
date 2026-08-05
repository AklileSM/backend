"""On-demand asset metadata for the explorer Details modal."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.database import get_db
from app.models import FileAsset, ProjectMember, Room, User
from app.schemas import FileAssetDetailsResponse

router = APIRouter()


@router.get("/{file_id}/details", response_model=FileAssetDetailsResponse)
def get_file_asset_details(
    file_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileAssetDetailsResponse:
    asset = db.scalar(
        select(FileAsset)
        .options(joinedload(FileAsset.room).joinedload(Room.project))
        .where(FileAsset.id == file_id)
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    room = asset.room
    project = room.project
    membership = None
    if not current_user.is_admin:
        membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == current_user.id,
            )
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="Not a member of this project")

    can_delete = current_user.is_admin or (
        membership is not None and membership.role in {"owner", "editor"}
    )
    return FileAssetDetailsResponse(
        id=asset.id,
        room_id=room.id,
        room_name=room.name,
        room_slug=room.slug,
        project_id=project.id,
        project_name=project.name,
        project_slug=project.slug,
        media_type=asset.media_type,
        display_name=asset.display_name,
        original_name=asset.original_name,
        capture_date=asset.capture_date,
        content_type=asset.content_type,
        file_size=asset.file_size,
        sha256_hash=asset.sha256_hash,
        created_at=asset.created_at,
        metadata=dict(asset.metadata_json or {}),
        can_delete=can_delete,
    )
