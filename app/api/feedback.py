"""Feedback widget endpoint.

Receives a comment + optional screenshots from any user (auth not required),
uploads screenshots to MinIO, and emails the project owner with the details.
No database row is written, the email is the durable record.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import decode_access_token
from app.database import get_db
from app.models import User
from app.services.email import send_feedback_email
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

router = APIRouter()

settings = get_settings()

FEEDBACK_TO = "atm9561@nyu.edu"
MAX_SCREENSHOTS = 10
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024  # 8 MB each

# Use a non-auto-error bearer so anonymous testers can still submit.
_optional_bearer = HTTPBearer(auto_error=False)


def _optional_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    if creds is None or creds.scheme.lower() != "bearer":
        return None
    try:
        payload = decode_access_token(creds.credentials)
    except ValueError:
        return None
    if payload.get("type") != "access":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        return None
    return user


@router.post("", status_code=204)
async def submit_feedback(
    comment: str = Form(""),
    page_url: str = Form(""),
    viewport: str = Form(""),
    user_agent: str = Form(""),
    screenshots: list[UploadFile] = File(default_factory=list),
    current_user: User | None = Depends(_optional_current_user),
) -> None:
    comment = (comment or "").strip()
    if not comment and not screenshots:
        raise HTTPException(
            status_code=400,
            detail="Provide a comment or at least one screenshot.",
        )

    if len(screenshots) > MAX_SCREENSHOTS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many screenshots, max {MAX_SCREENSHOTS}.",
        )

    submission_id = uuid.uuid4().hex
    screenshot_urls: list[str] = []

    for idx, upload in enumerate(screenshots):
        data = await upload.read()
        if not data:
            continue
        if len(data) > MAX_SCREENSHOT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Screenshot {idx + 1} exceeds the {MAX_SCREENSHOT_BYTES // (1024 * 1024)}MB limit.",
            )
        ext = os.path.splitext(upload.filename or "")[1].lower() or ".png"
        object_name = f"{submission_id}/screenshot-{idx + 1:02d}{ext}"
        try:
            storage_service.upload_bytes(
                bucket_name=settings.minio_bucket_feedback,
                object_name=object_name,
                data=data,
                content_type=upload.content_type or "application/octet-stream",
            )
            url = storage_service.get_presigned_url(
                settings.minio_bucket_feedback,
                object_name,
            )
            screenshot_urls.append(url)
        except Exception:
            logger.exception("Failed to store feedback screenshot %s", object_name)
            # Continue with what we have, the email still goes out.

    submitted_by = (
        f"{current_user.username} ({current_user.email})"
        if current_user and current_user.email
        else (current_user.username if current_user else "anonymous tester")
    )
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        send_feedback_email(
            to=FEEDBACK_TO,
            comment=comment,
            submitted_by=submitted_by,
            submitted_at=submitted_at,
            page_url=page_url or "(unknown)",
            viewport=viewport or "(unknown)",
            user_agent=user_agent or "(unknown)",
            screenshot_urls=screenshot_urls,
        )
    except Exception:
        logger.exception("Failed to send feedback email")
        raise HTTPException(
            status_code=500,
            detail="Feedback could not be delivered, please try again.",
        ) from None
