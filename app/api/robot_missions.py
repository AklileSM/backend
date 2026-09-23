from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import threading
import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_current_user, require_robot
from app.core.security import decode_access_token
from app.database import SessionLocal, get_db
from app.models import (
    Project,
    ProjectMember,
    RobotCapturePoint,
    RobotCommand,
    RobotMission,
    RobotMissionSchedule,
    RobotMissionStep,
    RobotPresence,
    User,
)
from app.schemas import (
    RobotCommandCreateRequest,
    RobotCommandResponse,
    RobotCommandStatusUpdateRequest,
    RobotHeartbeatRequest,
    RobotCapturePointCreateRequest,
    RobotCapturePointResponse,
    RobotCapturePointUpdateRequest,
    RobotMissionCreateRequest,
    RobotMissionControlResponse,
    RobotMissionResponse,
    RobotMissionScheduleCreateRequest,
    RobotMissionScheduleResponse,
    RobotMissionScheduleUpdateRequest,
    RobotMissionStatusUpdateRequest,
    RobotMissionStepResponse,
    RobotPresenceResponse,
    RobotSummaryResponse,
    RobotTelemetryRequest,
    RobotTelemetryResponse,
)
from app.services.activity import log_activity
from app.services.robot_schedules import (
    materialize_schedule,
    next_schedule_run,
    ordered_capture_points,
    robot_can_upload_to_project,
    utc_now,
    validate_timezone,
)

router = APIRouter()

_TELEMETRY_SUBSCRIBERS: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
_TELEMETRY_SUBSCRIBERS_LOCK = threading.Lock()

# Latest telemetry frame per robot, response-shaped. Telemetry is ephemeral realtime data:
# the pose arrives up to 10x per second and only the newest value matters, so routing every
# frame through a Postgres commit (and reading it back out in every subscriber loop) added
# a database round trip per frame for no durability we actually need. Frames now live here
# and fan out from memory; the presence row is persisted on a slow cadence purely so the
# last known pose survives a backend restart. In-process state is safe for the same reason
# _TELEMETRY_SUBSCRIBERS is: uvicorn runs this app as a single process.
_LATEST_TELEMETRY: dict[str, dict] = {}
_LATEST_TELEMETRY_LOCK = threading.Lock()

# How often the ingest socket flushes presence + last pose to Postgres.
_TELEMETRY_PERSIST_SECONDS = 5.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_robot_user(robot_id: str, db: Session) -> User:
    robot = db.scalar(
        select(User).where(
            User.is_robot == True,  # noqa: E712
            or_(User.id == robot_id, User.username == robot_id),
        )
    )
    if robot is None:
        raise HTTPException(status_code=404, detail="Robot not found")
    return robot


def _require_project_editor(project: Project, user: User, db: Session) -> None:
    if user.is_admin:
        return
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    )
    if member is None or member.role not in ("owner", "editor"):
        raise HTTPException(
            status_code=403,
            detail="Only project owners and editors can manage robot missions",
        )


def _require_robot_project_upload_access(project: Project, robot: User, db: Session) -> None:
    if robot_can_upload_to_project(
        db,
        robot_user_id=robot.id,
        project_id=project.id,
    ):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Robot {robot.username} cannot upload to this project. "
            "Add the robot as an editor or pair it with this project before starting a mission."
        ),
    )


def _require_project_access(project: Project, user: User, db: Session) -> None:
    if user.is_admin:
        return
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    )
    if member is None:
        raise HTTPException(status_code=403, detail="Project access required")


def _require_robot_identity(robot_id: str, current_user: User) -> None:
    if current_user.id != robot_id and current_user.username != robot_id:
        raise HTTPException(status_code=403, detail="Robot path does not match authenticated robot account")


def _current_user_from_access_token(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_access_token(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    return user


def _step_to_response(step: RobotMissionStep) -> RobotMissionStepResponse:
    return RobotMissionStepResponse(
        id=step.id,
        sequence_index=step.sequence_index,
        waypoint_name=step.waypoint_name,
        room_slug=step.room_slug,
        status=step.status,
        error_message=step.error_message,
        navigation_goal_id=step.navigation_goal_id,
        navigation_result=step.navigation_result,
        uploaded_file_id=step.uploaded_file_id,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )


def _mission_to_response(mission: RobotMission) -> RobotMissionResponse:
    return RobotMissionResponse(
        id=mission.id,
        robot_id=mission.robot_username,
        project_id=mission.project_id,
        project_slug=mission.project.slug if mission.project else "",
        status=mission.status,
        capture_mode=mission.capture_mode,
        capture_date=mission.capture_date,
        waypoints=list(mission.waypoints_json or []),
        room_slug_map=dict(mission.room_slug_map_json or {}),
        retry_policy=dict(mission.retry_policy_json or {}),
        robot_meta=dict(mission.robot_meta_json or {}),
        schedule_id=mission.schedule_id,
        scheduled_for=mission.scheduled_for,
        created_at=mission.created_at,
        dispatched_at=mission.dispatched_at,
        started_at=mission.started_at,
        completed_at=mission.completed_at,
        cancelled_at=mission.cancelled_at,
        cancel_requested_at=mission.cancel_requested_at,
        cancel_acknowledged_at=mission.cancel_acknowledged_at,
        cancel_error=mission.cancel_error,
        steps=[_step_to_response(step) for step in sorted(mission.steps, key=lambda s: s.sequence_index)],
        result=mission.result_json,
    )


def _schedule_to_response(schedule: RobotMissionSchedule) -> RobotMissionScheduleResponse:
    return RobotMissionScheduleResponse(
        id=schedule.id,
        name=schedule.name,
        robot_id=schedule.robot_username,
        project_id=schedule.project_id,
        project_slug=schedule.project.slug if schedule.project else "",
        capture_point_ids=[str(item) for item in (schedule.capture_point_ids_json or [])],
        local_time=schedule.local_time,
        timezone=schedule.timezone,
        weekdays=[int(item) for item in (schedule.weekdays_json or [])],
        enabled=schedule.enabled,
        capture_mode=schedule.capture_mode,
        retry_policy=dict(schedule.retry_policy_json or {}),
        robot_meta=dict(schedule.robot_meta_json or {}),
        busy_policy=schedule.busy_policy,
        auto_connect=schedule.auto_connect,
        max_lateness_minutes=schedule.max_lateness_minutes,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        last_outcome=schedule.last_outcome,
        last_error=schedule.last_error,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


_ACTIVE_COMMAND_STATUSES = ("queued", "dispatched", "running")


def _command_to_response(command: RobotCommand) -> RobotCommandResponse:
    return RobotCommandResponse(
        id=command.id,
        robot_id=command.robot_username,
        kind=command.kind,
        status=command.status,
        connection=command.connection,
        detail=command.detail,
        progress_events=list((command.progress_json or {}).get("progress_events") or []),
        created_at=command.created_at,
        dispatched_at=command.dispatched_at,
        completed_at=command.completed_at,
    )


def _capture_point_to_response(point: RobotCapturePoint) -> RobotCapturePointResponse:
    return RobotCapturePointResponse(
        id=point.id,
        project_id=point.project_id,
        name=point.name,
        room_slug=point.room_slug,
        map_x=point.map_x,
        map_y=point.map_y,
        yaw=point.yaw,
        floorplan_x=point.floorplan_x,
        floorplan_y=point.floorplan_y,
        source=point.source,
        metadata=dict(point.metadata_json or {}),
        created_at=point.created_at,
        updated_at=point.updated_at,
    )


def _waypoint_name(waypoint: object, index: int) -> str:
    if isinstance(waypoint, dict):
        raw = waypoint.get("name") or waypoint.get("label") or waypoint.get("capture_point_id")
        return str(raw or f"capture-point-{index}")
    return str(waypoint)


def _room_slug_for_waypoint(waypoint: object, name: str, room_slug_map: dict[str, str]) -> str:
    if isinstance(waypoint, dict) and waypoint.get("room_slug"):
        return str(waypoint["room_slug"])
    return room_slug_map.get(name) or name


def _capture_point_to_waypoint(point: RobotCapturePoint) -> dict:
    half_yaw = float(point.yaw or 0.0) / 2.0
    return {
        "name": point.name,
        "x": point.map_x,
        "y": point.map_y,
        "z": 0.0,
        "qx": 0.0,
        "qy": 0.0,
        "qz": math.sin(half_yaw),
        "qw": math.cos(half_yaw),
        "yaw": point.yaw,
        "frame": "map",
        "room_slug": point.room_slug or point.name,
        "capture_point_id": point.id,
    }


def _resolve_mission_waypoints(
    *,
    payload: RobotMissionCreateRequest,
    project: Project,
    db: Session,
) -> tuple[list[object], dict[str, str], dict]:
    waypoints: list[object] = list(payload.waypoints or [])
    room_slug_map = {str(k): str(v) for k, v in payload.room_slug_map.items()}
    capture_point_ids = [str(item) for item in payload.capture_point_ids]

    if capture_point_ids:
        points = db.scalars(
            select(RobotCapturePoint).where(
                RobotCapturePoint.project_id == project.id,
                RobotCapturePoint.id.in_(capture_point_ids),
            )
        ).all()
        by_id = {point.id: point for point in points}
        missing = [point_id for point_id in capture_point_ids if point_id not in by_id]
        if missing:
            raise HTTPException(status_code=404, detail=f"Capture point not found: {missing[0]}")
        for point_id in capture_point_ids:
            point = by_id[point_id]
            waypoint = _capture_point_to_waypoint(point)
            waypoints.append(waypoint)
            room_slug_map[point.name] = point.room_slug or point.name

    if not waypoints:
        raise HTTPException(status_code=422, detail="Provide at least one waypoint or capture point")

    robot_meta = dict(payload.robot_meta)
    if capture_point_ids:
        robot_meta["capture_point_ids"] = capture_point_ids
    return waypoints, room_slug_map, robot_meta


def _presence_to_response(presence: RobotPresence) -> RobotPresenceResponse:
    return RobotPresenceResponse(
        robot_id=presence.robot_username,
        status=presence.status,
        current_mission_id=presence.current_mission_id,
        hostname=presence.hostname,
        connection=_presence_connection(presence),
        last_seen_at=presence.last_seen_at,
    )


def _presence_payload(presence: RobotPresence) -> dict:
    return dict(presence.payload_json or {}) if isinstance(presence.payload_json, dict) else {}


def _presence_connection(presence: RobotPresence) -> str | None:
    heartbeat = _presence_payload(presence).get("heartbeat")
    if not isinstance(heartbeat, dict):
        return None
    connection = heartbeat.get("connection")
    if connection in {"disconnected", "connecting", "connected", "disconnecting"}:
        return str(connection)
    return None


def _record_robot_connection(
    *,
    robot_user_id: str,
    robot_username: str,
    connection: str | None,
    db: Session,
) -> bool:
    """Record physical stack state independently from lifecycle-command history."""
    if connection not in {"disconnected", "connecting", "connected", "disconnecting"}:
        return False
    presence = db.scalar(
        select(RobotPresence).where(RobotPresence.robot_user_id == robot_user_id)
    )
    if presence is None:
        presence = RobotPresence(
            robot_user_id=robot_user_id,
            robot_username=robot_username,
        )
        db.add(presence)
    presence_payload = _presence_payload(presence)
    heartbeat = presence_payload.get("heartbeat")
    heartbeat = dict(heartbeat) if isinstance(heartbeat, dict) else {}
    heartbeat["connection"] = connection
    presence_payload["heartbeat"] = heartbeat
    presence.payload_json = presence_payload
    # This update is authenticated as the robot and therefore also proves liveness.
    presence.last_seen_at = _utc_now()
    return True


def _reconcile_active_command_from_connection(
    *,
    robot_user_id: str,
    connection: str | None,
    db: Session,
) -> bool:
    """Finish a stale lifecycle command when the physical stack reached its target."""
    expected_kind = {
        "connected": "connect",
        "disconnected": "disconnect",
    }.get(connection or "")
    if expected_kind is None:
        return False

    command = db.scalar(
        select(RobotCommand)
        .where(
            RobotCommand.robot_user_id == robot_user_id,
            RobotCommand.kind == expected_kind,
            RobotCommand.status.in_(_ACTIVE_COMMAND_STATUSES),
        )
        .order_by(RobotCommand.created_at.desc())
    )
    if command is None:
        return False

    command.status = "succeeded"
    command.connection = connection
    command.detail = (
        "Reconciled from robot heartbeat: the control panel confirmed "
        f"the robot is {connection}."
    )
    command.completed_at = _utc_now()
    return True


def _telemetry_to_response(presence: RobotPresence) -> RobotTelemetryResponse:
    payload = _presence_payload(presence)
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict):
        raise HTTPException(status_code=404, detail="Robot telemetry not found")
    return RobotTelemetryResponse.model_validate({
        **telemetry,
        "robot_id": presence.robot_username,
    })


def _latest_robot_telemetry_payload(robot_user_id: str, db: Session) -> dict | None:
    with _LATEST_TELEMETRY_LOCK:
        cached = _LATEST_TELEMETRY.get(robot_user_id)
    if cached is not None:
        return cached
    # Nothing in memory yet (backend restarted, robot quiet) — fall back to the persisted copy.
    db.expire_all()
    presence = db.scalar(select(RobotPresence).where(RobotPresence.robot_user_id == robot_user_id))
    if presence is None:
        return None
    try:
        return _telemetry_to_response(presence).model_dump(mode="json")
    except HTTPException:
        return None


def _telemetry_signature(payload: dict) -> str:
    signature = str(payload.get("received_at_utc") or "")
    if signature:
        return signature
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _put_latest_telemetry(queue: asyncio.Queue, payload: dict) -> None:
    while queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    queue.put_nowait(payload)


def _publish_robot_telemetry(robot_user_id: str, payload: dict) -> None:
    with _TELEMETRY_SUBSCRIBERS_LOCK:
        subscribers = tuple(_TELEMETRY_SUBSCRIBERS.get(robot_user_id, set()))
    for loop, queue in subscribers:
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_put_latest_telemetry, queue, payload)
            except RuntimeError:
                pass


def _robot_to_summary(robot: User, presence: RobotPresence | None) -> RobotSummaryResponse:
    return RobotSummaryResponse(
        robot_id=robot.id,
        username=robot.username,
        status=presence.status if presence else None,
        current_mission_id=presence.current_mission_id if presence else None,
        hostname=presence.hostname if presence else None,
        connection=_presence_connection(presence) if presence else None,
        last_seen_at=presence.last_seen_at if presence else None,
    )


def _apply_step_results(mission: RobotMission, result: dict | None) -> None:
    if not isinstance(result, dict):
        return
    raw_steps = result.get("steps")
    if not isinstance(raw_steps, list):
        return

    by_index = {step.sequence_index: step for step in mission.steps}
    now = _utc_now()
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        idx = item.get("waypoint_index")
        if not isinstance(idx, int):
            continue
        step = by_index.get(idx)
        if step is None:
            continue
        raw_status = str(item.get("status") or "unknown").lower()
        step.status = raw_status
        step.error_message = item.get("error")
        step.navigation_goal_id = item.get("navigation_goal_id")
        step.navigation_result = item.get("navigation_result")
        step.uploaded_file_id = item.get("id")
        step.result_json = item
        step.started_at = step.started_at or mission.started_at or now
        step.completed_at = now


@router.get("/robots", response_model=list[RobotSummaryResponse])
def list_robots(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RobotSummaryResponse]:
    robots = db.scalars(
        select(User).where(User.is_robot == True).order_by(User.username.asc())  # noqa: E712
    ).all()
    presences = db.scalars(select(RobotPresence)).all()
    presence_by_user_id = {presence.robot_user_id: presence for presence in presences}
    return [_robot_to_summary(robot, presence_by_user_id.get(robot.id)) for robot in robots]


@router.get("/projects/{project_id}/robot-capture-points", response_model=list[RobotCapturePointResponse])
def list_robot_capture_points(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RobotCapturePointResponse]:
    project = db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_access(project, current_user, db)

    points = db.scalars(
        select(RobotCapturePoint)
        .where(RobotCapturePoint.project_id == project.id)
        .order_by(RobotCapturePoint.name.asc())
    ).all()
    return [_capture_point_to_response(point) for point in points]


@router.post(
    "/projects/{project_id}/robot-capture-points",
    response_model=RobotCapturePointResponse,
    status_code=201,
)
def create_robot_capture_point(
    project_id: str,
    payload: RobotCapturePointCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotCapturePointResponse:
    project = db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_editor(project, current_user, db)

    point = RobotCapturePoint(
        project_id=project.id,
        name=payload.name.strip(),
        room_slug=payload.room_slug.strip() if payload.room_slug else None,
        map_x=payload.map_x,
        map_y=payload.map_y,
        yaw=payload.yaw,
        floorplan_x=payload.floorplan_x,
        floorplan_y=payload.floorplan_y,
        source=payload.source,
        metadata_json=payload.metadata,
        created_by_user_id=current_user.id,
    )
    db.add(point)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Capture point name already exists for this project") from exc
    db.refresh(point)

    log_activity(
        db,
        project_id=project.id,
        actor=current_user,
        action="robot_capture_point.create",
        target_type="robot_capture_point",
        target_id=point.id,
        metadata={"name": point.name, "room_slug": point.room_slug},
    )
    return _capture_point_to_response(point)


@router.patch(
    "/projects/{project_id}/robot-capture-points/{point_id}",
    response_model=RobotCapturePointResponse,
)
def update_robot_capture_point(
    project_id: str,
    point_id: str,
    payload: RobotCapturePointUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotCapturePointResponse:
    project = db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_editor(project, current_user, db)

    point = db.scalar(
        select(RobotCapturePoint).where(
            RobotCapturePoint.id == point_id,
            RobotCapturePoint.project_id == project.id,
        )
    )
    if point is None:
        raise HTTPException(status_code=404, detail="Capture point not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        point.name = str(data["name"]).strip()
    if "room_slug" in data:
        point.room_slug = data["room_slug"].strip() if data["room_slug"] else None
    for field in ("map_x", "map_y", "yaw", "floorplan_x", "floorplan_y", "source"):
        if field in data:
            setattr(point, field, data[field])
    if "metadata" in data:
        point.metadata_json = data["metadata"] or {}

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Capture point name already exists for this project") from exc
    db.refresh(point)
    return _capture_point_to_response(point)


@router.delete("/projects/{project_id}/robot-capture-points/{point_id}", status_code=204)
def delete_robot_capture_point(
    project_id: str,
    point_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    project = db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_editor(project, current_user, db)

    point = db.scalar(
        select(RobotCapturePoint).where(
            RobotCapturePoint.id == point_id,
            RobotCapturePoint.project_id == project.id,
        )
    )
    if point is None:
        raise HTTPException(status_code=404, detail="Capture point not found")
    point_id_for_log = point.id
    point_name = point.name
    db.delete(point)
    db.commit()
    log_activity(
        db,
        project_id=project.id,
        actor=current_user,
        action="robot_capture_point.delete",
        target_type="robot_capture_point",
        target_id=point_id_for_log,
        metadata={"name": point_name},
    )
    return Response(status_code=204)


def _validate_schedule_configuration(
    db: Session,
    *,
    project: Project,
    capture_point_ids: list[str],
    timezone_name: str,
) -> None:
    try:
        validate_timezone(timezone_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _, missing = ordered_capture_points(
        db,
        project_id=project.id,
        capture_point_ids=capture_point_ids,
    )
    if missing:
        raise HTTPException(status_code=404, detail=f"Capture point not found: {missing[0]}")


@router.get("/robot/mission-schedules", response_model=list[RobotMissionScheduleResponse])
def list_robot_mission_schedules(
    robot_id: str | None = Query(default=None),
    project_slug: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RobotMissionScheduleResponse]:
    stmt = (
        select(RobotMissionSchedule)
        .options(joinedload(RobotMissionSchedule.project))
        .order_by(RobotMissionSchedule.created_at.desc())
    )
    if robot_id:
        robot = _resolve_robot_user(robot_id, db)
        stmt = stmt.where(RobotMissionSchedule.robot_user_id == robot.id)
    if project_slug:
        project = db.scalar(select(Project).where(Project.slug == project_slug))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        stmt = stmt.where(RobotMissionSchedule.project_id == project.id)

    if not current_user.is_admin:
        if current_user.is_robot:
            stmt = stmt.where(RobotMissionSchedule.robot_user_id == current_user.id)
        else:
            stmt = stmt.join(
                Project, RobotMissionSchedule.project_id == Project.id
            ).join(
                ProjectMember, ProjectMember.project_id == Project.id
            ).where(ProjectMember.user_id == current_user.id)

    schedules = db.scalars(stmt).unique().all()
    return [_schedule_to_response(schedule) for schedule in schedules]


@router.post(
    "/robot/mission-schedules",
    response_model=RobotMissionScheduleResponse,
    status_code=201,
)
def create_robot_mission_schedule(
    payload: RobotMissionScheduleCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionScheduleResponse:
    robot = _resolve_robot_user(payload.robot_id, db)
    project = db.scalar(select(Project).where(Project.slug == payload.project_slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_editor(project, current_user, db)
    _require_robot_project_upload_access(project, robot, db)
    _validate_schedule_configuration(
        db,
        project=project,
        capture_point_ids=payload.capture_point_ids,
        timezone_name=payload.timezone,
    )

    now = utc_now()
    schedule = RobotMissionSchedule(
        name=payload.name.strip(),
        robot_user_id=robot.id,
        robot_username=robot.username,
        project_id=project.id,
        requested_by_user_id=current_user.id,
        enabled=payload.enabled,
        timezone=payload.timezone,
        local_time=payload.local_time,
        weekdays_json=sorted(payload.weekdays),
        capture_point_ids_json=list(payload.capture_point_ids),
        capture_mode=payload.capture_mode,
        retry_policy_json=dict(payload.retry_policy),
        robot_meta_json=dict(payload.robot_meta),
        busy_policy=payload.busy_policy,
        auto_connect=payload.auto_connect,
        max_lateness_minutes=payload.max_lateness_minutes,
        next_run_at=(
            next_schedule_run(
                local_time=payload.local_time,
                timezone_name=payload.timezone,
                weekdays=payload.weekdays,
                after_utc=now,
            )
            if payload.enabled
            else None
        ),
    )
    db.add(schedule)
    db.commit()
    schedule = db.scalar(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.id == schedule.id)
        .options(joinedload(RobotMissionSchedule.project))
    )
    assert schedule is not None

    log_activity(
        db,
        project_id=project.id,
        actor=current_user,
        action="robot_mission_schedule.create",
        target_type="robot_mission_schedule",
        target_id=schedule.id,
        metadata={
            "name": schedule.name,
            "robot_id": schedule.robot_username,
            "local_time": schedule.local_time,
            "timezone": schedule.timezone,
        },
    )
    return _schedule_to_response(schedule)


@router.patch(
    "/robot/mission-schedules/{schedule_id}",
    response_model=RobotMissionScheduleResponse,
)
def update_robot_mission_schedule(
    schedule_id: str,
    payload: RobotMissionScheduleUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionScheduleResponse:
    schedule = db.scalar(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.id == schedule_id)
        .options(joinedload(RobotMissionSchedule.project))
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    _require_project_editor(schedule.project, current_user, db)

    project = schedule.project
    if payload.project_slug is not None and payload.project_slug != project.slug:
        project = db.scalar(select(Project).where(Project.slug == payload.project_slug))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        _require_project_editor(project, current_user, db)
        schedule.project_id = project.id
        schedule.project = project

    if payload.robot_id is not None and payload.robot_id != schedule.robot_username:
        robot = _resolve_robot_user(payload.robot_id, db)
        schedule.robot_user_id = robot.id
        schedule.robot_username = robot.username
    else:
        robot = _resolve_robot_user(schedule.robot_user_id, db)

    _require_robot_project_upload_access(project, robot, db)

    capture_point_ids = (
        list(payload.capture_point_ids)
        if payload.capture_point_ids is not None
        else [str(item) for item in (schedule.capture_point_ids_json or [])]
    )
    timezone_name = payload.timezone or schedule.timezone
    _validate_schedule_configuration(
        db,
        project=project,
        capture_point_ids=capture_point_ids,
        timezone_name=timezone_name,
    )

    if payload.name is not None:
        schedule.name = payload.name.strip()
    if payload.capture_point_ids is not None:
        schedule.capture_point_ids_json = capture_point_ids
    if payload.local_time is not None:
        schedule.local_time = payload.local_time
    if payload.timezone is not None:
        schedule.timezone = payload.timezone
    if payload.weekdays is not None:
        schedule.weekdays_json = sorted(payload.weekdays)
    if payload.capture_mode is not None:
        schedule.capture_mode = payload.capture_mode
    if payload.retry_policy is not None:
        schedule.retry_policy_json = dict(payload.retry_policy)
    if payload.robot_meta is not None:
        schedule.robot_meta_json = dict(payload.robot_meta)
    if payload.busy_policy is not None:
        schedule.busy_policy = payload.busy_policy
    if payload.auto_connect is not None:
        schedule.auto_connect = payload.auto_connect
    if payload.max_lateness_minutes is not None:
        schedule.max_lateness_minutes = payload.max_lateness_minutes
    if payload.enabled is not None:
        schedule.enabled = payload.enabled

    timing_fields = {"local_time", "timezone", "weekdays", "enabled"}
    if schedule.enabled and (timing_fields & payload.model_fields_set):
        schedule.next_run_at = next_schedule_run(
            local_time=schedule.local_time,
            timezone_name=schedule.timezone,
            weekdays=list(schedule.weekdays_json or []),
            after_utc=utc_now(),
        )
    elif not schedule.enabled:
        schedule.next_run_at = None

    schedule.last_error = None
    db.commit()
    schedule = db.scalar(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.id == schedule.id)
        .options(joinedload(RobotMissionSchedule.project))
    )
    assert schedule is not None

    log_activity(
        db,
        project_id=schedule.project_id,
        actor=current_user,
        action="robot_mission_schedule.update",
        target_type="robot_mission_schedule",
        target_id=schedule.id,
        metadata={"name": schedule.name, "enabled": schedule.enabled},
    )
    return _schedule_to_response(schedule)


@router.post(
    "/robot/mission-schedules/{schedule_id}/run",
    response_model=RobotMissionResponse,
    status_code=201,
)
def run_robot_mission_schedule_now(
    schedule_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    schedule = db.scalar(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.id == schedule_id)
        .options(joinedload(RobotMissionSchedule.project))
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    _require_project_editor(schedule.project, current_user, db)
    robot = _resolve_robot_user(schedule.robot_user_id, db)
    _require_robot_project_upload_access(schedule.project, robot, db)

    now = utc_now()
    schedule.last_run_at = now
    mission = materialize_schedule(
        db,
        schedule=schedule,
        scheduled_for=now,
        enforce_busy_policy=False,
    )
    if mission is None:
        db.commit()
        raise HTTPException(status_code=409, detail=schedule.last_error or "Schedule cannot run")
    db.commit()
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission.id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    assert mission is not None
    return _mission_to_response(mission)


@router.delete("/robot/mission-schedules/{schedule_id}", status_code=204)
def delete_robot_mission_schedule(
    schedule_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    schedule = db.scalar(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.id == schedule_id)
        .options(joinedload(RobotMissionSchedule.project))
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    _require_project_editor(schedule.project, current_user, db)
    project_id = schedule.project_id
    schedule_name = schedule.name
    db.delete(schedule)
    db.commit()
    log_activity(
        db,
        project_id=project_id,
        actor=current_user,
        action="robot_mission_schedule.delete",
        target_type="robot_mission_schedule",
        target_id=schedule_id,
        metadata={"name": schedule_name},
    )
    return Response(status_code=204)


@router.get("/robot/missions", response_model=list[RobotMissionResponse])
def list_robot_missions(
    robot_id: str | None = Query(default=None),
    project_slug: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RobotMissionResponse]:
    stmt = (
        select(RobotMission)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
        .order_by(RobotMission.created_at.desc())
        .limit(limit)
    )

    if robot_id:
        robot = _resolve_robot_user(robot_id, db)
        stmt = stmt.where(RobotMission.robot_user_id == robot.id)
    if project_slug:
        project = db.scalar(select(Project).where(Project.slug == project_slug))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        stmt = stmt.where(RobotMission.project_id == project.id)
    if status:
        stmt = stmt.where(RobotMission.status == status)

    if not current_user.is_admin:
        if current_user.is_robot:
            stmt = stmt.where(RobotMission.robot_user_id == current_user.id)
        else:
            stmt = stmt.join(Project, RobotMission.project_id == Project.id).join(
                ProjectMember, ProjectMember.project_id == Project.id
            ).where(ProjectMember.user_id == current_user.id)

    missions = db.scalars(stmt).unique().all()
    return [_mission_to_response(mission) for mission in missions]


@router.post("/robot/missions", response_model=RobotMissionResponse, status_code=201)
def create_robot_mission(
    payload: RobotMissionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    robot = _resolve_robot_user(payload.robot_id, db)
    project = db.scalar(select(Project).where(Project.slug == payload.project_slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_editor(project, current_user, db)
    _require_robot_project_upload_access(project, robot, db)
    resolved_waypoints, room_slug_map, robot_meta = _resolve_mission_waypoints(
        payload=payload,
        project=project,
        db=db,
    )

    mission = RobotMission(
        robot_user_id=robot.id,
        robot_username=robot.username,
        project_id=project.id,
        requested_by_user_id=current_user.id,
        status="queued",
        capture_mode=payload.capture_mode,
        capture_date=payload.capture_date,
        waypoints_json=resolved_waypoints,
        room_slug_map_json=room_slug_map,
        retry_policy_json=payload.retry_policy,
        robot_meta_json=robot_meta,
    )
    db.add(mission)
    db.flush()

    for index, waypoint in enumerate(resolved_waypoints, start=1):
        waypoint_name = _waypoint_name(waypoint, index)
        room_slug = _room_slug_for_waypoint(waypoint, waypoint_name, room_slug_map)
        db.add(
            RobotMissionStep(
                mission_id=mission.id,
                sequence_index=index,
                waypoint_name=waypoint_name,
                room_slug=room_slug,
                status="pending",
            )
        )

    db.commit()

    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission.id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    assert mission is not None

    log_activity(
        db,
        project_id=project.id,
        actor=current_user,
        action="robot_mission.create",
        target_type="robot_mission",
        target_id=mission.id,
        metadata={
            "robot_id": robot.username,
            "capture_mode": mission.capture_mode,
            "waypoint_count": len(mission.waypoints_json or []),
        },
    )
    return _mission_to_response(mission)


@router.get("/robot/missions/{mission_id}", response_model=RobotMissionResponse)
def get_robot_mission(
    mission_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission_id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    if not current_user.is_admin:
        if current_user.is_robot:
            if current_user.id != mission.robot_user_id:
                raise HTTPException(status_code=403, detail="Mission not assigned to this robot")
        else:
            _require_project_editor(mission.project, current_user, db)
    return _mission_to_response(mission)


@router.post("/robot/missions/{mission_id}/cancel", response_model=RobotMissionResponse)
def cancel_robot_mission(
    mission_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission_id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _require_project_editor(mission.project, current_user, db)

    if mission.status in ("succeeded", "failed", "cancelled", "cancel_failed"):
        return _mission_to_response(mission)
    if mission.status in (
        "cancel_requested",
        "cancelling",
        "returning_to_start",
        "stop_requested",
    ):
        return _mission_to_response(mission)

    now = _utc_now()
    mission.cancel_requested_at = now
    mission.cancel_requested_by_user_id = current_user.id
    mission.cancel_error = None

    # A queued mission has never reached the robot, so it can be cancelled
    # synchronously. Once dispatched, cancellation is only complete after the
    # robot acknowledges that it stopped and returned to its start position.
    if mission.status == "queued":
        mission.status = "cancelled"
        mission.cancelled_at = now
        mission.cancel_acknowledged_at = now
        mission.completed_at = now
        for step in mission.steps:
            if step.status == "pending":
                step.status = "cancelled"
                step.completed_at = now
    else:
        mission.status = "cancel_requested"

    db.commit()
    db.refresh(mission)

    log_activity(
        db,
        project_id=mission.project_id,
        actor=current_user,
        action=(
            "robot_mission.cancel"
            if mission.status == "cancelled"
            else "robot_mission.cancel_request"
        ),
        target_type="robot_mission",
        target_id=mission.id,
        metadata={
            "robot_id": mission.robot_username,
            "capture_mode": mission.capture_mode,
            "waypoint_count": len(mission.waypoints_json or []),
        },
    )
    return _mission_to_response(mission)


@router.post("/robot/missions/{mission_id}/stop", response_model=RobotMissionResponse)
def stop_robot_mission_return(
    mission_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    """Cancel the return-to-start goal without treating it as a robot failure."""
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission_id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _require_project_editor(mission.project, current_user, db)

    if mission.status in ("succeeded", "failed", "cancelled", "cancel_failed"):
        return _mission_to_response(mission)
    if mission.status == "stop_requested":
        return _mission_to_response(mission)
    if mission.status != "returning_to_start":
        raise HTTPException(
            status_code=409,
            detail="The robot can only be stopped here while it is returning to start",
        )

    mission.status = "stop_requested"
    db.commit()
    db.refresh(mission)

    log_activity(
        db,
        project_id=mission.project_id,
        actor=current_user,
        action="robot_mission.stop_request",
        target_type="robot_mission",
        target_id=mission.id,
        metadata={
            "robot_id": mission.robot_username,
            "capture_mode": mission.capture_mode,
            "waypoint_count": len(mission.waypoints_json or []),
        },
    )
    return _mission_to_response(mission)


@router.post("/robot/missions/{mission_id}/force-close", response_model=RobotMissionResponse)
def force_close_robot_mission(
    mission_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    """Close a stale cancellation when no robot agent is online to acknowledge it."""
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission_id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _require_project_editor(mission.project, current_user, db)

    terminal_statuses = ("succeeded", "failed", "cancelled", "cancel_failed")
    if mission.status in terminal_statuses:
        return _mission_to_response(mission)
    if mission.status not in (
        "cancel_requested",
        "cancelling",
        "returning_to_start",
        "stop_requested",
    ):
        raise HTTPException(
            status_code=409,
            detail="Only a mission already being cancelled can be force-closed",
        )

    previous_status = mission.status
    now = _utc_now()
    result = dict(mission.result_json or {})
    progress_events = list(result.get("progress_events") or [])
    progress_events.append(
        {
            "id": "task:force_closed",
            "phase": "task_cancelled",
            "label": "Task closed without robot confirmation",
            "status": "cancelled",
            "detail": "Force-closed by the operator while the robot agent was unavailable",
            "completed_at_utc": now.replace(tzinfo=timezone.utc).isoformat(),
        }
    )
    result.update(
        {
            "mission_id": mission.id,
            "status": "CANCELLED",
            "steps": list(result.get("steps") or []),
            "return_to_start": {
                "status": "CANCELLED" if previous_status == "stop_requested" else "UNCONFIRMED",
                "navigation_result": "FORCE_CLOSED_BY_OPERATOR",
                "target_waypoint": "__return_to_start__",
                "room_slug": None,
            },
            "progress_events": progress_events,
            "force_closed": True,
            "force_closed_from_status": previous_status,
        }
    )

    mission.status = "cancelled"
    mission.result_json = result
    mission.cancel_requested_at = mission.cancel_requested_at or now
    mission.cancel_requested_by_user_id = current_user.id
    mission.cancel_acknowledged_at = now
    mission.cancelled_at = now
    mission.completed_at = now
    mission.cancel_error = None
    for step in mission.steps:
        if step.status not in ("succeeded", "failed", "cancelled"):
            step.status = "cancelled"
            step.completed_at = now

    presence = db.scalar(
        select(RobotPresence).where(RobotPresence.robot_user_id == mission.robot_user_id)
    )
    if presence and presence.current_mission_id == mission.id:
        presence.current_mission_id = None
        presence.status = "idle"

    db.commit()
    db.refresh(mission)
    log_activity(
        db,
        project_id=mission.project_id,
        actor=current_user,
        action="robot_mission.force_close",
        target_type="robot_mission",
        target_id=mission.id,
        metadata={
            "robot_id": mission.robot_username,
            "previous_status": previous_status,
        },
    )
    return _mission_to_response(mission)


@router.delete("/robot/missions/{mission_id}", status_code=200)
def delete_robot_mission(
    mission_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission_id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _require_project_editor(mission.project, current_user, db)

    presence = db.scalar(select(RobotPresence).where(RobotPresence.robot_user_id == mission.robot_user_id))
    if presence and presence.current_mission_id == mission.id:
        presence.current_mission_id = None
        if presence.status in ("running", "busy", "dispatched"):
            presence.status = "idle"

    project_id = mission.project_id
    robot_username = mission.robot_username
    capture_mode = mission.capture_mode
    waypoint_count = len(mission.waypoints_json or [])

    db.delete(mission)
    db.commit()

    log_activity(
        db,
        project_id=project_id,
        actor=current_user,
        action="robot_mission.delete",
        target_type="robot_mission",
        target_id=mission_id,
        metadata={
            "robot_id": robot_username,
            "capture_mode": capture_mode,
            "waypoint_count": waypoint_count,
        },
    )


@router.post("/robots/{robot_id}/heartbeat", response_model=RobotPresenceResponse)
def post_robot_heartbeat(
    robot_id: str,
    payload: RobotHeartbeatRequest,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotPresenceResponse:
    _require_robot_identity(robot_id, current_user)
    robot = _resolve_robot_user(robot_id, db)

    presence = db.scalar(select(RobotPresence).where(RobotPresence.robot_user_id == robot.id))
    if presence is None:
        presence = RobotPresence(
            robot_user_id=robot.id,
            robot_username=robot.username,
        )
        db.add(presence)

    presence.status = payload.status
    presence.current_mission_id = payload.current_mission_id
    presence.hostname = payload.hostname
    presence_payload = _presence_payload(presence)
    heartbeat_payload = payload.model_dump(mode="json")
    # A transient local-panel read failure must not erase the last connection state.
    # The heartbeat still proves liveness; only replace connection when the robot
    # supplied an authoritative value.
    if payload.connection is None:
        previous_heartbeat = presence_payload.get("heartbeat")
        if isinstance(previous_heartbeat, dict):
            previous_connection = previous_heartbeat.get("connection")
            if previous_connection in {
                "disconnected",
                "connecting",
                "connected",
                "disconnecting",
            }:
                heartbeat_payload["connection"] = previous_connection
    presence_payload["heartbeat"] = heartbeat_payload
    presence.payload_json = presence_payload
    # Liveness is measured by when this server received the heartbeat. Robot
    # clocks can drift or be reset, so a client-supplied timestamp must not be
    # able to keep an offline robot looking alive indefinitely.
    presence.last_seen_at = _utc_now()

    # Final command-status delivery can be lost during a brief network interruption. The
    # heartbeat is an independent, authenticated observation of the real control-panel state,
    # so use it to close only an active command whose requested target has actually been reached.
    _reconcile_active_command_from_connection(
        robot_user_id=robot.id,
        connection=payload.connection,
        db=db,
    )

    db.commit()
    db.refresh(presence)
    return _presence_to_response(presence)


@router.get("/robots/{robot_id}/status", response_model=RobotPresenceResponse)
def get_robot_status(
    robot_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotPresenceResponse:
    if not current_user.is_admin and not current_user.is_robot:
        raise HTTPException(status_code=403, detail="Administrator or robot access required")
    if current_user.is_robot:
        _require_robot_identity(robot_id, current_user)

    robot = _resolve_robot_user(robot_id, db)
    presence = db.scalar(select(RobotPresence).where(RobotPresence.robot_user_id == robot.id))
    if presence is None:
        raise HTTPException(status_code=404, detail="Robot status not found")
    return _presence_to_response(presence)


@router.post("/robots/{robot_id}/telemetry", response_model=RobotTelemetryResponse)
def post_robot_telemetry(
    robot_id: str,
    payload: RobotTelemetryRequest,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotTelemetryResponse:
    _require_robot_identity(robot_id, current_user)
    robot = _resolve_robot_user(robot_id, db)

    presence = db.scalar(select(RobotPresence).where(RobotPresence.robot_user_id == robot.id))
    if presence is None:
        presence = RobotPresence(
            robot_user_id=robot.id,
            robot_username=robot.username,
        )
        db.add(presence)

    telemetry = payload.model_dump(mode="json")
    telemetry["received_at_utc"] = datetime.now(timezone.utc).isoformat()
    presence_payload = _presence_payload(presence)
    presence_payload["telemetry"] = telemetry
    presence.payload_json = presence_payload
    if payload.status:
        presence.status = payload.status
    if payload.mission_id:
        presence.current_mission_id = payload.mission_id
    # Deliberately NOT touching last_seen_at here. Telemetry is produced on the laptop, which keeps
    # publishing (AMCL republishes a pose) long after the robot itself is powered off. Letting it
    # bump last_seen_at made a dead robot read as "online" forever. Robot liveness now comes only
    # from the agent's /heartbeat, which runs on the robot and stops the moment it powers down.

    db.commit()
    db.refresh(presence)
    response = _telemetry_to_response(presence)
    response_payload = response.model_dump(mode="json")
    with _LATEST_TELEMETRY_LOCK:
        _LATEST_TELEMETRY[robot.id] = response_payload
    _publish_robot_telemetry(robot.id, response_payload)
    return response


@router.get("/robots/{robot_id}/telemetry", response_model=RobotTelemetryResponse)
def get_robot_telemetry(
    robot_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotTelemetryResponse:
    if current_user.is_robot:
        _require_robot_identity(robot_id, current_user)
    robot = _resolve_robot_user(robot_id, db)
    payload = _latest_robot_telemetry_payload(robot.id, db)
    if payload is None:
        raise HTTPException(status_code=404, detail="Robot telemetry not found")
    return payload


@router.get("/robots/{robot_id}/telemetry/stream")
async def stream_robot_telemetry(
    robot_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    if current_user.is_robot:
        _require_robot_identity(robot_id, current_user)
    robot = _resolve_robot_user(robot_id, db)
    robot_user_id = robot.id

    async def event_stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        last_signature: str | None = None
        last_keepalive_at = datetime.now(timezone.utc).timestamp()
        stream_db = SessionLocal()
        with _TELEMETRY_SUBSCRIBERS_LOCK:
            _TELEMETRY_SUBSCRIBERS.setdefault(robot_user_id, set()).add((loop, queue))
        try:
            initial_payload = _latest_robot_telemetry_payload(robot_user_id, stream_db)
            if initial_payload is not None:
                _put_latest_telemetry(queue, initial_payload)
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    payload = _latest_robot_telemetry_payload(robot_user_id, stream_db)

                if payload is not None:
                    signature = _telemetry_signature(payload)
                    if signature != last_signature:
                        last_signature = signature
                        last_keepalive_at = datetime.now(timezone.utc).timestamp()
                        yield f"{json.dumps(payload, separators=(',', ':'))}\n"

                now = datetime.now(timezone.utc).timestamp()
                if now - last_keepalive_at >= 15:
                    last_keepalive_at = now
                    yield "\n"
        finally:
            with _TELEMETRY_SUBSCRIBERS_LOCK:
                subscribers = _TELEMETRY_SUBSCRIBERS.get(robot_user_id)
                if subscribers is not None:
                    subscribers.discard((loop, queue))
                    if not subscribers:
                        _TELEMETRY_SUBSCRIBERS.pop(robot_user_id, None)
            stream_db.close()

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.websocket("/robots/{robot_id}/telemetry/ws")
async def websocket_robot_telemetry(
    websocket: WebSocket,
    robot_id: str,
    token: str | None = Query(None),
) -> None:
    auth_db = SessionLocal()
    try:
        try:
            current_user = _current_user_from_access_token(token, auth_db)
            if current_user.is_robot:
                _require_robot_identity(robot_id, current_user)
            robot = _resolve_robot_user(robot_id, auth_db)
        except HTTPException as exc:
            await websocket.close(code=1008, reason=str(exc.detail))
            return

        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        robot_user_id = robot.id
        last_signature: str | None = None
        last_keepalive_at = datetime.now(timezone.utc).timestamp()

        with _TELEMETRY_SUBSCRIBERS_LOCK:
            _TELEMETRY_SUBSCRIBERS.setdefault(robot_user_id, set()).add((loop, queue))

        try:
            initial_payload = _latest_robot_telemetry_payload(robot_user_id, auth_db)
            if initial_payload is not None:
                last_signature = _telemetry_signature(initial_payload)
                await websocket.send_json(initial_payload)

            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    payload = _latest_robot_telemetry_payload(robot_user_id, auth_db)

                if payload is not None:
                    signature = _telemetry_signature(payload)
                    if signature != last_signature:
                        last_signature = signature
                        last_keepalive_at = datetime.now(timezone.utc).timestamp()
                        await websocket.send_json(payload)
                        continue

                now = datetime.now(timezone.utc).timestamp()
                if now - last_keepalive_at >= 15:
                    last_keepalive_at = now
                    await websocket.send_json({
                        "type": "keepalive",
                        "server_time_utc": datetime.now(timezone.utc).isoformat(),
                    })
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass
        finally:
            with _TELEMETRY_SUBSCRIBERS_LOCK:
                subscribers = _TELEMETRY_SUBSCRIBERS.get(robot_user_id)
                if subscribers is not None:
                    subscribers.discard((loop, queue))
                    if not subscribers:
                        _TELEMETRY_SUBSCRIBERS.pop(robot_user_id, None)
    finally:
        auth_db.close()


@router.websocket("/robots/{robot_id}/telemetry/ingest")
async def websocket_robot_telemetry_ingest(
    websocket: WebSocket,
    robot_id: str,
    token: str | None = Query(None),
) -> None:
    """Persistent uplink for the robot's telemetry bridge.

    The bridge used to POST each frame, which pays a WAN round trip of HTTP overhead
    per frame and forced a Postgres commit per frame on this side. Over one long-lived
    socket a frame is a single small message: it is validated, cached in memory, fanned
    out to live subscribers, and only flushed to the presence row every
    _TELEMETRY_PERSIST_SECONDS so the last pose survives a restart.
    """
    db = SessionLocal()
    try:
        try:
            current_user = _current_user_from_access_token(token, db)
            if not current_user.is_robot:
                raise HTTPException(status_code=403, detail="Robot account required")
            _require_robot_identity(robot_id, current_user)
            robot = _resolve_robot_user(robot_id, db)
        except HTTPException as exc:
            await websocket.close(code=1008, reason=str(exc.detail))
            return

        await websocket.accept()
        robot_user_id = robot.id
        robot_username = robot.username
        last_persisted = 0.0
        pending: tuple[RobotTelemetryRequest, dict] | None = None

        def persist(payload: RobotTelemetryRequest, telemetry: dict) -> None:
            presence = db.scalar(
                select(RobotPresence).where(RobotPresence.robot_user_id == robot_user_id)
            )
            if presence is None:
                presence = RobotPresence(robot_user_id=robot_user_id, robot_username=robot_username)
                db.add(presence)
            presence_payload = _presence_payload(presence)
            presence_payload["telemetry"] = telemetry
            presence.payload_json = presence_payload
            if payload.status:
                presence.status = payload.status
            if payload.mission_id:
                presence.current_mission_id = payload.mission_id
            # See the HTTP /telemetry note: liveness comes only from the robot's /heartbeat, never
            # from laptop-produced telemetry, so a powered-off robot stops reading as online.
            db.commit()

        try:
            while True:
                raw = await websocket.receive_json()
                try:
                    payload = RobotTelemetryRequest.model_validate(raw)
                except ValidationError:
                    # One malformed frame should not kill the uplink.
                    continue

                telemetry = payload.model_dump(mode="json")
                telemetry["received_at_utc"] = datetime.now(timezone.utc).isoformat()
                response_payload = {**telemetry, "robot_id": robot_username}
                pending = (payload, telemetry)

                with _LATEST_TELEMETRY_LOCK:
                    _LATEST_TELEMETRY[robot_user_id] = response_payload
                _publish_robot_telemetry(robot_user_id, response_payload)

                now = time.monotonic()
                if now - last_persisted >= _TELEMETRY_PERSIST_SECONDS:
                    last_persisted = now
                    persist(payload, telemetry)
                    pending = None
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if pending is not None:
                # Flush the last unpersisted frame so a restart resumes from the true pose.
                try:
                    persist(*pending)
                except Exception:
                    db.rollback()
    finally:
        db.close()


@router.get(
    "/robots/{robot_id}/missions/next",
    response_model=RobotMissionResponse,
    responses={204: {"description": "No queued mission for this robot"}},
)
def get_next_robot_mission(
    robot_id: str,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotMissionResponse | Response:
    _require_robot_identity(robot_id, current_user)

    mission = db.scalar(
        select(RobotMission)
        .where(
            RobotMission.robot_user_id == current_user.id,
            RobotMission.status == "queued",
        )
        .order_by(RobotMission.created_at.asc())
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        return Response(status_code=204)

    mission.status = "dispatched"
    mission.dispatched_at = _utc_now()
    db.commit()
    db.refresh(mission)
    return _mission_to_response(mission)


@router.get(
    "/robots/{robot_id}/missions/{mission_id}/control",
    response_model=RobotMissionControlResponse,
)
def get_robot_mission_control(
    robot_id: str,
    mission_id: str,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotMissionControlResponse:
    _require_robot_identity(robot_id, current_user)
    mission = db.scalar(
        select(RobotMission).where(
            RobotMission.id == mission_id,
            RobotMission.robot_user_id == current_user.id,
        )
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    result = dict(mission.result_json or {})
    return RobotMissionControlResponse(
        mission_id=mission.id,
        status=mission.status,
        cancel_requested=mission.status in (
            "cancel_requested",
            "cancelling",
            "returning_to_start",
            "stop_requested",
        ),
        stop_requested=mission.status == "stop_requested",
        # If the backend gave up waiting because the robot was unreachable,
        # tell a still-running old agent to stop its work as soon as the link
        # returns. The mission remains failed because return was not confirmed.
        abort_requested=(
            mission.status == "failed"
            and result.get("failure_code")
            == "ROBOT_UNREACHABLE_DURING_CANCELLATION"
        ),
        cancel_requested_at=mission.cancel_requested_at,
    )


@router.post("/robot/missions/{mission_id}/status", response_model=RobotMissionResponse)
def post_robot_mission_status(
    mission_id: str,
    payload: RobotMissionStatusUpdateRequest,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotMissionResponse:
    mission = db.scalar(
        select(RobotMission)
        .where(RobotMission.id == mission_id)
        .options(joinedload(RobotMission.project), selectinload(RobotMission.steps))
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    if mission.robot_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Mission not assigned to this robot")

    terminal_statuses = ("succeeded", "failed", "cancelled", "cancel_failed")
    cancellation_statuses = (
        "cancel_requested",
        "cancelling",
        "returning_to_start",
        "stop_requested",
        "cancelled",
        "cancel_failed",
    )

    # Terminal states are sticky. In particular, a late success from work that
    # was already in flight must never revive an acknowledged cancellation.
    if mission.status in terminal_statuses:
        return _mission_to_response(mission)

    # Once an operator asks to stop the return journey, an in-flight status
    # update must not put the mission back into "returning_to_start". Only the
    # robot's terminal acknowledgement may advance this state.
    if mission.status == "stop_requested" and payload.status not in terminal_statuses:
        if payload.result is not None:
            mission.result_json = payload.result
            _apply_step_results(mission, payload.result)
        db.commit()
        db.refresh(mission)
        return _mission_to_response(mission)

    cancellation_in_progress = mission.status in cancellation_statuses
    effective_status = payload.status
    if cancellation_in_progress and payload.status == "succeeded":
        # The cancel request won a race with the robot's final success. Work may
        # already have completed, but it must not revive the task as succeeded.
        effective_status = "cancelled"
    elif cancellation_in_progress and payload.status not in cancellation_statuses:
        # Progress payloads can still enrich the timeline while cancellation is
        # being processed, but they cannot downgrade the mission back to running.
        # A final failure proves that the runner stopped, but it does not by
        # itself prove that cancellation returned the robot to start. Only close
        # as cancelled when the robot result confirms that return. Otherwise use
        # cancel_failed so the UI never implies that the robot is safely home.
        if payload.status == "failed":
            return_result = (
                payload.result.get("return_to_start")
                if isinstance(payload.result, dict)
                else None
            )
            return_status = (
                str(return_result.get("status") or "").upper()
                if isinstance(return_result, dict)
                else ""
            )
            return_confirmed = return_status == "SUCCEEDED" or (
                mission.status == "stop_requested" and return_status == "CANCELLED"
            )
            effective_status = "cancelled" if return_confirmed else "cancel_failed"
        else:
            if payload.result is not None:
                mission.result_json = payload.result
                _apply_step_results(mission, payload.result)
            db.commit()
            db.refresh(mission)
            return _mission_to_response(mission)

    mission.status = effective_status
    if payload.started_at_utc and mission.started_at is None:
        mission.started_at = payload.started_at_utc.replace(tzinfo=None)
    if payload.completed_at_utc:
        mission.completed_at = payload.completed_at_utc.replace(tzinfo=None)
    elif effective_status in terminal_statuses:
        mission.completed_at = _utc_now()
    if effective_status == "running":
        mission.started_at = mission.started_at or payload.started_at_utc or _utc_now()
    if effective_status == "cancelled":
        mission.cancelled_at = mission.completed_at or _utc_now()
        mission.cancel_acknowledged_at = mission.cancelled_at
        mission.cancel_error = None
    elif effective_status == "cancel_failed":
        mission.cancel_acknowledged_at = mission.completed_at or _utc_now()
    if payload.result is not None:
        mission.result_json = payload.result
        _apply_step_results(mission, payload.result)
        if effective_status == "cancel_failed":
            return_result = payload.result.get("return_to_start")
            mission.cancel_error = (
                str(return_result.get("error"))
                if isinstance(return_result, dict) and return_result.get("error")
                else str(payload.result.get("error") or "Return to start failed")
            )

    if effective_status in ("cancelled", "cancel_failed"):
        completed = mission.completed_at or _utc_now()
        for step in mission.steps:
            if step.status not in ("succeeded", "failed", "cancelled"):
                step.status = "cancelled"
                step.completed_at = completed

    if effective_status in terminal_statuses:
        presence = db.scalar(
            select(RobotPresence).where(RobotPresence.robot_user_id == mission.robot_user_id)
        )
        if presence and presence.current_mission_id == mission.id:
            presence.current_mission_id = None
            presence.status = "idle"

    db.commit()
    db.refresh(mission)

    if mission.status in terminal_statuses:
        log_activity(
            db,
            project_id=mission.project_id,
            actor=current_user,
            action=f"robot_mission.{mission.status}",
            target_type="robot_mission",
            target_id=mission.id,
            metadata={
                "robot_id": mission.robot_username,
                "step_count": len(mission.steps),
            },
        )
    return _mission_to_response(mission)


# -- robot lifecycle commands (connect / disconnect) ----------------------------
# These ride the same claim-by-poll / report-by-status rails as missions: the operator
# enqueues one, the on-site agent claims the next queued one and drives the laptop panel's
# bring-up choreography, and reports the progress tree back for the "Connect robot" button.


@router.post("/robot/commands", response_model=RobotCommandResponse, status_code=201)
def create_robot_command(
    payload: RobotCommandCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotCommandResponse:
    robot = _resolve_robot_user(payload.robot_id, db)

    # Robots are not scoped per-user in this tool (list_robots returns them all), so any
    # authenticated operator may connect one — matching how missions and presence already work.

    # One lifecycle command in flight at a time, so the progress tree can't show two
    # overlapping sequences. A repeat of the same kind is idempotent (returns the in-flight
    # one) so an operator clicking twice — or while the agent is still offline — is harmless;
    # only a conflicting kind is rejected.
    existing = db.scalar(
        select(RobotCommand)
        .where(
            RobotCommand.robot_user_id == robot.id,
            RobotCommand.status.in_(_ACTIVE_COMMAND_STATUSES),
        )
        .order_by(RobotCommand.created_at.desc())
    )
    if existing is not None:
        if existing.kind == payload.kind:
            return _command_to_response(existing)
        raise HTTPException(
            status_code=409,
            detail=f"A {existing.kind} is already in progress for this robot",
        )

    command = RobotCommand(
        robot_user_id=robot.id,
        robot_username=robot.username,
        requested_by_user_id=current_user.id,
        kind=payload.kind,
        status="queued",
        connection="connecting" if payload.kind == "connect" else "disconnecting",
    )
    db.add(command)
    db.commit()
    db.refresh(command)
    return _command_to_response(command)


@router.get(
    "/robots/{robot_id}/commands/next",
    response_model=RobotCommandResponse,
    responses={204: {"description": "No queued command for this robot"}},
)
def get_next_robot_command(
    robot_id: str,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotCommandResponse | Response:
    _require_robot_identity(robot_id, current_user)

    command = db.scalar(
        select(RobotCommand)
        .where(
            RobotCommand.robot_user_id == current_user.id,
            RobotCommand.status == "queued",
        )
        .order_by(RobotCommand.created_at.asc())
    )
    if command is None:
        return Response(status_code=204)

    command.status = "dispatched"
    command.dispatched_at = _utc_now()
    db.commit()
    db.refresh(command)
    return _command_to_response(command)


@router.post("/robot/commands/{command_id}/status", response_model=RobotCommandResponse)
def post_robot_command_status(
    command_id: str,
    payload: RobotCommandStatusUpdateRequest,
    current_user: User = Depends(require_robot),
    db: Session = Depends(get_db),
) -> RobotCommandResponse:
    command = db.scalar(select(RobotCommand).where(RobotCommand.id == command_id))
    if command is None:
        raise HTTPException(status_code=404, detail="Command not found")
    if command.robot_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Command not assigned to this robot")

    presence_changed = _record_robot_connection(
        robot_user_id=command.robot_user_id,
        robot_username=command.robot_username,
        connection=payload.connection,
        db=db,
    )

    # An operator can cancel a command mid-run; that is terminal and sticky, so a late agent
    # update must not revive command history. Its physical connection state is still recorded
    # above, because the panel may have finished the bring-up in the background.
    if command.status == "cancelled":
        if presence_changed:
            db.commit()
        return _command_to_response(command)

    command.status = payload.status
    if payload.connection is not None:
        command.connection = payload.connection
    if payload.detail is not None:
        command.detail = payload.detail
    if payload.progress_events is not None:
        command.progress_json = {"progress_events": payload.progress_events}
    if payload.completed_at_utc:
        command.completed_at = payload.completed_at_utc.replace(tzinfo=None)
    elif payload.status in ("succeeded", "failed"):
        command.completed_at = _utc_now()

    db.commit()
    db.refresh(command)
    return _command_to_response(command)


@router.get(
    "/robots/{robot_id}/commands/latest",
    response_model=RobotCommandResponse,
    responses={204: {"description": "This robot has no commands yet"}},
)
def get_latest_robot_command(
    robot_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotCommandResponse | Response:
    robot = _resolve_robot_user(robot_id, db)
    command = db.scalar(
        select(RobotCommand)
        .where(RobotCommand.robot_user_id == robot.id)
        .order_by(RobotCommand.created_at.desc())
    )
    if command is None:
        return Response(status_code=204)
    return _command_to_response(command)


@router.post("/robot/commands/{command_id}/cancel", response_model=RobotCommandResponse)
def cancel_robot_command(
    command_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotCommandResponse:
    command = db.scalar(select(RobotCommand).where(RobotCommand.id == command_id))
    if command is None:
        raise HTTPException(status_code=404, detail="Command not found")

    # Only an in-flight command can be cancelled; a finished one is returned unchanged so the
    # UI's "cancel then retry" is always safe to call.
    if command.status in _ACTIVE_COMMAND_STATUSES:
        command.status = "cancelled"
        command.connection = "disconnected"
        command.detail = "Cancelled by the operator."
        command.completed_at = _utc_now()
        db.commit()
        db.refresh(command)
    return _command_to_response(command)
