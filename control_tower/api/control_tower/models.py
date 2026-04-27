"""SQLAlchemy ORM models backing the Control Tower SQLite database."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.utcnow()


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    current_task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    tasks: Mapped[list["TaskRow"]] = relationship(
        "TaskRow",
        back_populates="agent",
        foreign_keys="TaskRow.agent_id",
    )


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    agent: Mapped[Optional[AgentRow]] = relationship(
        "AgentRow",
        back_populates="tasks",
        foreign_keys=[agent_id],
    )
    events: Mapped[list["EventRow"]] = relationship(
        "EventRow", back_populates="task", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["ArtifactRow"]] = relationship(
        "ArtifactRow", back_populates="task", cascade="all, delete-orphan"
    )


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False, index=True
    )

    task: Mapped[Optional[TaskRow]] = relationship("TaskRow", back_populates="events")


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )

    task: Mapped[Optional[TaskRow]] = relationship("TaskRow", back_populates="artifacts")


# ---------------------------------------------------------------------------
# Factory (singleton) — automated workflow control
# ---------------------------------------------------------------------------


class FactoryRow(Base):
    """Singleton row (id=1) holding the current factory state.

    Two parallel fields drive the lifecycle:
      * status         — what the factory IS doing right now
      * desired_status — what the user WANTS it to be doing

    A small reconciler thread (the watchdog) closes the gap between
    them so things like "auto-resume after a crash" or "loop forever"
    don't need a human button press. continuous_mode = True turns the
    workflow into a perpetual loop: each completed run is auto-restarted.
    """

    __tablename__ = "factory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    desired_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="idle"
    )
    continuous_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    current_stage: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_watchdog_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Local Runner + outbound command queue
# ---------------------------------------------------------------------------


class RunnerRow(Base):
    """A worker that polls this server for commands (e.g. the Mac runner)."""

    __tablename__ = "runners"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    current_command: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    commands: Mapped[list["CommandRow"]] = relationship(
        "CommandRow", back_populates="runner", cascade="all, delete-orphan"
    )


class CommandRow(Base):
    """A single outbound command queued for a runner to claim."""

    __tablename__ = "runner_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runners.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    runner: Mapped[RunnerRow] = relationship("RunnerRow", back_populates="commands")
