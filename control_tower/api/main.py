"""FastAPI entrypoint for the Control Tower API.

Run from `control_tower/api/` with:

    uvicorn main:app --reload
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from control_tower.db import SessionLocal, create_tables
from control_tower.orchestrator import seed_agents
from control_tower.routers import agents as agents_router
from control_tower.routers import demo as demo_router
from control_tower.routers import events as events_router
from control_tower.routers import factory as factory_router
from control_tower.routers import health as health_router
from control_tower.routers import runners as runners_router
from control_tower.routers import tasks as tasks_router
from control_tower.services import factory_service, runner_service


# How often the watchdog wakes up. Cheap query, kept tight enough that
# pause/resume feels live without flooding the SQLite write pipeline.
WATCHDOG_INTERVAL_SEC = float(os.environ.get("FACTORY_WATCHDOG_INTERVAL", "3.0"))

_watchdog_stop = threading.Event()


def _spawn_workflow_thread() -> None:
    """Launch the orchestrator on its own daemon thread.

    Called by the watchdog when continuous_mode auto-restarts the
    workflow. The watchdog has already flipped status→RUNNING and
    bumped run_count, so this thread just runs the workflow and
    handles terminal-state bookkeeping. Tasks/events accumulate
    across cycles — that's the desired "log of all runs" behavior.
    """
    from control_tower.orchestrator import run_demo_workflow
    from control_tower.schemas import FactoryStatus

    def _runner() -> None:
        local_db = SessionLocal()
        try:
            try:
                run_demo_workflow(local_db)
                final = factory_service.get_state(local_db)
                if final.status == FactoryStatus.RUNNING.value:
                    factory_service.mark_completed(local_db)
                elif final.status == FactoryStatus.STOPPING.value:
                    factory_service.mark_stopped(local_db)
            except Exception as e:  # noqa: BLE001
                factory_service.mark_failed(local_db, f"실행 중 예외: {e}")
        finally:
            local_db.close()

    threading.Thread(
        target=_runner, daemon=True, name="factory-runner-auto"
    ).start()


def _watchdog_loop() -> None:
    """Run forever (until _watchdog_stop is set), reconciling the
    factory's desired_status with its actual status and marking stale
    runners offline."""
    while not _watchdog_stop.is_set():
        try:
            db = SessionLocal()
            try:
                factory_service.reconcile_once(
                    db, restart_workflow=_spawn_workflow_thread
                )
                runner_service.mark_stale_runners(db)
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[watchdog] tick failed: {e}\n")
        # event.wait() makes shutdown immediate instead of waiting up to
        # WATCHDOG_INTERVAL_SEC for the next tick.
        _watchdog_stop.wait(WATCHDOG_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    db = SessionLocal()
    try:
        seed_agents(db)
    finally:
        db.close()
    _watchdog_stop.clear()
    t = threading.Thread(target=_watchdog_loop, daemon=True, name="factory-watchdog")
    t.start()
    try:
        yield
    finally:
        _watchdog_stop.set()


app = FastAPI(
    title="Stampport Lab Control Tower API",
    version="0.1.0",
    description="Backend for the Stampport AI Agent Studio control-tower dashboard.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router.router)
app.include_router(agents_router.router)
app.include_router(tasks_router.router)
app.include_router(events_router.router)
app.include_router(demo_router.router)
app.include_router(factory_router.router)
app.include_router(runners_router.router)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "control-tower",
        "docs": "/docs",
        "health": "/health",
    }
