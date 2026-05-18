# Reports: Backend Reference

This document covers the report system: data models, the draft/publish lifecycle, API endpoints, and the `viewer_kind` enum.

## Overview

Reports are PDF documents generated client-side in the browser and uploaded as finished blobs. The backend stores the PDF in MinIO and records metadata in PostgreSQL. There are two report types with different workflows.

**Reports are scoped to their creator.** A user can only list, view, and delete their own reports and drafts.

## Report types

### Viewer report

A field observation from a single file opened in any viewer (static image, panorama, or point cloud).

Workflow: `ViewerReportDraft` → publish → `Report` (draft is deleted on publish)

### Comparison report

A side-by-side comparison of two images from the Compare viewer. Multiple `ComparisonDraft` entries can be consolidated into a single published `Report`.

Workflow: `ComparisonDraft` (one or more) → publish → `Report` (all selected drafts are deleted on publish)

## Draft / publish lifecycle

```
create draft          update draft (optional, repeatable)        publish
     │                        │                                      │
     ▼                        ▼                                      ▼
ViewerReportDraft  ──────► state_json updated  ──────►  Report created
                                                         Draft deleted
                                                         PDF stored in MinIO
```

- **Drafts persist viewer state** (`state_json`) between sessions. A user can reopen a viewer, restore context from the draft, and continue editing before publishing.
- **Updating a draft** (`PATCH`) clears any previously stored draft PDF from MinIO (the draft PDF is a preview; the final PDF is generated fresh at publish time).
- **Publishing** generates the PDF client-side, uploads it to the `construction-reports` bucket, creates a `Report` record, and deletes the draft.
- A draft that was never published can be deleted at any time, its associated PDF preview (if any) is cleaned up from MinIO.

## viewer_kind values

The `viewer_kind` field on `ViewerReportDraft` identifies which viewer the draft originated from. These are the values the frontend sends:


| `viewer_kind`      | Viewer                                   |
| ------------------ | ---------------------------------------- |
| `static_360`       | Static image viewer (default for images) |
| `static_room`      | Static image, room-explorer origin       |
| `interactive_360`  | Panorama (360°) viewer                   |
| `interactive_room` | Panorama, room-explorer origin           |
| `static_pcd`       | Point cloud viewer                       |


The label shown in the UI is derived from `viewer_kind` plus `state_json.displayFileName`.

## flags

Both report types support a `flags` list (array of strings). The frontend writes these values:


| Flag string        | Meaning                      |
| ------------------ | ---------------------------- |
| `safety_concern`   | Visual safety issue observed |
| `quality_concern`  | Construction quality concern |
| `schedule_delayed` | Evidence of schedule delay   |


Flags are stored as a JSON column. The `_coerce_str_list` helper in `api/reports.py` normalises any legacy formats (the column previously stored strings or dicts in some older records).

## state_json

`state_json` is a free-form JSON object that the frontend writes and reads back. The backend treats it as opaque. Typical contents by viewer kind:

**Static viewer (`static_360`):**

```json
{
  "scale": 1.3,
  "annotationsCount": 2,
  "reportIncludeVisual": true,
  "reportIncludeComments": true,
  "reportSafetyConcern": false,
  "reportQualityConcern": false,
  "reportScheduleDelayed": false
}
```

**Panorama viewer (`interactive_360`):**

```json
{
  "mode": "panorama",
  "reportIncludeVisual": true,
  "reportIncludeComments": true,
  "reportSafetyConcern": false,
  "reportQualityConcern": false,
  "reportScheduleDelayed": false
}
```

**Point cloud viewer (`static_pcd`):**

```json
{
  "mode": "point-cloud",
  "reportIncludeVisual": false,
  "reportIncludeComments": true,
  "reportSafetyConcern": false,
  "reportQualityConcern": false,
  "reportScheduleDelayed": false
}
```

**Comparison draft (`ComparisonDraft`):**

```json
{
  "left": { "displayFileName": "room1-20260401-001.jpg", ... },
  "right": { "displayFileName": "room1-20260501-001.jpg", ... }
}
```

## PDF serving

Report PDFs are served via the backend at `/api/reports/{id}/pdf` rather than as presigned MinIO URLs. This keeps PDF access gated behind JWT auth and avoids exposing internal bucket paths. The endpoint supports HTTP Range requests (for in-browser PDF viewers that load pages progressively).

## API summary


| Method   | Endpoint                                  | Description                             |
| -------- | ----------------------------------------- | --------------------------------------- |
| `GET`    | `/api/reports`                            | List current user's published reports   |
| `POST`   | `/api/reports`                            | Create report (metadata only, no PDF)   |
| `POST`   | `/api/reports/with-pdf`                   | Create report with PDF upload           |
| `GET`    | `/api/reports/{id}/pdf`                   | Stream report PDF (range-request aware) |
| `DELETE` | `/api/reports/{id}`                       | Delete report and its PDF from MinIO    |
| `GET`    | `/api/reports/viewer-drafts`              | List current user's viewer drafts       |
| `POST`   | `/api/reports/viewer-drafts`              | Create viewer draft                     |
| `GET`    | `/api/reports/viewer-drafts/{id}`         | Get draft with full `state_json`        |
| `PATCH`  | `/api/reports/viewer-drafts/{id}`         | Update draft state                      |
| `POST`   | `/api/reports/viewer-drafts/{id}/publish` | Publish draft → Report                  |
| `DELETE` | `/api/reports/viewer-drafts/{id}`         | Delete draft                            |
| `GET`    | `/api/reports/comparison-drafts`          | List current user's comparison drafts   |
| `POST`   | `/api/reports/comparison-drafts`          | Create comparison draft                 |
| `GET`    | `/api/reports/comparison-drafts/{id}`     | Get comparison draft                    |
| `PATCH`  | `/api/reports/comparison-drafts/{id}`     | Update comparison draft                 |
| `POST`   | `/api/reports/comparison-drafts/publish`  | Publish one or more drafts → Report     |
| `DELETE` | `/api/reports/comparison-drafts/{id}`     | Delete comparison draft                 |


