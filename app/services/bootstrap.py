from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, Room

DEFAULT_PROJECT = {"name": "A6 Stern", "slug": "a6-stern"}
DEFAULT_ROOMS = [
    ("Room 1", "room1", 1),
    ("Room 2", "room2", 2),
    ("Room 3", "room3", 3),
    ("Room 4", "room4", 4),
    ("Room 5", "room5", 5),
    ("Room 6", "room6", 6),
]


def seed_defaults(db: Session) -> None:
    project = db.scalar(select(Project).where(Project.slug == DEFAULT_PROJECT["slug"]))
    if project is None:
        project = Project(**DEFAULT_PROJECT)
        db.add(project)
        db.flush()

    for name, slug, sort_order in DEFAULT_ROOMS:
        existing = db.scalar(select(Room).where(Room.slug == slug))
        if existing is None:
            db.add(
                Room(
                    project_id=project.id,
                    name=name,
                    slug=slug,
                    sort_order=sort_order,
                )
            )

    db.commit()
