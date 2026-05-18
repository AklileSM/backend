"""Project activity logger.

Single entry point `log_activity(...)` that records an audit row in the
`project_activity` table. Designed so a logging failure can never break
the request that triggered it, every call is wrapped in try/except and
swallowed at warning level. Activity logs are nice-to-have, not the
source of truth.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import ProjectActivity, User

logger = logging.getLogger(__name__)


def log_activity(
    db: Session,
    *,
    project_id: str | None,
    actor: User | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort write into project_activity.

    project_id can be None for events that don't have a project context
    (in which case we just skip, there's nowhere to file them). Same for
    actor when we have no authenticated user.

    The caller does NOT need to commit; we commit ourselves so the log is
    durable even if the caller's transaction later rolls back. That's a
    deliberate trade-off: we'd rather record "user X attempted Y" than
    drop the breadcrumb if Y partially failed downstream.
    """
    if project_id is None or actor is None:
        return
    try:
        row = ProjectActivity(
            project_id=project_id,
            user_id=actor.id,
            username=actor.username,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata or {},
        )
        db.add(row)
        db.commit()
    except Exception:
        # Log loudly enough that we'll notice in prod, but never raise —
        # an audit-write hiccup must not cascade into a 500 on the action
        # the user is actually doing.
        logger.exception("Failed to log project activity (action=%s)", action)
        try:
            db.rollback()
        except Exception:
            pass
