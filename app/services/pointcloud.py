import logging
import os
import subprocess
import tempfile
from pathlib import Path

from app.database import SessionLocal
from app.models import FileAsset
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

# PotreeConverter binary — installed in the Docker image at build time.
# Override with the POTREE_CONVERTER_PATH env var if needed.
_POTREE_CONVERTER = os.environ.get("POTREE_CONVERTER_PATH", "/opt/potree/PotreeConverter")

# Timeout in seconds for the conversion subprocess (10 minutes).
_CONVERSION_TIMEOUT = 600


def convert_pointcloud_background(asset_id: str, laz_tmp_path: str) -> None:
    """
    Background task: convert a LAZ file to Potree octree format and upload
    the three output files (metadata.json, hierarchy.bin, octree.bin) to MinIO.
    Updates FileAsset.metadata_json with the conversion status when done.
    """
    db = SessionLocal()
    try:
        _set_status(db, asset_id, "processing")

        with tempfile.TemporaryDirectory() as output_dir:
            cmd = [_POTREE_CONVERTER, laz_tmp_path, "-o", output_dir]
            logger.info("Starting PotreeConverter for asset %s: %s", asset_id, " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_CONVERSION_TIMEOUT,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"PotreeConverter exited {result.returncode}: {result.stderr[:500]}"
                )

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
