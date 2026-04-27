"""Agent listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AgentRow
from ..schemas import Agent

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[Agent])
def list_agents(db: Session = Depends(get_db)) -> list[Agent]:
    rows = db.query(AgentRow).order_by(AgentRow.id).all()
    return [Agent.model_validate(r) for r in rows]
