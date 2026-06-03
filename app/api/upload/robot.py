"""POST /robot, upload endpoint for autonomous agents (the Go2W).

This wraps the shared ``_store_upload`` core from ``single.py`` but accepts the
structured mission metadata a human upload does not provide: the capture pose,
mission id, and sensor type. It resolves the target room from human-readable
``project_slug`` + ``room_slug`` (what the robot's mission scheduler knows)
rather than the opaque ``room_id`` the browser uses.

No new storage or conversion code lives here: images/videos/PDFs stream through
the same path as ``/single``, and point clouds reuse the existing
PotreeConverter pool. The only additions are the robot JWT gate, slug-based
room lookup, robot provenance written into ``file_assets.metadata_json``, and a
``source: robot`` tag on the activity-feed entry
"""

from __future__ import annotations

import json
import logging
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_robot
from app.database import get_db
from app.models import Project, Room, User
from app.schemas import UploadResponse

from .common import _ALLOWED_MEDIA, _require_can_upload
from .single import _store_upload

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/robot", response_model=UploadResponse)
async def upload_robot(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    project_slug: str = Form(...),
    room_slug: str = Form(...),
    media_type: str = Form(...),
    capture_date: date = Form(...),
    robot_meta: str | None = Form(None),
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> UploadResponse:
    if media_type not in _ALLOWED_MEDIA:
        raise HTTPException(status_code=400, detail="Invalid media_type")

    project = db.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    room = db.scalar(
        select(Room).where(Room.project_id == project.id, Room.slug == room_slug)
    )
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found in project")

    # Robots authenticate as a normal user; they must be an owner/editor member
    # of the project (or admin) just like a human uploader.
    _require_can_upload(current_user, room, db)

    parsed_meta: dict = {}
    if robot_meta:
        try:
            parsed_meta = json.loads(robot_meta)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="robot_meta must be valid JSON") from None
        if not isinstance(parsed_meta, dict):
            raise HTTPException(status_code=400, detail="robot_meta must be a JSON object")

    return await _store_upload(
        file=file,
        room=room,
        media_type=media_type,
        capture_date=capture_date,
        background_tasks=background_tasks,
        current_user=current_user,
        db=db,
        extra_metadata={"robot": parsed_meta},
        activity_source="robot",
    )
