from __future__ import annotations

from datetime import datetime, timezone
import math
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_current_user, require_robot
from app.database import get_db
from app.models import Project, ProjectMember, RobotCapturePoint, RobotMission, RobotMissionStep, RobotPresence, User
from app.schemas import (
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
)
from app.services.activity import log_activity

router = APIRouter()


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
    presence.payload_json = payload.model_dump(mode="json")
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
