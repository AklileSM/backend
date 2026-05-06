"""Lightweight additive migrations for existing deployments (create_all does not ALTER)."""

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
