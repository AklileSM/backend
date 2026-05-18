"""Comparison report drafts (`ComparisonDraft`).

Lifecycle:

    create  ─►  update*  ─►  (one or more drafts) ─►  publish ─► Report
                                                      (all consolidated
                                                       drafts deleted)

A comparison report is built from the Compare page's two-image side-by-side
viewer. Each ComparisonDraft holds the state for one pair; users can save
several over time and then consolidate the ones they want into a single
published Report PDF. The PDF assembly happens client-side with pdf-lib;
the backend just stores the finished blob and clears the source drafts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response as PlainResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import ComparisonDraft, FileAsset, Report, User
from app.schemas import (
    ComparisonDraftCreateRequest,
    ComparisonDraftDetailResponse,
    ComparisonDraftResponse,
    ComparisonDraftUpdateRequest,
    ReportResponse,
)
from app.services.storage import storage_service

from .common import (
    _coerce_str_list,
    _log_report_published,
    _parse_draft_ids_json,
    _parse_flags_json,
    _parse_http_range,
    _report_to_response,
    _resolve_project_for_user,
)

router = APIRouter()
settings = get_settings()


# ---------------------------------------------------------------------------
# Local helpers (label / response shape / PDF cleanup)
# ---------------------------------------------------------------------------


def _draft_label_from_state(state: dict | None) -> str | None:
    if not isinstance(state, dict):
        return None
    left = state.get("left")
    right = state.get("right")
    ln = ""
    rn = ""
    if isinstance(left, dict):
        ln = str(left.get("displayFileName") or "").strip()
    if isinstance(right, dict):
        rn = str(right.get("displayFileName") or "").strip()
    if ln and rn:
        return f"{ln} vs {rn}"
    if ln:
        return ln
    if rn:
        return rn
    return None


def _draft_to_response(draft: ComparisonDraft) -> ComparisonDraftResponse:
    pdf_url = None
    if draft.pdf_bucket_name and draft.pdf_object_name:
        pdf_url = f"/api/reports/comparison-drafts/{draft.id}/pdf"
    st = draft.state_json if isinstance(draft.state_json, dict) else None
    return ComparisonDraftResponse(
        id=draft.id,
        file_id=draft.file_id,
        label=_draft_label_from_state(st),
        manual_observations=draft.manual_observations,
        flags=_coerce_str_list(draft.flags),
        pdf_url=pdf_url,
        created_at=draft.created_at,
    )


def _draft_to_detail_response(draft: ComparisonDraft) -> ComparisonDraftDetailResponse:
    base = _draft_to_response(draft)
    return ComparisonDraftDetailResponse(
        **base.model_dump(),
        state_json=draft.state_json if isinstance(draft.state_json, dict) else None,
    )


def _strip_draft_pdf_if_stored(draft: ComparisonDraft) -> None:
    """Remove draft PDF from object storage and clear keys (drafts are state-only until publish)."""
    b = (draft.pdf_bucket_name or "").strip()
    o = (draft.pdf_object_name or "").strip()
    if b and o:
        storage_service.remove_object_best_effort(b, o)
    draft.pdf_bucket_name = ""
    draft.pdf_object_name = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/comparison-drafts", response_model=list[ComparisonDraftResponse])
def list_comparison_drafts(
    project_slug: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ComparisonDraftResponse]:
    stmt = (
        select(ComparisonDraft)
        .where(ComparisonDraft.created_by == current_user.id)
        .order_by(ComparisonDraft.created_at.asc())
    )
    if project_slug:
        from app.models import Room  # local — only needed when filtering
        project = _resolve_project_for_user(db, project_slug, current_user)
        stmt = (
            stmt.join(FileAsset, ComparisonDraft.file_id == FileAsset.id)
            .join(Room, FileAsset.room_id == Room.id)
            .where(Room.project_id == project.id)
        )
    drafts = db.scalars(stmt).all()
    return [_draft_to_response(d) for d in drafts]


@router.get("/comparison-drafts/{draft_id}", response_model=ComparisonDraftDetailResponse)
def get_comparison_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ComparisonDraftDetailResponse:
    draft = db.scalar(select(ComparisonDraft).where(ComparisonDraft.id == draft_id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to access this draft")
    return _draft_to_detail_response(draft)


@router.get("/comparison-drafts/{draft_id}/pdf", response_model=None)
def get_comparison_draft_pdf(
    draft_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    draft = db.scalar(select(ComparisonDraft).where(ComparisonDraft.id == draft_id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to access this draft")

    b = (draft.pdf_bucket_name or "").strip()
    o = (draft.pdf_object_name or "").strip()
    if not b or not o:
        raise HTTPException(
            status_code=404,
            detail="This draft has no PDF. Open it in Compare to view or edit.",
        )

    try:
        total = storage_service.stat_object_size(b, o)
    except Exception:
        raise HTTPException(status_code=404, detail="Draft PDF not found in storage")

    range_header = request.headers.get("range")
    parsed = _parse_http_range(range_header, total)
    media_type = "application/pdf"

    if parsed is not None:
        first, last = parsed
        try:
            chunk = storage_service.get_object_range_bytes(
                b,
                o,
                first,
                last,
            )
        except Exception:
            raise HTTPException(status_code=404, detail="Draft PDF not found in storage")
        return PlainResponse(
            content=chunk,
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {first}-{last}/{total}",
                "Content-Length": str(len(chunk)),
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=300",
            },
        )

    data = storage_service.get_object_bytes(b, o)
    return PlainResponse(
        content=data,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post("/comparison-drafts", response_model=ComparisonDraftDetailResponse)
def create_comparison_draft(
    body: ComparisonDraftCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ComparisonDraftDetailResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == body.file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    draft_id = str(uuid.uuid4())
    draft = ComparisonDraft(
        id=draft_id,
        file_id=body.file_id,
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
    return _draft_to_detail_response(draft)


@router.patch("/comparison-drafts/{draft_id}", response_model=ComparisonDraftDetailResponse)
def update_comparison_draft(
    draft_id: str,
    body: ComparisonDraftUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ComparisonDraftDetailResponse:
    draft = db.scalar(select(ComparisonDraft).where(ComparisonDraft.id == draft_id))
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

    if body.manual_observations is not None:
        draft.manual_observations = body.manual_observations
    if body.flags is not None:
        draft.flags = body.flags
    if body.state is not None:
        _strip_draft_pdf_if_stored(draft)
        draft.state_json = body.state

    db.add(draft)
    db.commit()
    db.refresh(draft)
    return _draft_to_detail_response(draft)


@router.delete("/comparison-drafts/{draft_id}", status_code=204, response_model=None)
def delete_comparison_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    draft = db.scalar(select(ComparisonDraft).where(ComparisonDraft.id == draft_id))
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


@router.post("/comparison-drafts/publish", response_model=ReportResponse)
async def publish_comparison_drafts(
    file: UploadFile = File(...),
    file_id: str = Form(...),
    draft_ids_json: str | None = Form(None),
    manual_observations: str | None = Form(None),
    flags_json: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReportResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    draft_ids = _parse_draft_ids_json(draft_ids_json)
    if not draft_ids:
        raise HTTPException(status_code=400, detail="No comparison drafts selected for publish")

    drafts = db.scalars(
        select(ComparisonDraft).where(
            ComparisonDraft.created_by == current_user.id,
            ComparisonDraft.id.in_(draft_ids),
        )
    ).all()
    if len(drafts) != len(set(draft_ids)):
        raise HTTPException(status_code=400, detail="Some comparison drafts were not found")

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
        ai_description=None,
        manual_observations=manual_observations,
        flags=flags,
        screenshots=None,
        pdf_bucket_name=bucket,
        pdf_object_name=object_name,
        created_by=current_user.id,
        created_at=datetime.utcnow(),
    )
    db.add(report)

    for draft in drafts:
        b = (draft.pdf_bucket_name or "").strip()
        o = (draft.pdf_object_name or "").strip()
        if b and o:
            storage_service.remove_object_best_effort(b, o)
        db.delete(draft)

    db.commit()
    db.refresh(report)
    _log_report_published(db, report=report, file_asset=file_asset, current_user=current_user)
    return _report_to_response(report)
