"""Factory state service.

A `factory` is a singleton (id=1) that tracks the lifecycle of one
end-to-end automated workflow run. UI buttons (start / pause / resume /
stop / reset) translate to state transitions on this row, and the
orchestrator polls it between stages so pause/stop actually halt the
pipeline at the next checkpoint.

State machine:

       reset / boot
            │
            ▼
        [ idle ] ─── start ──▶ [ running ] ─── pause ──▶ [ paused ]
                                  │  ▲                        │
                                  │  │                        │
                                  │  └────── resume ──────────┘
                                  │
                                  ├── stop ──▶ [ stopping ] → [ stopped ]
                                  │
                                  ├── workflow done ──▶ [ completed ]
                                  │
                                  └── exception ─────▶ [ failed ]

`pause` is honored at the next inter-stage checkpoint; we do not kill
threads mid-stage. That's the deliberate trade-off documented in the
spec for MVP.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..event_bus import event_bus
from ..models import FactoryRow
from ..schemas import EventType, FactoryStatus

FACTORY_ID = 1
PAUSE_POLL_INTERVAL = 0.4   # seconds between pause-loop wakeups
PAUSE_MAX_WAIT = 60 * 30    # never block the orchestrator forever


# ---------------------------------------------------------------------------
# Read / write helpers
# ---------------------------------------------------------------------------


def _ensure_row(db: Session) -> FactoryRow:
    row = db.get(FactoryRow, FACTORY_ID)
    if row is None:
        row = FactoryRow(id=FACTORY_ID, status=FactoryStatus.IDLE.value)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_state(db: Session) -> FactoryRow:
    return _ensure_row(db)


def _set(
    db: Session,
    *,
    status: Optional[FactoryStatus] = None,
    desired_status: Optional[FactoryStatus] = None,
    continuous_mode: Optional[bool] = None,
    current_stage: Optional[str] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    last_message: Optional[str] = None,
    last_watchdog_at: Optional[datetime] = None,
    bump_run_count: bool = False,
) -> FactoryRow:
    row = _ensure_row(db)
    if status is not None:
        row.status = status.value
    if desired_status is not None:
        row.desired_status = desired_status.value
    if continuous_mode is not None:
        row.continuous_mode = continuous_mode
    if current_stage is not None:
        row.current_stage = current_stage
    if started_at is not None:
        row.started_at = started_at
    if finished_at is not None:
        row.finished_at = finished_at
    if last_message is not None:
        row.last_message = last_message
    if last_watchdog_at is not None:
        row.last_watchdog_at = last_watchdog_at
    if bump_run_count:
        row.run_count = (row.run_count or 0) + 1
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Public state transitions (called by /factory router)
# ---------------------------------------------------------------------------


def start(db: Session) -> FactoryRow:
    row = _ensure_row(db)
    if row.status == FactoryStatus.RUNNING.value:
        # Idempotent: refuse to start a second one in parallel.
        # Make sure desired_status reflects intent so the watchdog
        # doesn't try to restart what's already running.
        _set(db, desired_status=FactoryStatus.RUNNING)
        return row
    _set(
        db,
        status=FactoryStatus.RUNNING,
        desired_status=FactoryStatus.RUNNING,
        current_stage="planner",
        started_at=datetime.utcnow(),
        finished_at=None,
        last_message="자동 공장 시작",
        bump_run_count=True,
    )
    event_bus.emit(db, type=EventType.FACTORY_STARTED, message="자동 공장을 시작했습니다.")
    return _ensure_row(db)


def pause(db: Session) -> FactoryRow:
    row = _ensure_row(db)
    if row.status != FactoryStatus.RUNNING.value:
        # Still record intent — the watchdog will pause on next reconcile
        # if the factory transitions back to running.
        _set(db, desired_status=FactoryStatus.PAUSED)
        return row
    _set(
        db,
        status=FactoryStatus.PAUSED,
        desired_status=FactoryStatus.PAUSED,
        last_message="다음 단계 직전에 일시정지됩니다.",
    )
    event_bus.emit(db, type=EventType.FACTORY_PAUSED, message="자동 공장을 일시정지했습니다.")
    return _ensure_row(db)


def resume(db: Session) -> FactoryRow:
    row = _ensure_row(db)
    if row.status != FactoryStatus.PAUSED.value:
        _set(db, desired_status=FactoryStatus.RUNNING)
        return row
    _set(
        db,
        status=FactoryStatus.RUNNING,
        desired_status=FactoryStatus.RUNNING,
        last_message="자동 공장 재개",
    )
    event_bus.emit(db, type=EventType.FACTORY_RESUMED, message="자동 공장을 재개했습니다.")
    return _ensure_row(db)


def request_stop(db: Session) -> FactoryRow:
    row = _ensure_row(db)
    if row.status in {FactoryStatus.IDLE.value, FactoryStatus.STOPPED.value, FactoryStatus.COMPLETED.value, FactoryStatus.FAILED.value}:
        # Already terminal; just record intent so continuous mode won't
        # auto-restart this one.
        _set(db, desired_status=FactoryStatus.IDLE)
        return row
    _set(
        db,
        status=FactoryStatus.STOPPING,
        desired_status=FactoryStatus.IDLE,
        last_message="이번 단계 끝나면 중지됩니다.",
    )
    event_bus.emit(db, type=EventType.FACTORY_STOPPING, message="자동 공장을 중지 요청했습니다.")
    return _ensure_row(db)


def mark_stopped(db: Session, message: str = "자동 공장이 중지되었습니다.") -> FactoryRow:
    _set(db, status=FactoryStatus.STOPPED, finished_at=datetime.utcnow(), last_message=message)
    event_bus.emit(db, type=EventType.FACTORY_STOPPED, message=message)
    return _ensure_row(db)


def mark_completed(db: Session, message: str = "자동 공장이 모든 단계를 완료했습니다.") -> FactoryRow:
    _set(db, status=FactoryStatus.COMPLETED, finished_at=datetime.utcnow(), last_message=message)
    event_bus.emit(db, type=EventType.FACTORY_COMPLETED, message=message)
    return _ensure_row(db)


def mark_failed(db: Session, message: str) -> FactoryRow:
    _set(db, status=FactoryStatus.FAILED, finished_at=datetime.utcnow(), last_message=message)
    event_bus.emit(db, type=EventType.FACTORY_FAILED, message=message)
    return _ensure_row(db)


def reset(db: Session) -> FactoryRow:
    """Bounce the singleton back to idle. Caller is expected to also
    wipe tasks/events/artifacts via demo_service.reset_demo_state().

    Reset also clears desired_status to idle so continuous mode does
    not immediately re-launch a fresh run. Continuous_mode itself is
    preserved — that's a user setting, not a per-run state.
    """
    _set(
        db,
        status=FactoryStatus.IDLE,
        desired_status=FactoryStatus.IDLE,
        current_stage=None,
        started_at=None,
        finished_at=None,
        last_message="자동 공장을 초기화했습니다.",
    )
    event_bus.emit(db, type=EventType.FACTORY_RESET, message="자동 공장을 초기화했습니다.")
    return _ensure_row(db)


# ---------------------------------------------------------------------------
# desired_state / continuous_mode
# ---------------------------------------------------------------------------


def set_desired_status(db: Session, desired: FactoryStatus) -> FactoryRow:
    """Record the user's intended state. The watchdog reconciles on
    its next tick. Only IDLE / RUNNING / PAUSED are user-settable."""
    if desired not in {FactoryStatus.IDLE, FactoryStatus.RUNNING, FactoryStatus.PAUSED}:
        raise ValueError(
            "desired_status must be one of: idle, running, paused"
        )
    _set(db, desired_status=desired)
    event_bus.emit(
        db,
        type=EventType.FACTORY_DESIRED_CHANGED,
        message=f"자동 공장 desired_status: {desired.value}",
        payload={"desired_status": desired.value},
    )
    return _ensure_row(db)


def set_continuous_mode(db: Session, enabled: bool) -> FactoryRow:
    _set(db, continuous_mode=bool(enabled))
    event_bus.emit(
        db,
        type=EventType.FACTORY_CONTINUOUS_TOGGLED,
        message=(
            "계속 실행 모드 켜짐 — 한 사이클이 끝나면 자동으로 다음 사이클을 시작합니다."
            if enabled else
            "계속 실행 모드 꺼짐."
        ),
        payload={"continuous_mode": bool(enabled)},
    )
    return _ensure_row(db)


# ---------------------------------------------------------------------------
# Reconciler / watchdog
# ---------------------------------------------------------------------------


# A terminal state means the workflow stopped (cleanly or not) and there
# is nothing actively running. The reconciler is allowed to spawn a new
# run from a terminal state, but never from a live one.
_TERMINAL_STATES = {
    FactoryStatus.IDLE.value,
    FactoryStatus.STOPPED.value,
    FactoryStatus.COMPLETED.value,
    FactoryStatus.FAILED.value,
}


def reconcile_once(db: Session, *, restart_workflow) -> str:
    """Single watchdog tick. Called by the background thread.

    `restart_workflow` is a callback the caller provides that knows how
    to spawn the orchestrator thread (kept out of this module to avoid
    a circular import with the router).

    Returns a short string describing what was done — useful for tests
    and for the FACTORY_WATCHDOG_RECONCILED event payload.
    """
    row = _ensure_row(db)
    _set(db, last_watchdog_at=datetime.utcnow())
    desired = row.desired_status or FactoryStatus.IDLE.value
    actual = row.status or FactoryStatus.IDLE.value

    # Case 1: user wants paused, but it's still running → pause it.
    if desired == FactoryStatus.PAUSED.value and actual == FactoryStatus.RUNNING.value:
        pause(db)
        return "paused"

    # Case 2: user wants running, but it's currently paused → resume.
    if desired == FactoryStatus.RUNNING.value and actual == FactoryStatus.PAUSED.value:
        resume(db)
        return "resumed"

    # Case 3: user wants idle, but it's still running/paused → stop it.
    if desired == FactoryStatus.IDLE.value and actual in {
        FactoryStatus.RUNNING.value,
        FactoryStatus.PAUSED.value,
    }:
        request_stop(db)
        return "stopping"

    # Case 4: continuous mode + terminal state + desired==running →
    # auto-restart the workflow. This is the "계속 실행 모드".
    if (
        bool(row.continuous_mode)
        and desired == FactoryStatus.RUNNING.value
        and actual in _TERMINAL_STATES
    ):
        # Flip to RUNNING synchronously BEFORE the spawn, so the next
        # watchdog tick (which may fire within milliseconds) doesn't
        # see the same terminal state and spawn a duplicate thread.
        # The spawned thread's call to start() then becomes a no-op.
        next_run_no = (row.run_count or 0) + 1
        _set(
            db,
            status=FactoryStatus.RUNNING,
            current_stage="planner",
            started_at=datetime.utcnow(),
            finished_at=None,
            last_message=f"계속 실행 모드: 사이클 #{next_run_no} 시작",
            bump_run_count=True,
        )
        event_bus.emit(
            db,
            type=EventType.FACTORY_AUTO_RESTARTED,
            message=f"계속 실행 모드: 자동 공장을 재시작했습니다 (run #{next_run_no}).",
            payload={"run_count": next_run_no},
        )
        # Hand control back to the caller — it spawns the orchestrator
        # thread the same way POST /factory/start does.
        try:
            restart_workflow()
        except Exception as e:  # noqa: BLE001
            mark_failed(db, f"계속 실행 모드 재시작 실패: {e}")
        return "auto_restarted"

    return "noop"


# ---------------------------------------------------------------------------
# Orchestrator-facing checkpoint
# ---------------------------------------------------------------------------


def checkpoint(db: Session, stage: str) -> bool:
    """Block until the factory is allowed to advance into `stage`.

    Returns True if the orchestrator should proceed, False if it should
    abort (stop requested or factory not running). This is the only place
    the orchestrator should consult — it doesn't need to know the state
    machine.
    """
    waited = 0.0
    while True:
        row = _ensure_row(db)
        st = row.status
        if st == FactoryStatus.RUNNING.value:
            # Mark which stage we're entering so the UI shows progress.
            _set(db, current_stage=stage)
            return True
        if st == FactoryStatus.PAUSED.value:
            time.sleep(PAUSE_POLL_INTERVAL)
            waited += PAUSE_POLL_INTERVAL
            if waited > PAUSE_MAX_WAIT:
                # Safety valve: never block the API process forever.
                mark_failed(db, "일시정지 상태가 너무 오래 유지되어 안전하게 중단합니다.")
                return False
            continue
        if st in {
            FactoryStatus.STOPPING.value,
            FactoryStatus.STOPPED.value,
            FactoryStatus.FAILED.value,
            FactoryStatus.IDLE.value,   # someone reset us mid-run
            FactoryStatus.COMPLETED.value,
        }:
            return False
        # Unknown state: be conservative.
        return False
