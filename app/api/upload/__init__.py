"""Upload endpoints.

This package used to be a single 980-line `upload.py`. It was split by
upload kind:

* `single.py`               — `POST /single` for images / videos / PDFs.
* `pointcloud_chunked.py`   — `POST /pointcloud/{init,chunk,complete}` for
                              multi-GB LAZ/LAS uploads streamed via the
                              backend proxy.
* `pointcloud_direct.py`    — `POST /pointcloud/direct-{init,complete}` for
                              browser → MinIO uploads via a presigned PUT.
* `precheck.py`             — `POST /precheck-hash` for client-side dedupe.

* `common.py`               — shared constants, dedupe, manifest helpers,
                              display-name generation, permission checks,
                              activity logging.
* `cleanup.py`              — startup cleanup of abandoned chunked upload
                              directories.

`main.py` only consumes two names from this package:

* `router`               — the composed APIRouter.
* `cleanup_stale_uploads` — run once on backend startup.

Both are re-exported here so existing imports continue to work unchanged.
"""

from fastapi import APIRouter

from . import (
    pointcloud_chunked,
    pointcloud_direct,
    precheck,
    single,
    robot,
)
from .cleanup import cleanup_stale_uploads

router = APIRouter()
router.include_router(precheck.router)
router.include_router(single.router)
router.include_router(pointcloud_chunked.router)
router.include_router(pointcloud_direct.router)
router.include_router(robot.router)

__all__ = ["router", "cleanup_stale_uploads"]
