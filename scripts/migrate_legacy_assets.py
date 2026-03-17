import mimetypes
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import delete, select  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import FileAsset, Room  # noqa: E402
from app.services.bootstrap import seed_defaults  # noqa: E402
from app.services.storage import storage_service  # noqa: E402


FRONTEND_PUBLIC_DIR = ROOT / "frontend" / "public"
THUMBNAILS_DIR = FRONTEND_PUBLIC_DIR / "Images" / "thumbnails"
PANORAMAS_DIR = FRONTEND_PUBLIC_DIR / "Images" / "panoramas"
POINTCLOUD_DIR = FRONTEND_PUBLIC_DIR / "PCD"


def room_slug_from_name(file_name: str) -> str | None:
    match = re.search(r"room\s*0*([0-9]+)", file_name.lower())
    if not match:
        return None
    return f"room{int(match.group(1))}"


def upload_file(path: Path, bucket_name: str, object_name: str) -> tuple[str | None, int]:
    raw = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    storage_service.upload_bytes(
        bucket_name=bucket_name,
        object_name=object_name,
        data=raw,
        content_type=content_type,
    )
    return content_type, len(raw)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    storage_service.ensure_buckets()

    with SessionLocal() as db:
        seed_defaults(db)
        rooms = {room.slug: room for room in db.scalars(select(Room)).all()}

        # Optional clean rerun.
        db.execute(delete(FileAsset))
        db.commit()

        if THUMBNAILS_DIR.exists():
            for thumb_path in THUMBNAILS_DIR.rglob("*"):
                if not thumb_path.is_file():
                    continue

                room_slug = room_slug_from_name(thumb_path.name)
                if not room_slug or room_slug not in rooms:
                    continue

                try:
                    capture_date = datetime.strptime(thumb_path.parent.name, "%Y%m%d").date()
                except ValueError:
                    continue

                panorama_path = PANORAMAS_DIR / thumb_path.parent.name / thumb_path.name
                if not panorama_path.exists():
                    continue

                room = rooms[room_slug]
                image_object = f"{room.slug}/{capture_date.isoformat()}/{panorama_path.name}"
                thumbnail_object = f"{room.slug}/{capture_date.isoformat()}/thumb-{thumb_path.name}"

                content_type, file_size = upload_file(
                    panorama_path,
                    "construction-images",
                    image_object,
                )
                upload_file(
                    thumb_path,
                    "construction-thumbnails",
                    thumbnail_object,
                )

                db.add(
                    FileAsset(
                        room_id=room.id,
                        media_type="image",
                        capture_date=capture_date,
                        original_name=panorama_path.name,
                        display_name=panorama_path.name,
                        bucket_name="construction-images",
                        object_name=image_object,
                        thumbnail_bucket_name="construction-thumbnails",
                        thumbnail_object_name=thumbnail_object,
                        content_type=content_type,
                        file_size=file_size,
                        metadata_json={"source": "legacy-public"},
                    )
                )

        if POINTCLOUD_DIR.exists():
            for pointcloud_path in POINTCLOUD_DIR.rglob("*"):
                if not pointcloud_path.is_file():
                    continue

                room_slug = room_slug_from_name(pointcloud_path.name)
                if not room_slug or room_slug not in rooms:
                    continue

                try:
                    capture_date = datetime.strptime(pointcloud_path.parent.name, "%Y%m%d").date()
                except ValueError:
                    continue

                room = rooms[room_slug]
                object_name = f"{room.slug}/{capture_date.isoformat()}/{pointcloud_path.name}"
                content_type, file_size = upload_file(
                    pointcloud_path,
                    "construction-pointclouds",
                    object_name,
                )

                db.add(
                    FileAsset(
                        room_id=room.id,
                        media_type="pointcloud",
                        capture_date=capture_date,
                        original_name=pointcloud_path.name,
                        display_name=pointcloud_path.name,
                        bucket_name="construction-pointclouds",
                        object_name=object_name,
                        content_type=content_type,
                        file_size=file_size,
                        metadata_json={"source": "legacy-public"},
                    )
                )

        db.commit()
        print("Legacy asset migration completed.")


if __name__ == "__main__":
    main()
