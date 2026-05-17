# Annotations

Annotations are point-of-interest markers placed on a file (typically an image) by a user. They support a category flag, an optional linked annotation (for cross-references), and an optional image attachment. The frontend renders them as numbered pins on the static image viewer.

The matching frontend doc is `frontend-next/ANNOTATIONS.md`.

## Data model — `Annotation`

```python
class Annotation:
    id: str                            # UUID
    file_id: str                       # FK → file_assets.id
    annotation_type: str               # free-form, currently "pin" from the UI
    data: JSON                         # opaque blob; UI stores {x, y, text}
    flag: str | None                   # one of {"safety", "quality", "delayed"} or NULL
    linked_annotation_id: str | None   # FK → annotations.id on the same file
    attachment_bucket_name: str | None # MinIO bucket of the attached image
    attachment_object_name: str | None # MinIO object key of the attached image
    created_at: datetime
```

`data` is opaque to the backend. The frontend writes:

```json
{
  "x": 0.4231,    // normalized to [0, 1] of the displayed image width
  "y": 0.7812,    // normalized to [0, 1] of the displayed image height
  "text": "Crack in plaster near doorframe"
}
```

The backend never reads `x`/`y` — they only matter to the rendering UI.

## Flag taxonomy

Same three-flag system used by reports (see `REPORTS.md`):

| `flag` value | UI meaning |
|---|---|
| `safety`   | Safety concern |
| `quality`  | Quality / workmanship concern |
| `delayed`  | Schedule delay indicated |
| `null`     | Uncategorized — renders as a neutral pin |

The validator (`_validate_flag` at `annotations.py:52`) trims, lower-cases, and rejects anything else with **400**.

> **Note:** The annotation flag values are `safety`/`quality`/`delayed`, but the report flag values are `safety_concern`/`quality_concern`/`schedule_delayed`. Same intent, different strings. The frontend maps between them in `lib/observationReportFlags.ts`.

## Linked annotations

`linked_annotation_id` is a soft "see also" link to another annotation **on the same file**. It is used in PDF rendering to inject lines like *"See also: annotation #3"*. Constraints (`_validate_link` at `annotations.py:64`):

- Linked annotation must exist → 404 otherwise.
- Linked annotation must reference the **same `file_id`** → 400 otherwise.
- An annotation cannot link to itself → 400 otherwise.

To clear an existing link on PATCH, send `clear_link: true`. Sending `linked_annotation_id: null` leaves the existing link untouched (it means "no change", not "set to null").

## Attachments

Each annotation may have **one** image attachment. Used to add a close-up photo of the issue being flagged. Stored in the `construction-annotation-attachments` MinIO bucket (configurable via `MINIO_BUCKET_ANNOTATION_ATTACHMENTS`).

- Accepted content types: `image/jpeg`, `image/png`, `image/webp`, `image/gif`.
- Rejected anything else with 400 ("Attachment must be one of …").
- Empty body → 400.
- Larger than `MAX_UPLOAD_SIZE_BYTES` → 413.
- Replacing an attachment deletes the old object first to avoid leaks.
- Deleting an annotation removes its attachment best-effort.

### Attachment URL

`AnnotationResponse.attachment_url` is **not** a presigned MinIO URL. It is a backend-proxied path:

```
/api/annotations/{annotation_id}/attachment
```

This avoids embedding expiring URLs in annotation responses and keeps the bucket name out of the API. The endpoint streams the object from MinIO straight to the browser.

## API

| Method   | Path | Auth | Description |
|----------|------|------|---|
| `GET`    | `/api/annotations/file/{file_id}` | public | List all annotations for a file, ordered by `created_at` ascending |
| `POST`   | `/api/annotations/` | user | Create annotation. Logs an `annotation.create` activity entry |
| `PATCH`  | `/api/annotations/{id}` | user | Update any of `annotation_type`, `data`, `flag`, `linked_annotation_id`, `clear_link` |
| `DELETE` | `/api/annotations/{id}` | user | Delete annotation and its attachment (best-effort) |
| `POST`   | `/api/annotations/{id}/attachment` | user | Upload (or replace) the attached image — multipart `file` field |
| `DELETE` | `/api/annotations/{id}/attachment` | user | Drop the attached image (keeps the annotation) |
| `GET`    | `/api/annotations/{id}/attachment` | user | Stream the attachment as its original content-type |

**Annotations are not ownership-scoped.** Any authenticated user can update or delete any annotation. If you need per-user authorship, see "Adding author tracking" below.

## Activity log

On create, an entry is added to `project_activity`:

```json
{
  "action": "annotation.create",
  "target_type": "annotation",
  "target_id": "<annotation_id>",
  "metadata": {
    "file_id": "...",
    "file_name": "room1-20260401-001.jpg",
    "room_name": "Room 1",
    "flag": "safety",
    "preview": "Crack in plaster near doorframe (first 120 chars)"
  }
}
```

Update and delete actions are not currently logged. If you need full audit history, mirror the same `log_activity` call in `update_annotation` and `delete_annotation`.

## PDF rendering contract

The frontend PDF builder (`lib/engineeringReportPdf.ts`) accepts annotations as:

```ts
type PdfAnnotation = {
  index: number;                 // 1-based, used in "annotation #N"
  text: string;                  // from annotation.data.text
  flag: 'safety' | 'quality' | 'delayed' | null;
  linkedIndex: number | null;    // index of linked annotation in this report
  attachmentDataUrl: string | null;  // base64 data URL or null
};
```

`ReportBuilder.tsx` pre-fetches each attachment via `/api/annotations/{id}/attachment` and converts it to a base64 data URL before calling the builder, because `jsPDF.addImage` is synchronous. Failures are silent — the PDF renders an italic "could not be embedded" note instead of crashing.

## Adding author tracking

The model does not currently store who created an annotation. To add this:

1. Add `created_by: str` (FK → users.id) to `models.Annotation`.
2. Add a migration in `services/db_migrations.py` (additive column, default NULL).
3. Set `created_by = current_user.id` in `create_annotation`.
4. Gate `update_annotation` / `delete_annotation` / `delete_annotation_attachment` on either admin or `annotation.created_by == current_user.id`.

## Where the code lives

| Concern | File |
|---|---|
| Routes | `app/api/annotations.py` |
| ORM model | `app/models.py` (search `class Annotation`) |
| Request/response schemas | `app/schemas.py` (search `Annotation*Request`, `AnnotationResponse`) |
| Schema migration | `services/db_migrations.py::ensure_annotations_extensions` |
| MinIO bucket name | `settings.minio_bucket_annotation_attachments` |
| Activity log helper | `app/services/activity.py` |
