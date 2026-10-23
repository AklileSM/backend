import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.database import Base, get_db
from app.main import app
from app.models import User
from app.services import robot_map_preview
from app.services.robot_map_preview import clear_map_previews


def frame(session: str = "s1", pose: tuple[float, float] | None = (0.0, 0.0), seq: int | None = None) -> dict:
    body: dict = {"session_id": session}
    if pose is not None:
        body["pose"] = {"x": pose[0], "y": pose[1], "yaw": 0.5}
    if seq is not None:
        body["map"] = {
            "seq": seq, "width": 4, "height": 3, "resolution": 0.05, "origin_x": -1.0, "origin_y": -2.0,
            "mapped_area_m2": 1.5, "png_base64": f"png{seq}",
        }
    return body


class RobotMapPreviewTests(unittest.TestCase):
    """The live map the laptop posts while mapping, and what the wizard reads back."""

    def setUp(self) -> None:
        clear_map_previews()
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.operator = User(username="operator", password_hash="x", is_admin=True)
        self.robot = User(username="robot-1", password_hash="x", is_robot=True)
        self.other_robot = User(username="robot-2", password_hash="x", is_robot=True)
        self.db.add_all([self.operator, self.robot, self.other_robot])
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
        clear_map_previews()
        self.db.close()
        self.engine.dispose()

    def post(self, body: dict, robot: str = "robot-1"):
        return self.client.post(f"/api/robots/{robot}/map-preview", json=body)

    def get(self, image_seq: int | None = None):
        self.user = self.operator
        try:
            query = f"?image_seq={image_seq}" if image_seq is not None else ""
            return self.client.get(f"/api/robots/robot-1/map-preview{query}")
        finally:
            self.user = self.robot

    def test_no_preview_until_the_laptop_posts(self) -> None:
        self.assertEqual(self.get().status_code, 404)

    def test_map_image_is_sent_once_per_seq(self) -> None:
        self.assertEqual(self.post(frame(seq=1)).json(), {"map_seq": 1})
        first = self.get().json()
        self.assertEqual(first["map"]["image"], "data:image/png;base64,png1")
        self.assertEqual(first["map"]["mapped_area_m2"], 1.5)
        # The wizard already has seq 1, so only the pose data comes back.
        self.assertIsNone(self.get(image_seq=1).json()["map"]["image"])
        # A pose-only frame keeps the image and tells the laptop it is held.
        self.assertEqual(self.post(frame(pose=(1.0, 0.0))).json(), {"map_seq": 1})
        self.post(frame(seq=2))
        self.assertEqual(self.get(image_seq=1).json()["map"]["image"], "data:image/png;base64,png2")

    def test_after_a_restart_the_ack_asks_for_the_image_again(self) -> None:
        self.post(frame(seq=3))
        clear_map_previews()
        self.assertEqual(self.post(frame()).json(), {"map_seq": None})

    def test_trail_home_and_distance(self) -> None:
        for x in (0.0, 0.05, 1.0, 1.0, 2.0):
            self.post(frame(pose=(x, 0.0)))
        preview = self.get().json()
        self.assertEqual(preview["home"]["x"], 0.0)
        self.assertEqual(preview["pose"]["x"], 2.0)
        # The 5 cm step and the repeated point are skipped.
        self.assertEqual([p["x"] for p in preview["trail"]], [0.0, 1.0, 2.0])
        self.assertAlmostEqual(preview["distance_m"], 2.0)

    def test_a_new_session_starts_a_new_trail_and_home(self) -> None:
        self.post(frame(pose=(5.0, 5.0), seq=1))
        self.post(frame(session="s2", pose=(0.0, 0.0)))
        preview = self.get().json()
        self.assertEqual(preview["home"]["x"], 0.0)
        self.assertEqual(len(preview["trail"]), 1)
        self.assertIsNone(preview["map"])

    def test_long_trails_are_thinned(self) -> None:
        with mock.patch.object(robot_map_preview, "TRAIL_MAX_POINTS", 20):
            for i in range(21):
                self.post(frame(pose=(i * 0.2, 0.0)))
        trail = self.get().json()["trail"]
        self.assertEqual(len(trail), 11)
        # The latest point always stays, so the trail ends at the robot.
        self.assertAlmostEqual(trail[-1]["x"], 4.0)

    def test_only_the_robot_itself_can_post(self) -> None:
        self.assertEqual(self.post(frame(), robot="robot-2").status_code, 403)
        self.user = self.operator
        self.assertEqual(self.post(frame()).status_code, 403)

    def test_bad_frames_are_rejected(self) -> None:
        body = frame(seq=1)
        body["map"]["width"] = 0
        self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual(self.post({"session_id": ""}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
