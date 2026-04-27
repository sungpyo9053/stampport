"""Demo-lifecycle endpoints (reset, etc)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..services.demo_service import reset_demo_state

router = APIRouter(prefix="/demo", tags=["demo"])


@router.delete("/reset")
def reset(db: Session = Depends(get_db)) -> dict:
    reset_demo_state(db)
    return {"ok": True, "message": "demo state reset"}
