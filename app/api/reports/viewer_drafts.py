"""Viewer report drafts (`ViewerReportDraft`).

Lifecycle:

    create  ─►  update*  ─►  publish  ─►  Report row (draft deleted)

Each draft is bound to exactly one `FileAsset`. The frontend opens a viewer
(static, panorama, point cloud), edits the report builder, optionally saves
a draft, then either publishes (this module) or never comes back. Publishing
generates the PDF client-side and uploads it as a finished blob.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Report, User, ViewerReportDraft
from app.schemas import (
    ViewerDraftCreateRequest,
    ViewerDraftDetailResponse,
    ViewerDraftResponse,
    ViewerDraftUpdateRequest,
    ReportResponse,
)
from app.services.storage import storage_service

from .common import (
    _coerce_str_list,
    _log_report_published,
    _parse_flags_json,
    _report_to_response,
    _resolve_project_for_user,
)

router = APIRouter()
settings = get_settings()


# ---------------------------------------------------------------------------
# Local helpers (label / response shape / PDF cleanup)
# ---------------------------------------------------------------------------

_VIEWER_KIND_LABELS: dict[str, str] = {
    "static_360": "Static",
    "static_room": "Static room",
    "interactive_360": "Interactive",
    "interactive_room": "Interactive room",
    "static_pcd": "Point cloud",
}


def _viewer_draft_label_from_state(state: dict | None, viewer_kind: str) -> str | None:
    name = ""
    if isinstance(state, dict):
        name = str(state.get("displayFileName") or "").strip()
    prefix = _VIEWER_KIND_LABELS.get(viewer_kind, viewer_kind)
    if name:
        return f"{prefix}: {name}"
    return prefix


def _viewer_draft_to_response(draft: ViewerReportDraft) -> ViewerDraftResponse:
    st = draft.state_json if isinstance(draft.state_json, dict) else None
    return ViewerDraftResponse(
        id=draft.id,
        file_id=draft.file_id,
        viewer_kind=draft.viewer_kind,
        label=_viewer_draft_label_from_state(st, draft.viewer_kind),
        manual_observations=draft.manual_observations,
        flags=_coerce_str_list(draft.flags),
        created_at=draft.created_at,
    )


def _viewer_draft_to_detail_response(draft: ViewerReportDraft) -> ViewerDraftDetailResponse:
    base = _viewer_draft_to_response(draft)
    return ViewerDraftDetailResponse(
        **base.model_dump(),
        state_json=draft.state_json if isinstance(draft.state_json, dict) else None,
    )


def _strip_viewer_draft_pdf_if_stored(draft: ViewerReportDraft) -> None:
    b = (draft.pdf_bucket_name or "").strip()
    o = (draft.pdf_object_name or "").strip()
    if b and o:
        storage_service.remove_object_best_effort(b, o)
    draft.pdf_bucket_name = ""
    draft.pdf_object_name = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/viewer-drafts", response_model=list[ViewerDraftResponse])
def list_viewer_drafts(
    project_slug: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ViewerDraftResponse]:
    stmt = (
        select(ViewerReportDraft)
        .where(ViewerReportDraft.created_by == current_user.id)
        .order_by(ViewerReportDraft.created_at.asc())
    )
    if project_slug:
        from app.models import Room  # local — only needed when filtering
        project = _resolve_project_for_user(db, project_slug, current_user)
        stmt = (
            stmt.join(FileAsset, ViewerReportDraft.file_id == FileAsset.id)
            .join(Room, FileAsset.room_id == Room.id)
            .where(Room.project_id == project.id)
        )
    drafts = db.scalars(stmt).all()
    return [_viewer_draft_to_response(d) for d in drafts]


@router.get("/viewer-drafts/{draft_id}", response_model=ViewerDraftDetailResponse)
def get_viewer_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ViewerDraftDetailResponse:
    draft = db.scalar(select(ViewerReportDraft).where(ViewerReportDraft.id == draft_id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to access this draft")
    return _viewer_draft_to_detail_response(draft)


@router.post("/viewer-drafts", response_model=ViewerDraftDetailResponse)
def create_viewer_draft(
    body: ViewerDraftCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ViewerDraftDetailResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == body.file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    draft_id = str(uuid.uuid4())
    draft = ViewerReportDraft(
        id=draft_id,
        file_id=body.file_id,
        viewer_kind=body.viewer_kind.strip(),
        manual_observations=body.manual_observations,
        flags=body.flags or [],
        state_json=body.state,
        pdf_bucket_name="",
        pdf_object_name="",
        created_by=current_user.id,
        created_at=datetime.utcnow(),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return _viewer_draft_to_detail_response(draft)


@router.patch("/viewer-drafts/{draft_id}", response_model=ViewerDraftDetailResponse)
def update_viewer_draft(
    draft_id: str,
    body: ViewerDraftUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ViewerDraftDetailResponse:
    draft = db.scalar(select(ViewerReportDraft).where(ViewerReportDraft.id == draft_id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to update this draft")

    if body.file_id is not None and str(body.file_id).strip():
        fid = str(body.file_id).strip()
        file_asset = db.scalar(select(FileAsset).where(FileAsset.id == fid))
        if file_asset is None:
            raise HTTPException(status_code=404, detail="File not found")
        draft.file_id = fid

    if body.viewer_kind is not None and str(body.viewer_kind).strip():
        draft.viewer_kind = str(body.viewer_kind).strip()[:32]

    if body.manual_observations is not None:
        draft.manual_observations = body.manual_observations
    if body.flags is not None:
        draft.flags = body.flags
    if body.state is not None:
        _strip_viewer_draft_pdf_if_stored(draft)
        draft.state_json = body.state

    db.add(draft)
    db.commit()
    db.refresh(draft)
    return _viewer_draft_to_detail_response(draft)


@router.delete("/viewer-drafts/{draft_id}", status_code=204, response_model=None)
def delete_viewer_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    draft = db.scalar(select(ViewerReportDraft).where(ViewerReportDraft.id == draft_id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to delete this draft")
    b = (draft.pdf_bucket_name or "").strip()
    o = (draft.pdf_object_name or "").strip()
    if b and o:
        storage_service.remove_object_best_effort(b, o)
    db.delete(draft)
    db.commit()


@router.post("/viewer-drafts/{draft_id}/publish", response_model=ReportResponse)
async def publish_viewer_draft(
    draft_id: str,
    file: UploadFile = File(...),
    file_id: str = Form(...),
    label: str | None = Form(None),
    ai_description: str | None = Form(None),
    manual_observations: str | None = Form(None),
    flags_json: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReportResponse:
    draft = db.scalar(select(ViewerReportDraft).where(ViewerReportDraft.id == draft_id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to publish this draft")
    if draft.file_id != file_id.strip():
        raise HTTPException(status_code=400, detail="file_id does not match this draft")

    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="File too large")

    filename = (file.filename or "").lower()
    ct = (file.content_type or "").lower()
    if "pdf" not in ct and not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Expected a PDF file")

    flags = _parse_flags_json(flags_json)
    report_id = str(uuid.uuid4())
    bucket = settings.minio_bucket_reports
    object_name = f"{current_user.id}/{report_id}.pdf"
    storage_service.upload_bytes(
        bucket_name=bucket,
        object_name=object_name,
        data=raw,
        content_type="application/pdf",
    )

    report = Report(
        id=report_id,
        file_id=file_id,
        label=label or None,
        ai_description=ai_description,
        manual_observations=manual_observations,
        flags=flags,
        screenshots=None,
        pdf_bucket_name=bucket,
        pdf_object_name=object_name,
        created_by=current_user.id,
        created_at=datetime.utcnow(),
    )
    db.add(report)

    b = (draft.pdf_bucket_name or "").strip()
    o = (draft.pdf_object_name or "").strip()
    if b and o:
        storage_service.remove_object_best_effort(b, o)
    db.delete(draft)

    db.commit()
    db.refresh(report)
    _log_report_published(db, report=report, file_asset=file_asset, current_user=current_user)
    return _report_to_response(report)
