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
