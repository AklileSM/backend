"""The live map shown while a new robot map is recorded.

A script on the laptop runs beside SLAM and posts the growing occupancy grid (as a PNG) and the
robot's pose a few times a second. Only the latest frame per robot matters and it is useless once
mapping ends, so it lives in memory like the latest telemetry: a backend restart just waits for
the next frame, and the laptop resends the map image whenever the backend says it has none.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any

# Trail points closer than this to the previous one are skipped, so a robot standing still
# doesn't grow the trail; past the cap every other point is dropped, keeping the shape.
TRAIL_MIN_STEP_M = 0.10
TRAIL_MAX_POINTS = 4000

_LOCK = threading.Lock()
_PREVIEWS: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_map_preview(robot_user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Store one frame from the laptop. Returns which map image the backend now holds."""
    session_id = payload["session_id"]
    pose = payload.get("pose")
    image = payload.get("map")
    now = _now()
    with _LOCK:
        preview = _PREVIEWS.get(robot_user_id)
        if preview is None or preview["session_id"] != session_id:
            # A new mapping session: its first pose is where the robot started, i.e. home.
            preview = {
                "session_id": session_id,
                "started_at_utc": now,
                "home": None,
                "pose": None,
                "trail": [],
                "distance_m": 0.0,
                "map": None,
                "map_updated_at_utc": None,
            }
            _PREVIEWS[robot_user_id] = preview
        preview["updated_at_utc"] = now
        if pose is not None:
            preview["pose"] = pose
            if preview["home"] is None:
                preview["home"] = pose
            trail = preview["trail"]
            if not trail:
                trail.append({"x": pose["x"], "y": pose["y"]})
            else:
                step = math.hypot(pose["x"] - trail[-1]["x"], pose["y"] - trail[-1]["y"])
                if step >= TRAIL_MIN_STEP_M:
                    trail.append({"x": pose["x"], "y": pose["y"]})
                    preview["distance_m"] += step
                    if len(trail) > TRAIL_MAX_POINTS:
                        preview["trail"] = [*trail[:-1:2], trail[-1]]
        if image is not None and (preview["map"] is None or image["seq"] != preview["map"]["seq"]):
            preview["map"] = dict(image)
            preview["map_updated_at_utc"] = now
        return {"map_seq": preview["map"]["seq"] if preview["map"] else None}


def get_map_preview(robot_user_id: str, *, image_seq: int | None = None) -> dict[str, Any] | None:
    """The latest frame. The image itself is left out when the caller already has that seq."""
    with _LOCK:
        preview = _PREVIEWS.get(robot_user_id)
        if preview is None:
            return None
        result = {key: value for key, value in preview.items() if key not in ("map", "trail")}
        result["trail"] = list(preview["trail"])
        image = preview["map"]
    if image is not None:
        result["map"] = {key: value for key, value in image.items() if key != "png_base64"}
        result["map"]["image"] = (
            None if image_seq == image["seq"] else f"data:image/png;base64,{image['png_base64']}"
        )
    else:
        result["map"] = None
    return result


def clear_map_previews() -> None:
    with _LOCK:
        _PREVIEWS.clear()
