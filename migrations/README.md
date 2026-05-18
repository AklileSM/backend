# Migrations

**This directory is intentionally empty.**

A6-Stern does not use Alembic or a file-per-migration system. All schema changes are implemented as **additive, idempotent functions** in `app/services/db_migrations.py` and run on every backend startup from `app/main.py::lifespan`.

## Why

- The backend can be deployed by anyone with `docker compose up -d`, no `alembic upgrade head` step required.
- Forgotten migrations cannot block startup: every function is safe to re-run.
- Rollbacks are not needed because every column added is nullable / defaulted.

## Adding a schema change

Edit `app/services/db_migrations.py` and add a function. The pattern is:

```python
def ensure_my_new_column(engine: Engine) -> None:
    """
    Adds my_new_column to widgets. Safe to re-run.
    """
    with engine.begin() as conn:
        result = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'widgets' AND column_name = 'my_new_column'"
        )).fetchone()
        if result:
            return
        conn.execute(text("ALTER TABLE widgets ADD COLUMN my_new_column TEXT"))
```

Then wire it into `app/main.py::lifespan`:

```python
from app.services.db_migrations import ensure_my_new_column
...
ensure_my_new_column(engine)
```

Also update the corresponding SQLAlchemy model in `app/models.py` so the ORM knows the new column exists. Both must agree.

## Rules

1. **Additive only.** New nullable columns, new tables, new indexes. Don't `DROP COLUMN` or rename in place, old application code rolling back to a previous deploy needs the schema it expects.
2. **Idempotent.** Always guard with an `information_schema` check (or `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).
3. **Cheap.** Migrations run on every startup. Avoid expensive locks or rewrites, use a separate one-off script for those.
4. **No `DROP` migrations.** If you truly need to drop a column, do it manually via `psql` after the rolling deploy is complete, then remove the column from the model.

## Manual rollback

The migrations are additive, so application code rolling back to an older version is safe, old code ignores columns it doesn't know about. If you need to fully undo a migration:

```bash
docker exec -it a6_stern_db psql -U postgres a6_stern
# then
ALTER TABLE widgets DROP COLUMN my_new_column;
```

Identify the column to drop by reading the migration function in `services/db_migrations.py`.

## Current migration functions

As of writing, the following run at startup (see `app/main.py::lifespan`):

```
ensure_comparison_drafts_state_json
ensure_file_assets_sha256_hash
ensure_file_assets_ai_description
ensure_users_is_admin
ensure_users_role_dropped
ensure_projects_fields
ensure_project_members_table
ensure_project_floorplan_url
ensure_rooms_fields
ensure_rooms_slug_scoped_to_project
ensure_users_email_fields
ensure_reports_label
ensure_annotations_extensions
ensure_project_activity_table
ensure_search_trigram_indexes
```

Order is preserved by listing them in the lifespan handler. If a new migration depends on another, place it after its dependency.
