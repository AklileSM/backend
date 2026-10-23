import os
import unittest
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.robot_missions import (
    create_robot_mission,
    create_robot_mission_schedule,
    update_robot_mission_schedule,
)
from app.database import Base, get_db
from app.main import app
from app.models import Project, ProjectMember, RobotCapturePoint, RobotMission, RobotMissionSchedule, User
from app.schemas import (
    RobotMissionCreateRequest,
    RobotMissionScheduleCreateRequest,
    RobotMissionScheduleUpdateRequest,
)
from app.services.robot_capabilities import requested_outputs
from app.services.robot_schedules import materialize_schedule

CAMERA_OK = {"id": "camera", "output": "image", "detected": True, "name": "Insta360 X4", "detail": "USB 2e1a:0002"}
LIDAR_MISSING = {"id": "lidar", "output": "pointcloud", "detected": False, "name": "LiDAR",
                 "detail": "No point cloud arrived on /livox/lidar in 5 s."}


class RobotCapabilityTests(unittest.TestCase):
    """Capture types are offered only when SiteScope supports them and the robot's payload works."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.operator = User(username="operator", password_hash="x", is_admin=True)
        self.robot = User(username="robot-1", password_hash="x", is_robot=True)
        self.project = Project(name="Demo", slug="demo")
        self.db.add_all([self.operator, self.robot, self.project])
        self.db.flush()
        self.db.add(ProjectMember(project_id=self.project.id, user_id=self.robot.id, role="editor"))
        self.point = RobotCapturePoint(project_id=self.project.id, name="kitchen", map_x=1.0, map_y=2.0, metadata_json={})
        self.db.add(self.point)
        self.db.commit()

        def override_db():
            with Session(self.engine) as db:
                yield db

        self.user = self.robot
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def heartbeat(self, payloads: list[dict] | None) -> None:
        body = {"robot_id": "robot-1", "status": "idle"}
        if payloads is not None:
            body["payloads"] = payloads
        self.user = self.robot
        response = self.client.post("/api/robots/robot-1/heartbeat", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.db.expire_all()

    def capabilities(self) -> dict[str, dict]:
        self.user = self.operator
        robots = self.client.get("/api/robots").json()
        return {item["output"]: item for item in robots[0]["capture_capabilities"]}

    def mission(self, outputs: list[str]):
        return create_robot_mission(
            RobotMissionCreateRequest(robot_id="robot-1", project_slug="demo", capture_point_ids=[self.point.id],
                                      capture_date=date(2026, 10, 23), robot_meta={"capture_outputs": outputs}),
            current_user=self.operator, db=self.db,
        )

    def schedule(self, outputs: list[str]):
        return create_robot_mission_schedule(
            RobotMissionScheduleCreateRequest(name="s", robot_id="robot-1", project_slug="demo",
                                              capture_point_ids=[self.point.id], local_time="08:00",
                                              robot_meta={"capture_outputs": outputs}),
            current_user=self.operator, db=self.db,
        )

    def test_only_detected_payloads_with_a_supported_output_are_available(self) -> None:
        # A payload SiteScope can't process is ignored, however capable.
        self.heartbeat([CAMERA_OK, LIDAR_MISSING,
                        {"id": "thermal", "output": "thermal_image", "detected": True, "name": "Huge thermal camera"}])
        caps = self.capabilities()
        self.assertEqual(list(caps), ["image", "pointcloud"])
        self.assertEqual((caps["image"]["available"], caps["image"]["payload_name"]), (True, "Insta360 X4"))
        self.assertFalse(caps["pointcloud"]["available"])
        self.assertIn("No point cloud arrived", caps["pointcloud"]["reason"])

    def test_a_supported_output_with_no_payload_at_all_is_unavailable(self) -> None:
        self.heartbeat([CAMERA_OK])
        self.assertIn("no payload", self.capabilities()["pointcloud"]["reason"])

    def test_older_agents_that_report_nothing_keep_everything_available(self) -> None:
        self.heartbeat(None)
        caps = self.capabilities()
        self.assertTrue(all(item["available"] and not item["reported"] for item in caps.values()))
        self.mission(["image", "pointcloud"])

    def test_the_last_report_survives_a_heartbeat_without_one(self) -> None:
        self.heartbeat([CAMERA_OK, LIDAR_MISSING])
        self.heartbeat(None)  # the agent restarted and hasn't probed yet
        self.assertFalse(self.capabilities()["pointcloud"]["available"])

    def test_captures_needing_a_missing_payload_are_refused(self) -> None:
        self.heartbeat([CAMERA_OK, LIDAR_MISSING])
        self.mission(["image"])
        with self.assertRaises(HTTPException) as ctx:
            self.mission(["image", "pointcloud"])
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("can't take a point cloud right now", ctx.exception.detail)
        with self.assertRaises(HTTPException):
            self.schedule(["pointcloud"])

    def test_schedule_edits_check_only_what_is_captured(self) -> None:
        self.heartbeat([CAMERA_OK, {**LIDAR_MISSING, "detected": True}])
        schedule = self.schedule(["pointcloud"])
        self.heartbeat([CAMERA_OK, LIDAR_MISSING])
        renamed = update_robot_mission_schedule(
            schedule.id, RobotMissionScheduleUpdateRequest(name="renamed"), current_user=self.operator, db=self.db,
        )
        self.assertEqual(renamed.name, "renamed")
        with self.assertRaises(HTTPException):
            update_robot_mission_schedule(
                schedule.id, RobotMissionScheduleUpdateRequest(robot_meta={"capture_outputs": ["image", "pointcloud"]}),
                current_user=self.operator, db=self.db,
            )

    def test_a_scheduled_run_is_skipped_while_its_payload_is_missing(self) -> None:
        self.heartbeat([CAMERA_OK, {**LIDAR_MISSING, "detected": True}])
        schedule = self.schedule(["pointcloud"])
        self.heartbeat([CAMERA_OK, LIDAR_MISSING])
        row = self.db.get(RobotMissionSchedule, schedule.id)
        self.assertIsNone(materialize_schedule(self.db, schedule=row, scheduled_for=datetime(2026, 10, 23, 8)))
        self.assertEqual(row.last_outcome, "skipped_unavailable")
        self.assertIn("point cloud", row.last_error)
        self.assertTrue(row.enabled)
        self.assertEqual(self.db.scalars(select(RobotMission)).all(), [])
        # Plugged back in: the next run goes ahead.
        self.heartbeat([CAMERA_OK, {**LIDAR_MISSING, "detected": True}])
        row = self.db.get(RobotMissionSchedule, schedule.id)
        self.assertIsNotNone(materialize_schedule(self.db, schedule=row, scheduled_for=datetime(2026, 10, 24, 8)))

    def test_requested_outputs_read_like_the_robot_agent(self) -> None:
        self.assertEqual(requested_outputs({"capture_outputs": ["panorama", "lidar"]}, None), ["image", "pointcloud"])
        self.assertEqual(requested_outputs({"capture_outputs": "pcd"}, None), ["pointcloud"])
        self.assertEqual(requested_outputs({}, "pointcloud"), ["pointcloud"])
        self.assertEqual(requested_outputs(None, "panorama"), ["image"])


if __name__ == "__main__":
    unittest.main()
