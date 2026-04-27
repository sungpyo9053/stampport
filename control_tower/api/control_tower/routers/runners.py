"""Runner registry + outbound command queue endpoints.

Endpoints (all under /runners):

    POST /heartbeat                          (runner token)
    GET  /                                   (no auth — read-only listing)
    GET  /{rid}                              (no auth — read-only)
    POST /{rid}/commands                     (admin token)   — enqueue
    GET  /{rid}/commands/next                (runner token)  — claim
    POST /{rid}/commands/{cid}/result        (runner token)  — report
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_admin, require_runner
from ..db import get_db
from ..event_bus import event_bus
from ..schemas import (
    ALLOWED_COMMANDS,
    Command,
    CommandCreateRequest,
    CommandResultRequest,
    EventType,
    HeartbeatRequest,
    Runner,
    RunnerStatus,
)
from ..services import runner_service

router = APIRouter(prefix="/runners", tags=["runners"])


@router.post("/heartbeat", response_model=Runner, dependencies=[Depends(require_runner)])
def heartbeat(req: HeartbeatRequest, db: Session = Depends(get_db)) -> Runner:
    row = runner_service.upsert_runner(
        db,
        runner_id=req.runner_id,
        name=req.name,
        kind=req.kind,
        status=req.status,
        metadata=req.metadata,
    )
    event_bus.emit(
        db,
        type=EventType.LOCAL_RUNNER_HEARTBEAT,
        message=f"러너 '{row.name}'에서 heartbeat 수신.",
        payload={"runner_id": row.id, "status": row.status},
    )
    return Runner.model_validate(row)


@router.get("/", response_model=list[Runner])
def list_runners(db: Session = Depends(get_db)) -> list[Runner]:
    return [Runner.model_validate(r) for r in runner_service.list_runners(db)]


@router.get("/{runner_id}", response_model=Runner)
def get_runner(runner_id: str, db: Session = Depends(get_db)) -> Runner:
    row = runner_service.get_runner(db, runner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="runner not found")
    return Runner.model_validate(row)


@router.post(
    "/{runner_id}/commands",
    response_model=Command,
    dependencies=[Depends(require_admin)],
)
def enqueue_command(
    runner_id: str, req: CommandCreateRequest, db: Session = Depends(get_db)
) -> Command:
    if req.command not in ALLOWED_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"command must be one of: {', '.join(ALLOWED_COMMANDS)}",
        )
    if runner_service.get_runner(db, runner_id) is None:
        raise HTTPException(status_code=404, detail="runner not found — call /heartbeat first")
    row = runner_service.create_command(
        db, runner_id=runner_id, command=req.command, payload=req.payload
    )
    return Command.model_validate(row)


@router.get(
    "/{runner_id}/commands/next",
    response_model=Command | None,
    dependencies=[Depends(require_runner)],
)
def claim_next(runner_id: str, db: Session = Depends(get_db)) -> Command | None:
    row = runner_service.claim_next(db, runner_id)
    return Command.model_validate(row) if row is not None else None


@router.post(
    "/{runner_id}/commands/{command_id}/result",
    response_model=Command,
    dependencies=[Depends(require_runner)],
)
def report_result(
    runner_id: str,
    command_id: int,
    req: CommandResultRequest,
    db: Session = Depends(get_db),
) -> Command:
    try:
        row = runner_service.report_result(
            db,
            runner_id=runner_id,
            command_id=command_id,
            status=req.status,
            result_message=req.result_message,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Command.model_validate(row)
