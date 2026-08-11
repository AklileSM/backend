import os
import unittest
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.files.explorer import explorer_by_room
from app.database import Base
from app.models import FileAsset, Project, Room


class RoomExplorerProjectScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _add_asset(self, db: Session, room: Room, filename: str, capture_date: date) -> None:
        db.add(
            FileAsset(
                room=room,
                media_type="image",
                capture_date=capture_date,
                original_name=filename,
                display_name=filename,
                bucket_name="images",
                object_name=f"{room.project_id}/{filename}",
                content_type="image/jpeg",
                file_size=100,
            )
        )

    def test_duplicate_room_slug_is_scoped_by_project_id(self) -> None:
        with Session(self.engine) as db:
            a6 = Project(name="A6 Stern", slug="a6-stern")
            current = Project(name="Current Project", slug="current-project")
            a6_room = Room(name="Office A6", slug="office", project=a6)
            current_room = Room(name="Office Current", slug="office", project=current)
            db.add_all([a6, current, a6_room, current_room])
            db.flush()
            self._add_asset(db, a6_room, "a6.jpg", date(2026, 8, 1))
            self._add_asset(db, current_room, "current.jpg", date(2026, 8, 2))
            db.commit()

            result = explorer_by_room("office", project_id=current.id, db=db)

            self.assertEqual(result.room_name, "Office Current")
            self.assertEqual(set(result.dates), {"2026-08-02"})
            self.assertEqual(result.dates["2026-08-02"].images[0].file_name, "current.jpg")

    def test_ambiguous_legacy_slug_is_rejected(self) -> None:
        with Session(self.engine) as db:
            first = Project(name="First", slug="first")
            second = Project(name="Second", slug="second")
            db.add_all(
                [
                    Room(name="Office First", slug="office", project=first),
                    Room(name="Office Second", slug="office", project=second),
                ]
            )
            db.commit()

            with self.assertRaises(HTTPException) as raised:
                explorer_by_room("office", db=db)

            self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
