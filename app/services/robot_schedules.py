"""Recurring robot mission calculation and dispatch.

Schedules stay in the backend.  Each due occurrence is copied into the existing
one-shot mission queue, so the robot agent does not need clock, timezone, or
recurrence logic.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import logging
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    RobotCapturePoint,
    RobotCommand,
    RobotMission,
    RobotMissionSchedule,
    RobotMissionStep,
)

logger = logging.getLogger(__name__)

_ACTIVE_MISSION_STATUSES = (
    "queued",
    "dispatched",
    "running",
    "cancel_requested",
    "cancelling",
    "returning_to_start",
    "stop_requested",
)
_ACTIVE_COMMAND_STATUSES = ("queued", "dispatched", "running")


def utc_now() -> datetime:
    """Return the application's conventional naive UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_timezone(timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc


def next_schedule_run(
    *,
    local_time: str,
    timezone_name: str,
    weekdays: list[int],
    after_utc: datetime,
) -> datetime:
    """Return the first matching wall-clock occurrence strictly after ``after_utc``.

    Returned datetimes are naive UTC to match the rest of the backend models.
    Python weekday numbers are used: Monday=0 through Sunday=6.
    """
    validate_timezone(timezone_name)
    if not weekdays or any(day < 0 or day > 6 for day in weekdays):
        raise ValueError("At least one weekday from 0 through 6 is required")

    hour, minute = (int(part) for part in local_time.split(":", maxsplit=1))
    wall_time = datetime_time(hour=hour, minute=minute)
    zone = ZoneInfo(timezone_name)
    aware_after = (
        after_utc.replace(tzinfo=timezone.utc)
        if after_utc.tzinfo is None
        else after_utc.astimezone(timezone.utc)
    )
    local_after = aware_after.astimezone(zone)

    for offset in range(8):
        candidate_date = local_after.date() + timedelta(days=offset)
        if candidate_date.weekday() not in weekdays:
            continue
        local_candidate = datetime.combine(candidate_date, wall_time, tzinfo=zone)
        candidate_utc = local_candidate.astimezone(timezone.utc)
        if candidate_utc > aware_after:
            return candidate_utc.replace(tzinfo=None)

    raise ValueError("Could not calculate the next schedule occurrence")


def scheduled_capture_date(scheduled_for: datetime, timezone_name: str) -> date:
    aware = (
        scheduled_for.replace(tzinfo=timezone.utc)
        if scheduled_for.tzinfo is None
        else scheduled_for.astimezone(timezone.utc)
    )
    return aware.astimezone(ZoneInfo(timezone_name)).date()


def ordered_capture_points(
    db: Session,
    *,
    project_id: str,
    capture_point_ids: list[str],
) -> tuple[list[RobotCapturePoint], list[str]]:
    points = db.scalars(
        select(RobotCapturePoint).where(
            RobotCapturePoint.project_id == project_id,
            RobotCapturePoint.id.in_(capture_point_ids),
        )
    ).all()
    by_id = {point.id: point for point in points}
    missing = [point_id for point_id in capture_point_ids if point_id not in by_id]
    return [by_id[point_id] for point_id in capture_point_ids if point_id in by_id], missing


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


def _robot_has_active_mission(db: Session, robot_user_id: str) -> bool:
    return db.scalar(
        select(RobotMission.id)
        .where(
            RobotMission.robot_user_id == robot_user_id,
            RobotMission.status.in_(_ACTIVE_MISSION_STATUSES),
        )
        .limit(1)
    ) is not None


def _queue_connect_if_needed(db: Session, schedule: RobotMissionSchedule) -> None:
    if not schedule.auto_connect:
        return

    active = db.scalar(
        select(RobotCommand)
        .where(
            RobotCommand.robot_user_id == schedule.robot_user_id,
            RobotCommand.status.in_(_ACTIVE_COMMAND_STATUSES),
        )
        .order_by(RobotCommand.created_at.desc())
    )
    if active is not None:
        return

    latest = db.scalar(
        select(RobotCommand)
        .where(RobotCommand.robot_user_id == schedule.robot_user_id)
        .order_by(RobotCommand.created_at.desc())
        .limit(1)
    )
    if latest is not None and latest.connection == "connected":
        return

    db.add(
        RobotCommand(
            robot_user_id=schedule.robot_user_id,
            robot_username=schedule.robot_username,
            requested_by_user_id=schedule.requested_by_user_id,
            kind="connect",
            status="queued",
            connection="connecting",
            detail=f"Queued automatically by schedule {schedule.name}",
        )
    )


def materialize_schedule(
    db: Session,
    *,
    schedule: RobotMissionSchedule,
    scheduled_for: datetime,
    enforce_busy_policy: bool = True,
) -> RobotMission | None:
    """Create the immutable one-shot mission for one schedule occurrence."""
    if (
        enforce_busy_policy
        and schedule.busy_policy == "skip"
        and _robot_has_active_mission(db, schedule.robot_user_id)
    ):
        schedule.last_outcome = "skipped_busy"
        schedule.last_error = "Robot already has a queued or active task"
        return None

    capture_point_ids = [str(item) for item in (schedule.capture_point_ids_json or [])]
    points, missing = ordered_capture_points(
        db,
        project_id=schedule.project_id,
        capture_point_ids=capture_point_ids,
    )
    if missing:
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.last_outcome = "invalid"
        schedule.last_error = f"Capture point no longer exists: {missing[0]}"
        return None

    waypoints = [_capture_point_to_waypoint(point) for point in points]
    room_slug_map = {point.name: point.room_slug or point.name for point in points}
    robot_meta = dict(schedule.robot_meta_json or {})
    robot_meta.update(
        {
            "capture_point_ids": capture_point_ids,
            "source": "schedule",
            "schedule_id": schedule.id,
            "schedule_name": schedule.name,
            "scheduled_for": scheduled_for.replace(tzinfo=timezone.utc).isoformat(),
        }
    )

    mission = RobotMission(
        robot_user_id=schedule.robot_user_id,
        robot_username=schedule.robot_username,
        project_id=schedule.project_id,
        requested_by_user_id=schedule.requested_by_user_id,
        schedule_id=schedule.id,
        scheduled_for=scheduled_for,
        status="queued",
        capture_mode=schedule.capture_mode,
        capture_date=scheduled_capture_date(scheduled_for, schedule.timezone),
        waypoints_json=waypoints,
        room_slug_map_json=room_slug_map,
        retry_policy_json=dict(schedule.retry_policy_json or {}),
        robot_meta_json=robot_meta,
    )
    db.add(mission)
    db.flush()

    for index, point in enumerate(points, start=1):
        db.add(
            RobotMissionStep(
                mission_id=mission.id,
                sequence_index=index,
                waypoint_name=point.name,
                room_slug=point.room_slug or point.name,
                status="pending",
            )
        )

    _queue_connect_if_needed(db, schedule)
    schedule.last_outcome = "queued"
    schedule.last_error = None
    return mission


def dispatch_due_schedules(now: datetime | None = None, *, limit: int = 100) -> int:
    """Materialize due occurrences, returning the number of missions created."""
    effective_now = now or utc_now()
    created = 0

    with SessionLocal() as db:
        for _ in range(limit):
            schedule = db.scalar(
                select(RobotMissionSchedule)
                .where(
                    RobotMissionSchedule.enabled.is_(True),
                    RobotMissionSchedule.next_run_at.is_not(None),
                    RobotMissionSchedule.next_run_at <= effective_now,
                )
                .order_by(RobotMissionSchedule.next_run_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if schedule is None:
                break

            scheduled_for = schedule.next_run_at
            assert scheduled_for is not None
            schedule.last_run_at = effective_now
            schedule.next_run_at = next_schedule_run(
                local_time=schedule.local_time,
                timezone_name=schedule.timezone,
                weekdays=list(schedule.weekdays_json or []),
                after_utc=effective_now,
            )

            lateness = effective_now - scheduled_for
            if lateness > timedelta(minutes=schedule.max_lateness_minutes):
                schedule.last_outcome = "missed"
                schedule.last_error = (
                    f"Backend processed this occurrence {int(lateness.total_seconds() // 60)} "
                    "minutes late"
                )
            else:
                mission = materialize_schedule(
                    db,
                    schedule=schedule,
                    scheduled_for=scheduled_for,
                )
                if mission is not None:
                    created += 1

            db.commit()

    return created


async def run_schedule_dispatcher(
    *,
    interval_seconds: float = 15.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the lightweight dispatcher until cancelled or explicitly stopped."""
    logger.info("Robot mission schedule dispatcher started")
    while stop_event is None or not stop_event.is_set():
        try:
            created = await asyncio.to_thread(dispatch_due_schedules)
            if created:
                logger.info("Queued %d scheduled robot mission(s)", created)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Robot mission schedule dispatch failed")

        try:
            if stop_event is None:
                await asyncio.sleep(interval_seconds)
            else:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("Robot mission schedule dispatcher stopped")
