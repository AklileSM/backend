import csv
import io
import os
import unittest
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.files.quality_export import (
    _asset_statement,
    _iter_csv,
    _iter_export_rows,
    estimate_quality_export,
)
from app.database import Base
from app.models import FileAsset, Project, ProjectMember, Room, User


class QualityCsvExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _fixture(self, db: Session) -> tuple[Project, User, Room, Room]:
        project = Project(name="Research Project", slug="research-project")
        member = User(username="researcher", password_hash="x")
        room_a = Room(name="Room A", slug="room-a", project=project, sort_order=1)
        room_b = Room(name="Room B", slug="room-b", project=project, sort_order=2)
        db.add_all([project, member, room_a, room_b])
        db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=member.id, role="viewer"))

        image_robot = {
            "mission_id": "mission-1",
            "capture_point_id": "capture-point-1",
            "target_waypoint": "=unsafe-waypoint",
            "waypoint_index": 1,
            "waypoint_count": 2,
            "captured_at_utc": "2026-08-05T10:00:00+00:00",
            "target_waypoint_pose": {"frame": "map", "x": 1.0, "y": 2.0, "z": 0.0, "yaw": 0.5},
            "pose": {"frame": "map", "source": "tf:map->base_link", "x": 1.1, "y": 2.0, "z": 0.0, "yaw": 0.55},
            "quality": {
                "schema": 1,
                "media": "image",
                "canonical_width": 1024,
                "verdict": "pass",
                "advisory_flags": ["pose_deviation_deg_high"],
                "checks": {
                    "blur_laplacian_var": 120.0,
                    "mean_luminance": 130.0,
                    "rms_contrast": 50.0,
                    "clipped_highlight_frac": 0.01,
                    "clipped_shadow_frac": 0.02,
                    "pose_available": True,
                    "pose_deviation_m": 0.1,
                    "pose_deviation_xy_m": 0.1,
                    "pose_deviation_z_m": 0.0,
                    "pose_deviation_deg": 3.0,
                },
            },
            "quality_gate": {
                "mode": "retry",
                "outcome": "passed_after_retry",
                "passed": True,
                "attempt_count": 2,
                "max_attempts": 3,
                "selected_attempt": 2,
                "attempts": [
                    {
                        "attempt": 1,
                        "captured_at_utc": "2026-08-05T09:59:55+00:00",
                        "selected": False,
                        "quality": {
                            "schema": 1,
                            "verdict": "flag",
                            "advisory_flags": ["blur_laplacian_var_low"],
                            "checks": {"blur_laplacian_var": 20.0, "mean_luminance": 130.0},
                        },
                        "gate": {
                            "passed": False,
                            "evaluable": True,
                            "violation_score": 0.666667,
                            "flags": ["blur_laplacian_var_low"],
                        },
                    },
                    {
                        "attempt": 2,
                        "captured_at_utc": "2026-08-05T10:00:00+00:00",
                        "selected": True,
                        "quality": {
                            "schema": 1,
                            "verdict": "pass",
                            "advisory_flags": [],
                            "checks": {"blur_laplacian_var": 120.0, "mean_luminance": 130.0},
                        },
                        "gate": {
                            "passed": True,
                            "evaluable": True,
                            "violation_score": 0.0,
                            "flags": [],
                        },
                    },
                ],
            },
        }
        pointcloud_robot = {
            "mission_id": "mission-1",
            "target_waypoint": "Room B",
            "quality": {
                "schema": 1,
                "media": "pointcloud",
                "verdict": "pass",
                "advisory_flags": [],
                "checks": {
                    "point_count": 75000,
                    "bbox_extent_m": [3.0, 4.0, 2.0],
                    "bbox_max_extent_m": 4.0,
                    "bbox_volume_m3": 24.0,
                    "intensity_nonzero_frac": 0.9,
                    "intensity_sampled_points": 75000,
                },
            },
        }

        db.add_all(
            [
                FileAsset(
                    id="image-1",
                    room=room_a,
                    media_type="image",
                    capture_date=date(2026, 8, 5),
                    original_name="x4.jpg",
                    display_name="room-a-20260805-001.jpg",
                    bucket_name="images",
                    object_name="room-a/x4.jpg",
                    file_size=1000,
                    metadata_json={"uploaded_by_username": "go2w_001", "robot": image_robot},
                ),
                FileAsset(
                    id="cloud-1",
                    room=room_b,
                    media_type="pointcloud",
                    capture_date=date(2026, 8, 5),
                    original_name="scan.laz",
                    display_name="room-b-20260805-001.laz",
                    bucket_name="pointclouds",
                    object_name="room-b/scan.laz",
                    file_size=2000,
                    metadata_json={"uploaded_by_username": "go2w_001", "robot": pointcloud_robot},
                ),
                # Human uploads are deliberately excluded from a robot-quality export.
                FileAsset(
                    id="human-1",
                    room=room_a,
                    media_type="image",
                    capture_date=date(2026, 8, 5),
                    original_name="manual.jpg",
                    display_name="room-a-20260805-002.jpg",
                    bucket_name="images",
                    object_name="room-a/manual.jpg",
                    metadata_json={"uploaded_by_username": "researcher"},
                ),
            ]
        )
        db.commit()
        return project, member, room_a, room_b

    def test_estimate_expands_all_attempts_and_skips_human_uploads(self) -> None:
        with Session(self.engine) as db:
            _project, member, _room_a, _room_b = self._fixture(db)

            estimate = estimate_quality_export(
                project_slug="research-project",
                date_from=date(2026, 8, 5),
                date_to=date(2026, 8, 5),
                room_slug=[],
                media_type=["image", "pointcloud"],
                attempt_scope="all",
                db=db,
                current_user=member,
            )

            self.assertEqual(estimate.asset_count, 2)
            self.assertEqual(estimate.row_count, 3)
            self.assertEqual(
                estimate.filename,
                "research-project-capture-quality-2026-08-05.csv",
            )

    def test_selected_scope_and_filters_share_the_export_query(self) -> None:
        with Session(self.engine) as db:
            project, member, _room_a, _room_b = self._fixture(db)
            estimate = estimate_quality_export(
                project_slug=project.slug,
                date_from=date(2026, 8, 5),
                date_to=date(2026, 8, 5),
                room_slug=["room-a"],
                media_type=["image"],
                attempt_scope="selected",
                db=db,
                current_user=member,
            )
            self.assertEqual((estimate.asset_count, estimate.row_count), (1, 1))

            stmt = _asset_statement(
                project_id=project.id,
                date_from=date(2026, 8, 5),
                date_to=date(2026, 8, 5),
                room_slugs=["room-a"],
                media_types=("image",),
            )
            rows = list(
                _iter_export_rows(
                    db=db,
                    stmt=stmt,
                    project=project,
                    attempt_scope="selected",
                )
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["attempt_number"], 2)
            self.assertTrue(rows[0]["is_selected_attempt"])
            self.assertEqual(rows[0]["pose_deviation_m"], 0.1)

    def test_csv_retains_rejected_attempt_and_sanitizes_spreadsheet_formula(self) -> None:
        with Session(self.engine) as db:
            project, _member, _room_a, _room_b = self._fixture(db)
            stmt = _asset_statement(
                project_id=project.id,
                date_from=date(2026, 8, 5),
                date_to=date(2026, 8, 5),
                room_slugs=["room-a"],
                media_types=("image",),
            )
            rows = _iter_export_rows(db=db, stmt=stmt, project=project, attempt_scope="all")
            raw = "".join(_iter_csv(rows)).lstrip("\ufeff")
            parsed = list(csv.DictReader(io.StringIO(raw)))

            self.assertEqual(len(parsed), 2)
            self.assertEqual(parsed[0]["blur_laplacian_var"], "20.0")
            self.assertEqual(parsed[0]["gate_passed"], "false")
            self.assertEqual(parsed[1]["gate_passed"], "true")
            self.assertEqual(parsed[1]["target_waypoint"], "'=unsafe-waypoint")
            # A pose sampled for the selected upload is not attributed to a rejected attempt.
            self.assertEqual(parsed[0]["recorded_pose_x_m"], "")
            self.assertEqual(parsed[1]["recorded_pose_x_m"], "1.1")

    def test_non_member_cannot_estimate_export(self) -> None:
        with Session(self.engine) as db:
            project, _member, _room_a, _room_b = self._fixture(db)
            outsider = User(username="outsider", password_hash="x")
            db.add(outsider)
            db.commit()

            with self.assertRaises(HTTPException) as raised:
                estimate_quality_export(
                    project_slug=project.slug,
                    date_from=None,
                    date_to=None,
                    room_slug=[],
                    media_type=[],
                    attempt_scope="all",
                    db=db,
                    current_user=outsider,
                )

            self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
