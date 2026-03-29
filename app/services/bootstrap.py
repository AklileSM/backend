from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, Room

DEFAULT_PROJECTS = [
    {"name": "A6 Stern", "slug": "a6-stern"},
    {"name": "Project X", "slug": "projectx"},
    {"name": "Project Y", "slug": "projecty"},
]
DEFAULT_ROOMS = [
    ("Room 1", "room1", 1),
    ("Room 2", "room2", 2),
    ("Room 3", "room3", 3),
    ("Room 4", "room4", 4),
    ("Room 5", "room5", 5),
    ("Room 6", "room6", 6),
]


def seed_defaults(db: Session) -> None:
    for data in DEFAULT_PROJECTS:
        if db.scalar(select(Project).where(Project.slug == data["slug"])) is None:
            db.add(Project(**data))
    db.flush()

    a6 = db.scalar(select(Project).where(Project.slug == "a6-stern"))
    if a6 is None:
        db.commit()
        return

    for name, slug, sort_order in DEFAULT_ROOMS:
        existing = db.scalar(select(Room).where(Room.slug == slug))
        if existing is None:
            db.add(
                Room(
                    project_id=a6.id,
                    name=name,
                    slug=slug,
                    sort_order=sort_order,
                )
            )

    db.commit()
