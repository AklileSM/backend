"""Published reports (the `Report` rows).

All routes here are creator-scoped — even admins cannot list, view, download,
or delete another user's reports. The `with-pdf` route is the shortcut path
used by the frontend when the user publishes without ever saving a draft
first (see also `viewer_drafts.publish_viewer_draft` for the draft-aware
variant).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response as PlainResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Report, Room, User
from app.schemas import ReportCreateRequest, ReportResponse
from app.services.storage import storage_service

from .common import (
    _coerce_str_list,
    _log_report_published,
    _parse_flags_json,
    _parse_http_range,
    _report_to_response,
    _resolve_project_for_user,
)

router = APIRouter()
settings = get_settings()


@router.get("", response_model=list[ReportResponse])
@router.get("/", response_model=list[ReportResponse])
def list_reports(
    project_slug: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ReportResponse]:
    stmt = (
        select(Report)
        .where(Report.created_by == current_user.id)
        .order_by(Report.created_at.desc())
    )
    if project_slug:
        project = _resolve_project_for_user(db, project_slug, current_user)
        stmt = (
            stmt.join(FileAsset, Report.file_id == FileAsset.id)
            .join(Room, FileAsset.room_id == Room.id)
            .where(Room.project_id == project.id)
        )
    reports = db.scalars(stmt).all()
    return [_report_to_response(r) for r in reports]


@router.get("/{report_id}/pdf", response_model=None)
def get_report_pdf(
    report_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = db.scalar(select(Report).where(Report.id == report_id))
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to access this report")
    if not report.pdf_bucket_name or not report.pdf_object_name:
        raise HTTPException(status_code=404, detail="Report PDF not available")

    try:
        total = storage_service.stat_object_size(report.pdf_bucket_name, report.pdf_object_name)
    except Exception:
        raise HTTPException(status_code=404, detail="Report PDF not found in storage")

    range_header = request.headers.get("range")
    parsed = _parse_http_range(range_header, total)
    media_type = "application/pdf"

    if parsed is not None:
        first, last = parsed
        try:
            chunk = storage_service.get_object_range_bytes(
                report.pdf_bucket_name,
                report.pdf_object_name,
                first,
                last,
            )
        except Exception:
            raise HTTPException(status_code=404, detail="Report PDF not found in storage")
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

    _INLINE_MAX = 100 * 1024 * 1024
    if total <= _INLINE_MAX:
        try:
            data = storage_service.get_object_bytes(report.pdf_bucket_name, report.pdf_object_name)
        except Exception:
            raise HTTPException(status_code=404, detail="Report PDF not found in storage")
        return PlainResponse(
            content=data,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(data)),
                "Cache-Control": "private, max-age=300",
            },
        )

    stream = storage_service.stream_object(report.pdf_bucket_name, report.pdf_object_name)

    def body():
        try:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                yield chunk
        finally:
            stream.close()
            stream.release_conn()

    return StreamingResponse(
        body(),
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post("", response_model=ReportResponse)
@router.post("/", response_model=ReportResponse)
def create_report(
    payload: ReportCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReportResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    report = Report(
        file_id=payload.file_id,
        ai_description=payload.ai_description,
        manual_observations=payload.manual_observations,
        flags=payload.flags,
        screenshots=payload.screenshots,
        created_by=current_user.id,
        created_at=datetime.utcnow(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    _log_report_published(db, report=report, file_asset=file_asset, current_user=current_user)
    return _report_to_response(report)


@router.delete("/{report_id}", status_code=204, response_model=None)
def delete_report(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    report = db.scalar(select(Report).where(Report.id == report_id))
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to delete this report")

    if report.pdf_bucket_name and report.pdf_object_name:
        storage_service.remove_object_best_effort(report.pdf_bucket_name, report.pdf_object_name)

    db.delete(report)
    db.commit()


@router.post("/with-pdf", response_model=ReportResponse)
async def create_report_with_pdf(
    file: UploadFile = File(...),
    file_id: str = Form(...),
    label: str | None = Form(None),
    ai_description: str | None = Form(None),
    manual_observations: str | None = Form(None),
    flags_json: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReportResponse:
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
    db.commit()
    db.refresh(report)

    _log_report_published(db, report=report, file_asset=file_asset, current_user=current_user)
    return _report_to_response(report)
