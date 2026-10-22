import os
import unittest
from datetime import date, datetime
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.robot_missions import (
    create_robot_command,
    create_robot_mission,
    create_robot_mission_schedule,
    get_latest_robot_command,
)
from app.database import Base, get_db
from app.main import app
from app.models import Project, ProjectMember, RobotCapturePoint, RobotMission, RobotMissionSchedule, User
from app.schemas import RobotCommandCreateRequest, RobotMissionCreateRequest, RobotMissionScheduleCreateRequest
from app.services.robot_schedules import materialize_schedule

MAP_YAML = b"image: map.pgm\nresolution: 0.05\norigin: [-1.0, -2.0, 0.0]\nnegate: 0\n"


def pgm(width: int = 4, height: int = 3) -> bytes:
    return f"P5\n{width} {height}\n255\n".encode() + bytes([200] * (width * height))


class RobotMapVersionTests(unittest.TestCase):
    """New robot maps get versions; waypoints from another version retire and are refused."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.operator = User(username="operator", password_hash="x", is_admin=True)
        self.viewer = User(username="viewer", password_hash="x")
        self.robot = User(username="robot-1", password_hash="x", is_robot=True)
        self.project = Project(name="Demo", slug="demo")
        self.db.add_all([self.operator, self.viewer, self.robot, self.project])
        self.db.flush()
        self.db.add(ProjectMember(project_id=self.project.id, user_id=self.robot.id, role="editor"))
        self.db.add(ProjectMember(project_id=self.project.id, user_id=self.viewer.id, role="viewer"))
        self.db.commit()

        def override_db():
            with Session(self.engine) as db:
                yield db

        self.user = self.operator
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.storage = mock.patch("app.api.projects.storage_service")
        self.storage.start()

    def tearDown(self) -> None:
        self.storage.stop()
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def upload(self, name: str | None = None) -> dict:
        data = {"map_name": name} if name else {}
        response = self.client.post(
            f"/api/projects/{self.project.id}/robot-map",
            files={"yaml_file": ("m.yaml", MAP_YAML, "text/yaml"), "image_file": ("m.pgm", pgm(), "image/x-portable-graymap")},
            data=data,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def add_point(self, name: str) -> dict:
        response = self.client.post(
            f"/api/projects/{self.project.id}/robot-capture-points",
            json={"name": name, "map_x": 1.0, "map_y": 2.0, "yaw": 0.0},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def points(self) -> dict[str, dict]:
        response = self.client.get(f"/api/projects/{self.project.id}/robot-capture-points")
        return {point["name"]: point for point in response.json()}

    def test_versions_retire_and_switch_back(self) -> None:
        first = self.upload("site_map_v3")
        self.assertEqual((first["map_version"], first["map_name"], first["previous_map_name"]), (1, "site_map_v3", None))
        self.add_point("kitchen")
        self.assertFalse(self.points()["kitchen"]["retired"])
        self.assertEqual(self.points()["kitchen"]["map_version"], 1)

        second = self.upload("site_map_v4")
        self.assertEqual((second["map_version"], second["previous_map_name"]), (2, "site_map_v3"))
        self.assertIn("/v2/", second["image_url"] + str(second["image_object_name"]))
        self.assertTrue(self.points()["kitchen"]["retired"])
        self.add_point("lobby")
        self.assertFalse(self.points()["lobby"]["retired"])

        back = self.client.post(f"/api/projects/{self.project.id}/robot-map/rollback").json()
        self.assertEqual((back["map_version"], back["map_name"], back["previous_map_name"]), (1, "site_map_v3", "site_map_v4"))
        points = self.points()
        self.assertFalse(points["kitchen"]["retired"])
        self.assertTrue(points["lobby"]["retired"])

    def test_legacy_maps_and_points_stay_usable_until_the_next_upload(self) -> None:
        self.project.robot_map_json = {"resolution": 0.05, "origin_x": 0, "origin_y": 0, "width": 4, "height": 3,
                                       "image_object_name": "legacy.png"}
        self.db.add(RobotCapturePoint(project_id=self.project.id, name="old", map_x=0, map_y=0, metadata_json={}))
        self.db.commit()
        self.assertFalse(self.points()["old"]["retired"])
        self.upload("new_map")
        self.assertTrue(self.points()["old"]["retired"])
        self.assertEqual(self.client.get(f"/api/projects/{self.project.id}/robot-map").json()["previous_map_version"], None)

    def test_switch_back_without_history_is_refused(self) -> None:
        self.upload("only")
        response = self.client.post(f"/api/projects/{self.project.id}/robot-map/rollback")
        self.assertEqual(response.status_code, 409)

    def test_history_is_capped(self) -> None:
        for index in range(8):
            self.upload(f"map_{index}")
        self.db.expire_all()
        project = self.db.get(Project, self.project.id)
        self.assertEqual(len(project.robot_map_json["history"]), 5)
        self.assertEqual(project.robot_map_json["map_version"], 8)

    def test_missions_and_schedules_refuse_waypoints_from_another_map(self) -> None:
        self.upload("a")
        kitchen = self.add_point("kitchen")
        self.upload("b")
        with self.assertRaises(HTTPException) as ctx:
            create_robot_mission(
                RobotMissionCreateRequest(robot_id="robot-1", project_slug="demo", capture_point_ids=[kitchen["id"]],
                                          capture_date=date(2026, 10, 22)),
                current_user=self.operator, db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("older robot map", ctx.exception.detail)
        with self.assertRaises(HTTPException) as ctx:
            create_robot_mission_schedule(
                RobotMissionScheduleCreateRequest(name="s", robot_id="robot-1", project_slug="demo",
                                                  capture_point_ids=[kitchen["id"]], local_time="08:00"),
                current_user=self.operator, db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_a_schedule_made_before_the_map_changed_is_switched_off_not_run(self) -> None:
        self.upload("a")
        kitchen = self.add_point("kitchen")
        schedule = create_robot_mission_schedule(
            RobotMissionScheduleCreateRequest(name="s", robot_id="robot-1", project_slug="demo",
                                              capture_point_ids=[kitchen["id"]], local_time="08:00"),
            current_user=self.operator, db=self.db,
        )
        self.upload("b")
        self.db.expire_all()
        row = self.db.get(RobotMissionSchedule, schedule.id)
        self.assertIsNone(materialize_schedule(self.db, schedule=row, scheduled_for=datetime(2026, 10, 22, 8)))
        self.assertFalse(row.enabled)
        self.assertEqual(row.last_outcome, "invalid")
        self.assertIn("older robot map", row.last_error)
        self.assertEqual(self.db.scalars(select(RobotMission)).all(), [])

    def test_mapping_commands_validate_and_check_permissions(self) -> None:
        with self.assertRaises(ValidationError):
            RobotCommandCreateRequest(robot_id="robot-1", kind="map_save", params={"map_name": "Bad Name", "project_id": "x"})
        with self.assertRaises(ValidationError):
            RobotCommandCreateRequest(robot_id="robot-1", kind="map_rollback", params={})
        save = RobotCommandCreateRequest(robot_id="robot-1", kind="map_save",
                                         params={"map_name": "site_map_v4", "project_id": self.project.id})
        with self.assertRaises(HTTPException) as ctx:
            create_robot_command(save, current_user=self.viewer, db=self.db)
        self.assertEqual(ctx.exception.status_code, 403)
        command = create_robot_command(save, current_user=self.operator, db=self.db)
        self.assertEqual((command.kind, command.params["map_name"], command.connection), ("map_save", "site_map_v4", None))

    def test_mapping_cannot_start_during_a_capture(self) -> None:
        self.db.add(RobotMission(robot_user_id=self.robot.id, robot_username="robot-1", project_id=self.project.id,
                                 status="running", capture_mode="panorama", capture_date=date(2026, 10, 22),
                                 waypoints_json=[]))
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            create_robot_command(RobotCommandCreateRequest(robot_id="robot-1", kind="map_start"),
                                 current_user=self.operator, db=self.db)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_latest_command_can_be_narrowed_to_connection_or_map(self) -> None:
        create_robot_command(RobotCommandCreateRequest(robot_id="robot-1", kind="connect"), current_user=self.operator, db=self.db)
        self.db.query(__import__("app.models", fromlist=["RobotCommand"]).RobotCommand).update({"status": "succeeded"})
        self.db.commit()
        create_robot_command(RobotCommandCreateRequest(robot_id="robot-1", kind="map_start"), current_user=self.operator, db=self.db)
        self.assertEqual(get_latest_robot_command("robot-1", kind=None, current_user=self.operator, db=self.db).kind, "map_start")
        self.assertEqual(get_latest_robot_command("robot-1", kind="map", current_user=self.operator, db=self.db).kind, "map_start")
        self.assertEqual(get_latest_robot_command("robot-1", kind="connection", current_user=self.operator, db=self.db).kind, "connect")


if __name__ == "__main__":
    unittest.main()
