"""Pose repeatability and capture-quality analysis across a project's robot captures.

Reads ``file_assets.metadata_json.robot`` for every robot capture in a project
and reports the two things a longitudinal documentation study needs:

1. **Pose repeatability** — how tightly the robot re-occupies each capture point
   across missions. This is the metric that makes captures at a given station
   comparable over time; without it, change detection has no ground to stand on.

   Repeatability is *precision*, not accuracy, and the two must not be confused.
   A robot that stops 40 cm short of the commanded waypoint every single week is
   perfectly repeatable — every capture comes from the same physical spot, so
   the series is comparable. The commanded coordinate was an arbitrary click in
   RViz; nothing depends on hitting it. A robot that averages 5 cm from
   commanded but scatters randomly by ±30 cm has excellent accuracy and useless
   repeatability.

   So precision is measured against each station's **own centroid**, never
   against the commanded pose, and never as a mean of scalar distances: a visit
   25 cm north and a visit 25 cm south both have |deviation| = 0.25 and cancel
   to zero spread, while actually sitting 50 cm apart. Reported per station:

   * ``cep50`` / ``cep95`` — median and 95th-percentile radial distance from the
     station centroid. This is the repeatability number to publish.
   * ``max_pairwise_separation_m`` — the largest distance between any two visits.
     The bluntest statement of "how far apart can two captures in this series be".
   * ``yaw_circular_stdev_deg`` — angular spread about the circular mean (a plain
     mean is invalid for angles).
   * ``bias_m`` — distance from centroid to commanded pose. This is *accuracy*,
     and it is largely cosmetic for longitudinal work.
   * ``drift_m_per_day`` — magnitude of the least-squares trend of (x, y) over
     calendar time. Distinguishes a station that is creeping from one that is
     merely noisy.

   Note that ``pose`` records where AMCL *believes* the robot is, not ground
   truth. Control error and localisation error are folded together here and
   cannot be separated without external measurement.
2. **Quality metric distributions** — the empirical spread of every metric
   ``robot/capture_quality.py`` records, with suggested percentile thresholds.

Both work retroactively. Pose deviation is recomputed from the stored commanded
and achieved poses when ``quality`` is absent, so this reports on captures made
before capture_quality.py was ever deployed.

Threshold suggestions are percentiles of the observed distribution, offered as a
*starting point for labelling*, not as results. The honest path to a defensible
threshold is still: collect, hand-label a sample, fit the threshold to the
labels, report precision/recall. A percentile is where you begin that, not where
you end.

Usage
-----
Run inside the backend container (or anywhere ``app.database`` can reach PG)::

    python scripts/analyze_capture_quality.py --project a6-stern
    python scripts/analyze_capture_quality.py --project a6-stern --since 2026-08-01
    python scripts/analyze_capture_quality.py --project a6-stern --csv-out poses.csv
    python scripts/analyze_capture_quality.py --project a6-stern --json-out report.json

``--csv-out`` writes one row per capture (the long-format table to plot from in
a paper); ``--json-out`` writes the full computed report.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import joinedload  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import FileAsset, Project, Room  # noqa: E402

# Metrics worth summarising, and which tail indicates a problem.
# "low" = small values are suspect (blur), "high" = large values are (clipping).
METRIC_BAD_TAIL: dict[str, str] = {
    "blur_laplacian_var": "low",
    "mean_luminance": "both",
    "rms_contrast": "low",
    "clipped_highlight_frac": "high",
    "clipped_shadow_frac": "high",
    "pose_deviation_m": "high",
    "pose_deviation_deg": "high",
    "point_count": "low",
    "bbox_max_extent_m": "low",
    "intensity_nonzero_frac": "low",
    "file_bytes": "low",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _yaw_of(pose: dict[str, Any]) -> float | None:
    if pose.get("yaw") is not None:
        try:
            return float(pose["yaw"])
        except (TypeError, ValueError):
            return None
    try:
        qx, qy = float(pose["qx"]), float(pose["qy"])
        qz, qw = float(pose["qz"]), float(pose["qw"])
    except (KeyError, TypeError, ValueError):
        return None
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


def _recompute_pose_deviation(robot: dict[str, Any]) -> tuple[float | None, float | None]:
    """Pose deviation from raw stored poses, for captures with no quality blob."""
    commanded = robot.get("target_waypoint_pose")
    achieved = robot.get("pose")
    if not isinstance(commanded, dict) or not isinstance(achieved, dict):
        return None, None
    try:
        dx = float(achieved["x"]) - float(commanded["x"])
        dy = float(achieved["y"]) - float(commanded["y"])
    except (KeyError, TypeError, ValueError):
        return None, None
    dz = 0.0
    if commanded.get("z") is not None and achieved.get("z") is not None:
        try:
            dz = float(achieved["z"]) - float(commanded["z"])
        except (TypeError, ValueError):
            dz = 0.0
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)

    deg = None
    cyaw, ayaw = _yaw_of(commanded), _yaw_of(achieved)
    if cyaw is not None and ayaw is not None:
        diff = math.atan2(math.sin(ayaw - cyaw), math.cos(ayaw - cyaw))
        deg = abs(math.degrees(diff))
    return dist, deg


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile. ``pct`` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _summarise(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "p95": round(_percentile(values, 95) or 0.0, 4),
        "max": round(max(values), 4),
    }


def _linear_trend(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of y over x. Used for drift per day."""
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _pose_xy_yaw(pose: Any) -> tuple[float | None, float | None, float | None]:
    """Extract (x, y, yaw) from a stored pose dict, tolerating missing fields."""
    if not isinstance(pose, dict):
        return None, None, None
    try:
        x, y = float(pose["x"]), float(pose["y"])
    except (KeyError, TypeError, ValueError):
        return None, None, None
    return x, y, _yaw_of(pose)


def _circular_stats(angles_rad: list[float]) -> dict[str, Any]:
    """Circular mean and standard deviation for angles.

    A plain arithmetic mean is wrong for angles — averaging 359° and 1° gives
    180°. Both statistics come from the mean resultant vector instead.
    """
    if not angles_rad:
        return {"n": 0, "circular_mean_deg": None, "circular_stdev_deg": None}
    cos_mean = statistics.fmean(math.cos(a) for a in angles_rad)
    sin_mean = statistics.fmean(math.sin(a) for a in angles_rad)
    resultant = math.hypot(cos_mean, sin_mean)
    mean_angle = math.atan2(sin_mean, cos_mean)
    if len(angles_rad) < 2:
        stdev_deg: float | None = 0.0
    elif resultant <= 1e-12:
        stdev_deg = None  # fully dispersed; no meaningful mean direction
    else:
        # max(0, ...) keeps a resultant of exactly 1.0 from yielding -0.0
        stdev_deg = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(min(resultant, 1.0)))))
    return {
        "n": len(angles_rad),
        "circular_mean_deg": round(math.degrees(mean_angle), 3),
        "circular_stdev_deg": round(stdev_deg, 3) if stdev_deg is not None else None,
    }


def _max_pairwise_separation(points: list[tuple[float, float]]) -> float | None:
    """Largest distance between any two visits to a station.

    O(n^2), which is fine: n is the number of missions, not the number of points.
    The most directly interpretable comparability number — two captures this far
    apart are looking at the room from measurably different places.
    """
    if len(points) < 2:
        return None
    worst = 0.0
    for i in range(len(points) - 1):
        xi, yi = points[i]
        for j in range(i + 1, len(points)):
            worst = max(worst, math.hypot(points[j][0] - xi, points[j][1] - yi))
    return worst


def _repeatability(
    points: list[tuple[float, float]],
    commanded: tuple[float, float] | None,
) -> dict[str, Any]:
    """Precision of a station: spread of achieved poses about their own centroid.

    Deliberately *not* computed against the commanded pose — see the module
    docstring for why that measures the wrong thing.
    """
    out: dict[str, Any] = {
        "n": len(points),
        "centroid": None,
        "cep50": None,
        "cep95": None,
        "rms_2d": None,
        "max_radial": None,
        "stdev_x": None,
        "stdev_y": None,
        "max_pairwise_separation_m": _max_pairwise_separation(points),
        "bias_m": None,
        "bias_vector": None,
    }
    if not points:
        return out

    cx = statistics.fmean(p[0] for p in points)
    cy = statistics.fmean(p[1] for p in points)
    out["centroid"] = [round(cx, 4), round(cy, 4)]

    radial = [math.hypot(px - cx, py - cy) for px, py in points]
    out["cep50"] = round(statistics.median(radial), 4)
    out["cep95"] = round(_percentile(radial, 95) or 0.0, 4)
    out["rms_2d"] = round(math.sqrt(statistics.fmean(r * r for r in radial)), 4)
    out["max_radial"] = round(max(radial), 4)
    if len(points) > 1:
        out["stdev_x"] = round(statistics.stdev([p[0] for p in points]), 4)
        out["stdev_y"] = round(statistics.stdev([p[1] for p in points]), 4)
    if out["max_pairwise_separation_m"] is not None:
        out["max_pairwise_separation_m"] = round(out["max_pairwise_separation_m"], 4)

    if commanded is not None:
        bx, by = cx - commanded[0], cy - commanded[1]
        out["bias_vector"] = [round(bx, 4), round(by, 4)]
        out["bias_m"] = round(math.hypot(bx, by), 4)
    return out


def _drift_vector(
    dated_points: list[tuple[float, float, float]],
) -> float | None:
    """Magnitude of the per-day (x, y) trend — creep, as distinct from noise.

    Fitting x and y against time separately and combining the slopes keeps a
    consistent directional creep visible where a trend on scalar distance would
    average it away.
    """
    if len(dated_points) < 3:
        return None
    slope_x = _linear_trend([(t, x) for t, x, _ in dated_points])
    slope_y = _linear_trend([(t, y) for t, _, y in dated_points])
    if slope_x is None or slope_y is None:
        return None
    return math.hypot(slope_x, slope_y)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def collect_records(
    project_slug: str,
    *,
    since: date | None,
    until: date | None,
) -> list[dict[str, Any]]:
    """One flat record per robot capture in the project."""
    records: list[dict[str, Any]] = []
    with SessionLocal() as db:
        project = db.scalar(select(Project).where(Project.slug == project_slug))
        if project is None:
            raise SystemExit(f"No project with slug '{project_slug}'")

        stmt = (
            select(FileAsset)
            .join(Room, FileAsset.room_id == Room.id)
            .where(Room.project_id == project.id)
            .options(joinedload(FileAsset.room))
            .order_by(FileAsset.capture_date, FileAsset.id)
        )
        if since is not None:
            stmt = stmt.where(FileAsset.capture_date >= since)
        if until is not None:
            stmt = stmt.where(FileAsset.capture_date <= until)

        for asset in db.scalars(stmt).unique():
            meta = asset.metadata_json or {}
            robot = meta.get("robot")
            if not isinstance(robot, dict):
                continue  # not a robot capture

            quality = robot.get("quality") if isinstance(robot.get("quality"), dict) else {}
            checks = quality.get("checks", {}) if isinstance(quality.get("checks"), dict) else {}

            dev_m = checks.get("pose_deviation_m")
            dev_deg = checks.get("pose_deviation_deg")
            recomputed = False
            if dev_m is None:
                dev_m, dev_deg = _recompute_pose_deviation(robot)
                recomputed = dev_m is not None

            # capture_point_id is the stable identity of a physical station;
            # target_waypoint is the fallback for captures predating it.
            station = (
                robot.get("capture_point_id")
                or robot.get("target_waypoint")
                or asset.room.slug
            )

            # Raw poses are what repeatability is computed from; the scalar
            # deviation alone cannot express directional scatter.
            ax, ay, ayaw = _pose_xy_yaw(robot.get("pose"))
            cx, cy, cyaw = _pose_xy_yaw(robot.get("target_waypoint_pose"))

            record: dict[str, Any] = {
                "asset_id": asset.id,
                "capture_date": asset.capture_date.isoformat() if asset.capture_date else None,
                "media_type": asset.media_type,
                "room_slug": asset.room.slug if asset.room else None,
                "station": str(station),
                "mission_id": robot.get("mission_id"),
                "achieved_x": ax,
                "achieved_y": ay,
                "achieved_yaw_rad": ayaw,
                "commanded_x": cx,
                "commanded_y": cy,
                "commanded_yaw_rad": cyaw,
                "pose_deviation_m": dev_m,
                "pose_deviation_deg": dev_deg,
                "pose_deviation_recomputed": recomputed,
                "pose_capture_error": robot.get("pose_capture_error"),
                "quality_schema": quality.get("schema"),
                "advisory_flags": quality.get("advisory_flags") or [],
            }
            for key in METRIC_BAD_TAIL:
                if key not in record and key in checks:
                    record[key] = checks[key]
            records.append(record)
    return records


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyse(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_station[r["station"]].append(r)

    stations: list[dict[str, Any]] = []
    pooled_radial: list[float] = []
    for station, rows in sorted(by_station.items()):
        devs = [r["pose_deviation_m"] for r in rows if isinstance(r["pose_deviation_m"], (int, float))]
        degs = [r["pose_deviation_deg"] for r in rows if isinstance(r["pose_deviation_deg"], (int, float))]

        points = [
            (r["achieved_x"], r["achieved_y"])
            for r in rows
            if isinstance(r["achieved_x"], (int, float)) and isinstance(r["achieved_y"], (int, float))
        ]
        commanded_xy = next(
            (
                (r["commanded_x"], r["commanded_y"])
                for r in rows
                if isinstance(r["commanded_x"], (int, float))
                and isinstance(r["commanded_y"], (int, float))
            ),
            None,
        )
        repeat = _repeatability(points, commanded_xy)

        # Pool residuals about each station's own centroid so the overall figure
        # is a repeatability number too, not a mix of per-station offsets.
        if repeat["centroid"] and len(points) > 1:
            ccx, ccy = repeat["centroid"]
            pooled_radial.extend(math.hypot(px - ccx, py - ccy) for px, py in points)

        yaws = [r["achieved_yaw_rad"] for r in rows if isinstance(r["achieved_yaw_rad"], (int, float))]
        yaw_stats = _circular_stats(yaws)

        dated_xy = [
            (datetime.fromisoformat(r["capture_date"]).toordinal(), r["achieved_x"], r["achieved_y"])
            for r in rows
            if r["capture_date"]
            and isinstance(r["achieved_x"], (int, float))
            and isinstance(r["achieved_y"], (int, float))
        ]
        drift = _drift_vector(dated_xy)

        stations.append({
            "station": station,
            "captures": len(rows),
            "missions": len({r["mission_id"] for r in rows if r["mission_id"]}),
            "repeatability": repeat,
            "yaw": yaw_stats,
            "accuracy_vs_commanded_m": _summarise(devs),
            "pose_deviation_deg": _summarise(degs),
            "drift_m_per_day": round(drift, 6) if drift is not None else None,
            "unlocalised_captures": sum(1 for r in rows if r["pose_capture_error"]),
        })

    metrics: dict[str, Any] = {}
    for key, bad_tail in METRIC_BAD_TAIL.items():
        values = [r[key] for r in records if isinstance(r.get(key), (int, float))]
        if not values:
            continue
        summary = _summarise(values)
        # Suggested starting threshold: the 5th/95th percentile on the bad tail.
        if bad_tail == "low":
            summary["suggested_min_p5"] = round(_percentile(values, 5) or 0.0, 4)
        elif bad_tail == "high":
            summary["suggested_max_p95"] = round(_percentile(values, 95) or 0.0, 4)
        else:
            summary["suggested_min_p5"] = round(_percentile(values, 5) or 0.0, 4)
            summary["suggested_max_p95"] = round(_percentile(values, 95) or 0.0, 4)
        metrics[key] = summary

    flag_counts: dict[str, int] = defaultdict(int)
    for r in records:
        for flag in r["advisory_flags"]:
            flag_counts[flag] += 1

    all_devs = [r["pose_deviation_m"] for r in records if isinstance(r["pose_deviation_m"], (int, float))]
    return {
        "totals": {
            "captures": len(records),
            "stations": len(by_station),
            "missions": len({r["mission_id"] for r in records if r["mission_id"]}),
            "with_quality_blob": sum(1 for r in records if r["quality_schema"] is not None),
            "pose_deviation_recomputed": sum(1 for r in records if r["pose_deviation_recomputed"]),
            "unlocalised_captures": sum(1 for r in records if r["pose_capture_error"]),
        },
        # Precision, pooled about per-station centroids. This is the headline.
        "pose_repeatability_overall": {
            **_summarise(pooled_radial),
            "cep50": round(statistics.median(pooled_radial), 4) if pooled_radial else None,
            "cep95": round(_percentile(pooled_radial, 95) or 0.0, 4) if pooled_radial else None,
        },
        # Accuracy, i.e. offset from the commanded waypoints. Context only.
        "accuracy_vs_commanded_overall": _summarise(all_devs),
        "stations": stations,
        "metrics": metrics,
        "advisory_flag_counts": dict(sorted(flag_counts.items(), key=lambda kv: -kv[1])),
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _fmt(value: Any, width: int = 9, places: int = 3) -> str:
    if value is None:
        return "—".rjust(width)
    if isinstance(value, float):
        return f"{value:.{places}f}".rjust(width)
    return str(value).rjust(width)


def print_report(report: dict[str, Any]) -> None:
    totals = report["totals"]
    print("\n=== Totals ===")
    for key, value in totals.items():
        print(f"  {key.replace('_', ' '):<28} {value}")

    overall = report["pose_repeatability_overall"]
    print("\n=== Pose repeatability — PRECISION, pooled about station centroids ===")
    if overall.get("n"):
        print(f"  n={overall['n']}  CEP50={overall['cep50']} m  CEP95={overall['cep95']} m  "
              f"max={overall['max']} m  sd={overall['stdev']} m")
        print("  This is the number to publish: how tightly repeat visits cluster.")
    else:
        print("  Not computable — need >=2 localised captures at a station, with "
              "pose recorded.")

    accuracy = report["accuracy_vs_commanded_overall"]
    if accuracy.get("n"):
        print("\n=== Offset from commanded waypoints — ACCURACY (context only) ===")
        print(f"  n={accuracy['n']}  mean={accuracy['mean']} m  p95={accuracy['p95']} m  "
              f"max={accuracy['max']} m")
        print("  A consistent offset costs you nothing; only the scatter above does.")

    print("\n=== Per capture point ===")
    print(f"  {'station':<22}{'n':>4}{'miss':>5}{'CEP50':>8}{'CEP95':>8}{'maxsep':>8}"
          f"{'rms2d':>8}{'yaw_sd':>8}{'bias':>8}{'drift/day':>11}{'unloc':>6}")
    for s in report["stations"]:
        rp, yaw = s["repeatability"], s["yaw"]
        print(f"  {s['station'][:21]:<22}{s['captures']:>4}{s['missions']:>5}"
              f"{_fmt(rp.get('cep50'), 8)}{_fmt(rp.get('cep95'), 8)}"
              f"{_fmt(rp.get('max_pairwise_separation_m'), 8)}{_fmt(rp.get('rms_2d'), 8)}"
              f"{_fmt(yaw.get('circular_stdev_deg'), 8, 2)}{_fmt(rp.get('bias_m'), 8)}"
              f"{_fmt(s['drift_m_per_day'], 11, 5)}{s['unlocalised_captures']:>6}")
    print("  CEP50/CEP95 = median / 95th-pct radial distance from the station's own")
    print("  centroid. maxsep = furthest any two visits sat apart. bias = centroid")
    print("  offset from commanded (accuracy). yaw_sd = circular stdev, degrees.")

    thin = [s["station"] for s in report["stations"] if s["captures"] < 5]
    if thin:
        print(f"\n  NOTE: {len(thin)} station(s) have <5 captures; CEP95 and drift are "
              f"noise-dominated\n  at that sample size. Treat as provisional: "
              f"{', '.join(thin[:6])}{' …' if len(thin) > 6 else ''}")

    if report["metrics"]:
        print("\n=== Quality metric distributions ===")
        print(f"  {'metric':<26}{'n':>6}{'median':>12}{'p95':>12}{'min':>12}{'max':>12}   suggestion")
        for key, m in report["metrics"].items():
            suggestion = ""
            if "suggested_min_p5" in m:
                suggestion += f"min≥{m['suggested_min_p5']} "
            if "suggested_max_p95" in m:
                suggestion += f"max≤{m['suggested_max_p95']}"
            print(f"  {key:<26}{m['n']:>6}{_fmt(m['median'], 12)}{_fmt(m['p95'], 12)}"
                  f"{_fmt(m['min'], 12)}{_fmt(m['max'], 12)}   {suggestion}")

    if report["advisory_flag_counts"]:
        print("\n=== Advisory flags raised (PROVISIONAL thresholds) ===")
        for flag, count in report["advisory_flag_counts"].items():
            print(f"  {flag:<34} {count}")

    print("\nSuggested thresholds are percentiles of what you observed, not "
          "validated limits.\nHand-label a sample, fit thresholds to the labels, "
          "then report precision/recall.\n")


def write_csv(records: list[dict[str, Any]], path: str) -> None:
    if not records:
        return
    columns = sorted({k for r in records for k in r})
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = dict(r)
            row["advisory_flags"] = "|".join(row.get("advisory_flags") or [])
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pose repeatability and capture-quality analysis for robot captures."
    )
    parser.add_argument("--project", required=True, help="Project slug, e.g. a6-stern")
    parser.add_argument("--since", help="Only captures on/after this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only captures on/before this date (YYYY-MM-DD)")
    parser.add_argument("--csv-out", help="Write one row per capture to this CSV")
    parser.add_argument("--json-out", help="Write the full report as JSON")
    args = parser.parse_args()

    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None

    records = collect_records(args.project, since=since, until=until)
    if not records:
        raise SystemExit(
            f"No robot captures found for project '{args.project}'. "
            "Robot captures are those with metadata_json.robot set."
        )

    report = analyse(records)
    print_report(report)

    if args.csv_out:
        write_csv(records, args.csv_out)
        print(f"Wrote {len(records)} rows to {args.csv_out}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(f"Wrote report to {args.json_out}")


if __name__ == "__main__":
    main()
