"""
Vision model benchmark, hits Ollama directly, bypasses backend cache.

What it measures
----------------
  latency     — wall-clock ms per call (per run and aggregated)
  structure   — are all three expected sections present in every response?
  rouge_ref   — ROUGE-L F1 vs a reference description (only when eval set has one)
  consistency — ROUGE-L F1 between pairs of runs for the same image
                (higher = model gives similar answers every time)

Usage
-----
  # Auto-discover all panorama images on disk (recommended when no eval set matches)
  python scripts/benchmark_vision.py --image-dir ~/a6-stern/frontend/public/Images --discover

  # Use a prepared eval set with reference descriptions
  python scripts/benchmark_vision.py \\
      --image-dir ~/a6-stern/frontend/public/Images \\
      --eval-set scripts/eval_set.json

  # Save full results to JSON
  python scripts/benchmark_vision.py --image-dir ... --discover --output scripts/benchmark_results.json

Dependencies (in addition to the backend venv)
----------------------------------------------
  pip install rouge-score
  (httpx and minio are already in the backend requirements)
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import statistics
import sys
import time
from itertools import combinations
from pathlib import Path
from typing import Any

# ── allow imports from app.* ────────────────────────────────────────────────
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402
from rouge_score import rouge_scorer  # noqa: E402

from app.config import get_settings  # noqa: E402

# ── constants ────────────────────────────────────────────────────────────────
PROMPT = (
    "You are a construction site inspector. This is a 360-degree panoramic image captured with a wide-angle lens. All walls, floors, and ceilings in this building are straight and flat, any curvature you see is purely lens distortion from the panoramic projection, not actual curved architecture. Never describe walls or surfaces as curved. Treat all surfaces as straight.\n\n"
    "Look at this image and write a detailed report with three sections:\n\n"
    "SCENE: Describe what you see, the space, ongoing work, materials, workers, and overall condition.\n\n"
    "QUALITY ISSUES: Describe any quality or workmanship concerns in detail. Explain what is wrong and where.\n\n"
    "SAFETY ISSUES: Describe any safety concerns in detail. Explain the hazard and its location or cause.\n\n"
    "Write at upto 3 sentences per section. Do not repeat points across sections."
)

REQUIRED_SECTIONS = ["SCENE", "QUALITY ISSUES", "SAFETY ISSUES"]

SCORER = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def rouge_l(a: str, b: str) -> float:
    return SCORER.score(a, b)["rougeL"].fmeasure


def has_all_sections(text: str) -> bool:
    t = text.upper()
    return all(sec in t for sec in REQUIRED_SECTIONS)


def strip_think_blocks(text: str) -> str:
    """Remove <think>…</think> reasoning blocks emitted by Qwen3 thinking models."""
    match = re.search(r"<think>(.*?)</think>(.*)", text, re.DOTALL)
    if match:
        visible = match.group(2).strip()
        return visible if visible else match.group(1).strip()
    return text


def fetch_image_as_data_url(object_name: str, bucket: str, image_dir: Path | None) -> str:
    """
    Load image bytes from disk (--image-dir) or fall back to MinIO.
    Disk path = image_dir / object_name, e.g. /path/to/frontend/public/panoramas/…/room02.jpg
    """
    if image_dir is not None:
        file_path = image_dir / object_name
        if not file_path.exists():
            raise FileNotFoundError(f"not found on disk: {file_path}")
        raw = file_path.read_bytes()
        mime = mimetypes.guess_type(object_name)[0] or "image/jpeg"
        b64 = base64.standard_b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"

    # MinIO fallback
    from app.services.storage import storage_service  # lazy import
    raw = storage_service.get_object_bytes(bucket, object_name)
    mime = mimetypes.guess_type(object_name)[0] or "image/jpeg"
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def call_ollama(
    client: httpx.Client,
    ollama_url: str,
    model: str,
    data_url: str,
    api_key: str,
) -> tuple[str, float]:
    """
    Returns (description_text, elapsed_ms).
    Raises RuntimeError on API failure.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
        "top_p": 0.9,
        "frequency_penalty": 1.3,
        "stream": False,
    }

    t0 = time.perf_counter()
    response = client.post(ollama_url, headers=headers, json=payload)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (exc.response.text or "")[:500]
        raise RuntimeError(f"HTTP {exc.response.status_code}: {snippet}") from exc

    data: dict[str, Any] = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices in response: {data!r}"[:300])

    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))

    text = strip_think_blocks(content.strip())

    if not text:
        reasoning = (msg.get("reasoning") or msg.get("thinking") or "").strip()
        marker = re.search(r"(?i)(SCENE\s*:.*)", reasoning, re.DOTALL)
        text = marker.group(1).strip() if marker else reasoning

    if not text:
        raise RuntimeError("Model returned an empty description")

    return text, elapsed_ms


def percentile(data: list[float], pct: int) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100)
    return sorted_data[min(idx, len(sorted_data) - 1)]


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Benchmark the vision model via Ollama.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per image (default: 3)")
    parser.add_argument(
        "--ollama-url",
        default=settings.vision_api_url,
        help=f"Ollama completions URL (default: {settings.vision_api_url})",
    )
    parser.add_argument(
        "--model",
        default=settings.vision_model,
        help=f"Model name (default: {settings.vision_model})",
    )
    parser.add_argument(
        "--api-key",
        default=settings.vision_api_key,
        help="API key (empty for local Ollama)",
    )
    parser.add_argument(
        "--bucket",
        default=settings.minio_bucket_images,
        help=f"MinIO bucket, used only when --image-dir is not set (default: {settings.minio_bucket_images})",
    )
    parser.add_argument(
        "--image-dir",
        default=None,
        help=(
            "Read images from local disk instead of MinIO. "
            "Provide the directory that contains the 'panoramas/' folder, "
            "e.g. --image-dir ~/a6-stern/frontend/public/Images"
        ),
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Auto-discover all *.jpg images under <image-dir>/panoramas/ "
            "instead of using --eval-set. No reference descriptions, "
            "only structure, consistency, and latency are measured."
        ),
    )
    parser.add_argument(
        "--eval-set",
        default=str(BACKEND_ROOT / "scripts" / "eval_set.json"),
        help="Path to eval_set.json",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write full JSON results",
    )
    args = parser.parse_args()

    image_dir: Path | None = None
    if args.image_dir:
        image_dir = Path(args.image_dir).expanduser().resolve()
        if not image_dir.exists():
            print(f"ERROR: --image-dir does not exist: {image_dir}", file=sys.stderr)
            sys.exit(1)

    # Build eval list ---------------------------------------------------
    if args.discover:
        if image_dir is None:
            print("ERROR: --discover requires --image-dir", file=sys.stderr)
            sys.exit(1)
        panoramas_root = image_dir / "panoramas"
        if not panoramas_root.exists():
            print(f"ERROR: no 'panoramas/' folder found under {image_dir}", file=sys.stderr)
            sys.exit(1)
        discovered = sorted(panoramas_root.rglob("*.jpg"))
        if not discovered:
            print(f"ERROR: no .jpg files found under {panoramas_root}", file=sys.stderr)
            sys.exit(1)
        eval_items: list[dict] = [
            {"key": str(p.relative_to(image_dir)).replace("\\", "/"), "reference": None}
            for p in discovered
        ]
        print(f"Discovered {len(eval_items)} images under {panoramas_root}")
    else:
        eval_items = json.loads(Path(args.eval_set).read_text())

    image_source = str(image_dir) if image_dir else f"MinIO:{args.bucket}"

    print(f"\n{'='*65}")
    print(f"  VISION BENCHMARK")
    print(f"{'='*65}")
    print(f"  Model    : {args.model}")
    print(f"  Ollama   : {args.ollama_url}")
    print(f"  Runs/img : {args.runs}")
    print(f"  Images   : {len(eval_items)}")
    print(f"  Source   : {image_source}")
    print(f"{'='*65}\n")

    all_latencies: list[float] = []
    all_rouge_ref: list[float] = []
    all_consistency: list[float] = []
    total_responses = 0
    valid_structure = 0

    results: list[dict[str, Any]] = []

    col_w = 40
    header = (
        f"{'IMAGE':<{col_w}}  {'STRUCT':>6}  {'ROUGE-L↑':>8}  {'CONSIST↑':>8}  {'AVG_MS':>7}"
    )
    print(header)
    print("-" * len(header))

    with httpx.Client(timeout=180) as client:
        for item in eval_items:
            key: str = item["key"]
            reference: str = item["reference"]
            short_key = key if len(key) <= col_w else "…" + key[-(col_w - 1):]

            # Fetch image once, reuse across all runs
            try:
                data_url = fetch_image_as_data_url(key, args.bucket, image_dir)
            except Exception as exc:
                print(f"{short_key:<{col_w}}  SKIP (image fetch failed: {exc})")
                continue

            run_texts: list[str] = []
            run_latencies: list[float] = []
            run_valid: list[bool] = []

            for run_idx in range(args.runs):
                try:
                    text, ms = call_ollama(
                        client, args.ollama_url, args.model, data_url, args.api_key
                    )
                    run_texts.append(text)
                    run_latencies.append(ms)
                    run_valid.append(has_all_sections(text))
                    total_responses += 1
                    if run_valid[-1]:
                        valid_structure += 1
                except Exception as exc:
                    print(
                        f"  [{key}] run {run_idx + 1}/{args.runs} FAILED: {exc}",
                        file=sys.stderr,
                    )

            if not run_texts:
                print(f"{short_key:<{col_w}}  ALL RUNS FAILED")
                continue

            reference: str | None = item.get("reference")
            if reference:
                rouge_refs = [rouge_l(reference, t) for t in run_texts]
                avg_rouge_ref: float | None = statistics.mean(rouge_refs)
                all_rouge_ref.extend(rouge_refs)
            else:
                rouge_refs = []
                avg_rouge_ref = None

            consistency_scores: list[float] = []
            for a, b in combinations(run_texts, 2):
                consistency_scores.append(rouge_l(a, b))
            avg_consistency = statistics.mean(consistency_scores) if consistency_scores else float("nan")
            all_consistency.extend(consistency_scores)

            avg_ms = statistics.mean(run_latencies)
            all_latencies.extend(run_latencies)

            struct_label = f"{sum(run_valid)}/{len(run_valid)}"
            struct_ok = sum(run_valid) == len(run_valid)
            rouge_col = f"{avg_rouge_ref:>8.3f}" if avg_rouge_ref is not None else "     N/A"

            print(
                f"{short_key:<{col_w}}  {struct_label:>6}{'✓' if struct_ok else '✗'}  "
                f"{rouge_col}  {avg_consistency:>8.3f}  {avg_ms:>7.0f}"
            )

            results.append({
                "key": key,
                "runs": [
                    {
                        "text": run_texts[i],
                        "latency_ms": run_latencies[i],
                        "structure_valid": run_valid[i],
                        "rouge_l_ref": rouge_refs[i] if rouge_refs else None,
                    }
                    for i in range(len(run_texts))
                ],
                "avg_rouge_l_ref": avg_rouge_ref,
                "avg_consistency": avg_consistency,
                "avg_latency_ms": avg_ms,
            })

    # ── summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    if total_responses:
        print(f"  Structure valid   : {valid_structure}/{total_responses} ({100*valid_structure/total_responses:.1f}%)")
    if all_rouge_ref:
        print(f"  ROUGE-L vs ref    : mean={statistics.mean(all_rouge_ref):.3f}  "
              f"median={statistics.median(all_rouge_ref):.3f}  "
              f"stdev={statistics.stdev(all_rouge_ref) if len(all_rouge_ref) > 1 else 0:.3f}")
    if all_consistency:
        print(f"  Self-consistency  : mean={statistics.mean(all_consistency):.3f}  "
              f"median={statistics.median(all_consistency):.3f}  "
              f"stdev={statistics.stdev(all_consistency) if len(all_consistency) > 1 else 0:.3f}")
    if all_latencies:
        print(f"  Latency (ms)      : p50={percentile(all_latencies, 50):.0f}  "
              f"p95={percentile(all_latencies, 95):.0f}  "
              f"min={min(all_latencies):.0f}  max={max(all_latencies):.0f}")
    print("=" * 65)
    print()

    if args.output and results:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "config": {
                        "model": args.model,
                        "ollama_url": args.ollama_url,
                        "runs_per_image": args.runs,
                        "images": len(eval_items),
                    },
                    "summary": {
                        "structure_valid_pct": (
                            100 * valid_structure / total_responses if total_responses else None
                        ),
                        "rouge_l_ref_mean": statistics.mean(all_rouge_ref) if all_rouge_ref else None,
                        "consistency_mean": statistics.mean(all_consistency) if all_consistency else None,
                        "latency_p50_ms": percentile(all_latencies, 50) if all_latencies else None,
                        "latency_p95_ms": percentile(all_latencies, 95) if all_latencies else None,
                    },
                    "per_image": results,
                },
                indent=2,
            )
        )
        print(f"Full results written to: {out_path}")


if __name__ == "__main__":
    main()
