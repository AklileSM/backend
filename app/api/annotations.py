import uuid
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import Annotation, FileAsset, Room, User
from app.schemas import AnnotationCreateRequest, AnnotationResponse, AnnotationUpdateRequest
from app.services.activity import log_activity
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()

# Flag taxonomy is shared with reports (frontend lib/observationReportFlags.ts).
# Empty / null is allowed too — annotations without a category just render in
# the neutral pin color.
_ALLOWED_FLAGS = frozenset({"safety", "quality", "delayed"})
# Accept the common browser-renderable image types. Anything else is rejected
# so we don't end up serving a .heic or .tiff that the <img> tag won't display.
_ALLOWED_ATTACHMENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


def _attachment_url(annotation: Annotation) -> str | None:
    if not annotation.attachment_object_name:
        return None
    # Served through a backend-proxied endpoint so we don't need a presigned
    # URL with an expiry baked into the response. Same pattern as file
    # thumbnails.
    return f"/api/annotations/{annotation.id}/attachment"


def _to_response(annotation: Annotation) -> AnnotationResponse:
    return AnnotationResponse(
        id=annotation.id,
        file_id=annotation.file_id,
        annotation_type=annotation.annotation_type,
        data=annotation.data,
        flag=annotation.flag,
        linked_annotation_id=annotation.linked_annotation_id,
        attachment_url=_attachment_url(annotation),
        created_at=annotation.created_at,
    )


def _validate_flag(flag: str | None) -> str | None:
    """Trim, lowercase, and reject anything outside the taxonomy. None passes."""
    if flag is None:
        return None
    cleaned = flag.strip().lower()
    if not cleaned:
        return None
    if cleaned not in _ALLOWED_FLAGS:
        raise HTTPException(status_code=400, detail=f"Unknown flag '{cleaned}'")
    return cleaned


def _validate_link(
    db: Session, file_id: str, linked_id: str | None, self_id: str | None
) -> str | None:
    """Verify a linked_annotation_id points at another annotation on the SAME
    file_id and isn't the annotation linking to itself. Returns the normalised
    id or None.
    """
    if linked_id is None:
        return None
    if linked_id == self_id:
        raise HTTPException(status_code=400, detail="An annotation can't link to itself")
    target = db.scalar(select(Annotation).where(Annotation.id == linked_id))
    if target is None:
        raise HTTPException(status_code=404, detail="Linked annotation not found")
    if target.file_id != file_id:
        raise HTTPException(status_code=400, detail="Linked annotation must belong to the same file")
    return linked_id


@router.get("/file/{file_id}", response_model=list[AnnotationResponse])
def list_annotations(file_id: str, db: Session = Depends(get_db)) -> list[AnnotationResponse]:
    annotations = db.scalars(
        select(Annotation).where(Annotation.file_id == file_id).order_by(Annotation.created_at.asc())
    ).all()
    return [_to_response(a) for a in annotations]


@router.post("", response_model=AnnotationResponse)
@router.post("/", response_model=AnnotationResponse)
def create_annotation(
    payload: AnnotationCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnnotationResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    annotation = Annotation(
        file_id=payload.file_id,
        annotation_type=payload.annotation_type,
        data=payload.data,
        flag=_validate_flag(payload.flag),
        linked_annotation_id=_validate_link(db, payload.file_id, payload.linked_annotation_id, None),
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)

    # Resolve the project + room for the activity feed. Cheap because we
    # already have file_asset in hand and Room is a single PK lookup.
    room = db.scalar(select(Room).where(Room.id == file_asset.room_id))
    if room is not None:
        preview = ""
        if isinstance(annotation.data, dict):
            raw = annotation.data.get("text")
            if isinstance(raw, str):
                preview = raw.strip()[:120]
        log_activity(
            db,
            project_id=room.project_id,
            actor=current_user,
            action="annotation.create",
            target_type="annotation",
            target_id=annotation.id,
            metadata={
                "file_id": file_asset.id,
                "file_name": file_asset.display_name,
                "room_name": room.name,
                "flag": annotation.flag,
                "preview": preview,
            },
        )
    return _to_response(annotation)


@router.patch("/{annotation_id}", response_model=AnnotationResponse)
def update_annotation(
    annotation_id: str,
    payload: AnnotationUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnnotationResponse:
    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")

    if payload.annotation_type is not None and str(payload.annotation_type).strip():
        annotation.annotation_type = str(payload.annotation_type).strip()
    if payload.data is not None:
        annotation.data = payload.data
    if payload.flag is not None:
        annotation.flag = _validate_flag(payload.flag)
    # `clear_link` is the explicit "remove the link" path; `linked_annotation_id`
    # left as None means "no change" so existing links survive a partial PATCH.
    if payload.clear_link:
        annotation.linked_annotation_id = None
    elif payload.linked_annotation_id is not None:
        annotation.linked_annotation_id = _validate_link(
            db, annotation.file_id, payload.linked_annotation_id, annotation.id
        )

    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _to_response(annotation)


@router.delete("/{annotation_id}", status_code=204)
def delete_annotation(
    annotation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    # Tidy up any attachment in MinIO before the row goes.
    if annotation.attachment_bucket_name and annotation.attachment_object_name:
        storage_service.remove_object_best_effort(
            annotation.attachment_bucket_name, annotation.attachment_object_name
        )
    db.delete(annotation)
    db.commit()


@router.post("/{annotation_id}/attachment", response_model=AnnotationResponse)
async def upload_annotation_attachment(
    annotation_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnnotationResponse:
    """Attach (or replace) an image on an annotation.

    Stored in the dedicated annotation_attachments bucket — not in the regular
    media bucket — so it doesn't clutter the room/date file grid.
    """
    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_ATTACHMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Attachment must be one of: {', '.join(sorted(_ALLOWED_ATTACHMENT_TYPES))}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty attachment")
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="Attachment too large")

    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }[content_type]
    bucket = settings.minio_bucket_annotation_attachments
    object_name = f"{annotation.id}/{uuid.uuid4().hex}{extension}"

    # Drop any previous attachment first so we don't leak objects on replace.
    if annotation.attachment_bucket_name and annotation.attachment_object_name:
        storage_service.remove_object_best_effort(
            annotation.attachment_bucket_name, annotation.attachment_object_name
        )

    storage_service.upload_bytes(
        bucket_name=bucket,
        object_name=object_name,
        data=data,
        content_type=content_type,
    )
    annotation.attachment_bucket_name = bucket
    annotation.attachment_object_name = object_name
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _to_response(annotation)


@router.delete("/{annotation_id}/attachment", response_model=AnnotationResponse)
def delete_annotation_attachment(
    annotation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnnotationResponse:
    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.attachment_bucket_name and annotation.attachment_object_name:
        storage_service.remove_object_best_effort(
            annotation.attachment_bucket_name, annotation.attachment_object_name
        )
    annotation.attachment_bucket_name = None
    annotation.attachment_object_name = None
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _to_response(annotation)


@router.get("/{annotation_id}/attachment")
def get_annotation_attachment(
    annotation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream an annotation's attached image back to the browser.

    Goes through the backend so we don't have to embed presigned URLs (which
    expire) in annotation responses.
    """
    from fastapi.responses import StreamingResponse

    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if not (annotation.attachment_bucket_name and annotation.attachment_object_name):
        raise HTTPException(status_code=404, detail="Attachment not found")

    stream = storage_service.stream_object(
        annotation.attachment_bucket_name, annotation.attachment_object_name
    )
    return StreamingResponse(
        BytesIO(stream.read()),
        media_type=stream.headers.get("content-type", "application/octet-stream"),
    )
