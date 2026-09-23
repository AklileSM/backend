import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.robot_missions import (
    list_robots,
    post_robot_command_status,
    post_robot_heartbeat,
)
from app.database import Base
from app.models import RobotCommand, User
from app.schemas import RobotCommandStatusUpdateRequest, RobotHeartbeatRequest


class RobotConnectionPresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_connection_is_reported_in_presence_and_robot_summary(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
            operator = User(username="operator", password_hash="x", is_admin=True)
            robot = User(username="robot-1", password_hash="x", is_robot=True)
            db.add_all([operator, robot])
            db.commit()

            response = post_robot_heartbeat(
                robot.username,
                RobotHeartbeatRequest(
                    robot_id=robot.username,
                    status="idle",
                    connection="connected",
                ),
                current_user=robot,
                db=db,
            )
            self.assertEqual(response.connection, "connected")

            summaries = list_robots(operator, db)
            summary = next(item for item in summaries if item.username == robot.username)
            self.assertEqual(summary.connection, "connected")

    def test_missing_connection_preserves_last_reported_state(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
            robot = User(username="robot-1", password_hash="x", is_robot=True)
            db.add(robot)
            db.commit()

            post_robot_heartbeat(
                robot.username,
                RobotHeartbeatRequest(
                    robot_id=robot.username,
                    status="idle",
                    connection="connected",
                ),
                current_user=robot,
                db=db,
            )
            response = post_robot_heartbeat(
                robot.username,
                RobotHeartbeatRequest(
                    robot_id=robot.username,
                    status="idle",
                    connection=None,
                ),
                current_user=robot,
                db=db,
            )

            self.assertEqual(response.connection, "connected")

    def test_late_success_updates_presence_without_reviving_cancelled_command(self) -> None:
        with Session(self.engine, expire_on_commit=False) as db:
            robot = User(username="robot-1", password_hash="x", is_robot=True)
            db.add(robot)
            db.flush()
            command = RobotCommand(
                robot_user_id=robot.id,
                robot_username=robot.username,
                kind="connect",
                status="cancelled",
                connection="disconnected",
            )
            db.add(command)
            db.commit()

            response = post_robot_command_status(
                command.id,
                RobotCommandStatusUpdateRequest(
                    status="succeeded",
                    connection="connected",
                    detail="Robot connected and ready.",
                ),
                current_user=robot,
                db=db,
            )

            self.assertEqual(response.status, "cancelled")
            status = post_robot_heartbeat(
                robot.username,
                RobotHeartbeatRequest(
                    robot_id=robot.username,
                    status="idle",
                    connection=None,
                ),
                current_user=robot,
                db=db,
            )
            self.assertEqual(status.connection, "connected")


if __name__ == "__main__":
    unittest.main()
