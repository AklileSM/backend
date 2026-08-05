"""Filtered, streaming capture-quality CSV export.

The export is intentionally metadata-only: image and point-cloud object bytes
never leave MinIO. Image quality retries expand to one row per attempt so a
research export retains rejected observations as well as the selected capture.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import SessionLocal, get_db
from app.models import FileAsset, Project, ProjectMember, Room, User
from app.schemas import QualityExportEstimateResponse

router = APIRouter()

_SUPPORTED_MEDIA = frozenset({"image", "pointcloud"})
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_STREAM_CHUNK_CHARS = 64 * 1024

CSV_COLUMNS = (
    "project_id",
    "project_slug",
    "project_name",
    "room_id",
    "room_slug",
    "room_name",
    "asset_id",
    "media_type",
    "file_name",
    "original_name",
    "capture_date",
    "captured_at_utc",
    "uploaded_at_utc",
    "file_size_bytes",
    "robot_username",
    "mission_id",
    "capture_point_id",
    "target_waypoint",
    "waypoint_index",
    "waypoint_count",
    "navigation_goal_id",
    "navigation_result",
    "sensor",
    "capture_backend",
    "attempt_number",
    "attempt_count",
    "max_attempts",
    "is_selected_attempt",
    "gate_mode",
    "gate_outcome",
    "gate_passed",
    "gate_evaluable",
    "gate_violation_score",
    "gate_flags",
    "capture_error",
    "quality_schema",
    "quality_verdict",
    "advisory_flags",
    "canonical_width",
    "blur_laplacian_var",
    "mean_luminance",
    "rms_contrast",
    "clipped_highlight_frac",
    "clipped_shadow_frac",
    "image_width_px",
    "image_height_px",
    "is_equirectangular",
    "pose_available",
    "target_pose_frame",
    "target_pose_x_m",
    "target_pose_y_m",
    "target_pose_z_m",
    "target_pose_yaw_rad",
    "recorded_pose_source",
    "recorded_pose_frame",
    "recorded_pose_x_m",
    "recorded_pose_y_m",
    "recorded_pose_z_m",
    "recorded_pose_yaw_rad",
    "pose_deviation_m",
    "pose_deviation_xy_m",
    "pose_deviation_z_m",
    "pose_deviation_deg",
    "point_count",
    "bbox_extent_x_m",
    "bbox_extent_y_m",
    "bbox_extent_z_m",
    "bbox_max_extent_m",
    "bbox_volume_m3",
    "intensity_nonzero_frac",
    "intensity_sampled_points",
)


class _ProjectInfo(Protocol):
    id: str
    slug: str
    name: str


@dataclass(frozen=True)
class _ProjectSnapshot:
    id: str
    slug: str
    name: str


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sequence_value(value: Any, index: int) -> Any:
    if not isinstance(value, list) or index >= len(value):
        return None
    return value[index]


def _safe_csv_value(value: Any) -> Any:
    """Preserve numeric cells while preventing spreadsheet formula injection."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value) if _number(value) is not None else ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _require_project(project_slug: str, current_user: User, db: Session) -> Project:
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


def _validate_filters(
    *,
    date_from: date | None,
    date_to: date | None,
    media_types: Iterable[str],
) -> tuple[str, ...]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from must be on or before date_to")
    normalized = tuple(dict.fromkeys(item.strip().lower() for item in media_types if item.strip()))
    if not normalized:
        return ("image", "pointcloud")
    unsupported = sorted(set(normalized) - _SUPPORTED_MEDIA)
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported quality-export media type: {', '.join(unsupported)}",
        )
    return normalized


def _asset_statement(
    *,
    project_id: str,
    date_from: date | None,
    date_to: date | None,
    room_slugs: Iterable[str],
    media_types: tuple[str, ...],
) -> Select:
    stmt = (
        select(FileAsset, Room)
        .join(Room, FileAsset.room_id == Room.id)
        .where(
            Room.project_id == project_id,
            FileAsset.media_type.in_(media_types),
            FileAsset.metadata_json.is_not(None),
        )
    )
    if date_from is not None:
        stmt = stmt.where(FileAsset.capture_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(FileAsset.capture_date <= date_to)
    normalized_rooms = tuple(dict.fromkeys(slug for slug in room_slugs if slug))
    if normalized_rooms:
        stmt = stmt.where(Room.slug.in_(normalized_rooms))
    return stmt.order_by(
        FileAsset.capture_date.asc(),
        Room.sort_order.asc(),
        Room.slug.asc(),
        FileAsset.created_at.asc(),
        FileAsset.id.asc(),
    )


def _iter_robot_assets(db: Session, stmt: Select) -> Iterator[tuple[FileAsset, Room, dict[str, Any]]]:
    result = db.execute(stmt).yield_per(500)
    for asset, room in result:
        metadata = _record(asset.metadata_json)
        raw_robot = metadata.get("robot")
        if not isinstance(raw_robot, dict):
            continue
        yield asset, room, raw_robot


def _attempt_records(
    robot: dict[str, Any],
    attempt_scope: Literal["all", "selected"],
) -> list[dict[str, Any] | None]:
    gate = _record(robot.get("quality_gate"))
    raw_attempts = gate.get("attempts")
    attempts = [item for item in raw_attempts if isinstance(item, dict)] if isinstance(raw_attempts, list) else []
    if not attempts:
        return [None]
    if attempt_scope == "all":
        return attempts

    selected_number = gate.get("selected_attempt")
    selected = [
        item
        for item in attempts
        if item.get("selected") is True
        or (selected_number is not None and str(item.get("attempt")) == str(selected_number))
    ]
    # If legacy/corrupt metadata has attempts but no selected marker, fall back
    # to the uploaded asset-level quality record rather than guessing a winner.
    return selected[:1] or [None]


def _export_row(
    *,
    project: _ProjectInfo,
    room: Room,
    asset: FileAsset,
    robot: dict[str, Any],
    attempt: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = _record(asset.metadata_json)
    summary_gate = _record(robot.get("quality_gate"))
    asset_quality = _record(robot.get("quality"))
    asset_checks = _record(asset_quality.get("checks"))

    if attempt is None:
        attempt_quality = asset_quality
        attempt_gate = {}
        checks = asset_checks
        selected = True
        attempt_number = summary_gate.get("selected_attempt") or 1
        capture_error = None
    else:
        attempt_quality = _record(attempt.get("quality"))
        attempt_gate = _record(attempt.get("gate"))
        checks = _record(attempt_quality.get("checks"))
        attempt_number = attempt.get("attempt")
        selected = attempt.get("selected") is True or (
            summary_gate.get("selected_attempt") is not None
            and str(attempt_number) == str(summary_gate.get("selected_attempt"))
        )
        capture_error = attempt.get("capture_error")
        if selected:
            checks = {**checks, **asset_checks}
            attempt_quality = {**attempt_quality, **asset_quality, "checks": checks}

    target_pose = _record(robot.get("target_waypoint_pose"))
    recorded_pose = _record(robot.get("pose")) if selected else {}
    extent = checks.get("bbox_extent_m")
    quality_flags = _string_list(attempt_quality.get("advisory_flags"))
    if selected:
        quality_flags = list(
            dict.fromkeys([*quality_flags, *_string_list(asset_quality.get("advisory_flags"))])
        )

    return {
        "project_id": project.id,
        "project_slug": project.slug,
        "project_name": project.name,
        "room_id": room.id,
        "room_slug": room.slug,
        "room_name": room.name,
        "asset_id": asset.id,
        "media_type": asset.media_type,
        "file_name": asset.display_name,
        "original_name": asset.original_name,
        "capture_date": asset.capture_date.isoformat(),
        "captured_at_utc": attempt.get("captured_at_utc") if attempt else robot.get("captured_at_utc"),
        "uploaded_at_utc": asset.created_at.isoformat(),
        "file_size_bytes": asset.file_size,
        "robot_username": metadata.get("uploaded_by_username"),
        "mission_id": robot.get("mission_id"),
        "capture_point_id": robot.get("capture_point_id"),
        "target_waypoint": robot.get("target_waypoint"),
        "waypoint_index": robot.get("waypoint_index"),
        "waypoint_count": robot.get("waypoint_count"),
        "navigation_goal_id": robot.get("navigation_goal_id"),
        "navigation_result": robot.get("navigation_result"),
        "sensor": robot.get("sensor"),
        "capture_backend": robot.get("capture_backend"),
        "attempt_number": attempt_number,
        "attempt_count": summary_gate.get("attempt_count") or 1,
        "max_attempts": summary_gate.get("max_attempts") or 1,
        "is_selected_attempt": selected,
        "gate_mode": summary_gate.get("mode"),
        "gate_outcome": summary_gate.get("outcome"),
        "gate_passed": attempt_gate.get("passed") if attempt is not None else summary_gate.get("passed"),
        "gate_evaluable": attempt_gate.get("evaluable"),
        "gate_violation_score": attempt_gate.get("violation_score"),
        "gate_flags": "|".join(_string_list(attempt_gate.get("flags"))),
        "capture_error": capture_error,
        "quality_schema": attempt_quality.get("schema") or asset_quality.get("schema"),
        "quality_verdict": attempt_quality.get("verdict") or asset_quality.get("verdict"),
        "advisory_flags": "|".join(quality_flags),
        "canonical_width": attempt_quality.get("canonical_width") or asset_quality.get("canonical_width"),
        "blur_laplacian_var": checks.get("blur_laplacian_var"),
        "mean_luminance": checks.get("mean_luminance"),
        "rms_contrast": checks.get("rms_contrast"),
        "clipped_highlight_frac": checks.get("clipped_highlight_frac"),
        "clipped_shadow_frac": checks.get("clipped_shadow_frac"),
        "image_width_px": checks.get("width"),
        "image_height_px": checks.get("height"),
        "is_equirectangular": checks.get("is_equirectangular"),
        "pose_available": checks.get("pose_available") if selected else None,
        "target_pose_frame": target_pose.get("frame"),
        "target_pose_x_m": target_pose.get("x"),
        "target_pose_y_m": target_pose.get("y"),
        "target_pose_z_m": target_pose.get("z"),
        "target_pose_yaw_rad": target_pose.get("yaw") if target_pose.get("yaw") is not None else target_pose.get("yaw_rad"),
        "recorded_pose_source": recorded_pose.get("source") or recorded_pose.get("topic"),
        "recorded_pose_frame": recorded_pose.get("frame"),
        "recorded_pose_x_m": recorded_pose.get("x"),
        "recorded_pose_y_m": recorded_pose.get("y"),
        "recorded_pose_z_m": recorded_pose.get("z"),
        "recorded_pose_yaw_rad": recorded_pose.get("yaw") if recorded_pose.get("yaw") is not None else recorded_pose.get("yaw_rad"),
        "pose_deviation_m": checks.get("pose_deviation_m") if selected else None,
        "pose_deviation_xy_m": checks.get("pose_deviation_xy_m") if selected else None,
        "pose_deviation_z_m": checks.get("pose_deviation_z_m") if selected else None,
        "pose_deviation_deg": checks.get("pose_deviation_deg") if selected else None,
        "point_count": checks.get("point_count"),
        "bbox_extent_x_m": _sequence_value(extent, 0),
        "bbox_extent_y_m": _sequence_value(extent, 1),
        "bbox_extent_z_m": _sequence_value(extent, 2),
        "bbox_max_extent_m": checks.get("bbox_max_extent_m"),
        "bbox_volume_m3": checks.get("bbox_volume_m3"),
        "intensity_nonzero_frac": checks.get("intensity_nonzero_frac"),
        "intensity_sampled_points": checks.get("intensity_sampled_points"),
    }


def _iter_export_rows(
    *,
    db: Session,
    stmt: Select,
    project: _ProjectInfo,
    attempt_scope: Literal["all", "selected"],
) -> Iterator[dict[str, Any]]:
    for asset, room, robot in _iter_robot_assets(db, stmt):
        for attempt in _attempt_records(robot, attempt_scope):
            yield _export_row(
                project=project,
                room=room,
                asset=asset,
                robot=robot,
                attempt=attempt,
            )


def _iter_csv(rows: Iterable[dict[str, Any]]) -> Iterator[str]:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    # UTF-8 BOM keeps Excel from misreading non-ASCII room and waypoint names.
    yield "\ufeff" + buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow({column: _safe_csv_value(row.get(column)) for column in CSV_COLUMNS})
        if buffer.tell() >= _STREAM_CHUNK_CHARS:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    if buffer.tell():
        yield buffer.getvalue()


def _stream_export_csv(
    *,
    stmt: Select,
    project: _ProjectInfo,
    attempt_scope: Literal["all", "selected"],
) -> Iterator[str]:
    """Own the export session for the full lifetime of the streamed response."""
    with SessionLocal() as export_db:
        rows = _iter_export_rows(
            db=export_db,
            stmt=stmt,
            project=project,
            attempt_scope=attempt_scope,
        )
        yield from _iter_csv(rows)


def _export_filename(project_slug: str, date_from: date | None, date_to: date | None) -> str:
    if date_from is None and date_to is None:
        scope = "all-dates"
    elif date_from == date_to:
        scope = date_from.isoformat() if date_from is not None else "date-filtered"
    else:
        scope = f"{date_from.isoformat() if date_from else 'start'}-to-{date_to.isoformat() if date_to else 'latest'}"
    return f"{project_slug}-capture-quality-{scope}.csv"


@router.get("/quality-export/estimate", response_model=QualityExportEstimateResponse)
def estimate_quality_export(
    project_slug: str,
    date_from: date | None = None,
    date_to: date | None = None,
    room_slug: list[str] = Query(default=[]),
    media_type: list[str] = Query(default=[]),
    attempt_scope: Literal["all", "selected"] = "all",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> QualityExportEstimateResponse:
    project = _require_project(project_slug, current_user, db)
    media_types = _validate_filters(
        date_from=date_from,
        date_to=date_to,
        media_types=media_type,
    )
    stmt = _asset_statement(
        project_id=project.id,
        date_from=date_from,
        date_to=date_to,
        room_slugs=room_slug,
        media_types=media_types,
    )
    asset_count = 0
    row_count = 0
    for _asset, _room, robot in _iter_robot_assets(db, stmt):
        asset_count += 1
        row_count += len(_attempt_records(robot, attempt_scope))
    return QualityExportEstimateResponse(
        asset_count=asset_count,
        row_count=row_count,
        filename=_export_filename(project.slug, date_from, date_to),
    )


@router.get("/quality-export.csv")
def export_quality_csv(
    project_slug: str,
    date_from: date | None = None,
    date_to: date | None = None,
    room_slug: list[str] = Query(default=[]),
    media_type: list[str] = Query(default=[]),
    attempt_scope: Literal["all", "selected"] = "all",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    project = _require_project(project_slug, current_user, db)
    media_types = _validate_filters(
        date_from=date_from,
        date_to=date_to,
        media_types=media_type,
    )
    stmt = _asset_statement(
        project_id=project.id,
        date_from=date_from,
        date_to=date_to,
        room_slugs=room_slug,
        media_types=media_types,
    )
    project_snapshot = _ProjectSnapshot(
        id=project.id,
        slug=project.slug,
        name=project.name,
    )
    content = _stream_export_csv(
        stmt=stmt,
        project=project_snapshot,
        attempt_scope=attempt_scope,
    )
    filename = _export_filename(project.slug, date_from, date_to)
    return StreamingResponse(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
