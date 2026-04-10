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
