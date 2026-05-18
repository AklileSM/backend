"""File deletion and bulk operations.

* `DELETE /{file_id}`   — single-file delete. Permission-gated by
                          `_can_delete_file`: admin or owner/editor on the
                          file's project. 204 on success.
* `POST /bulk-delete`   — delete many files. Per-asset permission; failures
                          counted as `skipped` rather than 403-ing the
                          batch.
* `POST /bulk-download` — stream a ZIP of the original objects for the
                          requested files. Same per-asset permission gate as
                          delete (viewers cannot bulk-exfiltrate).
"""

from __future__ import annotations

import os
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.database import get_db
from app.models import FileAsset, User
from app.schemas import BulkActionResponse, BulkFileIdsRequest
from app.services.storage import storage_service

from .common import _can_delete_file

router = APIRouter()


def _drop_asset_storage(asset: FileAsset) -> None:
    """Remove an asset's blobs from MinIO. Best-effort; ignores 404s.

    Extracted so the bulk endpoint and the single-file endpoint share the
    same teardown semantics.
    """
    if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
        storage_service.remove_object_best_effort(
            asset.thumbnail_bucket_name, asset.thumbnail_object_name
        )
    if asset.media_type == "pointcloud":
        storage_service.remove_pointcloud_asset_best_effort(
            asset.bucket_name, asset.object_name, asset.metadata_json
        )
    else:
        storage_service.remove_object_best_effort(asset.bucket_name, asset.object_name)


def _iter_file(path: str, chunk_size: int = 1024 * 1024):
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@router.delete("/{file_id}", status_code=204, response_model=None)
def delete_file_asset(
    file_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    if not _can_delete_file(current_user, asset, db):
        raise HTTPException(status_code=403, detail="Not allowed to delete this file")

    if asset.thumbnail_bucket_name and asset.thumbnail_object_name:
        storage_service.remove_object_best_effort(asset.thumbnail_bucket_name, asset.thumbnail_object_name)
    if asset.media_type == "pointcloud":
        storage_service.remove_pointcloud_asset_best_effort(
            asset.bucket_name,
            asset.object_name,
            asset.metadata_json,
        )
    else:
        storage_service.remove_object_best_effort(asset.bucket_name, asset.object_name)

    db.delete(asset)
    db.commit()


@router.post("/bulk-delete", response_model=BulkActionResponse)
def bulk_delete_files(
    payload: BulkFileIdsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BulkActionResponse:
    """Delete many file assets in one call.

    Per-asset permission is checked the same way as the single-file delete;
    failures (missing rows, no permission) are silently counted as `skipped`
    so a partially-allowed batch still gets cleaned up rather than 403'ing
    the whole thing.
    """
    if not payload.ids:
        return BulkActionResponse(affected=0, skipped=0)
    # Dedupe + cap to avoid pathologically large requests.
    unique_ids = list(dict.fromkeys(payload.ids))[:500]

    assets = db.scalars(select(FileAsset).where(FileAsset.id.in_(unique_ids))).all()
    found_ids = {a.id for a in assets}
    affected = 0
    for asset in assets:
        if not _can_delete_file(current_user, asset, db):
            continue
        _drop_asset_storage(asset)
        db.delete(asset)
        affected += 1
    if affected:
        db.commit()
    skipped = len(unique_ids) - affected
    # Rows the user no longer has access to + rows that weren't found are
    # both counted as skipped; the frontend just shows the totals.
    skipped = max(skipped, len(unique_ids) - len(found_ids))
    return BulkActionResponse(affected=affected, skipped=skipped)


@router.post("/bulk-download")
def bulk_download_files(
    payload: BulkFileIdsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream a ZIP of the original objects for the requested files.

    Skips:
      - PCDs whose original LAZ was deleted after conversion (no usable
        single-file representation left in storage)
      - rows the caller can't view in the first place
      - rows whose object is missing in MinIO

    The ZIP is assembled on disk in a temp file, then streamed back. For
    typical "30-file accidental upload" batches this is well under 1 GB.
    """
    if not payload.ids:
        raise HTTPException(status_code=400, detail="No file ids supplied")
    unique_ids = list(dict.fromkeys(payload.ids))[:500]

    assets = db.scalars(
        select(FileAsset)
        .where(FileAsset.id.in_(unique_ids))
        .options(selectinload(FileAsset.room))
    ).all()

    # Build the ZIP in a temp file. Closing the file is the caller's job —
    # FastAPI's BackgroundTask runs the cleanup after the response is sent.
    tmp = tempfile.NamedTemporaryFile(prefix="bulk-", suffix=".zip", delete=False)
    tmp.close()
    skipped = 0
    written = 0
    seen_names: dict[str, int] = {}

    try:
        with zipfile.ZipFile(tmp.name, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for asset in assets:
                if not _can_delete_file(current_user, asset, db):
                    # Same gate as delete — viewers shouldn't be able to bulk
                    # exfiltrate either. Single-file download endpoint stays
                    # open for them.
                    skipped += 1
                    continue
                meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
                if asset.media_type == "pointcloud" and meta.get("original_removed_after_conversion"):
                    # Potree output is a directory of files, not the LAZ —
                    # not meaningful to include in a "download originals" zip.
                    skipped += 1
                    continue
                # Avoid name collisions when two assets have the same display
                # name (e.g. moved files retaining their old room prefix).
                base = asset.display_name or asset.id
                name = base
                if name in seen_names:
                    seen_names[name] += 1
                    stem, _, ext = base.partition(".")
                    name = f"{stem}-{seen_names[base]}{'.' + ext if ext else ''}"
                else:
                    seen_names[name] = 0

                try:
                    stream = storage_service.stream_object(asset.bucket_name, asset.object_name)
                except Exception:
                    skipped += 1
                    continue
                try:
                    with zf.open(name, mode="w") as out:
                        for chunk in stream.stream(amt=1024 * 1024):
                            out.write(chunk)
                    written += 1
                except Exception:
                    skipped += 1
                finally:
                    stream.close()
                    stream.release_conn()

        if written == 0:
            os.unlink(tmp.name)
            raise HTTPException(
                status_code=404,
                detail="None of the selected files could be downloaded",
            )

        # Stream the assembled zip back; clean up the temp file once the
        # client has finished reading.
        from starlette.background import BackgroundTask
        zip_size = os.path.getsize(tmp.name)
        filename = f"files-{written}.zip"
        return StreamingResponse(
            _iter_file(tmp.name),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(zip_size),
                "X-Bulk-Affected": str(written),
                "X-Bulk-Skipped": str(skipped),
            },
            background=BackgroundTask(_unlink_quietly, tmp.name),
        )
    except HTTPException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
