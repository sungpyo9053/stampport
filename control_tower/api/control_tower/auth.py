"""Bearer-token authentication for Control Tower control surfaces.

Two tokens, supplied via env vars:

    LOCAL_RUNNER_TOKEN          — required to hit /runners/* (the Mac runner)
    CONTROL_TOWER_ADMIN_TOKEN   — required to hit factory mutation + command-create

Both are read at request time (not import time) so a token added to the
systemd unit takes effect on the next request without an import-cache
restart.

Local-development convenience:
    If a token env var is **unset** the matching guard runs in
    "simulation mode" — it allows the request through but emits a single
    warning to stderr. This keeps `uvicorn main:app --reload` usable
    while developing without leaking the production token into a dotfile.

In production, set both tokens. Then any unauthenticated mutation is
rejected with 401.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)
_warned: set[str] = set()


def _expected(env_name: str) -> str | None:
    v = os.environ.get(env_name, "").strip()
    return v or None


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


def _guard(request: Request, env_name: str, label: str) -> None:
    expected = _expected(env_name)
    if expected is None:
        if env_name not in _warned:
            logger.warning(
                "%s is not set — running in simulation mode (no auth). "
                "Set this env var in production.",
                env_name,
            )
            _warned.add(env_name)
        return
    presented = _bearer(request)
    if presented != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{label} token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin(request: Request) -> None:
    """Guard for factory mutations and admin-issued commands."""
    _guard(request, "CONTROL_TOWER_ADMIN_TOKEN", "admin")


def require_runner(request: Request) -> None:
    """Guard for local-runner endpoints (heartbeat, claim, report)."""
    _guard(request, "LOCAL_RUNNER_TOKEN", "runner")
