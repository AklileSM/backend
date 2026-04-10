import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response as PlainResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import ComparisonDraft, FileAsset, Report, User, ViewerReportDraft
from app.schemas import (
    ComparisonDraftCreateRequest,
    ComparisonDraftDetailResponse,
    ComparisonDraftResponse,
    ComparisonDraftUpdateRequest,
    ReportCreateRequest,
    ReportResponse,
    ViewerDraftCreateRequest,
    ViewerDraftDetailResponse,
    ViewerDraftResponse,
    ViewerDraftUpdateRequest,
)
from app.services.storage import storage_service

router = APIRouter()
settings = get_settings()


def _parse_http_range(range_header: str | None, total: int) -> tuple[int, int] | None:
    if not range_header or total <= 0:
        return None
    if "=" not in range_header:
        return None
    unit, raw_spec = range_header.split("=", 1)
    if unit.strip().lower() != "bytes":
        return None
    spec = raw_spec.split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    left, right = spec.split("-", 1)
    try:
        if left == "":
            suffix_len = int(right)
            if suffix_len <= 0:
                return None
            first = max(0, total - suffix_len)
            last = total - 1
            return (first, last)
        first = int(left)
        if right == "":
            last = total - 1
        else:
            last = int(right)
    except ValueError:
        return None
    if first < 0 or first > last:
        return None
    if first >= total:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{total}"},
        )
    return (first, min(last, total - 1))


def _coerce_str_list(val: object | None) -> list[str]:
    """JSON columns may deserialize as list, str, dict, or None; Pydantic expects list[str]."""
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val]
    if isinstance(val, dict):
        return []
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        try:
            data = json.loads(s)
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            return []
    return []


def _report_to_response(report: Report) -> ReportResponse:
    pdf_url = None
    if report.pdf_bucket_name and report.pdf_object_name:
        # Use same-origin proxy URL so browser access works in public/proxied deployments.
        pdf_url = f"/api/reports/{report.id}/pdf"
    return ReportResponse(
        id=report.id,
        file_id=report.file_id,
        ai_description=report.ai_description,
        manual_observations=report.manual_observations,
        flags=_coerce_str_list(report.flags),
        screenshots=_coerce_str_list(report.screenshots),
        created_by=report.created_by,
        pdf_url=pdf_url,
        created_at=report.created_at,
    )


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


def _parse_draft_ids_json(draft_ids_json: str | None) -> list[str]:
    if draft_ids_json is None or not str(draft_ids_json).strip():
        return []
    try:
        data = json.loads(draft_ids_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="draft_ids_json must be a JSON array of ids") from e
    if not isinstance(data, list) or not all(isinstance(x, str) and x.strip() for x in data):
        raise HTTPException(status_code=400, detail="draft_ids_json must be a JSON array of ids")
    return [x.strip() for x in data]


def _strip_draft_pdf_if_stored(draft: ComparisonDraft) -> None:
    """Remove draft PDF from object storage and clear keys (drafts are state-only until publish)."""
    b = (draft.pdf_bucket_name or "").strip()
    o = (draft.pdf_object_name or "").strip()
    if b and o:
        storage_service.remove_object_best_effort(b, o)
    draft.pdf_bucket_name = ""
    draft.pdf_object_name = ""


def _parse_state_json(state_json: str | None) -> dict | None:
    if state_json is None or not str(state_json).strip():
        return None
    try:
        data = json.loads(state_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="state_json must be a JSON object") from e
    if data is None:
        return None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="state_json must be a JSON object")
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


@router.get("/comparison-drafts", response_model=list[ComparisonDraftResponse])
def list_comparison_drafts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ComparisonDraftResponse]:
    drafts = db.scalars(
        select(ComparisonDraft)
        .where(ComparisonDraft.created_by == current_user.id)
        .order_by(ComparisonDraft.created_at.asc())
    ).all()
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


@router.delete("/comparison-drafts/{draft_id}", status_code=204)
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
    return _report_to_response(report)


@router.get("/viewer-drafts", response_model=list[ViewerDraftResponse])
def list_viewer_drafts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ViewerDraftResponse]:
    drafts = db.scalars(
        select(ViewerReportDraft)
        .where(ViewerReportDraft.created_by == current_user.id)
        .order_by(ViewerReportDraft.created_at.asc())
    ).all()
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


@router.delete("/viewer-drafts/{draft_id}", status_code=204)
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
    return _report_to_response(report)


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
