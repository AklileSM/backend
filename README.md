# A6-Stern Backend

FastAPI backend for the A6-Stern construction documentation platform. Handles authentication, file storage (via MinIO), point cloud conversion (via PotreeConverter), AI image analysis, and report generation.

Works alongside the `frontend-next` and `deployment` repos.

## Prerequisites

- Python 3.11
- PostgreSQL 15 (local install or Docker)
- MinIO instance accessible from your machine
- PotreeConverter 2.1.2 only needed if you will upload LAZ/LAS point clouds locally. In Docker it is installed automatically. For local dev on Linux, download the pre-built binary from [GitHub](https://github.com/potree/PotreeConverter/releases/tag/2.1.2) and place it on your `PATH` as `PotreeConverter`.

## Local development

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# Edit .env — set DB_*, MINIO_*, and at minimum MINIO_ACCESS_KEY / MINIO_SECRET_KEY
python run.py
```

The server starts at `http://localhost:3001` with hot-reload enabled when `DEBUG=true`.

## API documentation

FastAPI generates interactive API docs automatically. Once the server is running:


| UI                              | URL                                      |
| ------------------------------- | ---------------------------------------- |
| Swagger UI (try endpoints live) | `http://localhost:3001/api/docs`         |
| ReDoc (readable reference)      | `http://localhost:3001/api/redoc`        |
| OpenAPI JSON                    | `http://localhost:3001/api/openapi.json` |


## Authentication

### First user is admin

There are no hardcoded credentials. **The first account registered via `POST /api/auth/register` is automatically granted admin rights.** All subsequent registrations create regular users.

On first startup the backend seeds three default projects (`A6 Stern`, `Project X`, `Project Y`) and six default rooms under `A6 Stern`. These only create if the slugs don't already exist.

### JWT tokens

Tokens are issued on login and registration. They expire after **7 days** (10,080 minutes). Include them in requests as:

```
Authorization: Bearer <token>
```

### Role system

There are two layers of access control:

**Global admin** (`is_admin` on the User model)

- Can manage all users and projects
- Can upload files to any project regardless of membership
- Can access the `/api/admin/`* routes

**Project membership roles** (stored in `project_members.role`)

- `owner` can manage project settings and members, can upload files
- `editor` can upload files, create annotations and reports
- `viewer` read-only access to project content

Admins have implicit access to all projects regardless of membership.

## Startup sequence

On every start the backend runs these steps in order before accepting requests:

1. Create all database tables (idempotent)
2. Run schema migrations (add columns, rename constraints safe to run on existing data)
3. Seed default projects and rooms (skips if slugs already exist)
4. Ensure all six MinIO buckets exist (creates them if missing)
5. Clean up any stale chunked uploads from interrupted sessions
6. Reset point cloud conversion jobs that were interrupted mid-conversion
7. Start the PotreeConverter worker pool (2 concurrent conversion workers)

## API routes


| Prefix             | Description                                       |
| ------------------ | ------------------------------------------------- |
| `/api/auth`        | Register, login, get current user                 |
| `/api/projects`    | Project CRUD, member management                   |
| `/api/rooms`       | Room CRUD                                         |
| `/api/files`       | File explorer by date and by room                 |
| `/api/upload`      | Single file upload and chunked point cloud upload |
| `/api/ai`          | AI image analysis (with result caching)           |
| `/api/reports`     | Report and draft CRUD, PDF generation             |
| `/api/annotations` | Annotation CRUD per file                          |
| `/api/admin`       | User and project administration (admin only)      |
| `/api/health`      | Health check (storage connectivity included)      |


## File uploads

Admins, project owners, and project editors can upload files. Viewers cannot.

**Supported types:** images (JPEG, PNG, etc.), video, PDF, LAZ/LAS point clouds

**Size limit:** configured via `MAX_UPLOAD_SIZE_BYTES` (default 5 GB)

**Duplicate detection:** every file is SHA-256 hashed on upload. If an identical file already exists anywhere in the system the upload is rejected with HTTP 409 and a message identifying where the duplicate lives. This is a global check — the same file cannot be uploaded twice even to a different room or project.

**Display names:** files are renamed at upload time to `<room-slug>-<YYYYMMDD>-<NNN>.<ext>` (e.g. `room1-20260401-003.jpg`). The sequence number `NNN` is per room+date+media_type, so images, videos, and PDFs each have independent sequences. The original filename is preserved in `original_name`.

**Point cloud uploads** use a two-path strategy see the architecture doc for the full flow. Conversion runs asynchronously in a background process pool; the file entry is created immediately with `conversion_status: "pending"`.

**Image thumbnails** are generated at upload time (400×300 px, quality 82) and stored in a separate MinIO bucket. AI description generation is also triggered automatically in a background task for every image upload.

## Storage (MinIO)

Six buckets are used. Bucket names are configurable via env vars (see below); these are the defaults:


| Bucket       | Default name               | Contents                                  |
| ------------ | -------------------------- | ----------------------------------------- |
| Images       | `construction-images`      | Uploaded images                           |
| Thumbnails   | `construction-thumbnails`  | Auto-generated image thumbnails           |
| Point clouds | `construction-pointclouds` | LAZ originals and Potree-converted output |
| PDFs         | `construction-pdfs`        | Uploaded PDF documents                    |
| Reports      | `construction-reports`     | Generated report PDFs                     |
| Floorplans   | `construction-floorplans`  | Project floor plan images                 |


Presigned URLs (for browser access) expire after **7 days**.

## AI vision

`POST /api/ai/analyze` sends an image to a vision model and returns a text description. The result is cached in the database so repeated analysis of the same file does not re-call the API.

The feature works with:

- **Local Ollama** set `VISION_API_URL` to your Ollama endpoint, `VISION_MODEL` to the model name. No API key needed.
- **Hyperbolic cloud API** set `VISION_API_KEY` to your Hyperbolic key. The endpoint and model can be left at their defaults or overridden.

If no model is reachable the endpoint returns an error; all other features are unaffected.

## Environment variables

Copy `.env.example` to `.env` for local development.

### Server


| Variable | Default   | Description                           |
| -------- | --------- | ------------------------------------- |
| `DEBUG`  | `true`    | Enable hot-reload and verbose logging |
| `HOST`   | `0.0.0.0` | Bind address                          |
| `PORT`   | `3001`    | Listen port                           |


### Database

Either set `DATABASE_URL` directly, or set the individual `DB_`* variables (the URL takes precedence):


| Variable       | Default     | Description                                                                                 |
| -------------- | ----------- | ------------------------------------------------------------------------------------------- |
| `DATABASE_URL` | *(empty)*   | Full SQLAlchemy URL, e.g. `postgresql+psycopg2://user:pass@host/db`. Overrides `DB_`* vars. |
| `DB_HOST`      | `localhost` |                                                                                             |
| `DB_PORT`      | `5432`      |                                                                                             |
| `DB_NAME`      | `a6_stern`  |                                                                                             |
| `DB_USER`      | `postgres`  |                                                                                             |
| `DB_PASSWORD`  | *(empty)*   |                                                                                             |


### MinIO


| Variable                       | Default                    | Description                                                           |
| ------------------------------ | -------------------------- | --------------------------------------------------------------------- |
| `MINIO_ENDPOINT`               | `127.0.0.1`                | MinIO host (no port, no scheme)                                       |
| `MINIO_API_PORT`               | `9000`                     | MinIO S3 API port                                                     |
| `MINIO_ACCESS_KEY`             | *(empty)*                  |                                                                       |
| `MINIO_SECRET_KEY`             | *(empty)*                  |                                                                       |
| `MINIO_USE_SSL`                | `false`                    |                                                                       |
| `MINIO_PUBLIC_UPLOAD_BASE_URL` | *(empty)*                  | Public base URL for presigned URLs if MinIO is behind a reverse proxy |
| `MINIO_BUCKET_IMAGES`          | `construction-images`      | Override individual bucket names if needed                            |
| `MINIO_BUCKET_THUMBNAILS`      | `construction-thumbnails`  |                                                                       |
| `MINIO_BUCKET_POINTCLOUDS`     | `construction-pointclouds` |                                                                       |
| `MINIO_BUCKET_PDFS`            | `construction-pdfs`        |                                                                       |
| `MINIO_BUCKET_REPORTS`         | `construction-reports`     |                                                                       |
| `MINIO_BUCKET_FLOORPLANS`      | `construction-floorplans`  |                                                                       |


### Auth & security


| Variable             | Default                 | Description                                                         |
| -------------------- | ----------------------- | ------------------------------------------------------------------- |
| `JWT_SECRET`         | *(empty)*               | **Required in production.** Long random string used to sign tokens. |
| `FRONTEND_URL`       | `http://localhost:5173` | Primary allowed CORS origin                                         |
| `CORS_EXTRA_ORIGINS` | *(empty)*               | Comma-separated extra origins to allow                              |


### AI vision


| Variable         | Default                                           | Description                                     |
| ---------------- | ------------------------------------------------- | ----------------------------------------------- |
| `VISION_API_KEY` | *(empty)*                                         | API key for Hyperbolic or other hosted provider |
| `VISION_API_URL` | `http://192.168.50.103:11434/v1/chat/completions` | Vision API endpoint (OpenAI-compatible)         |
| `VISION_MODEL`   | `qwen3-vl:8b`                                     | Model name passed to the API                    |


### Uploads & storage


| Variable                                      | Default             | Description                                                                                                                                                         |
| --------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MAX_UPLOAD_SIZE_BYTES`                       | `5368709120` (5 GB) | Maximum single-file upload size                                                                                                                                     |
| `DELETE_ORIGINAL_POINTCLOUD_AFTER_CONVERSION` | `true`              | Delete LAZ/LAS after Potree conversion. Set to `false` to keep originals.                                                                                           |
| `PRESIGNED_URL_EXPIRY_SECONDS`                | `604800` (7 days)   | How long presigned MinIO URLs remain valid                                                                                                                          |
| `POTREE_CONVERTER_PATH`                       | *(auto-detected)*   | Absolute path to the `PotreeConverter` binary. Override if the binary is not on `PATH`. Resolution order: this env var → `PATH` → `/usr/local/bin/PotreeConverter`. |


## Legacy asset migration

`scripts/migrate_legacy_assets.py` is a **one-time** script for importing files from the old SPA's `public/` directory into MinIO and PostgreSQL. Only needed when first migrating from the legacy frontend.

**Warning: the script deletes all existing `FileAsset` rows before importing.** Do not run it on a database that already has production uploads.

### Expected source directory layout

```
<frontend-public-dir>/
├── Images/
│   ├── thumbnails/
│   │   └── <YYYYMMDD>/       ← date folder
│   │       └── <filename>    ← must contain "room1"…"room6" in the name
│   └── panoramas/
│       └── <YYYYMMDD>/
│           └── <filename>    ← matched 1:1 with the thumbnail by filename
└── PCD/
    └── <YYYYMMDD>/
        └── <filename>        ← must contain "room1"…"room6" in the name
```

Room slugs are inferred from filenames via regex — any filename containing `room1` through `room6` (case-insensitive, optional zero-padding) maps to the corresponding room slug.

### Running the script

The script must be run from inside the backend environment with access to a configured `.env`:

```bash
# With explicit path:
python scripts/migrate_legacy_assets.py --frontend-public-dir /path/to/frontend/public

# Via env var (set in .env or shell):
LEGACY_FRONTEND_PUBLIC_DIR=/path/to/frontend/public python scripts/migrate_legacy_assets.py

# If backend and frontend repos are cloned side-by-side, the script finds
# ../frontend/public automatically — no argument needed.
python scripts/migrate_legacy_assets.py
```

In Docker, the `docker-compose.yml` mounts the legacy frontend public dir at `/legacy-frontend-public` (controlled by `LEGACY_FRONTEND_PUBLIC_DIR` in `.env`). Run the script inside the backend container:

```bash
docker exec -it a6_stern_api python scripts/migrate_legacy_assets.py \
  --frontend-public-dir /legacy-frontend-public
```

### What it does

1. Creates all DB tables and MinIO buckets (idempotent)
2. Seeds default projects and rooms
3. **Deletes all existing `FileAsset` rows**
4. Uploads each panorama image to `construction-images`, its paired thumbnail to `construction-thumbnails`
5. Uploads each point cloud file to `construction-pointclouds`
6. Creates a `FileAsset` record for each file with `metadata_json.source = "legacy-public"`

## Code structure

```
app/
├── main.py          # App factory, middleware, lifespan hooks, router registration
├── config.py        # All settings via Pydantic (reads .env automatically)
├── database.py      # SQLAlchemy engine and session factory
├── models.py        # ORM models (User, Project, Room, FileAsset, Report, ...)
├── schemas.py       # Pydantic request/response schemas
├── api/             # One module per route group
│   ├── deps.py      # Shared dependencies (get_current_user, require_admin, ...)
│   └── ...
├── core/
│   └── security.py  # Password hashing (bcrypt), JWT encode/decode
└── services/
    ├── storage.py   # MinIO client wrapper (upload, download, presigned URLs, thumbnails)
    ├── ai.py        # Vision API calls
    ├── pointcloud.py # PotreeConverter integration and worker pool
    ├── db_migrations.py # Startup schema migrations
    └── bootstrap.py # Default project/room seeding
scripts/
└── migrate_legacy_assets.py  # One-time migration from old SPA public folder to MinIO
```

