import os
import unittest
from datetime import date, datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.robot_missions import (
    cancel_robot_mission,
    force_close_robot_mission,
    get_robot_mission_control,
    post_robot_mission_status,
    stop_robot_mission_return,
)
from app.database import Base
from app.models import Project, RobotMission, RobotMissionStep, User
from app.schemas import RobotMissionStatusUpdateRequest


class RobotCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_running_cancel_waits_for_robot_ack_and_is_terminal(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
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
                status="running",
                capture_mode="panorama",
                capture_date=date(2026, 7, 24),
                waypoints_json=[{"name": "Room 1"}],
            )
            db.add(mission)
            db.flush()
            db.add(
                RobotMissionStep(
                    mission_id=mission.id,
                    sequence_index=1,
                    waypoint_name="Room 1",
                    room_slug="room1",
                    status="running",
                )
            )
            db.commit()

            response = cancel_robot_mission(
                mission.id,
                current_user=operator,
                db=db,
            )
            self.assertEqual(response.status, "cancel_requested")
            self.assertIsNone(response.completed_at)

            control = get_robot_mission_control(
                robot.username,
                mission.id,
                current_user=robot,
                db=db,
            )
            self.assertTrue(control.cancel_requested)

            returning = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(status="returning_to_start"),
                current_user=robot,
                db=db,
            )
            self.assertEqual(returning.status, "returning_to_start")

            stopping = stop_robot_mission_return(
                mission.id,
                current_user=operator,
                db=db,
            )
            self.assertEqual(stopping.status, "stop_requested")

            control = get_robot_mission_control(
                robot.username,
                mission.id,
                current_user=robot,
                db=db,
            )
            self.assertTrue(control.cancel_requested)
            self.assertTrue(control.stop_requested)

            stale_returning = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(status="returning_to_start"),
                current_user=robot,
                db=db,
            )
            self.assertEqual(stale_returning.status, "stop_requested")

            force_closed = force_close_robot_mission(
                mission.id,
                current_user=operator,
                db=db,
            )
            self.assertEqual(force_closed.status, "cancelled")
            self.assertTrue(force_closed.result["force_closed"])
            self.assertEqual(
                force_closed.result["return_to_start"]["navigation_result"],
                "FORCE_CLOSED_BY_OPERATOR",
            )

            cancelled = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(
                    status="cancelled",
                    result={
                        "status": "CANCELLED",
                        "steps": [{"waypoint_index": 1, "status": "CANCELLED"}],
                        "return_to_start": {"status": "CANCELLED"},
                    },
                ),
                current_user=robot,
                db=db,
            )
            self.assertEqual(cancelled.status, "cancelled")
            self.assertIsNotNone(cancelled.cancel_acknowledged_at)

            late_running = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(status="running"),
                current_user=robot,
                db=db,
            )
            self.assertEqual(late_running.status, "cancelled")

    def test_final_navigation_failure_without_return_confirmation_is_cancel_failed(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
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
                status="running",
                capture_mode="panorama",
                capture_date=date(2026, 8, 11),
                waypoints_json=[{"name": "room1"}],
            )
            db.add(mission)
            db.flush()
            step = RobotMissionStep(
                mission_id=mission.id,
                sequence_index=1,
                waypoint_name="room1",
                room_slug="room1",
                status="running",
            )
            db.add(step)
            db.commit()

            cancel_robot_mission(mission.id, current_user=operator, db=db)
            response = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(
                    status="failed",
                    completed_at_utc=datetime(2026, 8, 11, 14, 23, tzinfo=timezone.utc),
                    result={
                        "status": "FAILED",
                        "steps": [
                            {
                                "waypoint_index": 1,
                                "status": "FAILED",
                                "error": "The robot couldn't reach this point.",
                                "navigation_result": "ABORTED",
                            }
                        ],
                    },
                ),
                current_user=robot,
                db=db,
            )

            self.assertEqual(response.status, "cancel_failed")
            self.assertIsNotNone(response.completed_at)
            self.assertIsNotNone(response.cancel_acknowledged_at)
            self.assertEqual(response.steps[0].status, "failed")
            self.assertEqual(response.steps[0].navigation_result, "ABORTED")

    def test_final_navigation_failure_after_confirmed_return_is_cancelled(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
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
                status="running",
                capture_mode="panorama",
                capture_date=date(2026, 8, 11),
                waypoints_json=[{"name": "room1"}],
            )
            db.add(mission)
            db.commit()

            cancel_robot_mission(mission.id, current_user=operator, db=db)
            response = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(
                    status="failed",
                    result={
                        "status": "FAILED",
                        "steps": [],
                        "return_to_start": {
                            "status": "SUCCEEDED",
                            "navigation_result": "SUCCEEDED",
                        },
                    },
                ),
                current_user=robot,
                db=db,
            )

            self.assertEqual(response.status, "cancelled")
            self.assertIsNotNone(response.cancel_acknowledged_at)

    def test_late_success_after_cancel_closes_as_cancelled(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
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
                status="running",
                capture_mode="panorama",
                capture_date=date(2026, 8, 11),
                waypoints_json=[{"name": "room1"}],
            )
            db.add(mission)
            db.commit()

            cancel_robot_mission(mission.id, current_user=operator, db=db)
            response = post_robot_mission_status(
                mission.id,
                RobotMissionStatusUpdateRequest(
                    status="succeeded",
                    result={"status": "SUCCEEDED", "steps": []},
                ),
                current_user=robot,
                db=db,
            )

            self.assertEqual(response.status, "cancelled")
            self.assertIsNotNone(response.completed_at)
            self.assertIsNotNone(response.cancel_acknowledged_at)


if __name__ == "__main__":
    unittest.main()
