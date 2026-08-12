import os
import unittest
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.robot_missions import create_robot_mission
from app.database import Base
from app.models import Project, ProjectMember, User
from app.schemas import RobotMissionCreateRequest


class RobotProjectAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_mission_is_rejected_before_robot_upload_can_fail(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
            operator = User(username="operator", password_hash="x", is_admin=True)
            robot = User(username="robot-1", password_hash="x", is_robot=True)
            project = Project(name="New project", slug="new-project")
            db.add_all([operator, robot, project])
            db.commit()

            payload = RobotMissionCreateRequest(
                robot_id=robot.username,
                project_slug=project.slug,
                waypoints=[{"name": "room1", "x": 1.0, "y": 2.0}],
                capture_date=date(2026, 8, 12),
            )

            with self.assertRaises(HTTPException) as raised:
                create_robot_mission(payload, current_user=operator, db=db)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("Add the robot as an editor", str(raised.exception.detail))

            db.add(ProjectMember(project_id=project.id, user_id=robot.id, role="editor"))
            db.commit()
            mission = create_robot_mission(payload, current_user=operator, db=db)
            self.assertEqual(mission.status, "queued")
            self.assertEqual(mission.project_slug, project.slug)


if __name__ == "__main__":
    unittest.main()
