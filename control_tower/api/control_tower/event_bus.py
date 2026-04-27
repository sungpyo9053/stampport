"""Event bus for the Control Tower.

This module is the single place where events are created. It writes them to
the database and fans them out to in-process listeners. The listener hook
is the seam where a future SSE / WebSocket layer plugs in: a streaming
endpoint registers a callback, this bus invokes it on every emit.
"""

from __future__ import annotations

import logging
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from .models import EventRow
from .schemas import Event, EventType

logger = logging.getLogger(__name__)

EventListener = Callable[[Event], None]


class EventBus:
    """In-process event bus backed by the events table."""

    def __init__(self) -> None:
        self._listeners: list[EventListener] = []
        self._lock = RLock()

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """Register a listener. Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsubscribe

    def emit(
        self,
        db: Session,
        *,
        type: EventType,
        message: str,
        agent_id: Optional[str] = None,
        task_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> Event:
        """Persist an event and notify listeners."""
        row = EventRow(
            type=type.value,
            agent_id=agent_id,
            task_id=task_id,
            message=message,
            payload=payload or {},
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        event = Event.model_validate(row)
        self._notify(event)
        return event

    def recent(self, db: Session, limit: int = 100) -> list[Event]:
        """Return the most recent events, oldest-first within the slice."""
        rows = (
            db.query(EventRow)
            .order_by(EventRow.id.desc())
            .limit(limit)
            .all()
        )
        rows.reverse()
        return [Event.model_validate(r) for r in rows]

    def _notify(self, event: Event) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # pragma: no cover - listeners must not break emits
                logger.exception("event listener raised; continuing")


event_bus = EventBus()
