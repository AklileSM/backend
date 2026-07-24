import os
import unittest
from datetime import datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Project,
    RobotCapturePoint,
    RobotCommand,
    RobotMissionSchedule,
    User,
)
from app.services.robot_schedules import materialize_schedule, next_schedule_run


class RobotScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_next_run_respects_local_timezone(self) -> None:
        next_run = next_schedule_run(
            local_time="17:00",
            timezone_name="Asia/Dubai",
            weekdays=list(range(7)),
            after_utc=datetime(2026, 7, 24, 12, 0),
        )
        self.assertEqual(next_run, datetime(2026, 7, 24, 13, 0))

        following_run = next_schedule_run(
            local_time="17:00",
            timezone_name="Asia/Dubai",
            weekdays=list(range(7)),
            after_utc=next_run,
        )
        self.assertEqual(following_run, datetime(2026, 7, 25, 13, 0))

    def test_materialize_snapshots_ordered_points_and_queues_connect(self) -> None:
        with Session(self.engine) as db:
            operator = User(username="operator", password_hash="x", is_admin=True)
            robot = User(username="robot-1", password_hash="x", is_robot=True)
            project = Project(name="Demo", slug="demo")
            db.add_all([operator, robot, project])
            db.flush()

            room1 = RobotCapturePoint(
                project_id=project.id,
                name="Room 1",
                room_slug="room1",
                map_x=1,
                map_y=2,
                yaw=0,
            )
            room2 = RobotCapturePoint(
                project_id=project.id,
                name="Room 2",
                room_slug="room2",
                map_x=3,
                map_y=4,
                yaw=1,
            )
            db.add_all([room1, room2])
            db.flush()

            schedule = RobotMissionSchedule(
                name="Daily 5 PM",
                robot_user_id=robot.id,
                robot_username=robot.username,
                project_id=project.id,
                requested_by_user_id=operator.id,
                enabled=True,
                timezone="Asia/Dubai",
                local_time="17:00",
                weekdays_json=list(range(7)),
                capture_point_ids_json=[room1.id, room2.id],
                capture_mode="panorama",
                retry_policy_json={},
                robot_meta_json={"capture_outputs": ["image"]},
                busy_policy="skip",
                auto_connect=True,
                max_lateness_minutes=30,
            )
            db.add(schedule)
            db.flush()

            mission = materialize_schedule(
                db,
                schedule=schedule,
                scheduled_for=datetime(2026, 7, 24, 13, 0),
            )
            db.flush()

            self.assertIsNotNone(mission)
            assert mission is not None
            self.assertEqual(
                [waypoint["name"] for waypoint in mission.waypoints_json],
                ["Room 1", "Room 2"],
            )
            self.assertEqual(mission.capture_date.isoformat(), "2026-07-24")
            self.assertEqual(mission.schedule_id, schedule.id)
            command = db.scalar(select(RobotCommand))
            self.assertIsNotNone(command)
            self.assertEqual(command.kind, "connect")


if __name__ == "__main__":
    unittest.main()
