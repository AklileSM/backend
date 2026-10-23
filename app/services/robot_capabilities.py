"""Which capture types a robot can do right now.

A capture type is available only when SiteScope can process its output AND the robot reports the
payload for it as detected (robot/payloads.py, sent with each heartbeat). A payload whose output
SiteScope has no pipeline for is never offered, however capable; a supported output whose
payload is unplugged or silent is unavailable, with the robot's reason.

Agents that predate payload reporting send no `payloads`, so nothing is known: every supported
type stays available, as before, rather than blocking captures until the robot is updated.
"""

from __future__ import annotations

from typing import Any

from app.models import RobotPresence

# The outputs SiteScope can ingest and show, in the order they are offered.
SUPPORTED_OUTPUTS: dict[str, str] = {
    "image": "360° image",
    "pointcloud": "point cloud",
}

_OUTPUT_ALIASES = {
    "image": "image", "panorama": "image",
    "pointcloud": "pointcloud", "point_cloud": "pointcloud", "pcd": "pointcloud", "lidar": "pointcloud",
}


def reported_payloads(presence: RobotPresence | None) -> list[dict[str, Any]] | None:
    """The robot's last payload report, or None when it has never sent one."""
    if presence is None or not isinstance(presence.payload_json, dict):
        return None
    heartbeat = presence.payload_json.get("heartbeat")
    payloads = heartbeat.get("payloads") if isinstance(heartbeat, dict) else None
    return [item for item in payloads if isinstance(item, dict)] if isinstance(payloads, list) else None


def capture_capabilities(presence: RobotPresence | None) -> list[dict[str, Any]]:
    payloads = reported_payloads(presence)
    capabilities = []
    for output, label in SUPPORTED_OUTPUTS.items():
        if payloads is None:
            capabilities.append({"output": output, "label": label, "available": True, "reported": False,
                                 "payload_name": None, "reason": None})
            continue
        matching = [item for item in payloads if item.get("output") == output]
        detected = next((item for item in matching if item.get("detected")), None)
        if detected is not None:
            capabilities.append({"output": output, "label": label, "available": True, "reported": True,
                                 "payload_name": detected.get("name"), "reason": None})
            continue
        missing = matching[0] if matching else None
        capabilities.append({
            "output": output,
            "label": label,
            "available": False,
            "reported": True,
            "payload_name": missing.get("name") if missing else None,
            "reason": (missing.get("detail") if missing else None)
            or f"The robot has no payload for a {label}.",
        })
    return capabilities


def available_outputs(presence: RobotPresence | None) -> list[str]:
    return [item["output"] for item in capture_capabilities(presence) if item["available"]]


def requested_outputs(robot_meta: dict[str, Any] | None, capture_mode: str | None) -> list[str]:
    """The capture types a mission or schedule asks for, read the way the robot agent reads them."""
    raw = (robot_meta or {}).get("capture_outputs")
    if isinstance(raw, str):
        raw = raw.split(",")
    if isinstance(raw, list):
        outputs = {_OUTPUT_ALIASES.get(str(item).strip().lower()) for item in raw}
        return [output for output in SUPPORTED_OUTPUTS if output in outputs]
    return ["pointcloud"] if (capture_mode or "").lower() == "pointcloud" else ["image"]


def unavailable_detail(presence: RobotPresence | None, outputs: list[str]) -> str | None:
    """Why the robot can't capture `outputs` right now, or None when it can."""
    by_output = {item["output"]: item for item in capture_capabilities(presence)}
    for output in outputs:
        capability = by_output.get(output)
        if capability is not None and not capability["available"]:
            return f"The robot can't take a {capability['label']} right now: {capability['reason']}"
    return None
