# Projects, Rooms, and Members

Covers the project hierarchy: projects → rooms → files, project membership, floorplan images, and the activity feed. Permissions are summarized here; for the full matrix see `PERMISSIONS.md`.

## Hierarchy

```
Project (id, slug, name, status, owner_id, location, description, floorplan_url)
  ├── ProjectMember (user_id, role: owner | editor | viewer)
  ├── ProjectActivity (audit log, see below)
  └── Room (id, slug, name, sort_order, floor_plan_coordinates)
        └── FileAsset (image | video | pointcloud | pdf)
```

A user accesses a project through `ProjectMember`. Global admins (`User.is_admin=True`) bypass membership entirely and have implicit access to every project.

## Project lifecycle

### Create

```
POST /api/projects/ { name, slug, description?, location? }
```

The caller is automatically inserted as a `ProjectMember` with role `owner`. Slug must be unique; collision → 400. The project's `status` defaults to `"active"`.

### Listing

```
GET /api/projects/
```

- Admins see every project.
- Other users see projects they are a member of.

Sorted alphabetically by name.

### Lookup by slug

```
GET /api/projects/by-slug/{slug}
```

Used by the project page in the frontend (`/app/projects/[slug]`). Membership-gated.

### Update

```
PATCH /api/projects/{id} { name?, description?, location?, status? }
```

Owner only (admins bypass). `status` ∈ `{active, on_hold, completed, archived}`; anything else → 400.

### Delete

```
DELETE /api/projects/{id}
```

Owner only (admins bypass). Cascades to rooms, file assets, members, and project activity rows via SQLAlchemy relationships.

There is also an admin-only `DELETE /api/admin/projects/{id}` that does the same thing without the owner check.

## Floorplan

A project has at most one floorplan image, stored in the `construction-floorplans` MinIO bucket under `<project_id>/floorplan.<ext>`. Accepted content types: JPEG, PNG, WebP.

### `GET /api/projects/{id}/floorplan`

**Public**: no auth required, by design. The browser loads it as a normal `<img>` src. Sends `ETag` and `Cache-Control: public, max-age=86400`; honors `If-None-Match` with 304.

### `POST /api/projects/{id}/floorplan` (multipart `file`)

Owner or editor (admins bypass). Replaces any existing floorplan; the old object is deleted best-effort.

### `DELETE /api/projects/{id}/floorplan`

Owner or editor (admins bypass). Removes the MinIO object and clears `floorplan_url`.

## Rooms

Rooms are nested under projects. Each room has a per-project unique slug. The `floor_plan_coordinates` JSON column stores the bounding rect of the room on the project floorplan (used to draw clickable hotspots on the homepage):

```json
{ "x": 0.12, "y": 0.34, "w": 0.18, "h": 0.20 }
```

Coordinates are normalized to the floorplan image dimensions.

### `GET /api/projects/{id}/rooms`

Membership-gated. Returns rooms sorted by `sort_order` then name.

### `POST /api/projects/{id}/rooms { name, slug, sort_order }`

Owner or editor. Duplicate slug within the same project → 400.

### `PATCH /api/projects/{id}/rooms/{room_id}`

Owner or editor. Any of `name`, `slug`, `floor_plan_coordinates`, `sort_order`.

### `DELETE /api/projects/{id}/rooms/{room_id}`

Owner or editor. Cascades to file assets.

> There is also a public `/api/rooms/` and `/api/rooms/{slug}` (see `app/api/rooms.py`) used by the explorer to enumerate rooms across all projects without auth. Modifications all go through the project-scoped routes above.

## Members

`ProjectMember` is the join row between users and projects, with a role field.

```python
class ProjectMember:
    project_id: str   # FK → projects.id
    user_id: str      # FK → users.id
    role: str         # "owner" | "editor" | "viewer"
    joined_at: datetime
```

### `GET /api/projects/{id}/members`

Membership-gated. Lists current members with username and email.

### `POST /api/projects/{id}/members { user_id, role }`

Owner only. Target user must exist (404 otherwise) and not already be a member (400 otherwise). Logs `action=member.add` to the activity feed.

### `PATCH /api/projects/{id}/members/{user_id} { role }`

Owner only. Changes the target member's role.

### `DELETE /api/projects/{id}/members/{user_id}`

- Owners can remove anyone.
- Any other member can remove **themselves** (used by the "leave project" button), note that `user_id` in the path must equal the caller's id.
- Logs `action=member.remove`.

> Removing the last owner of a project is **not** prevented in code today. If you want that guard, add a count check in `remove_member`.

## Activity feed

`ProjectActivity` is an audit log per project. It records who did what, when, and lightweight metadata.

```python
class ProjectActivity:
    id: str
    project_id: str
    user_id: str | None       # NULL if the actor was deleted
    username: str             # snapshotted so it survives user deletion
    action: str               # "upload.image" | "annotation.create" | "member.add" | ...
    target_type: str | None   # "file_asset" | "annotation" | "project_member"
    target_id: str | None
    metadata_json: JSON       # action-specific payload
    created_at: datetime
```

### `GET /api/projects/by-slug/{slug}/activity?limit=50`

Owner or editor only (viewers cannot see who else is active). `limit` is capped at 200.

Returns newest first. Used by the project home page's activity sidebar.

### Currently-logged actions

| `action` | Logged by | `metadata` contents |
|---|---|---|
| `upload.image` / `upload.video` / `upload.pointcloud` / `upload.pdf` | every upload path | `file_name`, `room_name`, `room_slug`, `capture_date` |
| `annotation.create` | `POST /api/annotations/` | `file_id`, `file_name`, `room_name`, `flag`, `preview` (first 120 chars) |
| `member.add` | `POST /api/projects/{id}/members` | `added_username`, `role` |
| `member.remove` | `DELETE /api/projects/{id}/members/{uid}` | `removed_username` |

To log more actions, call `app.services.activity.log_activity(...)` from the relevant endpoint. The shape is uniform, `action`, `target_type`, `target_id`, `metadata` (dict).

## Default seed data

On first boot, `app/services/bootstrap.py::seed_defaults` creates:

- 3 projects: `A6 Stern`, `Project X`, `Project Y`.
- 6 rooms under `A6 Stern`: `room1`–`room6`.

Idempotent, only inserts if the slugs don't already exist.

## Where the code lives

| Concern | File |
|---|---|
| Routes | `app/api/projects.py`, `app/api/rooms.py` |
| ORM models | `app/models.py` (`Project`, `Room`, `ProjectMember`, `ProjectActivity`) |
| Membership helpers | `app/api/projects.py:83` `_get_member_or_403` |
| Activity helper | `app/services/activity.py` `log_activity` |
| Seeded projects/rooms | `app/services/bootstrap.py` |
| Schema migrations | `services/db_migrations.py::ensure_projects_fields`, `::ensure_project_members_table`, `::ensure_project_activity_table`, `::ensure_rooms_fields`, `::ensure_rooms_slug_scoped_to_project` |
