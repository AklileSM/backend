import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.database import SessionLocal
from app.models import FileAsset
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

# Timeout in seconds for the conversion subprocess (10 minutes).
_CONVERSION_TIMEOUT = 600

# --- Converter process pool ---------------------------------------------------
# Initialized by init_converter_pool() at server startup (main.py lifespan).
# Each submitted task runs in a separate process, so multiple conversions run
# in parallel without blocking the web server's thread pool.
_converter_pool: Optional[ProcessPoolExecutor] = None


def init_converter_pool(max_workers: int = 2) -> None:
    """Create the process pool. Call once at server startup."""
    global _converter_pool
    _converter_pool = ProcessPoolExecutor(max_workers=max_workers)
    logger.info("Pointcloud converter pool started (max_workers=%d)", max_workers)


def reset_interrupted_conversions() -> None:
    """Mark any pending/processing conversions as failed.

    Called at startup: if the server restarted mid-conversion the process pool
    is gone, so those jobs will never finish. Resetting them lets users re-upload
    rather than waiting forever.
    """
    from app.database import SessionLocal
    from app.models import FileAsset
    from sqlalchemy import select

    db = SessionLocal()
    try:
        assets = db.scalars(
            select(FileAsset).where(FileAsset.media_type == "pointcloud")
        ).all()
        reset_count = 0
        for asset in assets:
            status = (asset.metadata_json or {}).get("conversion_status")
            if status in ("pending", "processing"):
                meta = dict(asset.metadata_json or {})
                meta["conversion_status"] = "failed"
                meta["conversion_error"] = "Conversion interrupted by server restart — please re-upload."
                asset.metadata_json = meta
                reset_count += 1
        if reset_count:
            db.commit()
            logger.warning("Reset %d interrupted pointcloud conversion(s) to 'failed'", reset_count)
    except Exception:
        logger.exception("Failed to reset interrupted conversions on startup")
    finally:
        db.close()


def shutdown_converter_pool() -> None:
    """Shut down the process pool gracefully. Call on server shutdown."""
    global _converter_pool
    if _converter_pool is not None:
        _converter_pool.shutdown(wait=False, cancel_futures=False)
        _converter_pool = None
        logger.info("Pointcloud converter pool shut down")


def submit_conversion(asset_id: str, laz_tmp_path: str) -> None:
    """Submit a conversion job to the process pool.

    Raises RuntimeError if the pool has not been initialised yet.
    The submitted function runs in a separate process so it does not
    block FastAPI's async event loop or thread pool.
    """
    if _converter_pool is None:
        raise RuntimeError("Converter pool is not initialised — call init_converter_pool() at startup")
    _converter_pool.submit(convert_pointcloud_background, asset_id, laz_tmp_path)
    logger.info("Conversion job submitted for asset %s", asset_id)


def _remove_original_pointcloud_object(db, asset: FileAsset) -> None:
    """Drop the source LAZ/LAS from object storage once Potree outputs are committed."""
    try:
        storage_service.remove_object_best_effort(asset.bucket_name, asset.object_name)
    except Exception as exc:
        logger.warning(
            "Could not delete original point cloud object %s/%s after conversion: %s",
            asset.bucket_name,
            asset.object_name,
            exc,
        )
        return
    meta = dict(asset.metadata_json or {})
    meta["original_removed_after_conversion"] = True
    asset.metadata_json = meta
    asset.file_size = None
    db.commit()
    logger.info(
        "Removed original point cloud object after conversion (asset %s)",
        asset.id,
    )


def _find_converter() -> str:
    """Locate the PotreeConverter binary.

    Resolution order:
    1. POTREE_CONVERTER_PATH environment variable
    2. Anywhere on PATH  (symlinked to /usr/local/bin in the Docker image)
    3. Hard-coded fallback inside /opt/potree
    """
    env = os.environ.get("POTREE_CONVERTER_PATH")
    if env:
        return env
    on_path = shutil.which("PotreeConverter")
    if on_path:
        return on_path
    return "/usr/local/bin/PotreeConverter"


def convert_pointcloud_background(asset_id: str, laz_tmp_path: str) -> None:
    """
    Background task: convert a LAZ file to Potree octree format and upload
    the three output files (metadata.json, hierarchy.bin, octree.bin) to MinIO.
    Updates FileAsset.metadata_json with the conversion status when done.
    """
    db = SessionLocal()
    try:
        _set_status(db, asset_id, "processing")

        converter = _find_converter()
        if not os.path.isfile(converter):
            raise RuntimeError(
                f"PotreeConverter binary not found at '{converter}'. "
                "Set POTREE_CONVERTER_PATH or rebuild the Docker image."
            )

        with tempfile.TemporaryDirectory() as output_dir:
            cmd = [converter, laz_tmp_path, "-o", output_dir]
            logger.info("Starting PotreeConverter for asset %s: %s", asset_id, " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_CONVERSION_TIMEOUT,
            )

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "no output")[:600]
                raise RuntimeError(
                    f"PotreeConverter exited {result.returncode}: {detail}"
                )

            # PotreeConverter can run for minutes. The session opened at the
            # start of this function may have timed out. Close it and get a
            # fresh connection so the final commit doesn't hit a dead socket.
            db.close()
            db = SessionLocal()

            asset = db.get(FileAsset, asset_id)
            if asset is None:
                raise RuntimeError(f"Asset {asset_id} disappeared during conversion")

            # Store Potree files alongside the original LAZ, in a _potree/ subfolder.
            base_object = asset.object_name.rsplit(".", 1)[0] + "_potree/"

            for filename in ("metadata.json", "hierarchy.bin", "octree.bin"):
                out_path = Path(output_dir) / filename
                if not out_path.exists():
                    raise RuntimeError(f"PotreeConverter did not produce {filename}")
                ct = "application/json" if filename.endswith(".json") else "application/octet-stream"
                storage_service.upload_file_path(
                    bucket_name=asset.bucket_name,
                    object_name=base_object + filename,
                    file_path=str(out_path),
                    content_type=ct,
                )

            meta = dict(asset.metadata_json or {})
            meta["conversion_status"] = "ready"
            meta["potree_base_object"] = base_object
            meta.pop("conversion_error", None)
            asset.metadata_json = meta
            db.commit()
            logger.info("Point cloud conversion complete for asset %s", asset_id)

            if get_settings().delete_original_pointcloud_after_conversion:
                _remove_original_pointcloud_object(db, asset)

    except Exception as exc:
        logger.exception("Point cloud conversion failed for asset %s", asset_id)
        _set_status(db, asset_id, "failed", error=str(exc))
    finally:
        db.close()
        try:
            os.unlink(laz_tmp_path)
        except OSError:
            pass


def _set_status(db, asset_id: str, status: str, error: str | None = None) -> None:
    asset = db.get(FileAsset, asset_id)
    if asset is None:
        return
    meta = dict(asset.metadata_json or {})
    meta["conversion_status"] = status
    if error:
        meta["conversion_error"] = error
    else:
        meta.pop("conversion_error", None)
    asset.metadata_json = meta
    db.commit()
