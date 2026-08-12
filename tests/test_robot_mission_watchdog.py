import os
import unittest
from datetime import date, datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.robot_missions import get_robot_mission_control
from app.database import Base
from app.models import Project, RobotMission, RobotMissionStep, RobotPresence, User
from app.services.robot_mission_watchdog import fail_unreachable_cancellations


class RobotMissionWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _make_cancelling_mission(
        self,
        db: Session,
        *,
        now: datetime,
        heartbeat_age_seconds: int,
        cancellation_age_seconds: int,
    ) -> tuple[User, RobotMission, RobotPresence]:
        operator = User(username="operator", password_hash="x", is_admin=True)
        robot = User(username="robot-1", password_hash="x", is_robot=True)
        project = Project(name="Demo", slug="demo")
        db.add_all([operator, robot, project])
        db.flush()
        mission = RobotMission(
            robot_user_id=robot.id,
            robot_username=robot.username,
            project_id=project.id,
            requested_by_user_id=operator.id,
            status="cancel_requested",
            capture_mode="panorama",
            capture_date=date(2026, 8, 12),
            waypoints_json=[{"name": "room1"}],
            cancel_requested_at=now - timedelta(seconds=cancellation_age_seconds),
        )
        db.add(mission)
        db.flush()
        db.add(
            RobotMissionStep(
                mission_id=mission.id,
                sequence_index=1,
                waypoint_name="room1",
                status="running",
            )
        )
        presence = RobotPresence(
            robot_user_id=robot.id,
            robot_username=robot.username,
            status="busy",
            current_mission_id=mission.id,
            last_seen_at=now - timedelta(seconds=heartbeat_age_seconds),
        )
        db.add(presence)
        db.commit()
        return robot, mission, presence

    def test_recent_heartbeat_keeps_cancellation_active(self) -> None:
        now = datetime(2026, 8, 12, 12, 0)
        with Session(self.engine, expire_on_commit=False) as db:
            _, mission, _ = self._make_cancelling_mission(
                db,
                now=now,
                heartbeat_age_seconds=20,
                cancellation_age_seconds=300,
            )

            failed = fail_unreachable_cancellations(db, now=now)

            self.assertEqual(failed, 0)
            db.refresh(mission)
            self.assertEqual(mission.status, "cancel_requested")

    def test_delivery_grace_prevents_immediate_offline_failure(self) -> None:
        now = datetime(2026, 8, 12, 12, 0)
        with Session(self.engine, expire_on_commit=False) as db:
            _, mission, _ = self._make_cancelling_mission(
                db,
                now=now,
                heartbeat_age_seconds=300,
                cancellation_age_seconds=10,
            )

            failed = fail_unreachable_cancellations(db, now=now)

            self.assertEqual(failed, 0)
            db.refresh(mission)
            self.assertEqual(mission.status, "cancel_requested")

    def test_stale_robot_fails_mission_and_clears_active_presence(self) -> None:
        now = datetime(2026, 8, 12, 12, 0)
        with Session(self.engine, expire_on_commit=False) as db:
            robot, mission, presence = self._make_cancelling_mission(
                db,
                now=now,
                heartbeat_age_seconds=121,
                cancellation_age_seconds=121,
            )

            failed = fail_unreachable_cancellations(db, now=now)

            self.assertEqual(failed, 1)
            db.refresh(mission)
            db.refresh(presence)
            self.assertEqual(mission.status, "failed")
            self.assertEqual(
                mission.result_json["failure_code"],
                "ROBOT_UNREACHABLE_DURING_CANCELLATION",
            )
            self.assertIn("Return to the start position was not confirmed", mission.cancel_error)
            self.assertIsNone(presence.current_mission_id)
            self.assertEqual(presence.status, "offline")
            self.assertEqual(mission.steps[0].status, "failed")

            control = get_robot_mission_control(
                robot.username,
                mission.id,
                current_user=robot,
                db=db,
            )
            self.assertTrue(control.abort_requested)
            self.assertFalse(control.cancel_requested)


if __name__ == "__main__":
    unittest.main()
