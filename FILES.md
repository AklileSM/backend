# Files: Explorer, Search, Uploads, Pointclouds

Covers the file lifecycle and all endpoints under `/api/files` and `/api/upload`. The matching frontend docs are `frontend-next/EXPLORER.md` and the upload chapter in the main README.

## Domain model: `FileAsset`

```python
class FileAsset:
    id: str                          # UUID
    room_id: str                     # FK → rooms.id
    media_type: str                  # "image" | "video" | "pointcloud" | "pdf"
    capture_date: date               # the day the media was captured
    created_at: datetime             # when it was uploaded
    original_name: str               # filename as uploaded by the user
    display_name: str                # canonical "<room-slug>-<YYYYMMDD>-<NNN>.<ext>"
    bucket_name: str
    object_name: str
    thumbnail_bucket_name: str | None
    thumbnail_object_name: str | None
    content_type: str | None
    file_size: int | None
    sha256_hash: str | None          # null for legacy rows; required for new uploads
    ai_description: str | None       # cached vision-model output (images only)
    ai_description_status: str | None  # "generating" | "done" | "failed" | null
    metadata_json: JSON              # see below
```

Common `metadata_json` keys:

| Key | Set by | Used for |
|---|---|---|
| `uploaded_by_user_id` | every upload path | `/my-uploads` filter, report author display |
| `uploaded_by_username` | every upload path | display only |
| `conversion_status` | pointcloud paths | `"uploading"` → `"pending"` → `"processing"` → `"ready"` / `"failed"` |
| `conversion_error` | pointcloud worker | error text on failure |
| `potree_base_object` | pointcloud worker | object-name prefix where Potree output lives |
| `original_removed_after_conversion` | pointcloud worker | true if `DELETE_ORIGINAL_POINTCLOUD_AFTER_CONVERSION=true` |

## Display name format

Every upload is renamed to:

```
<room-slug>-<YYYYMMDD>-<NNN>.<ext>
```

`NNN` is per `(room_id, capture_date, media_type)`: images, videos, pdfs, and pointclouds in the same room/date each have an **independent** 1-based sequence. The original filename is preserved in `original_name`.

Built by `_generate_display_name` (`upload.py:127`).

## SHA-256 duplicate detection

Every uploaded file is hashed during the upload. If the same hash already exists **anywhere in the system** (any room, any project), the upload is rejected with **409**:

```
This file has already been uploaded to <room name> on <date> as "<display_name>".
```

This is a global check, not per-project, by design. Implemented in `_check_duplicate` (`upload.py:179`).

The `POST /api/upload/precheck-hash` endpoint lets the browser ask "would this hash be a duplicate?" before starting the actual upload, useful for very large pointclouds where you want to bail out before transferring gigabytes.

## Explorer endpoints (public)

These power the file grid. **No auth required**, the browser can fetch them directly. Combined with the proxy-served `/content` and `/thumbnail`, this is what makes the explorer feel snappy.

### `GET /api/files/explorer/dates`

Returns a date → counts summary used by the calendar to highlight days that have media.

```json
{
  "dates": {
    "2026-04-01": { "images": 12, "videos": 0, "pointclouds": 1, "pdfs": 0 },
    "2026-04-02": { "images":  7, "videos": 1, "pointclouds": 0, "pdfs": 2 }
  }
}
```

### `GET /api/files/explorer/date/{capture_date}`

Returns all assets captured on a specific date, **grouped by room name** (not slug). Used by the project file explorer page when a date is selected.

```json
{
  "date": "2026-04-01",
  "rooms": {
    "Room 1": { "images": [...], "videos": [], "pointclouds": [], "pdfs": [] },
    "Room 2": { "images": [...], "videos": [], "pointclouds": [], "pdfs": [] }
  }
}
```

> **Quirk:** rooms in this response are keyed by `Room.name` (display string), not `Room.slug`. The frontend handles both.

### `GET /api/files/explorer/room/{room_slug}`

The inverse, all assets in one room, grouped by date.

## Authenticated explorer endpoints

### `GET /api/files/my-uploads?project_slug=…&media_type=…`

Returns assets uploaded by the current user, newest first. Powers the Profile page's "My uploads" lists.

- `project_slug` filters to a single project; the caller must be a member (or global admin).
- `media_type` ∈ `{image, video, pointcloud, pdf}`; anything else → 400.

The uploader id is read from `metadata_json.uploaded_by_user_id` via `cast(JSONB)['key'].astext` to avoid the JSON-quoting pitfall a naive `cast(String)` would hit.

### `GET /api/files/search?q=…&project_slug=…`

Fuzzy, project-scoped search across `display_name`, `original_name`, and `Room.name`. Powers the header search box.

- Membership-gated (same as `my-uploads`).
- Empty `q` → empty list.
- `q` capped at 100 characters.
- Uses `pg_trgm` similarity (`%` operator) backed by the trigram GIN indexes ensured at startup by `ensure_search_trigram_indexes`.
- Also widens with `ILIKE %q%` for short prefixes that fall below the default 0.3 similarity threshold.
- If `q` parses as `YYYY-MM-DD`, matches `capture_date == q` exactly.
- Returns up to 20 rows, ordered by similarity descending.

## File serving

Files are served via the **backend proxy**, not presigned MinIO URLs. This keeps the browser on the same origin (no CORS), keeps internal MinIO endpoints hidden, and lets the backend control caching.

### `GET /api/files/{id}/thumbnail`

Returns the auto-generated 400×300 thumbnail. Sends `ETag` + `Last-Modified` + `Cache-Control: public, max-age=86400`; honors `If-None-Match` with 304.

### `GET /api/files/{id}/content`

Returns the full original file.

- Supports `Range:` requests for partial fetches (PDF viewers, video scrubbing). Returns 206 with `Content-Range`.
- Honors `If-None-Match` with 304, but only for full-file requests, not ranges.
- Files ≤ 5 MB are buffered into a single response with `Content-Length`. Larger files are streamed.
- For pointclouds with `conversion_status == "ready"`, returns 404 with "Use pointcloud routes", the caller should use `/{id}/pointcloud/metadata.json` (or `/url`) instead.

### `GET /api/files/{id}/url`

Returns the canonical serve URL for the file. Used by viewers that need a stable URL without fetching the bytes yet.

- For ready pointclouds: `/api/files/{id}/pointcloud/metadata.json`
- For everything else: `/api/files/{id}/content`

### `GET /api/files/{id}/pointcloud/{path:path}`

Proxy for the individual Potree output files. Only the three known filenames are accepted; anything else → 404.

| Path | Content-Type |
|---|---|
| `metadata.json` | `application/json` |
| `hierarchy.bin` | `application/octet-stream` |
| `octree.bin` | `application/octet-stream` |

**Range requests are required**: Potree 2.x issues `Range: bytes=X-Y` to read specific chunks of `hierarchy.bin` and `octree.bin`. If you serve more bytes than requested, Potree's node-count arithmetic corrupts.

## Delete

### `DELETE /api/files/{id}`

Permission gate (`_can_delete_file`):

- Global admin → ✅
- Project member with role `owner` or `editor` on the file's project → ✅
- Anyone else → 403

Cleans up MinIO before deleting the row:

- Thumbnail object (best-effort)
- Pointcloud Potree output + original LAZ (via `remove_pointcloud_asset_best_effort`)
- All other types: the single original object

### `POST /api/files/bulk-delete`

Same per-asset permission gate. Failures (missing rows, no permission, MinIO 404) count as `skipped`; the request never 403's the whole batch.

```json
// request
{ "ids": ["...", "...", "..."] }

// response
{ "affected": 2, "skipped": 1 }
```

Dedupes ids and caps the batch at 500.

### `POST /api/files/bulk-download`

Streams a ZIP of the original objects for the requested files. Same per-asset permission gate as delete (viewers cannot bulk-exfiltrate).

Skipped:
- Rows the caller cannot delete
- Pointcloud rows whose original LAZ was already removed after conversion
- Rows whose MinIO object is missing

Returns 404 only if **no** file in the batch could be included. Response headers:

- `Content-Disposition: attachment; filename="files-<N>.zip"`
- `X-Bulk-Affected: <N>`, written count
- `X-Bulk-Skipped: <N>`, skipped count

ZIP is assembled in a temp file with `ZIP_STORED` (no compression, the originals are already compressed) and unlinked when the response finishes.

## Pointcloud lifecycle

Pointclouds (LAZ/LAS) go through a 4-phase lifecycle:

```
1. upload   →   2. assemble   →   3. convert   →   4. ready
   (chunks)     (or direct)       (PotreeConverter)
```

`metadata_json.conversion_status` values:

| Status | Meaning |
|---|---|
| `uploading` | Chunks being received / assembling LAZ |
| `pending`   | Queued, waiting for a converter worker |
| `processing` | A worker is currently running PotreeConverter |
| `ready`     | Potree files written to MinIO, asset is browsable |
| `failed`    | Conversion failed (see `conversion_error`) |
| `unknown`   | Defensive fallback for legacy rows |

### Two upload paths

**Chunked (always available):**

```
POST /api/upload/pointcloud/init      → { upload_id, chunk_size: 33554432 }
POST /api/upload/pointcloud/chunk     → repeat for each chunk_index
POST /api/upload/pointcloud/complete  → asset created, conversion queued
```

The frontend slices the file into 32 MB chunks (the backend's `chunk_size`) and posts them in order to `/chunk`. Concurrent chunk uploads are allowed, chunks are written to disk as `00000000.part`, `00000001.part`, etc.

`/complete` verifies every part exists, then returns immediately with the asset record. Assembly + hashing + MinIO upload happens in a daemon thread to avoid request timeouts (chunked LAS uploads can be multi-minute).

**Direct (presigned MinIO PUT):**

```
POST /api/upload/pointcloud/direct-init     → { upload_id, upload_url (presigned PUT) }
PUT  <upload_url>                            → browser uploads straight to MinIO
POST /api/upload/pointcloud/direct-complete  → asset created, conversion queued
```

Requires `MINIO_PUBLIC_UPLOAD_BASE_URL` to be set to a URL the **browser** can reach. If unset, the endpoint returns 400 and the frontend falls back to the chunked path.

### Conversion

Performed by `app/services/pointcloud.py` using a `ProcessPoolExecutor` with `max_workers=2` (configurable in `main.py` lifespan). Worker steps:

1. Download (or use the local temp copy from upload completion).
2. Run `PotreeConverter <input> -o <out>`.
3. Upload the resulting `metadata.json`, `hierarchy.bin`, `octree.bin` to the pointcloud bucket under a `_potree/<asset_id>/` prefix.
4. Set `conversion_status=ready` and `potree_base_object=<prefix>`.
5. If `DELETE_ORIGINAL_POINTCLOUD_AFTER_CONVERSION=true` (default), remove the original LAZ; set `original_removed_after_conversion=true`.

### Retry

```
POST /api/upload/{id}/retry-conversion
```

Admin only (`require_user_can_upload`). Pre-conditions:

- `media_type == "pointcloud"` (else 400)
- `conversion_status == "failed"` (else 409 with the actual status)
- Original LAZ is still present in MinIO (else 409: "Re-upload the file")

Downloads the original to a temp file, resets `conversion_status=pending`, clears `conversion_error`, submits to the pool. On pool-rejection: marks `failed` again and returns 503.

### Polling

```
GET /api/files/{id}/conversion-status
→ { "status": "...", "error": null | "..." }
```

Used by the file grid to show a "converting…" indicator. The frontend polls every 2s; the in-progress UI elsewhere uses the same endpoint.

## Single-shot upload (images, videos, PDFs)

```
POST /api/upload/single
  fields: file, room_id, media_type, capture_date
```

- `media_type ∈ {image, video, pointcloud, pdf}`. `pdf` files must have a `.pdf` extension or a PDF content-type, otherwise 400.
- Images get a thumbnail generated at upload time (400×300 px, JPEG quality 82) and stored in the thumbnails bucket.
- Images also fire `generate_and_cache_ai_description` as a `BackgroundTask`, the AI description is computed asynchronously and stored on the asset row. The user can also trigger it later via `/api/ai/analyze`.
- Pointclouds delegate to the chunked path under the hood.
- SHA-256 duplicate check is enforced; 409 on duplicate.
- 5 GB default size limit (`MAX_UPLOAD_SIZE_BYTES`); 413 if exceeded.
- Activity log entry written: `action=upload.<media_type>`.

## HTTP error reference (file endpoints)

| Code | When |
|---|---|
| `400` | Invalid `media_type`, missing chunk, `total_chunks ≤ 0`, declared/uploaded size mismatch, non-PDF sent to PDF slot |
| `401` | Missing/invalid token on a gated route |
| `403` | Project membership doesn't permit upload/delete on the target file's project |
| `404` | Room, file, upload session, or storage object missing |
| `409` | SHA-256 duplicate, or retry requested in an invalid state |
| `413` | File exceeds `MAX_UPLOAD_SIZE_BYTES` |
| `416` | Range header beyond file end |
| `502` | Cannot retrieve original LAZ from storage on retry |
| `503` | Converter pool rejected submission |

## Where the code lives

| Concern | File |
|---|---|
| Explorer endpoints | `app/api/files.py` (top half) |
| Bulk delete/download | `app/api/files.py:439`, `:475` |
| File serving (proxy) | `app/api/files.py:617`, `:641`, `:802` |
| Range parsing | `app/api/files.py:64` `_parse_http_range` |
| Pointcloud retry | `app/api/files.py:720` |
| Upload paths | `app/api/upload.py` |
| Storage abstraction | `app/services/storage.py` |
| Pointcloud worker | `app/services/pointcloud.py` |
| Conversion-status reset on startup | `services/pointcloud.py::reset_interrupted_conversions` |
