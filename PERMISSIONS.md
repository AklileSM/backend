# Permissions: Endpoint × Role Matrix

This document explains exactly who can call what. Authorization happens at two layers:

1. **Global admin**: `User.is_admin` (re-read from the DB on every request, JWT claim ignored).
2. **Project membership**: `project_members.role` ∈ `{owner, editor, viewer}`. Global admins bypass membership checks.

> **Reports and drafts are scoped to the creator**: see `REPORTS.md`. Even admins cannot list, view, download, or delete another user's reports or drafts.

## Roles

| Role | Source | Granted to |
|---|---|---|
| `anonymous` | No `Authorization` header | The public internet |
| `user` | Valid JWT, `is_active=True` | Any registered account |
| `admin` | `User.is_admin=True` | First registered user; promoted by another admin |
| `project owner` | `project_members.role='owner'` | Creator of the project (auto-granted on create) |
| `project editor` | `project_members.role='editor'` | Granted by a project owner |
| `project viewer` | `project_members.role='viewer'` | Granted by a project owner |

The first registered user (when `user_count == 0`) is automatically `is_admin=True`. All others default to non-admin. See `backend/app/api/auth.py:43`.

## Globally enforced rules

These apply to every authenticated route via `get_current_user` (`backend/app/api/deps.py:13`):

- Missing/invalid/expired JWT → **401**.
- JWT decodes but `User` row missing → **401**.
- `User.is_active=False` → **403** ("Account disabled").

## Matrix by endpoint

Legend:
- ✅ allowed
- 🅰 admin only
- 🅾 project owner only
- 🅴 owner or editor
- 👤 the request's own user
- ❌ never allowed
- Anonymous request is rejected before reaching the route

### `/api/auth`, `auth.py`

| Endpoint | Anonymous | User | Admin | Notes |
|---|---|---|---|---|
| `POST /register` | ✅ | ✅ | ✅ | First user → admin |
| `POST /login` | ✅ | ✅ | ✅ | |
| `GET  /me` | ❌ | ✅ | ✅ | |
| `POST /resend-verification` | — | ✅ (own) | ✅ (own) | Requires email on account |
| `POST /verify-email` | ✅ | ✅ | ✅ | Token-gated; token from email |
| `POST /request-password-reset` | ✅ | ✅ | ✅ | Always 204 (no enumeration) |
| `GET  /validate-reset-token` | ✅ | ✅ | ✅ | Token-gated |
| `POST /reset-password` | ✅ | ✅ | ✅ | Token-gated |

See `AUTH_AND_EMAIL.md` for the full token lifecycle.

### `/api/admin`, `admin.py`

All routes use `Depends(require_admin)` (`deps.py:36`) except `user-search`.

| Endpoint | User | Admin | Notes |
|---|---|---|---|
| `GET    /users` | ❌ 403 | ✅ | |
| `GET    /user-search` | ✅ | ✅ | Used by project member picker; not admin-gated |
| `GET    /users/{id}` | ❌ 403 | ✅ | |
| `PATCH  /users/{id}` | ❌ 403 | ✅ | Cannot demote/deactivate **yourself** (`400`) |
| `GET    /projects` | ❌ 403 | ✅ | Lists every project |
| `DELETE /projects/{id}` | ❌ 403 | ✅ | Hard-deletes; cascades to rooms, files, memberships |

### `/api/projects`, `projects.py`

`_get_member_or_403` returns `None` for admins (admins bypass), or the `ProjectMember` row for everyone else (403 if no membership).

| Endpoint | Anonymous | Non-member user | Project viewer | Project editor | Project owner | Admin |
|---|---|---|---|---|---|---|
| `GET    /` | ❌ 401 | ✅ (own list) | ✅ | ✅ | ✅ | ✅ (all) |
| `POST   /` | ❌ | ✅ → becomes owner | — | — | — | ✅ |
| `GET    /by-slug/{slug}` | ❌ | ❌ 403 | ✅ | ✅ | ✅ | ✅ |
| `GET    /{id}` | ❌ | ❌ 403 | ✅ | ✅ | ✅ | ✅ |
| `PATCH  /{id}` | ❌ | ❌ 403 | ❌ 403 | ❌ 403 | ✅ | ✅ |
| `DELETE /{id}` | ❌ | ❌ 403 | ❌ 403 | ❌ 403 | ✅ | ✅ |
| `GET    /{id}/floorplan` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `POST   /{id}/floorplan` | ❌ | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `DELETE /{id}/floorplan` | ❌ | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `GET    /{id}/rooms` | ❌ | ❌ 403 | ✅ | ✅ | ✅ | ✅ |
| `POST   /{id}/rooms` | ❌ | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `PATCH  /{id}/rooms/{rid}` | ❌ | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `DELETE /{id}/rooms/{rid}` | ❌ | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `GET    /{id}/members` | ❌ | ❌ 403 | ✅ | ✅ | ✅ | ✅ |
| `POST   /{id}/members` | ❌ | ❌ 403 | ❌ 403 | ❌ 403 | ✅ | ✅ |
| `PATCH  /{id}/members/{uid}` | ❌ | ❌ 403 | ❌ 403 | ❌ 403 | ✅ | ✅ |
| `DELETE /{id}/members/{uid}` | ❌ | ❌ 403 | ✅ if `uid==self` | ✅ if `uid==self` | ✅ | ✅ |
| `GET    /by-slug/{slug}/activity` | ❌ | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |

> `GET /{id}/floorplan` returns the floorplan image without an auth check, there is no `Depends(get_current_user)` on the route. The floorplan endpoint is effectively public so the browser can fetch it as a normal `<img>` src.

### `/api/rooms`, `rooms.py`

These are **public** (no `get_current_user`). Used by the file explorer to enumerate rooms across all projects without an auth round-trip.

| Endpoint | Access |
|---|---|
| `GET /` | ✅ everyone |
| `GET /{room_slug}` | ✅ everyone |

### `/api/files`, `files.py`

Most file endpoints are public-by-design so thumbnails, image content, and PDF pages load via plain `<img>` and `<iframe>` tags. The narrower endpoints are gated:

| Endpoint | Anonymous | User | Admin | Notes |
|---|---|---|---|---|
| `GET    /my-uploads` | ❌ 401 | ✅ (own) | ✅ (own) | `project_slug` membership-gated |
| `GET    /search` | ❌ 401 | ✅ if member | ✅ | Project-scoped |
| `GET    /explorer/dates` | ✅ | ✅ | ✅ | |
| `GET    /explorer/date/{d}` | ✅ | ✅ | ✅ | |
| `GET    /explorer/room/{slug}` | ✅ | ✅ | ✅ | |
| `DELETE /{file_id}` | ❌ | ✅ if owner/editor on file's project | ✅ | See `_can_delete_file` |
| `POST   /bulk-delete` | ❌ 401 | ✅ per-asset | ✅ | Rows you can't delete are silently `skipped` |
| `POST   /bulk-download` | ❌ 401 | ✅ per-asset (same gate as delete) | ✅ | Viewers cannot bulk-download |
| `GET    /{id}/url` | ✅ | ✅ | ✅ | |
| `GET    /{id}/thumbnail` | ✅ | ✅ | ✅ | |
| `GET    /{id}/content` | ✅ | ✅ | ✅ | Range requests supported |
| `POST   /{id}/retry-conversion` | ❌ | ❌ 403 (admin only) | ✅ | Wraps `require_user_can_upload` |
| `GET    /{id}/conversion-status` | ✅ | ✅ | ✅ | |
| `GET    /{id}/pointcloud/{path}` | ✅ | ✅ | ✅ | Range requests required for Potree |

> Single-file download (`/content`) is **not gated**. If you need that locked down, gate it on auth + project membership. The bulk-download endpoint is gated because it is a higher-value exfiltration target.

### `/api/upload`, `upload.py`

All routes require auth. `_require_can_upload` enforces project owner/editor (or global admin); `require_user_can_upload` enforces global admin.

| Endpoint | Non-member | Project viewer | Project editor | Project owner | Admin |
|---|---|---|---|---|---|
| `POST /precheck-hash` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `POST /pointcloud/init` | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `POST /pointcloud/direct-init` | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |
| `POST /pointcloud/chunk` | ✅ (must hold a valid `upload_id`) | ✅ | ✅ | ✅ | ✅ |
| `POST /pointcloud/complete` | — | — | ✅ | ✅ | ✅ |
| `POST /pointcloud/direct-complete` | — | — | ✅ | ✅ | ✅ |
| `POST /single` | ❌ 403 | ❌ 403 | ✅ | ✅ | ✅ |

> `pointcloud/chunk` only verifies that the session directory exists, it does **not** re-check membership on each chunk. The session ID acts as the bearer token for chunk submission. If you need stricter chunk auth, add a membership re-check inside `upload_pointcloud_chunk`.

### `/api/ai`, `ai.py`

`POST /analyze` has **no auth dependency**, fully public at the API level. The frontend protects it via `ProtectedRoute`, but anyone who hits the route directly can analyze an image (and trigger an outbound vision-API call). See `AI.md` for the abuse-surface discussion.

### `/api/reports`, `reports.py`

All endpoints require auth; ownership is enforced by `created_by == current_user.id`. **Admins do not bypass this**, the privacy boundary is per-user.

| Endpoint | User (own) | User (other's) | Admin (other's) |
|---|---|---|---|
| `GET    /` | ✅ | — | ❌ 404 (filtered out) |
| `POST   /` | ✅ | — | — |
| `POST   /with-pdf` | ✅ | — | — |
| `GET    /{id}/pdf` | ✅ | ❌ 404 | ❌ 404 |
| `DELETE /{id}` | ✅ | ❌ 404 | ❌ 404 |
| `*  /viewer-drafts*` | ✅ | ❌ 404 | ❌ 404 |
| `*  /comparison-drafts*` | ✅ | ❌ 404 | ❌ 404 |

### `/api/annotations`, `annotations.py`

| Endpoint | Anonymous | User |
|---|---|---|
| `GET    /file/{file_id}` | ✅ | ✅ |
| `POST   /` | ❌ 401 | ✅ |
| `PATCH  /{id}` | ❌ 401 | ✅ |
| `DELETE /{id}` | ❌ 401 | ✅ |
| `POST   /{id}/attachment` | ❌ 401 | ✅ |
| `DELETE /{id}/attachment` | ❌ 401 | ✅ |
| `GET    /{id}/attachment` | ❌ 401 | ✅ |

> Annotations are **not** scoped by author. Any authenticated user can edit or delete any annotation on any file. If you need per-user ownership here, add `created_by` to the model and a check in `update_annotation`/`delete_annotation`.

### `/api/media`

Generic proxy under a `path:path` route, public, used for legacy asset paths.

### `/api/health`

Public.

## Where the rules live in code

- Global admin requirement: `app/api/deps.py:36` `require_admin`
- Upload privilege check: `app/api/upload.py:91` `_require_can_upload` (per-file project membership) and `app/api/deps.py:42` `require_user_can_upload` (global admin)
- File delete gate: `app/api/files.py:159` `_can_delete_file`
- Project membership check: `app/api/projects.py:83` `_get_member_or_403`
- Report ownership filter: see `app/api/reports.py`, all list/get queries include `.where(Report.created_by == current_user.id)`

## How to extend

When adding a new endpoint:

1. Pick the right base dependency: `get_current_user` (any user), `require_admin` (global admin), or no dep (public).
2. For project-scoped data, call `_get_member_or_403` early and check `.role` against the allowed set.
3. For per-user data (reports, drafts), filter `where(Model.created_by == current_user.id)` in every list/get/delete.
4. Update this matrix.
