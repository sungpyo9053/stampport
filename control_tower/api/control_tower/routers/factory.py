"""Factory lifecycle endpoints — start/pause/resume/stop/reset/status."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..db import SessionLocal, get_db
from ..models import EventRow
from ..schemas import (
    ContinuousModeRequest,
    DesiredStatusRequest,
    Event,
    Factory,
    FactoryStatus,
)
from ..services import factory_service
from ..services.demo_service import reset_demo_state

router = APIRouter(prefix="/factory", tags=["factory"])


@router.get("/status", response_model=Factory)
def status(db: Session = Depends(get_db)) -> Factory:
    return Factory.model_validate(factory_service.get_state(db))


@router.post("/start", response_model=Factory, dependencies=[Depends(require_admin)])
def start(db: Session = Depends(get_db)) -> Factory:
    state = factory_service.get_state(db)
    if state.status == FactoryStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="이미 실행 중입니다.")

    # Start fresh: wipe demo state and bounce factory back to running.
    reset_demo_state(db)
    factory_service.start(db)

    # Run the workflow in a background thread so the HTTP request
    # returns immediately. The orchestrator already knows how to honor
    # factory checkpoints (see orchestrator.run_demo_workflow).
    def _runner() -> None:
        # Use a fresh session — the request-scoped one closes when the
        # endpoint returns.
        from ..orchestrator import run_demo_workflow
        local_db = SessionLocal()
        try:
            try:
                run_demo_workflow(local_db)
                # Only mark completed if we weren't asked to stop in flight.
                final = factory_service.get_state(local_db)
                if final.status == FactoryStatus.RUNNING.value:
                    factory_service.mark_completed(local_db)
                elif final.status == FactoryStatus.STOPPING.value:
                    factory_service.mark_stopped(local_db)
            except Exception as e:  # noqa: BLE001
                factory_service.mark_failed(local_db, f"실행 중 예외: {e}")
        finally:
            local_db.close()

    threading.Thread(target=_runner, daemon=True, name="factory-runner").start()
    return Factory.model_validate(factory_service.get_state(db))


@router.post("/pause", response_model=Factory, dependencies=[Depends(require_admin)])
def pause(db: Session = Depends(get_db)) -> Factory:
    return Factory.model_validate(factory_service.pause(db))


@router.post("/resume", response_model=Factory, dependencies=[Depends(require_admin)])
def resume(db: Session = Depends(get_db)) -> Factory:
    return Factory.model_validate(factory_service.resume(db))


@router.post("/stop", response_model=Factory, dependencies=[Depends(require_admin)])
def stop(db: Session = Depends(get_db)) -> Factory:
    return Factory.model_validate(factory_service.request_stop(db))


@router.post("/reset", response_model=Factory, dependencies=[Depends(require_admin)])
def reset(db: Session = Depends(get_db)) -> Factory:
    reset_demo_state(db)
    return Factory.model_validate(factory_service.reset(db))


@router.post(
    "/desired",
    response_model=Factory,
    dependencies=[Depends(require_admin)],
)
def set_desired(
    req: DesiredStatusRequest, db: Session = Depends(get_db)
) -> Factory:
    """Override desired_status. The watchdog will reconcile within a few
    seconds. Useful for: 'I want it running, even if it crashed' or
    'pause without waiting for the next stage'."""
    try:
        row = factory_service.set_desired_status(db, req.desired_status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Factory.model_validate(row)


@router.post(
    "/continuous",
    response_model=Factory,
    dependencies=[Depends(require_admin)],
)
def set_continuous(
    req: ContinuousModeRequest, db: Session = Depends(get_db)
) -> Factory:
    """Toggle 계속 실행 모드. When on, the watchdog auto-restarts the
    workflow after each terminal state."""
    return Factory.model_validate(
        factory_service.set_continuous_mode(db, req.enabled)
    )


@router.get("/events", response_model=list[Event])
def factory_events(limit: int = 50, db: Session = Depends(get_db)) -> list[Event]:
    """Recent factory-lifecycle and deploy events for the timeline view."""
    factory_types = (
        "factory_started", "factory_paused", "factory_resumed",
        "factory_stopping", "factory_stopped", "factory_completed",
        "factory_failed", "factory_reset",
        "deploy_started", "deploy_build_checked", "deploy_nginx_checked",
        "deploy_service_restarted", "deploy_healthcheck_passed",
        "deploy_completed", "deploy_failed",
    )
    rows = (
        db.execute(
            select(EventRow)
            .where(EventRow.type.in_(factory_types))
            .order_by(desc(EventRow.id))
            .limit(min(max(limit, 1), 200))
        )
        .scalars()
        .all()
    )
    return [Event.model_validate(r) for r in rows]
