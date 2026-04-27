"""SQLite database connection and session management."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


# Columns added after the initial schema. SQLAlchemy's create_all does NOT
# alter existing tables, so for the SQLite file we patch missing columns
# at startup. Each entry is (table, column, ddl_fragment).
_SQLITE_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("factory", "desired_status", "VARCHAR(32) NOT NULL DEFAULT 'idle'"),
    ("factory", "continuous_mode", "BOOLEAN NOT NULL DEFAULT 0"),
    ("factory", "last_watchdog_at", "DATETIME"),
    ("factory", "run_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _resolve_db_url() -> str:
    """Resolve the SQLite URL.

    Defaults to a file in control_tower/api/control_tower.db so the file
    sits next to main.py when running `uvicorn main:app --reload`.
    """
    env_url = os.environ.get("CONTROL_TOWER_DB_URL")
    if env_url:
        return env_url

    api_dir = Path(__file__).resolve().parent.parent
    db_path = api_dir / "control_tower.db"
    return f"sqlite:///{db_path}"


DATABASE_URL = _resolve_db_url()

engine: Engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def create_tables() -> None:
    """Create all tables if they do not exist, then run lightweight
    additive migrations for columns added after initial deploy."""
    Base.metadata.create_all(bind=engine)
    _apply_sqlite_migrations()


def _apply_sqlite_migrations() -> None:
    """ALTER TABLE ADD COLUMN for any column missing from an existing DB.

    SQLite supports adding columns but not changing them, which is fine
    for the additive migrations we have. Idempotent — safe to call on
    every boot.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _SQLITE_MIGRATIONS:
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if column in cols:
                continue
            conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
