"""Demo lifecycle helpers — reset everything between demo runs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from ..models import AgentRow, ArtifactRow, EventRow, TaskRow
from ..schemas import AgentStatus


def reset_demo_state(db: Session) -> None:
    """Wipe events / tasks / artifacts and put every agent back to idle.

    Order matters: agents.current_task_id references tasks.id with an
    `ON DELETE SET NULL`, but SQLite's foreign-key cascade is off by default.
    To be safe we null out the agent->task reference first, then drop the
    child tables, then the parent.
    """
    # 1. unhook agents from any task FK before we drop tasks
    db.execute(
        update(AgentRow).values(
            status=AgentStatus.IDLE.value,
            current_task_id=None,
            updated_at=datetime.utcnow(),
        )
    )
    # 2. delete in order: artifacts → events → tasks
    db.execute(delete(ArtifactRow))
    db.execute(delete(EventRow))
    db.execute(delete(TaskRow))
    db.commit()
