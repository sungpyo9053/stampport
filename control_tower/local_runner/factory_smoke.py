"""Stampport Factory Smoke Runner — self-verifying acceptance entrypoint.

Purpose
-------
Replace the "operator stares at tail -f for 5 minutes and judges by feel"
loop with a single, scriptable command:

    python3 -m control_tower.local_runner.factory_smoke \
        --mode local-cycle --timeout 1800

The smoke runner performs preflight cleanup, drives a cycle (or
simulates one), polls .runtime state, applies timeouts per stage,
invokes the Observer, classifies the run into a final verdict
(PASS / FAIL / HOLD / READY_TO_REVIEW / READY_TO_PUBLISH), and emits
a Markdown report plus a Claude repair prompt when the run fails.

Outputs (always written, relative to .runtime/):
    factory_smoke_state.json     — machine-readable verdict + stage table
    factory_smoke_report.md      — human-readable summary
    factory_smoke.log            — chronological log of the smoke run
    factory_failure_report.md    — failure report (when verdict != PASS)
    claude_repair_prompt.md      — Claude-direct repair prompt (when failed)

CLI
---
    --mode local-cycle           Drive control_tower.local_runner.cycle as
                                 a subprocess and watch the run end-to-end.
    --mode bridge                Run the runner subprocess + verify the
                                 Control Tower bridge pause policy
                                 (desired=running + continuous=false MUST
                                 NOT produce a pause marker).
    --mode observer-only         Tick the Observer once, classify, and
                                 emit the report — no subprocess.
    --self-test                  Run built-in acceptance fixtures.
    --timeout SEC                Overall wall-clock cap (default 1800).
    --json                       Print the verdict JSON on stdout for
                                 scripted consumption.

Stdlib-only. Imports factory_observer (also stdlib-only) for shared
classification machinery; never imports runner.py / cycle.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import factory_observer as _observer


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _runtime_dir() -> Path:
    repo = Path(os.environ.get("LOCAL_RUNNER_REPO", str(Path.cwd())))
    return repo / ".runtime"


def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _state_path() -> Path:
    return _runtime_dir() / "factory_state.json"


def _log_path() -> Path:
    return _runtime_dir() / "local_factory.log"


def _smoke_state_path() -> Path:
    return _runtime_dir() / "factory_smoke_state.json"


def _smoke_report_path() -> Path:
    return _runtime_dir() / "factory_smoke_report.md"


def _smoke_log_path() -> Path:
    return _runtime_dir() / "factory_smoke.log"


def _failure_report_path() -> Path:
    return _runtime_dir() / "factory_failure_report.md"


def _claude_repair_path() -> Path:
    return _runtime_dir() / "claude_repair_prompt.md"


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _safe_write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        sys.stderr.write(f"[factory_smoke] write_json failed: {exc}\n")


def _safe_write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[factory_smoke] write_text failed: {exc}\n")


# ---------------------------------------------------------------------------
# Stage tracking
# ---------------------------------------------------------------------------


# Stage timeout policy — matches the spec in docs/factory-smoke.md.
# Stages NOT listed here inherit the global timeout.
STAGE_TIMEOUTS_SEC: dict[str, int] = {
    "git_check": 60,
    "publish_blocker_check": 60,
    "publish_blocker_resolve": 120,
    "product_planning": 600,
    "designer_critique": 360,
    "planner_revision": 360,
    "designer_final_review": 360,
    "pm_decision": 180,
    "build_app": 180,
    "build_control": 180,
    "syntax_check": 180,
    "claude_propose": 420,
    "implementation_ticket": 120,
    "claude_apply": 600,
    "qa_gate": 180,
    "qa_feedback": 180,
    "qa_fix_propose": 420,
    "qa_fix_apply": 600,
    "qa_recheck": 180,
}


# Verdicts the smoke test can emit. Order matters for verdict_summary().
VERDICTS = ("PASS", "READY_TO_REVIEW", "READY_TO_PUBLISH", "HOLD", "FAIL")


@dataclass
class StageObservation:
    name: str
    status: str = "pending"   # pending | running | passed | failed | skipped | timeout
    started_at: str | None = None
    finished_at: str | None = None
    duration_sec: float = 0.0
    timeout_sec: int = 0
    message: str = ""


@dataclass
class SmokeRun:
    mode: str
    timeout_sec: int
    started_at: str = ""
    finished_at: str = ""
    verdict: str = "FAIL"
    failure_code: str | None = None
    failure_reason: str | None = None
    last_successful_stage: str | None = None
    failed_stage: str | None = None
    stages: list[StageObservation] = field(default_factory=list)
    cycle_subprocess_exit: int | None = None
    cycle_id: int | None = None
    factory_status: str | None = None
    qa_status: str | None = None
    changed_files_count: int = 0
    ticket_status: str | None = None
    pm_decision_ship_ready: bool | None = None
    publish_executed: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _smoke_log(line: str) -> None:
    try:
        path = _smoke_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{_utc_now_iso()}] {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


# Files we move aside during preflight so the smoke runner doesn't
# misread leftover state from a previous run as the current one. We
# DO NOT delete them — operators want forensic access to the previous
# run, so we rename to *.prev.json in the same directory.
PREFLIGHT_RUNTIME_FILES: tuple[str, ...] = (
    "factory_state.json",
    "factory_observer_state.json",
    "factory_smoke_state.json",
    "factory_failure_report.md",
    "claude_repair_prompt.md",
    "factory_smoke_report.md",
    "factory_smoke.log",
    "auto_publish_request.json",
    "operator_request.md",
    "operator_request.json",
)


def _backup_one(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        backup = path.with_suffix(path.suffix + ".prev")
        if backup.exists():
            backup.unlink()
        path.rename(backup)
        return True
    except OSError:
        return False


def _detect_runner_processes() -> tuple[list[str], list[str]]:
    """Wrapper around the observer helper so the smoke runner doesn't
    duplicate ps-parsing logic."""
    return _observer.detect_runner_processes()


def preflight(mode: str, *, dry_run: bool = False) -> dict:
    """Run pre-cycle hygiene and return a summary dict.

    Returns:
        {
            "ok": bool,
            "duplicate_runner": bool,
            "runner_processes": list[str],
            "caffeinate_processes": list[str],
            "backed_up": list[str],
            "git_dirty": bool,
            "git_status_text": str,
            "warnings": list[str],
        }
    """
    out: dict[str, Any] = {
        "ok": True,
        "duplicate_runner": False,
        "runner_processes": [],
        "caffeinate_processes": [],
        "backed_up": [],
        "git_dirty": False,
        "git_status_text": "",
        "warnings": [],
    }

    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)

    # 1. Duplicate-runner gate. observer-only mode is informational —
    # we still report duplicates but don't refuse to run.
    py_procs, caff_procs = _detect_runner_processes()
    out["runner_processes"] = py_procs
    out["caffeinate_processes"] = caff_procs
    if len(py_procs) >= 2:
        out["duplicate_runner"] = True
        out["ok"] = False
        out["warnings"].append(
            f"duplicate_runner: {len(py_procs)} python runners detected"
        )

    # 2. Git status (informational). Avoid -uall because of memory issues
    # on large trees (the project's CLAUDE rule).
    git_ok, git_text = _run_git_status()
    out["git_status_text"] = git_text
    out["git_dirty"] = bool(git_ok and git_text.strip())

    # 3. Backup stale runtime files. local-cycle / bridge modes need a
    # clean state file so we can detect "the new cycle hasn't written
    # state yet" vs "the previous cycle's state is still around".
    if mode in {"local-cycle", "bridge"} and not dry_run:
        for name in PREFLIGHT_RUNTIME_FILES:
            target = runtime / name
            if _backup_one(target):
                out["backed_up"].append(str(target))

    # 4. Default-safe env. Smoke runs with auto-publish DISABLED unless
    # the operator explicitly enables it. We only set defaults — never
    # overwrite an explicit operator value.
    _set_default_env("LOCAL_RUNNER_ALLOW_PUBLISH", "false")
    _set_default_env("LOCAL_RUNNER_PUBLISH_DRY_RUN", "true")
    _set_default_env("LOCAL_RUNNER_RESTART_DRY_RUN", "true")
    _set_default_env("FACTORY_RUN_CLAUDE", "true")
    _set_default_env("FACTORY_APPLY_CLAUDE", "true")
    _set_default_env("FACTORY_PRODUCT_PLANNER_MODE", "true")
    _set_default_env("FACTORY_PLANNER_DESIGNER_PINGPONG", "true")
    if mode == "bridge":
        _set_default_env("FACTORY_WATCHDOG_ENABLED", "true")
    else:
        _set_default_env("FACTORY_WATCHDOG_ENABLED", "false")

    return out


def _set_default_env(name: str, default: str) -> None:
    if name not in os.environ:
        os.environ[name] = default


def _run_git_status() -> tuple[bool, str]:
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
        return res.returncode == 0, res.stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return False, f"git status failed: {exc}"


# ---------------------------------------------------------------------------
# Stage poller
# ---------------------------------------------------------------------------


def _read_factory_state() -> dict:
    return _read_json(_state_path()) or {}


def _read_log_tail(n_lines: int = 200) -> str:
    path = _log_path()
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-n_lines:])
    except OSError:
        return ""


def _stage_status_for_name(state: dict, name: str) -> str | None:
    for stage in state.get("stages") or []:
        if stage.get("name") == name:
            return stage.get("status")
    return None


# ---------------------------------------------------------------------------
# Verdict resolver
# ---------------------------------------------------------------------------


# Statuses that a healthy cycle can finish in.
PASS_FACTORY_STATUSES: frozenset[str] = frozenset({
    "succeeded", "completed",
})
READY_FACTORY_STATUSES: frozenset[str] = frozenset({
    "ready_to_review", "ready_to_publish",
})
HOLD_FACTORY_STATUSES: frozenset[str] = frozenset({
    "hold_for_rework", "needs_rework",
})


def resolve_verdict(state: dict, *, exit_code: int | None = None) -> tuple[str, str | None, str | None]:
    """Map factory_state.json + supplemental signals to a smoke verdict.

    Returns (verdict, failure_code, reason).

    Precedence (first match wins):
      1. Subprocess exit non-zero AND state says failed → FAIL
      2. factory_state.status in HOLD_FACTORY_STATUSES → HOLD
      3. factory_state.status in READY_FACTORY_STATUSES → READY_TO_REVIEW
         (or READY_TO_PUBLISH if publish is enabled)
      4. supervisor / state says ready_to_publish → READY_TO_PUBLISH
      5. factory_state.status in PASS_FACTORY_STATUSES + qa passed +
         changed_files > 0 → PASS / READY_TO_REVIEW depending on publish
         policy
      6. factory_state.status == "planning_only" / "no_code_change" →
         PASS (no failure but no code shipped — operator decision)
      7. anything else → FAIL with the appropriate failure_code
    """
    fs_status = (state.get("status") or "").strip()
    qa_status = (state.get("qa_status") or "").strip()
    apply_status = (state.get("claude_apply_status") or "").strip()
    changed = list(state.get("claude_apply_changed_files") or [])
    ticket_status = (state.get("implementation_ticket_status") or "").strip()

    publish_disabled = (
        os.environ.get("LOCAL_RUNNER_ALLOW_PUBLISH", "false").strip().lower()
        in {"", "false", "0", "no", "off"}
    )

    if exit_code is not None and exit_code != 0 and fs_status == "failed":
        return ("FAIL", "cycle_subprocess_failed",
                f"cycle exited {exit_code}, factory_state.status=failed")

    if fs_status in HOLD_FACTORY_STATUSES:
        return ("HOLD", "pm_hold_for_rework",
                "PM 결정이 HOLD — 의도적 재작업 사이클")

    if fs_status == "ready_to_review":
        return ("READY_TO_REVIEW", "ready_to_review",
                "코드 변경 + qa 통과, 자동 배포 비활성 — 사람 리뷰 대기")

    if fs_status == "ready_to_publish":
        if publish_disabled:
            return ("READY_TO_REVIEW", "publish_disabled",
                    "ready_to_publish but LOCAL_RUNNER_ALLOW_PUBLISH=false")
        return ("READY_TO_PUBLISH", "publish_required",
                "code shipped + qa passed — publish 명령 대기")

    if fs_status in PASS_FACTORY_STATUSES:
        # Differentiate: code-changed cycle (real PASS) vs no-op
        # supervised "completed".
        if (
            apply_status == "applied"
            and len(changed) > 0
            and qa_status == "passed"
            and ticket_status == "generated"
        ):
            if publish_disabled:
                return ("READY_TO_REVIEW", "publish_disabled",
                        f"코드 변경 {len(changed)}개 + QA 통과 — 자동 배포 비활성")
            return ("READY_TO_PUBLISH", "publish_required",
                    f"코드 변경 {len(changed)}개 + QA 통과 — publish 명령 대기")
        return ("PASS", None, "factory cycle succeeded")

    if fs_status in {"planning_only", "no_code_change", "docs_only"}:
        return ("PASS", None,
                f"cycle ended with status={fs_status} (no failure)")

    if fs_status == "paused":
        return ("FAIL", "factory_paused",
                "factory.paused marker present — cycle did not start")

    if fs_status == "failed":
        return ("FAIL", "cycle_failed",
                state.get("failed_reason") or "factory_state.status=failed")

    if fs_status == "running":
        return ("FAIL", "current_stage_stuck",
                f"cycle still running at smoke timeout — current_stage="
                f"{state.get('current_stage')}")

    # No factory_state.json or unknown status. Fresh runtime is not a
    # failure if mode=observer-only, but for local-cycle / bridge we
    # expected SOME state. Caller decides — return a neutral FAIL with
    # fresh_idle code so the report explains the situation clearly.
    if not fs_status:
        return ("FAIL", "fresh_idle",
                "factory_state.json absent or empty — cycle did not run")
    return ("FAIL", "unknown",
            f"unrecognized factory_state.status={fs_status}")


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------


def _spawn_cycle_subprocess(timeout_sec: int) -> subprocess.Popen:
    """Spawn `python3 -m control_tower.local_runner.cycle` with stdout
    redirected to .runtime/factory_smoke.log so the operator has a
    single tail target."""
    smoke_log = _smoke_log_path()
    smoke_log.parent.mkdir(parents=True, exist_ok=True)
    fh = smoke_log.open("ab")
    return subprocess.Popen(
        [sys.executable, "-m", "control_tower.local_runner.cycle"],
        stdout=fh, stderr=subprocess.STDOUT,
        cwd=os.environ.get("LOCAL_RUNNER_REPO") or os.getcwd(),
    )


def _wait_for_state_file(timeout_sec: int = 30) -> bool:
    """Wait for factory_state.json to appear after spawning cycle.py."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _state_path().is_file():
            return True
        time.sleep(0.5)
    return False


def _poll_cycle(
    proc: subprocess.Popen,
    *,
    overall_deadline: float,
) -> tuple[int | None, list[StageObservation]]:
    """Poll factory_state.json + the subprocess until completion or
    timeout. Returns (exit_code, stage_observations).

    Per-stage timeouts apply BUT only as an early-exit signal — we
    record a "timeout" status on a stage that exceeds its budget and
    keep polling until the overall deadline. If the cycle naturally
    finishes shortly after a stage timeout, we still surface the
    stage-level timeout in the report.
    """
    observed: dict[str, StageObservation] = {}
    last_stage: str | None = None
    last_stage_started_at: float | None = None

    while True:
        # 1. Subprocess exit?
        rc = proc.poll()
        if rc is not None:
            return rc, _finalize_observations(observed)

        # 2. Overall deadline?
        if time.time() >= overall_deadline:
            _smoke_log(f"overall deadline hit while waiting for cycle (rc=None)")
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
            return None, _finalize_observations(observed)

        # 3. Inspect factory_state.json. The cycle.py loop overwrites
        # state.current_stage on every stage transition so we can read
        # it directly.
        state = _read_factory_state()
        current = (state.get("current_stage") or "").strip()
        if current and current != last_stage:
            now = time.time()
            # Close the previous stage's observation.
            if last_stage and last_stage in observed:
                obs = observed[last_stage]
                if obs.status == "running":
                    obs.status = "passed"
                    obs.finished_at = _utc_now_iso()
                    if last_stage_started_at:
                        obs.duration_sec = round(now - last_stage_started_at, 3)
            # Open a new observation for the current stage.
            obs = observed.setdefault(
                current,
                StageObservation(name=current,
                                 timeout_sec=STAGE_TIMEOUTS_SEC.get(current, 0)),
            )
            obs.status = "running"
            obs.started_at = obs.started_at or _utc_now_iso()
            last_stage = current
            last_stage_started_at = now
            _smoke_log(f"stage transition → {current}")

        # 4. Per-stage timeout check (advisory).
        if last_stage and last_stage_started_at:
            budget = STAGE_TIMEOUTS_SEC.get(last_stage, 0)
            if budget and (time.time() - last_stage_started_at) > budget:
                obs = observed[last_stage]
                if obs.status == "running":
                    obs.status = "timeout"
                    obs.message = (
                        f"stage exceeded its budget ({budget}s) — still polling"
                    )
                    _smoke_log(
                        f"stage {last_stage} exceeded budget {budget}s"
                    )

        time.sleep(2.0)


def _finalize_observations(observed: dict[str, StageObservation]) -> list[StageObservation]:
    out = list(observed.values())
    # Last running stage gets closed.
    for obs in out:
        if obs.status == "running":
            obs.status = "passed"
            obs.finished_at = _utc_now_iso()
    return out


def run_local_cycle(timeout_sec: int) -> SmokeRun:
    """Drive the cycle subprocess and watch it run end-to-end."""
    run = SmokeRun(mode="local-cycle", timeout_sec=timeout_sec)
    run.started_at = _utc_now_iso()
    _smoke_log(f"local-cycle smoke started — timeout={timeout_sec}s")

    pre = preflight("local-cycle")
    if not pre["ok"]:
        run.verdict = "FAIL"
        run.failure_code = "preflight_failed"
        run.failure_reason = "; ".join(pre["warnings"]) or "preflight failed"
        run.finished_at = _utc_now_iso()
        return _finalize_run(run, factory_state={}, observer_classification=None)

    deadline = time.time() + timeout_sec
    try:
        proc = _spawn_cycle_subprocess(timeout_sec)
    except OSError as exc:
        run.verdict = "FAIL"
        run.failure_code = "cycle_spawn_failed"
        run.failure_reason = f"could not spawn cycle subprocess: {exc}"
        run.finished_at = _utc_now_iso()
        return _finalize_run(run, factory_state={}, observer_classification=None)

    rc, observed = _poll_cycle(proc, overall_deadline=deadline)
    run.cycle_subprocess_exit = rc
    run.stages = observed

    if rc is None:
        run.verdict = "FAIL"
        run.failure_code = "smoke_timeout"
        run.failure_reason = (
            f"smoke wall-clock timeout ({timeout_sec}s) before cycle exit"
        )
        run.finished_at = _utc_now_iso()
        state = _read_factory_state()
        return _finalize_run(run, factory_state=state, observer_classification=None)

    state = _read_factory_state()
    verdict, code, reason = resolve_verdict(state, exit_code=rc)
    run.verdict = verdict
    run.failure_code = code if verdict == "FAIL" else None
    run.failure_reason = reason
    run.finished_at = _utc_now_iso()

    classification = _observer.tick() if verdict == "FAIL" else None
    return _finalize_run(run, factory_state=state,
                         observer_classification=classification)


def run_bridge(timeout_sec: int) -> SmokeRun:
    """Verify the runner's bridge pause policy.

    Acceptance: with desired=running + continuous=false, the runner
    must NOT write factory.paused / factory.continuous_pause and must
    NOT log "factory bridge · pause applied (... desired=running)".

    This mode does NOT actually run runner.py end-to-end (which would
    require a live API server). Instead it inspects the existing
    .runtime/local_factory.log for the bad pattern and verifies the
    pause markers are absent.
    """
    run = SmokeRun(mode="bridge", timeout_sec=timeout_sec)
    run.started_at = _utc_now_iso()
    pre = preflight("bridge")

    runtime = _runtime_dir()
    pause_marker = runtime / "factory.paused"
    continuous_pause = runtime / "factory.continuous_pause"
    log_tail = _read_log_tail(400)

    bad = _observer._looks_like_bridge_pause_mismatch(log_tail)
    pause_present = pause_marker.exists() or continuous_pause.exists()

    if bad:
        run.verdict = "FAIL"
        run.failure_code = "bridge_pause_mismatch"
        run.failure_reason = (
            "log contains 'pause applied (... desired=running)' — runner "
            "policy violation"
        )
    elif pause_present:
        run.verdict = "FAIL"
        run.failure_code = "bridge_pause_marker_present"
        run.failure_reason = (
            "factory.paused / factory.continuous_pause exists — bridge "
            "should not pause when desired=running"
        )
    else:
        run.verdict = "PASS"
        run.failure_reason = (
            "no pause markers, no bad pause-applied log lines"
        )

    run.notes.append(f"runner_processes={len(pre['runner_processes'])}")
    run.notes.append(f"caffeinate_processes={len(pre['caffeinate_processes'])}")
    run.finished_at = _utc_now_iso()
    return _finalize_run(run, factory_state=_read_factory_state(),
                         observer_classification=None)


def run_observer_only() -> SmokeRun:
    """Tick the Observer once and surface its classification."""
    run = SmokeRun(mode="observer-only", timeout_sec=0)
    run.started_at = _utc_now_iso()
    classification = _observer.tick()
    code = (classification or {}).get("diagnostic_code")
    is_fail = bool((classification or {}).get("is_failure"))
    cat = (classification or {}).get("category")

    if cat == "healthy":
        run.verdict = "PASS"
    elif cat == "review":
        run.verdict = "READY_TO_REVIEW"
    elif cat == "hold":
        run.verdict = "HOLD"
    elif is_fail:
        run.verdict = "FAIL"
        run.failure_code = code
        run.failure_reason = (classification or {}).get("root_cause") or code
    else:
        run.verdict = "PASS"

    run.finished_at = _utc_now_iso()
    return _finalize_run(run, factory_state=_read_factory_state(),
                         observer_classification=classification)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _finalize_run(
    run: SmokeRun,
    *,
    factory_state: dict,
    observer_classification: dict | None,
) -> SmokeRun:
    run.cycle_id = factory_state.get("cycle")
    run.factory_status = factory_state.get("status")
    run.qa_status = factory_state.get("qa_status")
    run.changed_files_count = len(
        factory_state.get("claude_apply_changed_files") or []
    )
    run.ticket_status = factory_state.get("implementation_ticket_status")
    if "pm_decision_ship_ready" in factory_state:
        run.pm_decision_ship_ready = bool(
            factory_state.get("pm_decision_ship_ready")
        )

    # last successful stage / failed stage
    for obs in run.stages:
        if obs.status == "passed":
            run.last_successful_stage = obs.name
        if obs.status in {"failed", "timeout"} and not run.failed_stage:
            run.failed_stage = obs.name

    write_outputs(run, factory_state, observer_classification)
    return run


def write_outputs(
    run: SmokeRun,
    factory_state: dict,
    observer_classification: dict | None,
) -> None:
    _safe_write_json(_smoke_state_path(), _serialize_run(run))
    _safe_write_text(_smoke_report_path(), _build_report(run, factory_state))
    if run.verdict == "FAIL":
        _safe_write_text(
            _failure_report_path(),
            _build_failure_report(run, observer_classification),
        )
        _safe_write_text(
            _claude_repair_path(),
            _build_repair_prompt(run, observer_classification),
        )


def _serialize_run(run: SmokeRun) -> dict:
    return {
        "schema_version": 1,
        "mode": run.mode,
        "timeout_sec": run.timeout_sec,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "verdict": run.verdict,
        "failure_code": run.failure_code,
        "failure_reason": run.failure_reason,
        "last_successful_stage": run.last_successful_stage,
        "failed_stage": run.failed_stage,
        "cycle_subprocess_exit": run.cycle_subprocess_exit,
        "cycle_id": run.cycle_id,
        "factory_status": run.factory_status,
        "qa_status": run.qa_status,
        "changed_files_count": run.changed_files_count,
        "ticket_status": run.ticket_status,
        "pm_decision_ship_ready": run.pm_decision_ship_ready,
        "publish_executed": run.publish_executed,
        "stages": [_serialize_stage(s) for s in run.stages],
        "notes": list(run.notes),
    }


def _serialize_stage(s: StageObservation) -> dict:
    return {
        "name": s.name,
        "status": s.status,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "duration_sec": s.duration_sec,
        "timeout_sec": s.timeout_sec,
        "message": s.message,
    }


def _build_report(run: SmokeRun, factory_state: dict) -> str:
    lines = [
        "# Stampport Factory Smoke Report",
        "",
        f"- 모드: `{run.mode}`",
        f"- 시작: `{run.started_at}`",
        f"- 종료: `{run.finished_at}`",
        f"- Verdict: **{run.verdict}**",
    ]
    if run.failure_code:
        lines.append(f"- Failure code: `{run.failure_code}`")
    if run.failure_reason:
        lines.append(f"- Reason: {run.failure_reason}")

    lines += [
        "",
        "## Cycle context",
        f"- cycle_id: `{run.cycle_id}`",
        f"- factory_state.status: `{run.factory_status}`",
        f"- qa_status: `{run.qa_status}`",
        f"- changed_files_count: `{run.changed_files_count}`",
        f"- implementation_ticket_status: `{run.ticket_status}`",
        f"- pm_decision_ship_ready: `{run.pm_decision_ship_ready}`",
        f"- cycle subprocess exit: `{run.cycle_subprocess_exit}`",
        f"- 자동 배포: `LOCAL_RUNNER_ALLOW_PUBLISH={os.environ.get('LOCAL_RUNNER_ALLOW_PUBLISH', '(unset)')}` "
        f"— commit/push 실행 여부: `{run.publish_executed}`",
        "",
        "## Stage table",
    ]
    if run.stages:
        lines.append("| Stage | Status | Duration (s) | Budget (s) | Message |")
        lines.append("|-------|--------|--------------|------------|---------|")
        for s in run.stages:
            msg = (s.message or "").replace("|", "\\|")[:60]
            lines.append(
                f"| `{s.name}` | {s.status} | {s.duration_sec} | "
                f"{s.timeout_sec or '—'} | {msg} |"
            )
    else:
        lines.append("- (no stage transitions observed)")

    lines += [
        "",
        "## Last successful stage",
        f"- `{run.last_successful_stage or '—'}`",
        "",
        "## Failed / blocked stage",
        f"- `{run.failed_stage or '—'}`",
        "",
        "## Operator next action",
    ]
    lines.extend(_recommend_next(run))

    lines += [
        "",
        "## Output files",
        f"- 상태: `.runtime/factory_smoke_state.json`",
        f"- 리포트: `.runtime/factory_smoke_report.md`",
        f"- 로그: `.runtime/factory_smoke.log`",
    ]
    if run.verdict == "FAIL":
        lines += [
            f"- 실패 리포트: `.runtime/factory_failure_report.md`",
            f"- Claude repair prompt: `.runtime/claude_repair_prompt.md`",
        ]
    if run.notes:
        lines += ["", "## Notes"]
        lines.extend(f"- {n}" for n in run.notes)
    return "\n".join(lines) + "\n"


def _recommend_next(run: SmokeRun) -> list[str]:
    if run.verdict == "PASS":
        return [
            "- 사이클이 정상 종료되었습니다. 변경 파일이 있다면 사람 리뷰 후 publish 결정.",
        ]
    if run.verdict in {"READY_TO_REVIEW", "READY_TO_PUBLISH"}:
        return [
            f"- 코드 변경 {run.changed_files_count}개 + QA 통과. "
            "자동 배포가 꺼져 있어 사람 리뷰 대기 상태입니다.",
            "- `git diff` / `.runtime/claude_apply.diff` 확인 후 운영자 판단으로 commit/push.",
            "- 자동 commit/push 를 원하면 `LOCAL_RUNNER_ALLOW_PUBLISH=true` 로 다시 실행.",
        ]
    if run.verdict == "HOLD":
        return [
            "- PM 결정이 HOLD — 디자이너/기획자 rework 항목을 반영해 다음 사이클을 진행하세요.",
            "- `pm_decision.md` / `designer_final_review.md` 참고.",
        ]
    # FAIL
    if run.failure_code == "smoke_timeout":
        return [
            f"- factory_smoke 가 timeout({run.timeout_sec}s) 초과로 종료. "
            "stuck 단계 / Claude 호출 hang 가능성.",
            "- `last_successful_stage`={} 의 다음 stage 를 의심하세요.".format(
                run.last_successful_stage or "—"
            ),
            "- `tail -200 .runtime/factory_smoke.log` 로 실제 출력 확인.",
        ]
    return [
        "- `.runtime/factory_failure_report.md` 와 `.runtime/claude_repair_prompt.md` 확인.",
        "- Observer 가 진단한 코드: `{}`".format(run.failure_code or "—"),
    ]


def _build_failure_report(
    run: SmokeRun,
    classification: dict | None,
) -> str:
    lines = [
        "# Stampport Factory Failure Report",
        "",
        f"- 시각: `{_utc_now_iso()}`",
        f"- Smoke verdict: **{run.verdict}**",
        f"- Failure code: `{run.failure_code}`",
        f"- Reason: {run.failure_reason}",
        f"- last_successful_stage: `{run.last_successful_stage or '—'}`",
        f"- failed_stage: `{run.failed_stage or '—'}`",
        "",
    ]
    if classification:
        lines += [
            "## Observer classification",
            f"- diagnostic_code: `{classification.get('diagnostic_code')}`",
            f"- severity: `{classification.get('severity')}`",
            f"- category: `{classification.get('category')}`",
            f"- root_cause: {classification.get('root_cause')}",
            "",
            "### Evidence",
        ]
        for e in (classification.get("evidence") or [])[:12]:
            lines.append(f"- {e}")
        lines.append("")
    lines += [
        "## Stage table",
        "| Stage | Status | Duration (s) | Budget (s) |",
        "|-------|--------|--------------|------------|",
    ]
    for s in run.stages:
        lines.append(
            f"| `{s.name}` | {s.status} | {s.duration_sec} | "
            f"{s.timeout_sec or '—'} |"
        )
    return "\n".join(lines) + "\n"


def _build_repair_prompt(
    run: SmokeRun,
    classification: dict | None,
) -> str:
    code = run.failure_code or (
        (classification or {}).get("diagnostic_code") or "unknown"
    )
    targets = _observer.REPAIR_TARGETS_BY_CODE.get(code) or [
        "control_tower/local_runner/factory_smoke.py",
        "control_tower/local_runner/factory_observer.py",
    ]
    requirements = _observer.REPAIR_REQUIREMENTS_BY_CODE.get(code) or (
        "1. .runtime/factory_smoke_report.md 의 stage table 에서 stuck 위치를 확인.\n"
        "2. 해당 stage 의 cycle.py 코드와 .runtime/local_factory.log 의 마지막 출력을 비교.\n"
        "3. fix 후 `python3 -m control_tower.local_runner.factory_smoke --self-test` 통과 확인."
    )
    lines = [
        "# Stampport Factory Smoke — Claude Repair Prompt",
        "",
        f"Smoke verdict: **{run.verdict}** · failure_code: `{code}`",
        "",
        f"## 문제 (factory_smoke 에서 자동 분류)",
        run.failure_reason or "—",
        "",
        "## 수정 대상",
    ]
    for t in targets:
        lines.append(f"- {t}")
    lines += [
        "",
        "## 요구 사항",
        requirements,
        "",
        "## 검증",
        "1. `python3 -m control_tower.local_runner.factory_observer --self-test`",
        "2. `python3 -m control_tower.local_runner.factory_smoke --self-test`",
        f"3. `python3 -m control_tower.local_runner.factory_smoke --mode {run.mode} "
        f"--timeout {run.timeout_sec or 1800}`",
        "",
        "## 컨텍스트",
    ]
    if classification:
        lines.append(f"- Observer code: `{classification.get('diagnostic_code')}`")
        lines.append(f"- Observer root_cause: {classification.get('root_cause')}")
    lines.append(f"- last_successful_stage: `{run.last_successful_stage or '—'}`")
    lines.append(f"- failed_stage: `{run.failed_stage or '—'}`")
    lines.append(f"- factory_state.status: `{run.factory_status}`")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Filtering helpers (used by acceptance fixtures + future runner work)
# ---------------------------------------------------------------------------


_GIT_IGNORED_PATH_FRAGMENTS: tuple[str, ...] = (
    "__pycache__",
    ".pyc",
    ".pyo",
    "/.runtime/",
    ".runtime/",
    "node_modules/",
    "dist/",
    "build/",
)


def filter_git_addable_paths(paths: list[str]) -> list[str]:
    """Drop paths that should never be passed to `git add` regardless of
    .gitignore. This is the same filter the smoke runner expects the
    runner / cycle to apply before calling git_add."""
    out: list[str] = []
    for p in paths:
        norm = p.replace("\\", "/")
        if any(frag in norm for frag in _GIT_IGNORED_PATH_FRAGMENTS):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def self_test() -> tuple[int, int, list[str]]:
    """Return (passed, total, failure_messages).

    Acceptance fixtures matching the smoke spec — every numbered case
    in the spec maps to one fixture below.
    """
    failures: list[str] = []
    passed = 0
    total = 0

    # 1. fresh runtime → idle / ready, NOT blocked.
    total += 1
    fresh_state: dict = {}  # no factory_state.json content
    verdict, code, reason = resolve_verdict(fresh_state, exit_code=None)
    obs_state = _observer._empty_state()
    obs_state["control_state"] = {"liveness": {"runner_online": True}}
    obs_class = _observer.classify(
        obs_state,
        runner_processes=["fake python -m control_tower.local_runner.runner"],
        caffeinate_processes=[],
    )
    if (
        verdict == "FAIL" and code == "fresh_idle"
        and obs_class["diagnostic_code"] == "fresh_idle"
        and obs_class["is_failure"] is False
    ):
        passed += 1
    else:
        failures.append(
            f"1: fresh runtime — verdict={verdict}/{code}, "
            f"observer={obs_class['diagnostic_code']}/{obs_class['is_failure']}"
        )

    # 2. desired=running + continuous=false → no pause applied (observer
    # NOT classifying as bridge_pause_mismatch when log is clean).
    total += 1
    obs_state = _observer._empty_state()
    obs_state["control_state"] = {"liveness": {"runner_online": True}}
    obs_state["log_tail"] = (
        "[2026-05-02T01:00:00Z] factory bridge · run requested "
        "(desired=running, continuous=False)\n"
    )
    obs_class = _observer.classify(
        obs_state,
        runner_processes=["fake python -m control_tower.local_runner.runner"],
        caffeinate_processes=[],
    )
    if obs_class["diagnostic_code"] != "bridge_pause_mismatch":
        passed += 1
    else:
        failures.append(
            "2: desired=running + continuous=false should NOT trigger "
            "bridge_pause_mismatch (log clean of bad pattern)"
        )

    # 3. PM HOLD → hold_for_rework, dev stages must NOT run.
    total += 1
    state = {
        "status": "hold_for_rework",
        "cycle": 1,
        "pm_decision_status": "generated",
        "pm_decision_ship_ready": False,
        "claude_proposal_status": "skipped",
        "claude_apply_status": "skipped",
        "implementation_ticket_status": "skipped_hold",
    }
    verdict, code, reason = resolve_verdict(state, exit_code=0)
    if verdict == "HOLD" and code == "pm_hold_for_rework":
        passed += 1
    else:
        failures.append(
            f"3: PM HOLD → expected HOLD/pm_hold_for_rework, got {verdict}/{code}"
        )

    # 4. PM SHIP + target_files → ticket generated.
    total += 1
    state = {
        "status": "succeeded",
        "cycle": 2,
        "pm_decision_status": "generated",
        "pm_decision_ship_ready": True,
        "implementation_ticket_status": "generated",
        "claude_apply_status": "applied",
        "claude_apply_changed_files": ["app/web/src/screens/Foo.jsx"],
        "qa_status": "passed",
    }
    prev = os.environ.pop("LOCAL_RUNNER_ALLOW_PUBLISH", None)
    try:
        verdict, code, reason = resolve_verdict(state, exit_code=0)
    finally:
        if prev is not None:
            os.environ["LOCAL_RUNNER_ALLOW_PUBLISH"] = prev
    if verdict == "READY_TO_REVIEW" and state["implementation_ticket_status"] == "generated":
        passed += 1
    else:
        failures.append(
            f"4: PM SHIP + ticket generated → expected READY_TO_REVIEW, "
            f"got {verdict}/{code}"
        )

    # 5. changed_files=3 + qa=passed + publish disabled → READY_TO_REVIEW,
    # no git push.
    total += 1
    state = {
        "status": "ready_to_publish",
        "cycle": 3,
        "claude_apply_status": "applied",
        "claude_apply_changed_files": ["a.py", "b.py", "c.py"],
        "qa_status": "passed",
        "implementation_ticket_status": "generated",
    }
    prev = os.environ.pop("LOCAL_RUNNER_ALLOW_PUBLISH", None)
    try:
        verdict, code, reason = resolve_verdict(state, exit_code=0)
    finally:
        if prev is not None:
            os.environ["LOCAL_RUNNER_ALLOW_PUBLISH"] = prev
    if verdict == "READY_TO_REVIEW":
        passed += 1
    else:
        failures.append(
            f"5: changed_files=3 + publish disabled → expected READY_TO_REVIEW, "
            f"got {verdict}/{code}"
        )

    # 6. __pycache__ in changed_files → excluded from git_add candidates.
    total += 1
    candidates = [
        "app/web/src/foo.jsx",
        "app/api/app/__pycache__/main.cpython-311.pyc",
        "control_tower/local_runner/__pycache__/cycle.cpython-311.pyc",
        ".runtime/factory_state.json",
        "node_modules/foo/index.js",
        "control_tower/web/src/App.tsx",
    ]
    filtered = filter_git_addable_paths(candidates)
    if (
        "app/web/src/foo.jsx" in filtered
        and "control_tower/web/src/App.tsx" in filtered
        and not any("__pycache__" in p for p in filtered)
        and not any(p.endswith(".pyc") for p in filtered)
        and not any(".runtime" in p for p in filtered)
        and not any("node_modules" in p for p in filtered)
    ):
        passed += 1
    else:
        failures.append(f"6: filter_git_addable_paths produced {filtered}")

    # 7. stale old deploy failed must NOT contaminate latest ready_to_review.
    total += 1
    obs_state = _observer._empty_state()
    obs_state["factory_state"] = {
        "status": "ready_to_publish",
        "cycle": 5,
        "claude_apply_status": "applied",
        "claude_apply_changed_files": ["x.py"],
        "qa_status": "passed",
    }
    obs_state["control_state"] = {
        "status": "ready_to_publish",
        "deploy": {
            "changed_files_count": 1, "qa_status": "passed",
            "commit_hash": None, "push_status": None,
            "status": "ready",
        },
        "liveness": {"runner_online": True},
    }
    obs_state["deploy_progress"] = {
        "status": "failed", "failed_stage": "git_push",
    }
    prev = os.environ.pop("LOCAL_RUNNER_ALLOW_PUBLISH", None)
    try:
        obs_class = _observer.classify(
            obs_state,
            runner_processes=["fake python -m control_tower.local_runner.runner"],
            caffeinate_processes=[],
        )
    finally:
        if prev is not None:
            os.environ["LOCAL_RUNNER_ALLOW_PUBLISH"] = prev
    # ready_to_review must win over the stale old_deploy_failed_stale signal.
    if obs_class["diagnostic_code"] == "ready_to_review":
        passed += 1
    else:
        failures.append(
            f"7: stale deploy failed contaminated latest review — got "
            f"{obs_class['diagnostic_code']}"
        )

    # 8. bridge_pause_mismatch precisely classified.
    total += 1
    obs_state = _observer._empty_state()
    obs_state["control_state"] = {"liveness": {"runner_online": True}}
    obs_state["log_tail"] = (
        "[2026-05-01T01:00:00Z] factory bridge · pause applied "
        "(continuous=False, desired=running)\n"
    )
    obs_class = _observer.classify(
        obs_state,
        runner_processes=["fake python -m control_tower.local_runner.runner"],
        caffeinate_processes=[],
    )
    if obs_class["diagnostic_code"] == "bridge_pause_mismatch":
        passed += 1
    else:
        failures.append(
            f"8: expected bridge_pause_mismatch, got {obs_class['diagnostic_code']}"
        )

    # 9. smoke timeout → smoke_timeout verdict + report contains repair prompt.
    total += 1
    run = SmokeRun(mode="local-cycle", timeout_sec=10)
    run.started_at = _utc_now_iso()
    run.finished_at = _utc_now_iso()
    run.verdict = "FAIL"
    run.failure_code = "smoke_timeout"
    run.failure_reason = "test fixture: timeout simulation"
    run.last_successful_stage = "product_planning"
    run.failed_stage = "designer_critique"
    run.stages = [
        StageObservation(
            name="product_planning", status="passed",
            duration_sec=5.0, timeout_sec=600,
        ),
        StageObservation(
            name="designer_critique", status="timeout",
            duration_sec=361.0, timeout_sec=360,
            message="exceeded budget",
        ),
    ]
    repair = _build_repair_prompt(run, None)
    if (
        "smoke_timeout" in repair
        and "designer_critique" in repair
        and "factory_smoke --self-test" in repair
    ):
        passed += 1
    else:
        failures.append(
            "9: smoke_timeout repair prompt missing required content"
        )

    # 10. local-cycle mock fixture — verdict resolves to PASS / READY / HOLD.
    total += 1
    cases: tuple[tuple[dict, str], ...] = (
        (
            {
                "status": "succeeded", "cycle": 11,
                "claude_apply_status": "applied",
                "claude_apply_changed_files": ["a.py"],
                "qa_status": "passed",
                "implementation_ticket_status": "generated",
            },
            "READY_TO_REVIEW",  # publish disabled by default
        ),
        (
            {"status": "hold_for_rework", "cycle": 12},
            "HOLD",
        ),
        (
            {"status": "planning_only", "cycle": 13},
            "PASS",
        ),
        (
            {"status": "no_code_change", "cycle": 14},
            "PASS",
        ),
    )
    prev = os.environ.pop("LOCAL_RUNNER_ALLOW_PUBLISH", None)
    try:
        all_ok = True
        for state, expected in cases:
            v, _c, _r = resolve_verdict(state, exit_code=0)
            if v != expected:
                all_ok = False
                failures.append(
                    f"10: local-cycle mock — state={state['status']} expected "
                    f"{expected}, got {v}"
                )
                break
    finally:
        if prev is not None:
            os.environ["LOCAL_RUNNER_ALLOW_PUBLISH"] = prev
    if all_ok:
        passed += 1

    # 11. write_outputs always emits factory_smoke_state.json + report.
    total += 1
    repo = os.environ.get("LOCAL_RUNNER_REPO")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCAL_RUNNER_REPO"] = tmp
        try:
            run = SmokeRun(mode="observer-only", timeout_sec=0)
            run.started_at = _utc_now_iso()
            run.finished_at = _utc_now_iso()
            run.verdict = "PASS"
            write_outputs(run, factory_state={}, observer_classification=None)
            sp = Path(tmp) / ".runtime" / "factory_smoke_state.json"
            rp = Path(tmp) / ".runtime" / "factory_smoke_report.md"
            if sp.is_file() and rp.is_file():
                passed += 1
            else:
                failures.append(
                    f"11: write_outputs missing — state={sp.is_file()} "
                    f"report={rp.is_file()}"
                )
        finally:
            if repo is not None:
                os.environ["LOCAL_RUNNER_REPO"] = repo
            else:
                os.environ.pop("LOCAL_RUNNER_REPO", None)

    return passed, total, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory_smoke",
        description="Stampport factory self-verifying smoke runner.",
    )
    parser.add_argument(
        "--mode",
        choices=("local-cycle", "bridge", "observer-only"),
        default="observer-only",
        help="What to drive (default: observer-only).",
    )
    parser.add_argument(
        "--timeout", type=int, default=1800,
        help="Overall wall-clock cap in seconds (default 1800).",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run built-in acceptance fixtures and exit.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit verdict JSON to stdout (machine-readable).",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        passed, total, fails = self_test()
        print(f"[factory_smoke self-test] {passed}/{total} passed")
        for msg in fails:
            print(f"  FAIL · {msg}")
        return 0 if passed == total else 1

    if args.mode == "local-cycle":
        run = run_local_cycle(args.timeout)
    elif args.mode == "bridge":
        run = run_bridge(args.timeout)
    else:
        run = run_observer_only()

    if args.json:
        print(json.dumps(_serialize_run(run), ensure_ascii=False, indent=2))
    else:
        _print_summary(run)

    if run.verdict in {"PASS", "READY_TO_REVIEW", "READY_TO_PUBLISH", "HOLD"}:
        return 0
    return 1


def _print_summary(run: SmokeRun) -> None:
    print(f"[factory_smoke] mode={run.mode} verdict={run.verdict}")
    if run.failure_code:
        print(f"  failure_code={run.failure_code}")
    if run.failure_reason:
        print(f"  reason={run.failure_reason}")
    print(f"  last_successful_stage={run.last_successful_stage or '—'}")
    print(f"  factory_state.status={run.factory_status or '—'}")
    print(f"  changed_files={run.changed_files_count}")
    print(f"  report=.runtime/factory_smoke_report.md")
    print(f"  state=.runtime/factory_smoke_state.json")
    if run.verdict == "FAIL":
        print(f"  failure=.runtime/factory_failure_report.md")
        print(f"  repair=.runtime/claude_repair_prompt.md")


if __name__ == "__main__":
    raise SystemExit(main())
