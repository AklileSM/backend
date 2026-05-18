"""Pointcloud admin/operational endpoints.

* `POST /{id}/retry-conversion`   — re-queue a failed conversion. Admin only
                                     (`require_user_can_upload`). 409 if the
                                     asset isn't in `failed` state or the
                                     original LAZ was deleted after a
                                     previous success.
* `GET  /{id}/conversion-status`  — poll the conversion lifecycle status.
                                     Public.
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_user_can_upload
from app.database import get_db
from app.models import FileAsset, User
from app.services.pointcloud import submit_conversion
from app.services.storage import storage_service

router = APIRouter()


@router.post("/{file_id}/retry-conversion", status_code=202)
def retry_pointcloud_conversion(
    file_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_can_upload),
) -> dict[str, str]:
    """Re-queue a failed point cloud conversion. Admin only.

    Downloads the original LAZ from MinIO and resubmits it to the converter
    pool. Returns 409 if the asset is not in 'failed' state or if the original
    file was already deleted after a previous successful conversion.
    """
    asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    if asset.media_type != "pointcloud":
        raise HTTPException(status_code=400, detail="Only point cloud assets can be retried")

    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    status = meta.get("conversion_status")
    if status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry: conversion status is '{status}', not 'failed'",
        )
    if meta.get("original_removed_after_conversion"):
        raise HTTPException(
            status_code=409,
            detail="Cannot retry: the original LAZ was deleted after a previous successful conversion. Re-upload the file.",
        )

    extension = os.path.splitext(asset.object_name)[1] or ".laz"
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=extension)
    os.close(tmp_fd)

    try:
        storage_service.download_object_to_path(asset.bucket_name, asset.object_name, tmp_path)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=502, detail=f"Could not retrieve original file from storage: {exc}")

    new_meta = dict(meta)
    new_meta["conversion_status"] = "pending"
    new_meta.pop("conversion_error", None)
    asset.metadata_json = new_meta
    db.commit()

    try:
        submit_conversion(asset.id, tmp_path)
    except Exception as exc:
        new_meta["conversion_status"] = "failed"
        new_meta["conversion_error"] = f"Failed to queue retry: {exc}"
        asset.metadata_json = new_meta
        db.commit()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=503, detail=f"Could not queue conversion: {exc}")

    return {"status": "queued", "asset_id": asset.id}


@router.get("/{asset_id}/conversion-status")
def get_conversion_status(
    asset_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Poll the conversion status of a point cloud asset."""
    asset = db.scalar(select(FileAsset).where(FileAsset.id == asset_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    meta = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    return {
        "status": meta.get("conversion_status", "unknown"),
        "error": meta.get("conversion_error"),
    }
