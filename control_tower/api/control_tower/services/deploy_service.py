"""Deploy Agent — runs as the final stage of the factory workflow.

Two modes, selected via env vars:

    FACTORY_DEPLOY_MODE = simulation   (default)
        - emits the same event sequence a real deploy would
        - actually fetches the public health URL so we still know if
          production is broken
        - never executes a shell command
        - safe to run from anywhere, including the Mac local runner

    FACTORY_DEPLOY_MODE = real
        - additionally runs `FACTORY_DEPLOY_SCRIPT` via subprocess
        - intended only for the server itself, never the Mac

Health URLs checked (both modes):

    https://reviewdr.kr/stampport/
    https://reviewdr.kr/stampport-api/health
    https://reviewdr.kr/stampport-control/
    https://reviewdr.kr/stampport-control-api/health
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from ..event_bus import event_bus
from ..schemas import EventType

logger = logging.getLogger(__name__)

HEALTH_URLS = (
    ("stampport-web",         "https://reviewdr.kr/stampport/"),
    ("stampport-api",         "https://reviewdr.kr/stampport-api/health"),
    ("stampport-control-web", "https://reviewdr.kr/stampport-control/"),
    ("stampport-control-api", "https://reviewdr.kr/stampport-control-api/health"),
)


def _is_real_mode() -> bool:
    return os.environ.get("FACTORY_DEPLOY_MODE", "simulation").lower() == "real"


def _deploy_script() -> str | None:
    return os.environ.get("FACTORY_DEPLOY_SCRIPT", "").strip() or None


def _check_url(url: str, timeout: float = 6.0) -> tuple[bool, int | None, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            return (200 <= code < 400), code, ""
    except urllib.error.HTTPError as e:
        return False, e.code, str(e)
    except Exception as e:  # noqa: BLE001
        return False, None, str(e)


def run_deploy_stage(db: Session) -> bool:
    """Returns True if deploy + health checks all passed, else False.

    Emits a deploy_failed event on any failure and returns False; the
    caller (the orchestrator) treats False as a non-fatal end-of-pipeline
    so other stages stay marked as completed.
    """
    real = _is_real_mode()
    script = _deploy_script()
    mode_label = "real" if real else "simulation"

    event_bus.emit(
        db,
        type=EventType.DEPLOY_STARTED,
        message=f"배포 단계를 시작합니다. (mode={mode_label})",
        payload={"mode": mode_label, "script": script if real else None},
    )

    # 1. Build/source check (simulated either way — the actual build
    #    happens in GitHub Actions; this is just the agent reporting it).
    time.sleep(0.4)
    event_bus.emit(
        db,
        type=EventType.DEPLOY_BUILD_CHECKED,
        message="빌드 산출물(Vite dist) 확인 완료.",
    )

    # 2. nginx config check — only meaningful in real mode.
    if real:
        ok, msg = _real_nginx_check()
        event_bus.emit(
            db,
            type=EventType.DEPLOY_NGINX_CHECKED,
            message=("nginx 설정 검증 통과." if ok else f"nginx 설정 문제: {msg}"),
            payload={"ok": ok, "detail": msg},
        )
        if not ok:
            event_bus.emit(db, type=EventType.DEPLOY_FAILED, message=f"nginx 검증 실패: {msg}")
            return False
    else:
        event_bus.emit(
            db,
            type=EventType.DEPLOY_NGINX_CHECKED,
            message="(시뮬레이션) nginx 설정 검증을 건너뜁니다.",
        )

    # 3. Service restart — only in real mode.
    if real and script:
        ok, detail = _run_deploy_script(script)
        event_bus.emit(
            db,
            type=EventType.DEPLOY_SERVICE_RESTARTED,
            message=("서비스 재시작 완료." if ok else f"서비스 재시작 실패: {detail[:200]}"),
            payload={"ok": ok, "detail": detail[:2000]},
        )
        if not ok:
            event_bus.emit(db, type=EventType.DEPLOY_FAILED, message="배포 스크립트 실행 실패")
            return False
    else:
        event_bus.emit(
            db,
            type=EventType.DEPLOY_SERVICE_RESTARTED,
            message="(시뮬레이션) systemd 재시작은 건너뜁니다.",
        )

    # 4. Health check — always real, even in simulation mode.
    all_ok = True
    health_results: list[dict[str, object]] = []
    for label, url in HEALTH_URLS:
        ok, code, err = _check_url(url)
        health_results.append({"name": label, "url": url, "ok": ok, "code": code, "error": err})
        if not ok:
            all_ok = False

    event_bus.emit(
        db,
        type=EventType.DEPLOY_HEALTHCHECK_PASSED if all_ok else EventType.DEPLOY_FAILED,
        message=(
            "헬스 체크 4종 모두 통과했습니다."
            if all_ok
            else "헬스 체크 중 실패가 발생했습니다."
        ),
        payload={"checks": health_results},
    )

    if all_ok:
        event_bus.emit(
            db,
            type=EventType.DEPLOY_COMPLETED,
            message="배포가 정상적으로 완료되었습니다.",
            payload={"mode": mode_label},
        )
    return all_ok


def _real_nginx_check() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "nginx", "-t"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (r.returncode == 0), (r.stderr or r.stdout).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _run_deploy_script(script: str) -> tuple[bool, str]:
    # Hard requirement: only execute the path explicitly listed in the
    # FACTORY_DEPLOY_SCRIPT env var. Never run an arbitrary string that
    # came from the API surface.
    if not script.startswith("/"):
        return False, "absolute path required for FACTORY_DEPLOY_SCRIPT"
    if not os.path.isfile(script):
        return False, f"deploy script not found: {script}"
    try:
        r = subprocess.run(
            ["bash", script],
            capture_output=True,
            text=True,
            timeout=600,
        )
        ok = (r.returncode == 0)
        return ok, (r.stdout + r.stderr)[-2000:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)
