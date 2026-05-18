"""Client-side dedupe preflight: tell the browser whether a SHA-256 is
already in the system *before* it transfers any bytes.  Saves the round-trip
of assembling + hashing + uploading a multi-GB LAS only to discover it is a
duplicate."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.database import get_db
from app.models import FileAsset, User
from app.schemas import PrecheckHashRequest, PrecheckHashResponse

router = APIRouter()


@router.post("/precheck-hash", response_model=PrecheckHashResponse)
def precheck_hash(
    payload: PrecheckHashRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PrecheckHashResponse:
    """Tell the client whether this SHA-256 is already in the system.

    Same lookup as `_check_duplicate`, but returns the info instead of raising
    409 so the frontend can warn the user *before* sending any chunks.
    """
    digest = (payload.sha256_hash or "").strip().lower()
    # SHA-256 hex is exactly 64 lowercase hex chars; anything else is junk.
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise HTTPException(status_code=400, detail="Invalid SHA-256 hash")

    existing = db.scalar(
        select(FileAsset)
        .where(FileAsset.sha256_hash == digest)
        .options(joinedload(FileAsset.room))
    )
    if existing is None:
        return PrecheckHashResponse(duplicate=False)

    return PrecheckHashResponse(
        duplicate=True,
        room_name=existing.room.name if existing.room else None,
        capture_date=existing.capture_date,
        display_name=existing.display_name,
    )
