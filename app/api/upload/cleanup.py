"""Startup cleanup for abandoned chunked-upload sessions.

Runs once during the FastAPI lifespan hook.  Drops staging directories under
``a6_pointcloud_uploads/`` whose mtime is older than four hours, so the temp
directory does not grow unbounded when uploads are interrupted.
"""

from __future__ import annotations

import logging
import shutil
import time

from .common import _POINTCLOUD_UPLOAD_DIR

logger = logging.getLogger(__name__)

_STALE_UPLOAD_AGE_SECONDS = 4 * 3600  # 4 hours


def cleanup_stale_uploads() -> None:
    """Remove abandoned chunked-upload temp directories older than 4 hours.

    Called once at server startup. Safe to call multiple times, errors on
    individual directories are logged and skipped.
    """
    if not _POINTCLOUD_UPLOAD_DIR.exists():
        return
    now = time.time()
    for d in _POINTCLOUD_UPLOAD_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            age = now - d.stat().st_mtime
            if age > _STALE_UPLOAD_AGE_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                logger.info("Removed stale upload directory %s (age %.0f s)", d, age)
        except OSError as exc:
            logger.warning("Could not check/remove stale upload dir %s: %s", d, exc)
