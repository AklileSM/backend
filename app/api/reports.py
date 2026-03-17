from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FileAsset, Report
from app.schemas import ReportCreateRequest, ReportResponse
from app.services.storage import storage_service

router = APIRouter()


@router.get("/", response_model=list[ReportResponse])
def list_reports(db: Session = Depends(get_db)) -> list[ReportResponse]:
    reports = db.scalars(select(Report).order_by(Report.created_at.desc())).all()
    response: list[ReportResponse] = []
    for report in reports:
        pdf_url = None
        if report.pdf_bucket_name and report.pdf_object_name:
            pdf_url = storage_service.get_presigned_url(report.pdf_bucket_name, report.pdf_object_name)
        response.append(
            ReportResponse(
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
        )
    return response


@router.post("/", response_model=ReportResponse)
def create_report(payload: ReportCreateRequest, db: Session = Depends(get_db)) -> ReportResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    report = Report(
        file_id=payload.file_id,
        ai_description=payload.ai_description,
        manual_observations=payload.manual_observations,
        flags=payload.flags,
        screenshots=payload.screenshots,
        created_by=payload.created_by,
        created_at=datetime.utcnow(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    return ReportResponse(
        id=report.id,
        file_id=report.file_id,
        ai_description=report.ai_description,
        manual_observations=report.manual_observations,
        flags=report.flags or [],
        screenshots=report.screenshots or [],
        created_by=report.created_by,
        pdf_url=None,
        created_at=report.created_at,
    )
