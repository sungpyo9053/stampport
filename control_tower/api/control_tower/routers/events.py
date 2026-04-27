"""Event endpoints.

`GET /events` returns events from the database.
`GET /events/stream` is a placeholder for the SSE/WebSocket layer that
will be wired in later — for now it returns a small synthetic preview so
the frontend can integrate against the shape early.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..event_bus import event_bus
from ..schemas import Event

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[Event])
def list_events(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Event]:
    return event_bus.recent(db, limit=limit)


@router.get("/stream")
def stream_events(
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Placeholder for the future SSE/WebSocket stream.

    Returns the most recent events plus a flag describing the current
    transport so the frontend can branch its rendering until live push
    is wired up.
    """
    recent = event_bus.recent(db, limit=limit)
    return {
        "transport": "polling",
        "live": False,
        "note": "Live SSE/WebSocket not yet implemented; poll GET /events instead.",
        "events": [event.model_dump(mode="json") for event in recent],
    }
