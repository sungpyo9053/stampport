"""Task listing and demo trigger."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import TaskRow
from ..orchestrator import DEMO_WORKFLOW, WORKFLOW_NAME, run_demo_workflow
from ..schemas import RunDemoResponse, Task

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[Task])
def list_tasks(db: Session = Depends(get_db)) -> list[Task]:
    rows = db.query(TaskRow).order_by(TaskRow.id.desc()).all()
    return [Task.model_validate(r) for r in rows]


def _run_demo_in_background() -> None:
    """Background entrypoint — opens its own session so it survives the request."""
    db = SessionLocal()
    try:
        run_demo_workflow(db)
    finally:
        db.close()


@router.post("/run-demo", response_model=RunDemoResponse)
def run_demo(background_tasks: BackgroundTasks) -> RunDemoResponse:
    background_tasks.add_task(_run_demo_in_background)
    return RunDemoResponse(
        started=True,
        workflow=WORKFLOW_NAME,
        task_count=len(DEMO_WORKFLOW),
        message=(
            "Demo workflow scheduled. "
            "Watch GET /events to see the agents work in order."
        ),
    )
