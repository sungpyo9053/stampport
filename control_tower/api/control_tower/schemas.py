"""Pydantic schemas for the Control Tower API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    DONE = "done"
    ERROR = "error"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    TASK_CREATED = "task_created"
    AGENT_STARTED = "agent_started"
    AGENT_MESSAGE = "agent_message"
    ARTIFACT_CREATED = "artifact_created"
    TASK_COMPLETED = "task_completed"
    HANDOFF = "handoff"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    ERROR = "error"

    # Factory lifecycle
    FACTORY_STARTED = "factory_started"
    FACTORY_PAUSED = "factory_paused"
    FACTORY_RESUMED = "factory_resumed"
    FACTORY_STOPPING = "factory_stopping"
    FACTORY_STOPPED = "factory_stopped"
    FACTORY_COMPLETED = "factory_completed"
    FACTORY_FAILED = "factory_failed"
    FACTORY_RESET = "factory_reset"

    # Deploy agent
    DEPLOY_STARTED = "deploy_started"
    DEPLOY_BUILD_CHECKED = "deploy_build_checked"
    DEPLOY_NGINX_CHECKED = "deploy_nginx_checked"
    DEPLOY_SERVICE_RESTARTED = "deploy_service_restarted"
    DEPLOY_HEALTHCHECK_PASSED = "deploy_healthcheck_passed"
    DEPLOY_COMPLETED = "deploy_completed"
    DEPLOY_FAILED = "deploy_failed"

    # Local runner
    LOCAL_RUNNER_HEARTBEAT = "local_runner_heartbeat"
    LOCAL_RUNNER_COMMAND_CREATED = "local_runner_command_created"
    LOCAL_RUNNER_COMMAND_CLAIMED = "local_runner_command_claimed"
    LOCAL_RUNNER_RESULT_REPORTED = "local_runner_result_reported"
    LOCAL_RUNNER_STALE = "local_runner_stale"

    # Watchdog / continuous mode
    FACTORY_DESIRED_CHANGED = "factory_desired_changed"
    FACTORY_CONTINUOUS_TOGGLED = "factory_continuous_toggled"
    FACTORY_AUTO_RESTARTED = "factory_auto_restarted"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class Agent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    role: str
    status: AgentStatus = AgentStatus.IDLE
    current_task_id: Optional[int] = None
    updated_at: Optional[datetime] = None


class Task(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: Optional[str] = None
    agent_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class Event(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: EventType
    agent_id: Optional[str] = None
    task_id: Optional[int] = None
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Artifact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: Optional[int] = None
    agent_id: Optional[str] = None
    type: str
    title: str
    content: str
    created_at: datetime


class ApprovalAction(BaseModel):
    """Action a human takes on an approval-requested event."""

    decision: ApprovalDecision
    comment: Optional[str] = None


class RunDemoResponse(BaseModel):
    """Response when kicking off the demo workflow."""

    started: bool
    workflow: str
    task_count: int
    message: str


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class FactoryStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class StageStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"


class Factory(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = 1
    status: FactoryStatus = FactoryStatus.IDLE
    desired_status: FactoryStatus = FactoryStatus.IDLE
    continuous_mode: bool = False
    current_stage: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_message: Optional[str] = None
    last_watchdog_at: Optional[datetime] = None
    run_count: int = 0
    updated_at: Optional[datetime] = None


class DesiredStatusRequest(BaseModel):
    desired_status: FactoryStatus


class ContinuousModeRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Runner / Command
# ---------------------------------------------------------------------------


class RunnerKind(str, Enum):
    LOCAL = "local"
    SERVER = "server"


class RunnerStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"


class CommandStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Allowlist of command names a runner may execute. Anything else is rejected.
ALLOWED_COMMANDS: tuple[str, ...] = (
    "start_factory",
    "stop_factory",
    "restart_factory",
    "pause_factory",
    "resume_factory",
    "status",
    "git_pull",
    "build_check",
    "test_check",
    # Auto-deploy entry point. The local runner runs publish-time
    # validation, classifies the diff, and only commits/pushes when
    # both LOCAL_RUNNER_ALLOW_PUBLISH=true and
    # LOCAL_RUNNER_PUBLISH_DRY_RUN=false are set on the Mac side.
    "publish_changes",
    # Full server deployment. Wraps publish_changes with an SSH-driven
    # remote build + dist copy + healthcheck pass so a single click
    # ships local edits all the way to /var/www/stampport(/-control).
    # Single-flight guarded; same dry-run levers as publish_changes
    # plus LOCAL_RUNNER_DEPLOY_DRY_RUN for the SSH side.
    "deploy_to_server",
    # Self-management commands. The runner can replace itself with
    # the latest on-disk code (`restart_runner`) or fast-forward main +
    # bounce factory + replace itself (`update_runner`). The existing
    # `restart_factory` above already covers bouncing the factory loop
    # — both are dry-run-able via LOCAL_RUNNER_RESTART_DRY_RUN=true.
    "restart_runner",
    "update_runner",
    # Operator Fix Request — a free-form bug/improvement request the
    # admin types into the dashboard. The runner writes it to
    # .runtime/operator_request.md, calls Claude Code with restricted
    # tools (Read/Glob/Grep/Edit only), runs build + syntax + QA Gate,
    # and (only for the *_and_publish variant) chains into the same
    # publish_changes path the dashboard's 배포하기 button uses. The
    # *_request variant never touches git history.
    "operator_fix_request",
    "operator_fix_and_publish",
)


class Runner(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: RunnerKind = RunnerKind.LOCAL
    status: RunnerStatus = RunnerStatus.OFFLINE
    last_heartbeat_at: Optional[datetime] = None
    current_command: Optional[str] = None
    last_result: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


class HeartbeatRequest(BaseModel):
    runner_id: str
    name: Optional[str] = None
    kind: RunnerKind = RunnerKind.LOCAL
    status: RunnerStatus = RunnerStatus.ONLINE
    metadata: dict[str, Any] = Field(default_factory=dict)


class Command(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    runner_id: str
    command: str
    status: CommandStatus = CommandStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    result_message: Optional[str] = None
    created_at: datetime
    claimed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class CommandCreateRequest(BaseModel):
    command: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CommandResultRequest(BaseModel):
    status: CommandStatus
    result_message: Optional[str] = None
