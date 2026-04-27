"""Runner registry + outbound command queue.

Server-side abstraction for the Mac Local Runner. The Lightsail box
never reaches out — runners poll. Each runner identifies itself by a
stable `runner_id` (e.g. `sungpyo-macbook`) and bears a token in
the Authorization header.

This module is pure data + queueing. It never spawns a process.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..event_bus import event_bus
from ..models import CommandRow, RunnerRow
from ..schemas import (
    ALLOWED_COMMANDS,
    CommandStatus,
    EventType,
    RunnerKind,
    RunnerStatus,
)


# Heartbeat default is 15s; allow 5× before declaring the runner stale.
# The cost of a wrong "offline" flag is user confusion, so don't tune
# aggressively.
RUNNER_STALE_AFTER_SEC = 75


def upsert_runner(
    db: Session,
    *,
    runner_id: str,
    name: Optional[str] = None,
    kind: RunnerKind = RunnerKind.LOCAL,
    status: RunnerStatus = RunnerStatus.ONLINE,
    metadata: Optional[dict] = None,
) -> RunnerRow:
    row = db.get(RunnerRow, runner_id)
    if row is None:
        row = RunnerRow(
            id=runner_id,
            name=name or runner_id,
            kind=kind.value,
            status=status.value,
            metadata_json=metadata or {},
        )
        db.add(row)
    else:
        if name is not None:
            row.name = name
        row.kind = kind.value
        row.status = status.value
        if metadata is not None:
            row.metadata_json = metadata
    row.last_heartbeat_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def list_runners(db: Session) -> list[RunnerRow]:
    return db.execute(select(RunnerRow).order_by(RunnerRow.id)).scalars().all()


def get_runner(db: Session, runner_id: str) -> RunnerRow | None:
    return db.get(RunnerRow, runner_id)


def mark_stale_runners(db: Session) -> list[RunnerRow]:
    """Flip any runner whose heartbeat is older than RUNNER_STALE_AFTER_SEC
    to status='offline'. Returns the rows that flipped."""
    cutoff = datetime.utcnow() - timedelta(seconds=RUNNER_STALE_AFTER_SEC)
    flipped: list[RunnerRow] = []
    rows = db.execute(select(RunnerRow)).scalars().all()
    for r in rows:
        if r.status == RunnerStatus.OFFLINE.value:
            continue
        if r.last_heartbeat_at is None:
            continue
        if r.last_heartbeat_at < cutoff:
            r.status = RunnerStatus.OFFLINE.value
            r.updated_at = datetime.utcnow()
            db.add(r)
            flipped.append(r)
    if flipped:
        db.commit()
        for r in flipped:
            event_bus.emit(
                db,
                type=EventType.LOCAL_RUNNER_STALE,
                message=f"러너 '{r.name}' heartbeat이 끊겨 offline으로 전환했습니다.",
                payload={"runner_id": r.id, "last_heartbeat_at": r.last_heartbeat_at.isoformat() if r.last_heartbeat_at else None},
            )
    return flipped


def create_command(
    db: Session, *, runner_id: str, command: str, payload: dict | None = None
) -> CommandRow:
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"command '{command}' is not in the allowlist")
    row = CommandRow(
        runner_id=runner_id,
        command=command,
        status=CommandStatus.PENDING.value,
        payload=payload or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    event_bus.emit(
        db,
        type=EventType.LOCAL_RUNNER_COMMAND_CREATED,
        message=f"로컬 러너에 '{command}' 명령을 큐에 넣었습니다.",
        payload={"runner_id": runner_id, "command": command, "command_id": row.id},
    )
    return row


def claim_next(db: Session, runner_id: str) -> CommandRow | None:
    """Pop the oldest pending command for this runner (FIFO)."""
    row = (
        db.execute(
            select(CommandRow)
            .where(CommandRow.runner_id == runner_id)
            .where(CommandRow.status == CommandStatus.PENDING.value)
            .order_by(CommandRow.id)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    row.status = CommandStatus.CLAIMED.value
    row.claimed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)

    # Mirror the in-flight command on the runner row so the UI shows it.
    runner = db.get(RunnerRow, runner_id)
    if runner is not None:
        runner.current_command = row.command
        runner.status = RunnerStatus.BUSY.value
        runner.updated_at = datetime.utcnow()
        db.commit()

    event_bus.emit(
        db,
        type=EventType.LOCAL_RUNNER_COMMAND_CLAIMED,
        message=f"로컬 러너가 '{row.command}' 명령을 가져갔습니다.",
        payload={"runner_id": runner_id, "command": row.command, "command_id": row.id},
    )
    return row


def report_result(
    db: Session,
    *,
    runner_id: str,
    command_id: int,
    status: CommandStatus,
    result_message: str | None,
) -> CommandRow:
    row = db.get(CommandRow, command_id)
    if row is None or row.runner_id != runner_id:
        raise LookupError("command not found for this runner")
    row.status = status.value
    row.result_message = result_message
    row.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)

    runner = db.get(RunnerRow, runner_id)
    if runner is not None:
        runner.current_command = None
        runner.status = (
            RunnerStatus.ONLINE.value
            if status == CommandStatus.SUCCEEDED
            else RunnerStatus.ERROR.value
        )
        runner.last_result = (result_message or "")[:500]
        runner.updated_at = datetime.utcnow()
        db.commit()

    event_bus.emit(
        db,
        type=EventType.LOCAL_RUNNER_RESULT_REPORTED,
        message=f"로컬 러너가 '{row.command}' 결과를 보고했습니다 ({status.value}).",
        payload={
            "runner_id": runner_id,
            "command": row.command,
            "command_id": command_id,
            "status": status.value,
            "result_message": (result_message or "")[:500],
        },
    )
    return row
