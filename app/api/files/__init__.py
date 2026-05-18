"""File endpoints.

This package used to be a single 883-line `files.py`. It was split by
concern:

* `explorer.py`   — by-date / by-room / dates-summary endpoints plus
                    `/my-uploads` and `/search`. The user-facing listing
                    surface.
* `serve.py`      — proxy-streaming of file bytes from MinIO: `/url`,
                    `/thumbnail`, `/content`, and the Potree
                    `/pointcloud/{path}` route. HTTP Range requests and
                    `If-None-Match` are honoured here.
* `mutations.py`  — `DELETE /{file_id}` plus `/bulk-delete` and
                    `/bulk-download`. Permission-gated by membership +
                    role.
* `pointcloud.py` — admin/operational pointcloud endpoints:
                    `/{id}/retry-conversion` (admin only) and
                    `/{id}/conversion-status` (public poll).

* `common.py`     — shared helpers: HTTP Range parsing, cache headers,
                    delete-permission check, response serialisers.

Only `router` is consumed by `main.py`; it is the composed APIRouter
covering every endpoint that used to live in the old file.
"""

from fastapi import APIRouter

from . import explorer, mutations, pointcloud, serve

# Splice route lists onto a single composed router. `include_router` cannot
# be used here because some original routes use `path=""` paired with `"/"`
# (for `redirect_slashes=False`), and FastAPI 0.115+ rejects empty path +
# empty prefix in that call. (No empty paths in `files` today, but keeping
# the splice pattern consistent with `app/api/reports/__init__.py` so any
# future `""` path doesn't break the package.)
router = APIRouter()
for _sub in (explorer.router, serve.router, mutations.router, pointcloud.router):
    router.routes.extend(_sub.routes)

__all__ = ["router"]
