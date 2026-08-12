"""Close cancellation requests when the assigned robot is no longer reachable."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import SessionLocal
from app.models import RobotMission, RobotPresence

logger = logging.getLogger(__name__)

CANCELLATION_STATUSES = (
    "cancel_requested",
    "cancelling",
    "returning_to_start",
    "stop_requested",
)
ROBOT_UNREACHABLE_SECONDS = 120
CANCEL_DELIVERY_GRACE_SECONDS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fail_unreachable_cancellations(
    db: Session,
    *,
    now: datetime | None = None,
    heartbeat_timeout_seconds: int = ROBOT_UNREACHABLE_SECONDS,
    delivery_grace_seconds: int = CANCEL_DELIVERY_GRACE_SECONDS,
) -> int:
    """Fail cancellations whose robot heartbeat has gone stale.

    A separate grace from ``cancel_requested_at`` prevents a robot that is only
    momentarily between heartbeats from being failed immediately after the
    operator presses Cancel. The terminal result is deliberately ``failed``:
    without a live robot, return-to-start cannot be confirmed.
    """
    effective_now = now or utc_now()
    heartbeat_cutoff = effective_now - timedelta(seconds=heartbeat_timeout_seconds)
    delivery_cutoff = effective_now - timedelta(seconds=delivery_grace_seconds)
    missions = db.scalars(
        select(RobotMission)
        .where(
            RobotMission.status.in_(CANCELLATION_STATUSES),
            RobotMission.cancel_requested_at.is_not(None),
            RobotMission.cancel_requested_at <= delivery_cutoff,
        )
        .options(selectinload(RobotMission.steps))
        .with_for_update(skip_locked=True)
    ).all()

    failed = 0
    for mission in missions:
        presence = db.scalar(
            select(RobotPresence).where(
                RobotPresence.robot_user_id == mission.robot_user_id
            )
        )
        if presence is not None and presence.last_seen_at > heartbeat_cutoff:
            continue

        detail = (
            f"SiteScope could not reach robot {mission.robot_username} for at least "
            f"{heartbeat_timeout_seconds} seconds while cancellation was in progress. "
            "Return to the start position was not confirmed."
        )
        result = dict(mission.result_json or {})
        progress_events = list(result.get("progress_events") or [])
        progress_events.append(
            {
                "id": "task:robot_unreachable",
                "phase": "task_failed",
                "label": "Task failed — robot unreachable",
                "status": "failed",
                "detail": detail,
                "completed_at_utc": effective_now.replace(
                    tzinfo=timezone.utc
                ).isoformat(),
            }
        )
        result.update(
            {
                "mission_id": mission.id,
                "status": "FAILED",
                "error": detail,
                "failure_code": "ROBOT_UNREACHABLE_DURING_CANCELLATION",
                "progress_events": progress_events,
                "return_to_start": {
                    "status": "UNCONFIRMED",
                    "error": detail,
                    "navigation_result": "ROBOT_UNREACHABLE",
                    "target_waypoint": "__return_to_start__",
                    "room_slug": None,
                },
            }
        )

        mission.status = "failed"
        mission.result_json = result
        mission.completed_at = effective_now
        mission.cancel_error = detail
        for step in mission.steps:
            if step.status == "running":
                step.status = "failed"
                step.error_message = step.error_message or detail
                step.completed_at = effective_now
            elif step.status == "pending":
                step.status = "cancelled"
                step.completed_at = effective_now

        if presence is not None and presence.current_mission_id == mission.id:
            presence.current_mission_id = None
            presence.status = "offline"

        failed += 1
        logger.warning(
            "Failed cancellation for mission %s: robot %s unreachable",
            mission.id,
            mission.robot_username,
        )

    if failed:
        db.commit()
    return failed


async def run_mission_cancellation_watchdog(
    *,
    interval_seconds: float = 10.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodically close unreachable cancellations until shutdown."""
    logger.info("Robot mission cancellation watchdog started")
    while stop_event is None or not stop_event.is_set():
        try:
            def run_once() -> int:
                # Construct and use the SQLAlchemy session in the same worker
                # thread; sessions and SQLite connections are not thread-safe.
                with SessionLocal() as db:
                    return fail_unreachable_cancellations(db)

            failed = await asyncio.to_thread(run_once)
            if failed:
                logger.warning("Failed %d unreachable robot cancellation(s)", failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Robot mission cancellation watchdog failed")

        try:
            if stop_event is None:
                await asyncio.sleep(interval_seconds)
            else:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("Robot mission cancellation watchdog stopped")
