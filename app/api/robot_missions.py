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
    RobotMissionResponse,
    RobotMissionStatusUpdateRequest,
    RobotMissionStepResponse,
    RobotPresenceResponse,
    RobotSummaryResponse,
    RobotTelemetryRequest,
    RobotTelemetryResponse,
)
from app.services.activity import log_activity

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
        created_at=mission.created_at,
        dispatched_at=mission.dispatched_at,
        started_at=mission.started_at,
        completed_at=mission.completed_at,
        cancelled_at=mission.cancelled_at,
        steps=[_step_to_response(step) for step in sorted(mission.steps, key=lambda s: s.sequence_index)],
        result=mission.result_json,
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
        last_seen_at=presence.last_seen_at,
    )


def _presence_payload(presence: RobotPresence) -> dict:
    return dict(presence.payload_json or {}) if isinstance(presence.payload_json, dict) else {}


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

    if mission.status in ("succeeded", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail="Mission already finished")

    mission.status = "cancelled"
    mission.cancelled_at = _utc_now()
    mission.completed_at = mission.completed_at or mission.cancelled_at
    for step in mission.steps:
        if step.status in ("pending", "queued", "dispatched", "running", "navigating", "capturing", "uploading"):
            step.status = "cancelled"
            step.completed_at = mission.cancelled_at

    presence = db.scalar(select(RobotPresence).where(RobotPresence.robot_user_id == mission.robot_user_id))
    if presence and presence.current_mission_id == mission.id:
        presence.current_mission_id = None
        presence.status = "idle"

    db.commit()
    db.refresh(mission)

    log_activity(
        db,
        project_id=mission.project_id,
        actor=current_user,
        action="robot_mission.cancel",
        target_type="robot_mission",
        target_id=mission.id,
        metadata={
            "robot_id": mission.robot_username,
            "capture_mode": mission.capture_mode,
            "waypoint_count": len(mission.waypoints_json or []),
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
    presence_payload["heartbeat"] = payload.model_dump(mode="json")
    presence.payload_json = presence_payload
    presence.last_seen_at = (
        payload.reported_at_utc.replace(tzinfo=None) if payload.reported_at_utc else _utc_now()
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

    now = _utc_now()
    telemetry = payload.model_dump(mode="json")
    telemetry["received_at_utc"] = datetime.now(timezone.utc).isoformat()
    presence_payload = _presence_payload(presence)
    presence_payload["telemetry"] = telemetry
    presence.payload_json = presence_payload
    if payload.status:
        presence.status = payload.status
    if payload.mission_id:
        presence.current_mission_id = payload.mission_id
    presence.last_seen_at = payload.reported_at_utc.replace(tzinfo=None) if payload.reported_at_utc else now

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
            presence.last_seen_at = (
                payload.reported_at_utc.replace(tzinfo=None) if payload.reported_at_utc else _utc_now()
            )
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

    mission.status = payload.status
    if payload.started_at_utc and mission.started_at is None:
        mission.started_at = payload.started_at_utc.replace(tzinfo=None)
    if payload.completed_at_utc:
        mission.completed_at = payload.completed_at_utc.replace(tzinfo=None)
    elif payload.status in ("succeeded", "failed", "cancelled"):
        mission.completed_at = _utc_now()
    if payload.status == "running":
        mission.started_at = mission.started_at or payload.started_at_utc or _utc_now()
    if payload.status == "cancelled":
        mission.cancelled_at = mission.completed_at or _utc_now()
    if payload.result is not None:
        mission.result_json = payload.result
        _apply_step_results(mission, payload.result)

    db.commit()
    db.refresh(mission)

    if mission.status in ("succeeded", "failed", "cancelled"):
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
