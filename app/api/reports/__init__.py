"""Reports endpoints.

This package used to be a single 974-line `reports.py`. It was split by
domain:

* `published.py`          — CRUD for published `Report`s, including the
                            `with-pdf` shortcut and the byte-range PDF
                            serving endpoint.
* `viewer_drafts.py`      — CRUD + `/publish` for `ViewerReportDraft`s
                            (one file → one report).
* `comparison_drafts.py`  — CRUD + `/publish` for `ComparisonDraft`s
                            (N drafts → one consolidated report).
* `common.py`             — shared serializers, helpers, JSON parsers,
                            project resolution, and activity logging.

Only `router` is consumed by `main.py`; it is the composed APIRouter
covering every endpoint that used to live in the old file.
"""

from fastapi import APIRouter

from . import comparison_drafts, published, viewer_drafts

# Splice each sub-router's routes onto a single composed router. We can't use
# `include_router` here because some routes use an empty path "" (paired with
# "/" for `redirect_slashes=False`), and `include_router` rejects empty path
# + empty prefix in FastAPI 0.115+. Splicing the route lists keeps the
# original path strings intact; main.py applies the `/api/reports` prefix
# when it mounts this router on the app.
router = APIRouter()
for _sub in (published.router, comparison_drafts.router, viewer_drafts.router):
    router.routes.extend(_sub.routes)

__all__ = ["router"]
