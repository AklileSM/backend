import os
import unittest
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.files.details import get_file_asset_details
from app.database import Base
from app.models import FileAsset, Project, ProjectMember, Room, User


class FileAssetDetailsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _fixture(self, db: Session, *, role: str = "viewer") -> tuple[FileAsset, User]:
        project = Project(name="Demo", slug="demo")
        user = User(username=f"{role}-user", password_hash="x")
        room = Room(name="Room 1", slug="room-1", project=project)
        db.add_all([project, user, room])
        db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=user.id, role=role))
        asset = FileAsset(
            room_id=room.id,
            media_type="image",
            capture_date=date(2026, 8, 5),
            original_name="x4.jpg",
            display_name="room-1-20260805-001.jpg",
            bucket_name="images",
            object_name="room-1/x4.jpg",
            content_type="image/jpeg",
            file_size=1234,
            metadata_json={
                "robot": {
                    "quality": {"checks": {"blur_laplacian_var": 592.018}},
                    "quality_gate": {"outcome": "passed_first_attempt"},
                }
            },
        )
        db.add(asset)
        db.commit()
        return asset, user

    def test_member_can_read_quality_metadata(self) -> None:
        with Session(self.engine) as db:
            asset, user = self._fixture(db)

            result = get_file_asset_details(asset.id, db=db, current_user=user)

            self.assertEqual(result.display_name, "room-1-20260805-001.jpg")
            self.assertEqual(
                result.metadata["robot"]["quality_gate"]["outcome"],
                "passed_first_attempt",
            )
            self.assertFalse(result.can_delete)

    def test_editor_is_told_the_asset_can_be_deleted(self) -> None:
        with Session(self.engine) as db:
            asset, user = self._fixture(db, role="editor")

            result = get_file_asset_details(asset.id, db=db, current_user=user)

            self.assertTrue(result.can_delete)

    def test_non_member_cannot_read_asset_metadata(self) -> None:
        with Session(self.engine) as db:
            asset, _member = self._fixture(db)
            outsider = User(username="outsider", password_hash="x")
            db.add(outsider)
            db.commit()

            with self.assertRaises(HTTPException) as raised:
                get_file_asset_details(asset.id, db=db, current_user=outsider)

            self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
