"""Dummy agent runner.

Simulates one agent doing one task. No LLM calls. The runner:
  1. flips the agent to `working` and emits `agent_started`
  2. emits a few `agent_message` events
  3. creates an artifact and emits `artifact_created`
  4. flips the agent to `done`, marks the task `completed`,
     and emits `task_completed`

The orchestrator is responsible for sequencing runners and emitting handoffs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from .event_bus import event_bus
from .models import AgentRow, ArtifactRow, TaskRow
from .schemas import AgentStatus, EventType, TaskStatus


@dataclass
class AgentScript:
    """A scripted run for one agent — what to say and what to produce."""

    agent_id: str
    task_title: str
    task_description: str
    messages: list[str] = field(default_factory=list)
    artifact_type: str = "note"
    artifact_title: str = "Untitled"
    artifact_content: str = ""
    # Visible-pacing knobs (see comments in run_agent for where each fires).
    step_delay_seconds: float = 0.8       # gap between consecutive agent_message events
    pre_artifact_delay: float = 0.7       # quiet beat after the last message before the artifact lands
    post_complete_delay: float = 0.5      # how long the ✅ "done" state stays visible before handoff


def _set_agent_status(
    db: Session,
    agent_id: str,
    status: AgentStatus,
    *,
    current_task_id: Optional[int] = None,
) -> AgentRow:
    agent = db.get(AgentRow, agent_id)
    if agent is None:
        raise ValueError(f"Unknown agent: {agent_id}")
    agent.status = status.value
    agent.current_task_id = current_task_id
    agent.updated_at = datetime.utcnow()
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _create_task(db: Session, script: AgentScript) -> TaskRow:
    task = TaskRow(
        title=script.task_title,
        description=script.task_description,
        agent_id=script.agent_id,
        status=TaskStatus.PENDING.value,
        created_at=datetime.utcnow(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _agent_display_name(db: Session, agent_id: str) -> str:
    """Look up the agent's Korean display name from the DB row.

    Falls back to the agent_id if no row exists yet, which keeps the runner
    usable from tests / scripts that haven't seeded agents.
    """
    agent = db.get(AgentRow, agent_id)
    if agent and agent.name:
        return agent.name
    return agent_id


def run_agent(db: Session, script: AgentScript) -> TaskRow:
    """Execute the dummy run for one agent and return the completed task.

    All event messages are emitted in Korean so the dashboard reads naturally
    during a Korean-language demo.
    """

    display_name = _agent_display_name(db, script.agent_id)

    task = _create_task(db, script)
    event_bus.emit(
        db,
        type=EventType.TASK_CREATED,
        message=f"{display_name} 작업 생성: {script.task_title}",
        agent_id=script.agent_id,
        task_id=task.id,
        payload={"task_title": script.task_title},
    )

    _set_agent_status(
        db, script.agent_id, AgentStatus.WORKING, current_task_id=task.id
    )
    task.status = TaskStatus.IN_PROGRESS.value
    task.started_at = datetime.utcnow()
    db.add(task)
    db.commit()

    event_bus.emit(
        db,
        type=EventType.AGENT_STARTED,
        message=f"{display_name}이(가) '{script.task_title}' 작업을 시작했습니다.",
        agent_id=script.agent_id,
        task_id=task.id,
    )

    for line in script.messages:
        time.sleep(script.step_delay_seconds)
        event_bus.emit(
            db,
            type=EventType.AGENT_MESSAGE,
            message=line,
            agent_id=script.agent_id,
            task_id=task.id,
        )

    # one extra beat of "still working" before the artifact lands
    time.sleep(script.pre_artifact_delay)

    artifact = ArtifactRow(
        task_id=task.id,
        agent_id=script.agent_id,
        type=script.artifact_type,
        title=script.artifact_title,
        content=script.artifact_content,
        created_at=datetime.utcnow(),
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    event_bus.emit(
        db,
        type=EventType.ARTIFACT_CREATED,
        message=f"{display_name}이(가) '{script.artifact_title}' 산출물을 만들었습니다.",
        agent_id=script.agent_id,
        task_id=task.id,
        payload={
            "artifact_id": artifact.id,
            "artifact_type": artifact.type,
            "artifact_title": artifact.title,
            "preview": artifact.content[:200],
        },
    )

    task.status = TaskStatus.COMPLETED.value
    task.completed_at = datetime.utcnow()
    db.add(task)
    db.commit()
    db.refresh(task)

    _set_agent_status(db, script.agent_id, AgentStatus.DONE, current_task_id=None)

    event_bus.emit(
        db,
        type=EventType.TASK_COMPLETED,
        message=f"{display_name}이(가) '{script.task_title}' 작업을 완료했습니다.",
        agent_id=script.agent_id,
        task_id=task.id,
    )

    # let the ✅ badge be visible before we start the next agent
    time.sleep(script.post_complete_delay)

    return task
