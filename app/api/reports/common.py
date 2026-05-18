"""Shared helpers for the reports endpoints.

Anything that more than one report submodule needs lives here: JSON-column
coercion, HTTP Range parsing, multipart-form JSON parsers, project lookup
for the caller, the activity-log entry shape, and the response serializer
for published Reports.

Draft-specific serializers live next to the routes that use them in
`viewer_drafts.py` and `comparison_drafts.py`.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FileAsset, Project, ProjectMember, Report, Room, User
from app.schemas import ReportResponse
from app.services.activity import log_activity


# ---------------------------------------------------------------------------
# Project resolution + access gate
# ---------------------------------------------------------------------------


def _resolve_project_for_user(
    db: Session, project_slug: str, current_user: User
) -> Project:
    """Look up a project by slug and confirm the caller can see it.

    Used by the profile listing endpoints to scope results to the project the
    user is currently in. Admins always pass; everyone else must be a
    ProjectMember.
    """
    project = db.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not current_user.is_admin:
        membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == current_user.id,
            )
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="Not a member of this project")
    return project


# ---------------------------------------------------------------------------
# HTTP Range parsing (PDF streaming)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# JSON column / form coercion
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Activity log for publish events
# ---------------------------------------------------------------------------


def _log_report_published(
    db: Session, *, report: Report, file_asset: FileAsset, current_user: User
) -> None:
    """Record `report.publish` on the project activity feed.

    Walks file → room → project to find the right project_id. Best-effort
    via log_activity, so a logging failure can't block the publish.
    """
    room = db.scalar(select(Room).where(Room.id == file_asset.room_id))
    if room is None:
        return
    log_activity(
        db,
        project_id=room.project_id,
        actor=current_user,
        action="report.publish",
        target_type="report",
        target_id=report.id,
        metadata={
            "report_label": (report.label or "").strip() or None,
            "file_id": file_asset.id,
            "file_name": file_asset.display_name,
            "room_name": room.name,
            "flags": list(report.flags or []),
        },
    )


# ---------------------------------------------------------------------------
# Response serializer for published Report
# ---------------------------------------------------------------------------


def _report_to_response(report: Report) -> ReportResponse:
    pdf_url = None
    if report.pdf_bucket_name and report.pdf_object_name:
        # Use same-origin proxy URL so browser access works in public/proxied deployments.
        pdf_url = f"/api/reports/{report.id}/pdf"
    return ReportResponse(
        id=report.id,
        file_id=report.file_id,
        label=report.label,
        ai_description=report.ai_description,
        manual_observations=report.manual_observations,
        flags=_coerce_str_list(report.flags),
        screenshots=_coerce_str_list(report.screenshots),
        created_by=report.created_by,
        pdf_url=pdf_url,
        created_at=report.created_at,
    )
