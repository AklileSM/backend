"""Robot map versions.

Every robot map uploaded to a project gets a version number, and every capture point records the
version it was placed on. A point from another version belongs to a different map frame: its x/y
would send the robot somewhere else, so it is "retired" and refused for missions and schedules.
Switching the project back to the previous map makes that map's points current again.

Maps uploaded before versioning existed have no version (None), and neither do the points placed
on them, so existing projects keep working unchanged until their next map upload.
"""

from __future__ import annotations

from typing import Any

from app.models import Project, RobotCapturePoint

#: How many earlier maps a project keeps, so it can switch back.
MAP_HISTORY_LIMIT = 5


def map_meta(project: Project) -> dict[str, Any]:
    return dict(project.robot_map_json) if isinstance(project.robot_map_json, dict) else {}


def current_map_version(project: Project) -> int | None:
    version = map_meta(project).get("map_version")
    return int(version) if version is not None else None


def point_map_version(point: RobotCapturePoint) -> int | None:
    version = (point.metadata_json or {}).get("map_version")
    return int(version) if version is not None else None


def is_retired(point: RobotCapturePoint, project: Project) -> bool:
    """True when the point was placed on a different map than the project's current one."""
    return point_map_version(point) != current_map_version(project)


def retired_detail(points: list[RobotCapturePoint]) -> str:
    names = ", ".join(point.name for point in points[:3])
    more = f" and {len(points) - 3} more" if len(points) > 3 else ""
    return (
        f"{names}{more} {'was' if len(points) == 1 else 'were'} placed on an older robot map. "
        "Place the waypoints again on the current map, or switch the project back to the map they belong to."
    )


def next_map_version(meta: dict[str, Any]) -> int:
    versions = [meta.get("map_version")] + [entry.get("map_version") for entry in meta.get("history") or []]
    return max([int(v) for v in versions if v is not None] or [0]) + 1
