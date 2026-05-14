"""Lightweight additive schema migrations for existing deployments.

Why this exists
---------------
SQLAlchemy's Base.metadata.create_all() only creates *missing* tables, it
never ALTERs existing ones. So when a new column or constraint is added to a
model after the table already exists in production, create_all silently skips
it. These functions fill that gap by inspecting the live schema and applying
only the missing change.

All functions are idempotent: safe to run on every startup regardless of how
many times the server has been restarted or what state the schema is in.

Execution order
---------------
Functions are called in main.py's lifespan() hook in this order, after
create_all and before seeding:

  1. ensure_comparison_drafts_state_json
  2. ensure_file_assets_sha256_hash
  3. ensure_file_assets_ai_description
  4. ensure_users_is_admin
  5. ensure_users_role_dropped          ← must run after (4)
  6. ensure_projects_fields
  7. ensure_project_members_table
  8. ensure_project_floorplan_url
  9. ensure_rooms_fields
  10. ensure_rooms_slug_scoped_to_project

How to add a new migration
--------------------------
1. Write a new ``ensure_<table>_<change>(engine)`` function here following the
   existing pattern: inspect, return early if already done, ALTER inside a
   transaction, log at INFO level.
2. Import and call it in app/main.py's lifespan() after the existing calls.
3. Do NOT rely on ordering relative to create_all for tables that may not exist
   yet — guard with ``if not inspector.has_table(...): return``.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def ensure_comparison_drafts_state_json(engine: Engine) -> None:
    """Add state_json to comparison_drafts if the table exists but the column does not."""
    inspector = inspect(engine)
    if not inspector.has_table("comparison_drafts"):
        return
    cols = {c["name"] for c in inspector.get_columns("comparison_drafts")}
    if "state_json" in cols:
        return
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE comparison_drafts ADD COLUMN state_json JSON"))
        elif dialect == "sqlite":
            conn.execute(text("ALTER TABLE comparison_drafts ADD COLUMN state_json JSON"))
        else:
            conn.execute(text("ALTER TABLE comparison_drafts ADD COLUMN state_json JSON"))
    logger.info("Added comparison_drafts.state_json column")


def ensure_file_assets_sha256_hash(engine: Engine) -> None:
    """Add sha256_hash to file_assets if the table exists but the column does not."""
    inspector = inspect(engine)
    if not inspector.has_table("file_assets"):
        return
    cols = {c["name"] for c in inspector.get_columns("file_assets")}
    if "sha256_hash" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE file_assets ADD COLUMN sha256_hash VARCHAR(64)"))
    logger.info("Added file_assets.sha256_hash column")


def ensure_file_assets_ai_description(engine: Engine) -> None:
    """Add ai_description and ai_description_status to file_assets if missing."""
    inspector = inspect(engine)
    if not inspector.has_table("file_assets"):
        return
    cols = {c["name"] for c in inspector.get_columns("file_assets")}
    with engine.begin() as conn:
        if "ai_description" not in cols:
            conn.execute(text("ALTER TABLE file_assets ADD COLUMN ai_description TEXT"))
            logger.info("Added file_assets.ai_description column")
        if "ai_description_status" not in cols:
            conn.execute(text("ALTER TABLE file_assets ADD COLUMN ai_description_status VARCHAR(20)"))
            logger.info("Added file_assets.ai_description_status column")


def ensure_users_is_admin(engine: Engine) -> None:
    """Replace users.role with users.is_admin boolean if not already done."""
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    if "is_admin" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"))
            # Promote anyone whose role was 'admin'
            if "role" in cols:
                conn.execute(text("UPDATE users SET is_admin = TRUE WHERE role = 'admin'"))
        logger.info("Added users.is_admin column")


def ensure_projects_fields(engine: Engine) -> None:
    """Add owner_id, description, location, status, updated_at to projects if missing."""
    inspector = inspect(engine)
    if not inspector.has_table("projects"):
        return
    cols = {c["name"] for c in inspector.get_columns("projects")}
    with engine.begin() as conn:
        if "owner_id" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN owner_id VARCHAR(36)"))
            logger.info("Added projects.owner_id column")
        if "description" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN description TEXT"))
            logger.info("Added projects.description column")
        if "location" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN location VARCHAR(255)"))
            logger.info("Added projects.location column")
        if "status" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'"))
            logger.info("Added projects.status column")
        if "updated_at" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN updated_at TIMESTAMP"))
            conn.execute(text("UPDATE projects SET updated_at = created_at WHERE updated_at IS NULL"))
            logger.info("Added projects.updated_at column")


def ensure_project_floorplan_url(engine: Engine) -> None:
    """Add floorplan_url to projects if missing."""
    inspector = inspect(engine)
    if not inspector.has_table("projects"):
        return
    cols = {c["name"] for c in inspector.get_columns("projects")}
    if "floorplan_url" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE projects ADD COLUMN floorplan_url VARCHAR(500)"))
    logger.info("Added projects.floorplan_url column")


def ensure_rooms_fields(engine: Engine) -> None:
    """Add floor_plan_coordinates and sort_order to rooms if missing."""
    inspector = inspect(engine)
    if not inspector.has_table("rooms"):
        return
    cols = {c["name"] for c in inspector.get_columns("rooms")}
    with engine.begin() as conn:
        if "floor_plan_coordinates" not in cols:
            conn.execute(text("ALTER TABLE rooms ADD COLUMN floor_plan_coordinates JSON"))
            logger.info("Added rooms.floor_plan_coordinates column")
        if "sort_order" not in cols:
            conn.execute(text("ALTER TABLE rooms ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"))
            logger.info("Added rooms.sort_order column")


def ensure_rooms_slug_scoped_to_project(engine: Engine) -> None:
    """Replace the global UNIQUE(slug) constraint on rooms with UNIQUE(project_id, slug)."""
    inspector = inspect(engine)
    if not inspector.has_table("rooms"):
        return

    unique_constraints = inspector.get_unique_constraints("rooms")
    has_composite = any(
        set(c["column_names"]) == {"project_id", "slug"}
        for c in unique_constraints
    )
    if has_composite:
        return

    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE rooms DROP CONSTRAINT IF EXISTS rooms_slug_key"))
            conn.execute(text("DROP INDEX IF EXISTS ix_rooms_slug"))
            conn.execute(text(
                "ALTER TABLE rooms ADD CONSTRAINT uq_rooms_project_slug UNIQUE (project_id, slug)"
            ))
        elif dialect == "sqlite":
            # SQLite cannot drop constraints — recreate the table with the correct definition.
            conn.execute(text("""
                CREATE TABLE rooms_new (
                    id VARCHAR(36) PRIMARY KEY,
                    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    slug VARCHAR(100) NOT NULL,
                    floor_plan_coordinates JSON,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (project_id, slug)
                )
            """))
            conn.execute(text("INSERT INTO rooms_new SELECT * FROM rooms"))
            conn.execute(text("DROP TABLE rooms"))
            conn.execute(text("ALTER TABLE rooms_new RENAME TO rooms"))
        else:
            conn.execute(text("ALTER TABLE rooms DROP CONSTRAINT IF EXISTS rooms_slug_key"))
            conn.execute(text(
                "ALTER TABLE rooms ADD CONSTRAINT uq_rooms_project_slug UNIQUE (project_id, slug)"
            ))
    logger.info("Updated rooms slug uniqueness constraint to be scoped per project")


def ensure_users_role_dropped(engine: Engine) -> None:
    """Drop the legacy users.role column now that is_admin boolean is in use."""
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    if "role" not in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN role"))
    logger.info("Dropped legacy users.role column")


def ensure_users_email_fields(engine: Engine) -> None:
    """Add email verification and password reset columns to users if missing."""
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "email_verified" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT FALSE"))
            logger.info("Added users.email_verified column")
        if "email_verification_token" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verification_token VARCHAR(128)"))
            logger.info("Added users.email_verification_token column")
        if "email_verification_token_expires_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verification_token_expires_at TIMESTAMP"))
            logger.info("Added users.email_verification_token_expires_at column")
        if "password_reset_token" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN password_reset_token VARCHAR(128)"))
            logger.info("Added users.password_reset_token column")
        if "password_reset_token_expires_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN password_reset_token_expires_at TIMESTAMP"))
            logger.info("Added users.password_reset_token_expires_at column")


def ensure_reports_label(engine: Engine) -> None:
    """Add label column to reports if missing."""
    inspector = inspect(engine)
    if not inspector.has_table("reports"):
        return
    cols = {c["name"] for c in inspector.get_columns("reports")}
    if "label" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE reports ADD COLUMN label VARCHAR(255)"))
    logger.info("Added reports.label column")


def ensure_project_members_table(engine: Engine) -> None:
    """Create project_members table if it does not exist."""
    inspector = inspect(engine)
    if inspector.has_table("project_members"):
        return
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("""
                CREATE TABLE project_members (
                    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL DEFAULT 'viewer',
                    joined_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (project_id, user_id)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE project_members (
                    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL DEFAULT 'viewer',
                    joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, user_id)
                )
            """))
    logger.info("Created project_members table")
