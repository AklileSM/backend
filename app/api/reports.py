import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import FileAsset, Report, User
from app.schemas import ReportCreateRequest, ReportResponse
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()


def _report_to_response(report: Report) -> ReportResponse:
    pdf_url = None
    if report.pdf_bucket_name and report.pdf_object_name:
        pdf_url = storage_service.get_presigned_url(report.pdf_bucket_name, report.pdf_object_name)
    return ReportResponse(
        id=report.id,
        file_id=report.file_id,
        ai_description=report.ai_description,
        manual_observations=report.manual_observations,
        flags=report.flags or [],
        screenshots=report.screenshots or [],
        created_by=report.created_by,
        pdf_url=pdf_url,
        created_at=report.created_at,
    )


def _parse_flags_json(flags_json: str | None) -> list[str]:
    if flags_json is None or not str(flags_json).strip():
        return []
    try:
        data = json.loads(flags_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="flags_json must be a JSON array of strings") from e
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise HTTPException(status_code=400, detail="flags_json must be a JSON array of strings")
    return data


@router.get("/", response_model=list[ReportResponse])
def list_reports(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ReportResponse]:
    reports = db.scalars(
        select(Report)
        .where(Report.created_by == current_user.id)
        .order_by(Report.created_at.desc())
    ).all()
    return [_report_to_response(r) for r in reports]


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

    return _report_to_response(report)


@router.delete("/{report_id}", status_code=204)
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

    return _report_to_response(report)


@router.delete("/{report_id}", status_code=204)
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
