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


def _claude_rework_path() -> Path:
    return _runtime_dir() / "claude_rework_prompt.md"


def _smoke_history_path() -> Path:
    return _runtime_dir() / "factory_smoke_history.jsonl"


def _pm_decision_path() -> Path:
    return _runtime_dir() / "pm_decision.md"


def _designer_final_review_path() -> Path:
    return _runtime_dir() / "designer_final_review.md"


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
    # design_spec / spec-mode signals captured from factory_state.json
    # at finalize time. Surfaced in factory_smoke_state.json so the
    # dashboard / observer don't have to re-parse the cycle's state.
    pm_hold_spec_keywords: list[str] = field(default_factory=list)
    design_spec_status: str | None = None
    design_spec_acceptance_passed: bool | None = None
    design_spec_target_files_count: int = 0
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
    "claude_rework_prompt.md",
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
    spec_kw = factory_state.get("pm_hold_spec_keywords") or []
    if isinstance(spec_kw, list):
        run.pm_hold_spec_keywords = list(spec_kw)
    run.design_spec_status = factory_state.get("design_spec_status")
    if factory_state.get("design_spec_acceptance_passed") is not None:
        run.design_spec_acceptance_passed = bool(
            factory_state.get("design_spec_acceptance_passed")
        )
    target_files = factory_state.get("design_spec_target_files") or []
    if isinstance(target_files, list):
        run.design_spec_target_files_count = len(target_files)

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
    prior_history = _load_history()
    entry = _build_history_entry(run, factory_state, prior_history)
    _append_history_line(entry)
    history = prior_history + [entry]
    signal = compute_maturity_signal(history)
    hold_progress = compute_hold_progress(history)

    _safe_write_json(_smoke_state_path(), _serialize_run(run))
    _safe_write_text(
        _smoke_report_path(),
        _build_report(run, factory_state, signal, hold_progress=hold_progress),
    )
    if run.verdict == "FAIL":
        _safe_write_text(
            _failure_report_path(),
            _build_failure_report(run, observer_classification),
        )
        _safe_write_text(
            _claude_repair_path(),
            _build_repair_prompt(run, observer_classification),
        )
    if run.verdict == "HOLD":
        # PM HOLD is a successful — but rework-pending — verdict.
        # claude_repair_prompt.md is intentionally absent (nothing to
        # repair); claude_rework_prompt.md is *required* so the next
        # cycle's planner has the prior weakness as its input.
        _safe_write_text(
            _claude_rework_path(),
            _build_rework_prompt(run, factory_state),
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
        "pm_hold_spec_keywords": list(run.pm_hold_spec_keywords),
        "design_spec_status": run.design_spec_status,
        "design_spec_acceptance_passed": run.design_spec_acceptance_passed,
        "design_spec_target_files_count": run.design_spec_target_files_count,
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


def _build_report(
    run: SmokeRun,
    factory_state: dict,
    maturity_signal: dict | None = None,
    *,
    hold_progress: dict | None = None,
) -> str:
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

    if hold_progress is None and run.verdict == "HOLD":
        # Best-effort: load full history (current entry already
        # appended by write_outputs) so the section reflects state on
        # disk.
        hold_progress = compute_hold_progress(_load_history())
    if hold_progress and hold_progress.get("hold_repeat_count", 0) > 0:
        lines.extend(_build_hold_progress_section(hold_progress))

    if maturity_signal is None:
        maturity_signal = compute_maturity_signal(_load_history())
    lines.extend(_build_maturity_section(maturity_signal))

    lines += [
        "",
        "## Output files",
        f"- 상태: `.runtime/factory_smoke_state.json`",
        f"- 리포트: `.runtime/factory_smoke_report.md`",
        f"- 로그: `.runtime/factory_smoke.log`",
        f"- 히스토리: `.runtime/factory_smoke_history.jsonl`",
    ]
    if run.verdict == "FAIL":
        lines += [
            f"- 실패 리포트: `.runtime/factory_failure_report.md`",
            f"- Claude repair prompt: `.runtime/claude_repair_prompt.md`",
        ]
    if run.verdict == "HOLD":
        lines.append(
            f"- Claude rework prompt: `.runtime/claude_rework_prompt.md`"
        )
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
            "- `.runtime/claude_rework_prompt.md` 가 다음 사이클 planner 입력으로 자동 작성됩니다.",
            "- 원본 산출물: `.runtime/pm_decision.md` / `.runtime/designer_final_review.md`.",
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
# PM HOLD rework prompt
#
# When the smoke verdict is HOLD, claude_repair_prompt.md is intentionally
# absent (the cycle didn't fail — it produced a valid HOLD verdict). The
# rework prompt below is *the* hand-off doc the next planner cycle reads
# to know "this is not a fresh ideation — solve the prior weakness first".
# ---------------------------------------------------------------------------


def _read_md_section_local(md: str, heading: str) -> str:
    """Mini extractor — pulls the body under "## heading" (or any of the
    accepted heading aliases) until the next ## or end-of-doc.

    factory_smoke is stdlib-only and must NOT import from cycle.py at
    runtime, so this duplicates the small subset of `_extract_md_section`
    behavior we actually need here.
    """
    import re as _re
    aliases: dict[str, tuple[str, ...]] = {
        "신규 기능 아이디어 후보": ("신규 장치 아이디어 후보",),
        "이번 사이클 선정 기능":   ("이번 사이클 선정 장치",),
    }
    variants = (heading, *aliases.get(heading, ()))
    for v in variants:
        pat = (
            r"^##\s+" + _re.escape(v) + r"\s*\n(.*?)(?=\n##\s|\Z)"
        )
        m = _re.search(pat, md, _re.MULTILINE | _re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def _build_rework_prompt(run: SmokeRun, factory_state: dict) -> str:
    """Build .runtime/claude_rework_prompt.md from pm_decision.md +
    designer_final_review.md.

    The next cycle's planner (or an operator running Claude directly)
    reads this file to understand:
      - why the prior cycle was held
      - which axes (Visual Desire / Share / Rarity / Revisit) underscored
      - which 약점 the designer flagged
      - which 다음 단계 the PM assigned to designer / planner
      - the smoke command to re-run after the rework
    """
    pm_md = ""
    designer_md = ""
    try:
        if _pm_decision_path().is_file():
            pm_md = _pm_decision_path().read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        if _designer_final_review_path().is_file():
            designer_md = _designer_final_review_path().read_text(encoding="utf-8")
    except OSError:
        pass

    decision = _read_md_section_local(pm_md, "출하 결정") or "hold"
    reason = _read_md_section_local(pm_md, "결정 이유")
    ship_unit = _read_md_section_local(pm_md, "출하 단위 (가장 작은)")
    next_owners = _read_md_section_local(pm_md, "다음 단계 담당")
    qa_extra = _read_md_section_local(pm_md, "QA가 추가로 점검할 것")
    weaknesses = _read_md_section_local(designer_md, "약점")
    score_section = _read_md_section_local(designer_md, "욕구 점수표")
    final_judgment = _read_md_section_local(designer_md, "최종 판단")
    improve_guide = _read_md_section_local(designer_md, "개선 지침")

    pm_message = factory_state.get("pm_decision_message") or "—"
    cycle_id = factory_state.get("cycle") or run.cycle_id or "—"

    lines: list[str] = [
        "# Stampport Factory Smoke — PM HOLD Rework Prompt",
        "",
        f"Smoke verdict: **HOLD** · cycle: `{cycle_id}` · "
        f"factory_state.status: `{run.factory_status or '—'}`",
        "",
        "## PM HOLD 요약",
        f"- PM 결정: {decision.strip() or 'hold'}",
        f"- PM 메시지: {pm_message}",
    ]
    if reason:
        lines += ["", "## 결정 이유", reason.strip()]
    if score_section:
        lines += ["", "## 미달 점수 (욕구 점수표)", score_section.strip()]
    if weaknesses:
        lines += ["", "## 디자이너가 지적한 약점", weaknesses.strip()]
    if next_owners:
        lines += ["", "## PM 다음 단계 담당", next_owners.strip()]
    if ship_unit:
        lines += ["", "## 출하 단위 (가장 작은)", ship_unit.strip()]
    if improve_guide:
        lines += ["", "## 디자이너 개선 지침", improve_guide.strip()]
    if final_judgment:
        lines += ["", "## 디자이너 최종 판단", final_judgment.strip()]
    if qa_extra:
        lines += ["", "## QA 추가 점검 항목", qa_extra.strip()]

    spec_keywords = list(run.pm_hold_spec_keywords or [])
    if spec_keywords:
        kw_str = ", ".join(f"`{k}`" for k in spec_keywords[:12])
        lines += [
            "",
            "## ⚠️ design_spec 우선 모드",
            "이번 HOLD 사유에 다음 spec-mode keyword 가 포함되었습니다:",
            f"- {kw_str}",
            "",
            "다음 사이클의 designer 는 `.runtime/design_spec.md` 를 작성해야 합니다.",
            "PM 은 design_spec acceptance (SVG 3종 숫자 좌표 / titleLabel ≥ 13 /"
            " 수정 대상 파일 ≥ 3 / ShareCard 렌더 조건 / QA 기준) 가 통과하면",
            "점수 미달이라도 SHIP 으로 넘어갑니다 — 추상 논의 루프 차단.",
        ]

    lines += [
        "",
        "## 해결해야 할 항목",
        "- 직전 사이클 PM HOLD 의 **모든** 약점을 다음 사이클의 첫 번째 후보로 삼는다.",
        "- 디자이너가 명시한 SVG / 레이아웃 / 문구 지침은 새 후보의 MVP 구현 범위에 그대로 포함.",
        "- PM \"다음 단계 담당\" 의 디자이너/기획자 지시를 무시하지 않는다.",
        "",
        "## 다음 cycle 목표",
        "- 직전 약점 해소 후 desire scorecard 의 미달 게이트 (Visual Desire / Share / Rarity 등) 가",
        "  ship 기준 (≥4 / ≥4 / ≥3) 을 충족하도록 디자인 + 코드 변경을 함께 ship.",
        "- 새로운 무관한 후보 3개를 무작위 제안하지 마라.",
        "",
        "## planner / designer 에게 전달할 제약",
        "- planner: 후보 3개 중 최소 1개는 위 \"디자이너가 지적한 약점\" 을 직접 해소.",
        "- designer: 위 \"개선 지침\" 의 색상 / 카드 / 아이콘 / 문구 지침을 그대로 비주얼 가이드로 사용.",
        "- PM: 미달 점수 게이트가 회복되지 않으면 다시 HOLD.",
        "",
        "## smoke 재실행 명령",
        f"`python3 -m control_tower.local_runner.factory_smoke --mode {run.mode or 'local-cycle'} "
        f"--timeout {run.timeout_sec or 1800}`",
        "",
        "## 참고 산출물",
        "- `.runtime/pm_decision.md`",
        "- `.runtime/designer_final_review.md`",
        "- `.runtime/desire_scorecard.json`",
        "- `.runtime/factory_smoke_report.md`",
        "- `.runtime/design_spec.md` (작성 후)",
    ]
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
# Factory Maturity Signal
#
# The smoke runner writes one JSONL line per run to
# .runtime/factory_smoke_history.jsonl, then summarizes the last 5 runs
# in factory_smoke_report.md and emits a single concrete recommendation
# for the operator (keep_sequential_loop / improve_pm_rework_feedback /
# add_parallel_designer_review / ...). Goal: replace gut-feel decisions
# about whether to introduce parallel auxiliary loops with a measured
# signal grounded in observed verdicts and stage durations.
# ---------------------------------------------------------------------------


_MATURITY_RECOMMENDATIONS: tuple[str, ...] = (
    "keep_sequential_loop",
    "improve_planner_contract",
    "improve_pm_rework_feedback",
    "add_parallel_designer_review",
    "add_parallel_qa_review",
    "add_diagnostic_repair_loop",
    "split_product_and_control_tower_cycles",
)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (TypeError, ValueError):
            continue
    return None


def _compute_duration_sec(started: str | None, ended: str | None) -> float:
    s = _parse_iso(started)
    e = _parse_iso(ended)
    if not s or not e:
        return 0.0
    return max(0.0, (e - s).total_seconds())


def _human_action_count_for(verdict: str) -> int:
    """Translate a verdict into the number of distinct human actions the
    operator must take afterwards. Used to compute "ops automation
    maturity" — if average per run is > 1, the loop still costs the
    operator more than one decision per cycle on average.
    """
    if verdict == "PASS":
        return 0
    if verdict in ("READY_TO_REVIEW", "READY_TO_PUBLISH"):
        return 1
    if verdict == "HOLD":
        return 2
    if verdict == "FAIL":
        return 2
    return 1


def _load_history() -> list[dict]:
    path = _smoke_history_path()
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _append_history_line(entry: dict) -> None:
    path = _smoke_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write(f"[factory_smoke] history append failed: {exc}\n")


def _build_history_entry(
    run: SmokeRun,
    factory_state: dict,
    prior_history: list[dict],
) -> dict:
    duration_sec = _compute_duration_sec(run.started_at, run.finished_at)
    current_stage = factory_state.get("current_stage") or None
    last_stage = run.last_successful_stage or None
    stage_durations: dict[str, float] = {}
    for s in run.stages:
        if s.duration_sec:
            stage_durations[s.name] = round(float(s.duration_sec), 3)

    failure_code = run.failure_code or None
    repeated = 0
    if failure_code:
        # Count this run + matching codes in the prior 4 entries (so the
        # value is "occurrences within the most recent 5 runs ending now").
        repeated = 1 + sum(
            1 for e in prior_history[-4:]
            if e.get("failure_code") == failure_code
        )

    pm_decision_message = factory_state.get("pm_decision_message")
    spec_keywords = factory_state.get("pm_hold_spec_keywords") or []
    design_spec_status = factory_state.get("design_spec_status")
    design_spec_acceptance_passed = factory_state.get(
        "design_spec_acceptance_passed"
    )
    return {
        "started_at": run.started_at,
        "ended_at": run.finished_at,
        "duration_sec": round(duration_sec, 3),
        "verdict": run.verdict,
        "failure_code": failure_code,
        "current_stage": current_stage,
        "last_stage": last_stage,
        "stage_durations": stage_durations,
        "changed_files_count": run.changed_files_count,
        "qa_status": run.qa_status,
        "pm_decision_ship_ready": run.pm_decision_ship_ready,
        "implementation_ticket_status": run.ticket_status,
        "human_action_required": _human_action_count_for(run.verdict),
        "repeated_failure_count": repeated,
        # HOLD-progress signals — let compute_hold_progress() reason
        # about whether the same HOLD reason is repeating, getting more
        # concrete, or close to the spec_bypass threshold.
        "pm_decision_message": pm_decision_message,
        "pm_hold_spec_keywords": list(spec_keywords) if spec_keywords else [],
        "design_spec_status": design_spec_status,
        "design_spec_acceptance_passed": bool(design_spec_acceptance_passed)
        if design_spec_acceptance_passed is not None else None,
    }


def compute_hold_progress(
    history: list[dict], current_entry: dict | None = None,
) -> dict:
    """Inspect recent HOLD verdicts in history (current_entry should be
    appended already, or passed separately) and decide whether the
    rework loop is making progress.

    Returns:
        hold_repeat_count: consecutive HOLD verdicts ending at the
            most recent entry (1 if current is HOLD but previous was
            not, 0 if current isn't HOLD).
        same_reason_as_prev: True when the current HOLD's spec keyword
            set matches the previous HOLD exactly.
        more_concrete: True when the current HOLD has *more* spec
            keywords than the previous, OR when design_spec_status
            advanced toward generated/insufficient compared to prior.
        next_action: one of
            "design_spec 생성 필요"   — HOLD has spec keywords but no design_spec.
            "design_spec 보완 필요"   — design_spec exists but acceptance failed.
            "구현 진입 가능"          — design_spec acceptance passed; planner
                                         should ship next cycle.
            "PM 기준 완화 필요"       — repeated HOLDs with same reasons and a
                                         passing design_spec already (rare loop).
            "—"                       — current isn't HOLD.
    """
    series = list(history)
    if current_entry is not None:
        series = [*series, current_entry]
    if not series:
        return {
            "hold_repeat_count": 0,
            "same_reason_as_prev": False,
            "more_concrete": False,
            "next_action": "—",
            "current_keywords": [],
            "prev_keywords": [],
        }
    last = series[-1]
    if (last.get("verdict") or "") != "HOLD":
        return {
            "hold_repeat_count": 0,
            "same_reason_as_prev": False,
            "more_concrete": False,
            "next_action": "—",
            "current_keywords": [],
            "prev_keywords": [],
        }

    repeat = 0
    for e in reversed(series):
        if (e.get("verdict") or "") == "HOLD":
            repeat += 1
        else:
            break

    # The most recent prior HOLD (excluding current).
    prev_hold: dict | None = None
    for e in reversed(series[:-1]):
        if (e.get("verdict") or "") == "HOLD":
            prev_hold = e
            break

    cur_kw = list(last.get("pm_hold_spec_keywords") or [])
    prev_kw = list((prev_hold or {}).get("pm_hold_spec_keywords") or [])
    cur_set = set(cur_kw)
    prev_set = set(prev_kw)
    same_reason = bool(prev_hold) and cur_set == prev_set and cur_set != set()
    more_concrete = False
    if prev_hold:
        if cur_set and prev_set and cur_set > prev_set:
            more_concrete = True
        # design_spec_status promotion ladder: skipped < failed <
        # insufficient < generated. If we moved up, that's progress
        # even if keyword sets are the same.
        ladder = {"skipped": 0, "failed": 1, "insufficient": 2, "generated": 3}
        cur_rank = ladder.get(str(last.get("design_spec_status") or ""), 0)
        prev_rank = ladder.get(
            str(prev_hold.get("design_spec_status") or ""), 0
        )
        if cur_rank > prev_rank:
            more_concrete = True

    cur_acc = last.get("design_spec_acceptance_passed")
    cur_status = last.get("design_spec_status") or ""
    if cur_acc is True and cur_status == "generated":
        next_action = "구현 진입 가능"
    elif cur_status == "insufficient":
        next_action = "design_spec 보완 필요"
    elif cur_kw and cur_status in {"skipped", "", "failed"}:
        next_action = "design_spec 생성 필요"
    elif repeat >= 3 and same_reason and cur_acc is True:
        next_action = "PM 기준 완화 필요"
    elif repeat >= 3 and same_reason:
        next_action = "design_spec 보완 필요"
    else:
        next_action = "design_spec 생성 필요" if cur_kw else "—"

    return {
        "hold_repeat_count": repeat,
        "same_reason_as_prev": same_reason,
        "more_concrete": more_concrete,
        "next_action": next_action,
        "current_keywords": cur_kw,
        "prev_keywords": prev_kw,
    }


def _build_hold_progress_section(progress: dict) -> list[str]:
    """Markdown section appended to factory_smoke_report.md when the
    current verdict is HOLD."""
    if progress.get("hold_repeat_count", 0) <= 0:
        return []
    cur_kw = progress.get("current_keywords") or []
    prev_kw = progress.get("prev_keywords") or []
    return [
        "",
        "## HOLD progress",
        f"- HOLD 반복 횟수: **{progress['hold_repeat_count']}**",
        f"- 직전 HOLD 와 같은 사유: `{progress['same_reason_as_prev']}`",
        f"- 이번 HOLD 가 더 구체화: `{progress['more_concrete']}`",
        "- 직전 HOLD 키워드: " + (", ".join(f"`{k}`" for k in prev_kw) or "—"),
        "- 이번 HOLD 키워드: " + (", ".join(f"`{k}`" for k in cur_kw) or "—"),
        f"- 다음 행동: **{progress['next_action']}**",
    ]


def compute_maturity_signal(history: list[dict]) -> dict:
    """Summarize the last 5 history entries and pick a single recommendation.

    Returned keys:
        recent_count, verdict_distribution,
        ready_count, hold_count, fail_count, pass_count,
        avg_duration_sec,
        most_common_failure_code, most_common_failure_count,
        longest_stage, longest_stage_avg_duration_sec,
        avg_human_action_required,
        signals (list of human-readable signal strings),
        recommendation (one of _MATURITY_RECOMMENDATIONS),
        operator_message (one paragraph the operator can read directly)
    """
    recent = history[-5:]
    n = len(recent)
    if n == 0:
        return {
            "recent_count": 0,
            "verdict_distribution": {},
            "ready_count": 0,
            "hold_count": 0,
            "fail_count": 0,
            "pass_count": 0,
            "avg_duration_sec": 0.0,
            "most_common_failure_code": None,
            "most_common_failure_count": 0,
            "longest_stage": None,
            "longest_stage_avg_duration_sec": 0.0,
            "avg_human_action_required": 0.0,
            "signals": [],
            "recommendation": "keep_sequential_loop",
            "operator_message": (
                "아직 history 가 없습니다. smoke 를 몇 회 더 돌린 뒤 다시 보세요."
            ),
        }

    verdict_dist: dict[str, int] = {}
    for h in recent:
        v = h.get("verdict") or "UNKNOWN"
        verdict_dist[v] = verdict_dist.get(v, 0) + 1

    ready_count = (
        verdict_dist.get("READY_TO_REVIEW", 0)
        + verdict_dist.get("READY_TO_PUBLISH", 0)
    )
    hold_count = verdict_dist.get("HOLD", 0)
    fail_count = sum(
        1 for h in recent
        if h.get("verdict") == "FAIL"
        or h.get("failure_code") == "smoke_timeout"
    )
    pass_count = verdict_dist.get("PASS", 0)

    durations = [float(h.get("duration_sec") or 0) for h in recent]
    avg_duration = sum(durations) / n

    failure_counts: dict[str, int] = {}
    for h in recent:
        code = h.get("failure_code")
        if code:
            failure_counts[code] = failure_counts.get(code, 0) + 1
    most_common_code: str | None = None
    most_common_count = 0
    if failure_counts:
        most_common_code, most_common_count = max(
            failure_counts.items(), key=lambda kv: (kv[1], kv[0])
        )

    # Longest stage: rank by AVERAGE duration across the recent runs that
    # actually saw the stage.
    stage_total: dict[str, float] = {}
    stage_seen: dict[str, int] = {}
    for h in recent:
        for name, dur in (h.get("stage_durations") or {}).items():
            try:
                d = float(dur)
            except (TypeError, ValueError):
                continue
            stage_total[name] = stage_total.get(name, 0.0) + d
            stage_seen[name] = stage_seen.get(name, 0) + 1
    longest_stage: str | None = None
    longest_stage_avg = 0.0
    for name, total in stage_total.items():
        avg = total / stage_seen[name]
        if avg > longest_stage_avg:
            longest_stage_avg = avg
            longest_stage = name

    avg_human = sum(
        int(h.get("human_action_required") or 0) for h in recent
    ) / n

    signals: list[str] = []
    if ready_count >= 3:
        signals.append("운영 가능")
    if hold_count >= 3:
        signals.append("rework feedback 개선 필요")
    if most_common_count >= 3 and most_common_code:
        signals.append(
            f"진단별 자동 수리 루프 필요 ({most_common_code})"
        )
    if avg_duration > 1800:
        signals.append("병렬 보조 평가 검토")
    if avg_human > 1:
        signals.append("운영 자동화 미성숙")
    if ready_count >= 3 and avg_duration > 1800:
        signals.append("부분 병렬화 후보")

    # Recommendation — pick exactly one in priority order so the operator
    # sees a single concrete next step rather than a checklist.
    recommendation = "keep_sequential_loop"
    operator_message = ""

    if most_common_count >= 3 and most_common_code:
        recommendation = "add_diagnostic_repair_loop"
        operator_message = (
            f"같은 failure_code(`{most_common_code}`) 가 최근 {n}회 중 "
            f"{most_common_count}회 반복되었습니다. 병렬화 전에 진단별 "
            f"자동 수리 루프를 먼저 도입하세요."
        )
    elif hold_count >= 3:
        recommendation = "improve_pm_rework_feedback"
        operator_message = (
            f"아직 병렬화하지 마세요. 최근 {n}회 중 READY_TO_REVIEW가 "
            f"{ready_count}회이고 HOLD가 {hold_count}회입니다. 먼저 PM HOLD "
            f"피드백 주입을 안정화하세요."
        )
    elif ready_count >= 3 and avg_duration > 1800:
        worst = (longest_stage or "").lower()
        if "designer" in worst:
            recommendation = "add_parallel_designer_review"
        elif "qa" in worst:
            recommendation = "add_parallel_qa_review"
        else:
            recommendation = "split_product_and_control_tower_cycles"
        mins = avg_duration / 60.0
        operator_message = (
            f"부분 병렬화를 검토할 수 있습니다. 최근 {n}회 중 "
            f"READY_TO_REVIEW {ready_count}회, 평균 소요 시간 {mins:.0f}분으로 "
            f"병목은 {longest_stage or '미상'}입니다."
        )
    elif fail_count >= 3:
        recommendation = "improve_planner_contract"
        operator_message = (
            f"최근 {n}회 중 FAIL/TIMEOUT 이 {fail_count}회입니다. 병렬화 전에 "
            f"planner 계약과 cycle 진입 조건을 강화해 실패율을 낮추세요."
        )
    elif avg_human > 1:
        recommendation = "improve_pm_rework_feedback"
        operator_message = (
            f"운영 자동화가 아직 미성숙합니다 (run당 평균 human_action "
            f"{avg_human:.1f}). PM rework 피드백 주입과 자동 ship 정책을 "
            f"먼저 다듬으세요."
        )
    else:
        recommendation = "keep_sequential_loop"
        operator_message = (
            f"아직 병렬화 신호가 없습니다 (READY_TO_REVIEW {ready_count}회, "
            f"HOLD {hold_count}회, FAIL/TIMEOUT {fail_count}회, 평균 "
            f"{avg_duration:.0f}s). 순차 루프를 유지하세요."
        )

    return {
        "recent_count": n,
        "verdict_distribution": verdict_dist,
        "ready_count": ready_count,
        "hold_count": hold_count,
        "fail_count": fail_count,
        "pass_count": pass_count,
        "avg_duration_sec": round(avg_duration, 3),
        "most_common_failure_code": most_common_code,
        "most_common_failure_count": most_common_count,
        "longest_stage": longest_stage,
        "longest_stage_avg_duration_sec": round(longest_stage_avg, 3),
        "avg_human_action_required": round(avg_human, 3),
        "signals": signals,
        "recommendation": recommendation,
        "operator_message": operator_message,
    }


def _build_maturity_section(signal: dict) -> list[str]:
    lines = ["", "## Factory Maturity Signal"]
    if signal["recent_count"] == 0:
        lines += [
            "- (history 없음 — smoke 를 더 돌리세요)",
            "",
            "### 운영자에게",
            f"> {signal['operator_message']}",
            "",
            f"### 추천: `{signal['recommendation']}`",
        ]
        return lines

    avg_min = signal["avg_duration_sec"] / 60.0
    dist = signal["verdict_distribution"]
    dist_str = ", ".join(f"{k}={v}" for k, v in sorted(dist.items())) or "—"
    lines += [
        f"- 표본: 최근 {signal['recent_count']}회",
        f"- verdict 분포: {dist_str}",
        f"- READY_TO_REVIEW (READY_TO_PUBLISH 포함): "
        f"{signal['ready_count']}회",
        f"- HOLD: {signal['hold_count']}회",
        f"- FAIL/TIMEOUT: {signal['fail_count']}회",
        f"- 평균 소요 시간: {signal['avg_duration_sec']:.1f}s "
        f"({avg_min:.1f}분)",
        f"- 가장 많이 반복된 failure_code: "
        f"`{signal['most_common_failure_code'] or '—'}` "
        f"({signal['most_common_failure_count']}회)",
        f"- 가장 오래 걸린 stage: "
        f"`{signal['longest_stage'] or '—'}` "
        f"(avg {signal['longest_stage_avg_duration_sec']:.1f}s)",
        f"- 평균 human_action_required: "
        f"{signal['avg_human_action_required']:.2f}",
        "",
        "### 신호",
    ]
    if signal["signals"]:
        lines.extend(f"- {s}" for s in signal["signals"])
    else:
        lines.append("- (해당 없음)")
    lines += [
        "",
        "### 운영자에게",
        f"> {signal['operator_message']}",
        "",
        f"### 추천: `{signal['recommendation']}`",
    ]
    return lines


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

    # 12. Planner heading contract: alias "신규 장치 아이디어 후보" passes
    #     guard (case A); canonical form passes too (case B); selected
    #     feature is extracted under both "## 이번 사이클 선정 장치"
    #     (case C) and "## 이번 사이클 선정 기능" (case D).
    total += 1
    try:
        from . import cycle as _cycle
    except Exception as exc:
        _cycle = None  # type: ignore
        failures.append(f"12: import cycle.py failed: {exc}")
    if _cycle is not None:
        body_alias = _STAMPPORT_PLANNER_FIXTURE.replace(
            "## 신규 기능 아이디어 후보", "## 신규 장치 아이디어 후보"
        ).replace(
            "## 이번 사이클 선정 기능", "## 이번 사이클 선정 장치"
        )
        body_canonical = _STAMPPORT_PLANNER_FIXTURE
        # Normalize is the gate the cycle.stage_product_planning calls
        # before validation — exercise that path.
        norm_alias = _cycle._normalize_planner_body(body_alias)
        norm_canon = _cycle._normalize_planner_body(body_canonical)
        fails_alias = _cycle._validate_planner_report(norm_alias)
        fails_canon = _cycle._validate_planner_report(norm_canon)
        sel_alias = _cycle._extract_selected_feature(norm_alias)
        sel_canon = _cycle._extract_selected_feature(norm_canon)
        # Also confirm direct extraction from the *un*-normalized alias
        # body works through the alias-aware extractor.
        sel_alias_raw = _cycle._extract_selected_feature(body_alias)
        if (
            not fails_alias
            and not fails_canon
            and sel_alias
            and sel_canon
            and sel_alias_raw
        ):
            passed += 1
        else:
            failures.append(
                f"12: planner heading alias — fails_alias={fails_alias[:2]} "
                f"fails_canon={fails_canon[:2]} sel_alias={sel_alias!r} "
                f"sel_canon={sel_canon!r} sel_alias_raw={sel_alias_raw!r}"
            )

    # 13. PM HOLD fixture — claude_rework_prompt.md is created with the
    #     designer 약점, PM 다음 단계, 미달 점수 surfaced; the report's
    #     Output files lists the rework prompt path.
    total += 1
    repo = os.environ.get("LOCAL_RUNNER_REPO")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCAL_RUNNER_REPO"] = tmp
        try:
            runtime = Path(tmp) / ".runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "pm_decision.md").write_text(
                _PM_DECISION_FIXTURE, encoding="utf-8"
            )
            (runtime / "designer_final_review.md").write_text(
                _DESIGNER_FINAL_REVIEW_FIXTURE, encoding="utf-8"
            )
            run = SmokeRun(mode="local-cycle", timeout_sec=1800)
            run.started_at = _utc_now_iso()
            run.finished_at = _utc_now_iso()
            run.verdict = "HOLD"
            run.factory_status = "hold_for_rework"
            run.cycle_id = 1
            run.ticket_status = "skipped_hold"
            run.pm_decision_ship_ready = False
            run.changed_files_count = 0
            fs = {
                "status": "hold_for_rework",
                "cycle": 1,
                "pm_decision_status": "generated",
                "pm_decision_ship_ready": False,
                "implementation_ticket_status": "skipped_hold",
                "pm_decision_message": "HOLD (총점 19/30)",
            }
            write_outputs(run, factory_state=fs, observer_classification=None)
            rework = runtime / "claude_rework_prompt.md"
            repair = runtime / "claude_repair_prompt.md"
            report = (runtime / "factory_smoke_report.md").read_text(
                encoding="utf-8"
            )
            ok = (
                rework.is_file()
                and not repair.exists()
                and "claude_rework_prompt.md" in report
                and "selectedTitle" in rework.read_text(encoding="utf-8")
                and "디자이너가 지적한 약점" in rework.read_text(encoding="utf-8")
                and "PM 다음 단계 담당" in rework.read_text(encoding="utf-8")
                # implementation_ticket_status must remain skipped_hold —
                # we don't write a ticket on HOLD.
                and run.ticket_status == "skipped_hold"
            )
            if ok:
                passed += 1
            else:
                failures.append(
                    "13: PM HOLD rework prompt — missing artifact / sections / "
                    f"rework_exists={rework.is_file()} repair_absent={not repair.exists()}"
                )
        finally:
            if repo is not None:
                os.environ["LOCAL_RUNNER_REPO"] = repo
            else:
                os.environ.pop("LOCAL_RUNNER_REPO", None)

    # 14. PM HOLD fixture also feeds the next planner prompt — when
    #     pm_decision.md/designer_final_review.md are present and HOLD,
    #     _build_product_planner_prompt prepends a "Previous PM HOLD"
    #     section that mirrors the 약점 / 다음 단계 / 미달 점수.
    total += 1
    if _cycle is None:
        failures.append("14: skipped — cycle import failed earlier")
    else:
        repo_root_prev = os.environ.get("REPO_ROOT")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".runtime").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / ".runtime" / "pm_decision.md").write_text(
                _PM_DECISION_FIXTURE, encoding="utf-8"
            )
            (Path(tmp) / ".runtime" / "designer_final_review.md").write_text(
                _DESIGNER_FINAL_REVIEW_FIXTURE, encoding="utf-8"
            )
            # cycle.py reads PM_DECISION_FILE / DESIGNER_FINAL_REVIEW_FILE
            # at import time from REPO_ROOT — patch the constants directly
            # for this test instead of re-importing the module.
            saved_pm = _cycle.PM_DECISION_FILE
            saved_dr = _cycle.DESIGNER_FINAL_REVIEW_FILE
            _cycle.PM_DECISION_FILE = Path(tmp) / ".runtime" / "pm_decision.md"
            _cycle.DESIGNER_FINAL_REVIEW_FILE = (
                Path(tmp) / ".runtime" / "designer_final_review.md"
            )
            try:
                prompt = _cycle._build_product_planner_prompt("test goal")
            finally:
                _cycle.PM_DECISION_FILE = saved_pm
                _cycle.DESIGNER_FINAL_REVIEW_FILE = saved_dr
                if repo_root_prev is not None:
                    os.environ["REPO_ROOT"] = repo_root_prev
        ok = (
            "Previous PM HOLD" in prompt
            and "디자이너가 지적한 약점" in prompt
            and "PM 다음 단계 담당" in prompt
            and "selectedTitle" in prompt
            and "기존 HOLD 해소" in prompt
        )
        if ok:
            passed += 1
        else:
            failures.append(
                "14: planner prompt did not include Previous PM HOLD context"
            )

    # 15. HOLD verdict is NOT FAIL — observer + smoke + factory_status agree.
    total += 1
    state = {
        "status": "hold_for_rework",
        "cycle": 99,
        "pm_decision_status": "generated",
        "pm_decision_ship_ready": False,
        "implementation_ticket_status": "skipped_hold",
    }
    verdict, code, _ = resolve_verdict(state, exit_code=0)
    obs_state = _observer._empty_state()
    obs_state["factory_state"] = state
    obs_state["control_state"] = {"status": "hold_for_rework", "liveness": {"runner_online": True}}
    cls = _observer.classify(
        obs_state,
        runner_processes=["python -m control_tower.local_runner.runner"],
        caffeinate_processes=[],
    )
    if (
        verdict == "HOLD"
        and code == "pm_hold_for_rework"
        and cls["diagnostic_code"] == "pm_hold_for_rework"
        and cls["category"] == "hold"
        and cls["is_failure"] is False
        and state["implementation_ticket_status"] == "skipped_hold"
    ):
        passed += 1
    else:
        failures.append(
            f"15: HOLD!=FAIL contract — verdict={verdict}/{code} "
            f"observer={cls['diagnostic_code']}/{cls['is_failure']}"
        )

    # 16. Maturity signal — keep_sequential_loop fixture.
    #     5 mostly-PASS cycles, modest duration, no recurring failures.
    total += 1
    fixture_keep = [
        _maturity_fixture_entry(
            verdict="PASS", duration_sec=540.0,
            stage_durations={"product_planning": 80.0, "claude_apply": 200.0},
        ),
        _maturity_fixture_entry(
            verdict="READY_TO_REVIEW", duration_sec=720.0,
            stage_durations={"product_planning": 100.0, "claude_apply": 240.0},
        ),
        _maturity_fixture_entry(
            verdict="PASS", duration_sec=560.0,
            stage_durations={"product_planning": 90.0, "claude_apply": 210.0},
        ),
        _maturity_fixture_entry(
            verdict="READY_TO_REVIEW", duration_sec=640.0,
            stage_durations={"product_planning": 95.0, "claude_apply": 220.0},
        ),
        _maturity_fixture_entry(
            verdict="PASS", duration_sec=600.0,
            stage_durations={"product_planning": 88.0, "claude_apply": 215.0},
        ),
    ]
    sig = compute_maturity_signal(fixture_keep)
    if (
        sig["recommendation"] == "keep_sequential_loop"
        and sig["ready_count"] == 2
        and sig["hold_count"] == 0
        and sig["fail_count"] == 0
    ):
        passed += 1
    else:
        failures.append(
            f"16: keep_sequential_loop fixture — got "
            f"{sig['recommendation']} ready={sig['ready_count']} "
            f"hold={sig['hold_count']} fail={sig['fail_count']}"
        )

    # 17. Maturity signal — improve_pm_rework_feedback fixture.
    #     >= 3 HOLD verdicts in last 5.
    total += 1
    fixture_hold = [
        _maturity_fixture_entry(verdict="HOLD", duration_sec=900.0),
        _maturity_fixture_entry(verdict="PASS", duration_sec=600.0),
        _maturity_fixture_entry(verdict="HOLD", duration_sec=860.0),
        _maturity_fixture_entry(verdict="HOLD", duration_sec=920.0),
        _maturity_fixture_entry(verdict="READY_TO_REVIEW", duration_sec=750.0),
    ]
    sig = compute_maturity_signal(fixture_hold)
    if (
        sig["recommendation"] == "improve_pm_rework_feedback"
        and sig["hold_count"] == 3
        and "rework feedback 개선 필요" in sig["signals"]
        and "PM HOLD 피드백" in sig["operator_message"]
    ):
        passed += 1
    else:
        failures.append(
            f"17: improve_pm_rework_feedback fixture — got "
            f"{sig['recommendation']} hold={sig['hold_count']} "
            f"signals={sig['signals']}"
        )

    # 18. Maturity signal — add_parallel_designer_review fixture.
    #     >= 3 READY_TO_REVIEW verdicts AND avg duration > 1800 AND
    #     designer_critique is the longest stage.
    total += 1
    fixture_designer = [
        _maturity_fixture_entry(
            verdict="READY_TO_REVIEW", duration_sec=2400.0,
            stage_durations={
                "designer_critique": 1200.0, "claude_apply": 500.0,
            },
        ),
        _maturity_fixture_entry(
            verdict="READY_TO_REVIEW", duration_sec=2300.0,
            stage_durations={
                "designer_critique": 1150.0, "claude_apply": 480.0,
            },
        ),
        _maturity_fixture_entry(
            verdict="READY_TO_REVIEW", duration_sec=2500.0,
            stage_durations={
                "designer_critique": 1300.0, "claude_apply": 520.0,
            },
        ),
        _maturity_fixture_entry(
            verdict="READY_TO_REVIEW", duration_sec=2200.0,
            stage_durations={
                "designer_critique": 1100.0, "claude_apply": 460.0,
            },
        ),
        _maturity_fixture_entry(
            verdict="PASS", duration_sec=1900.0,
            stage_durations={
                "designer_critique": 1000.0, "claude_apply": 420.0,
            },
        ),
    ]
    sig = compute_maturity_signal(fixture_designer)
    if (
        sig["recommendation"] == "add_parallel_designer_review"
        and sig["ready_count"] == 4
        and sig["avg_duration_sec"] > 1800.0
        and sig["longest_stage"] == "designer_critique"
        and "부분 병렬화 후보" in sig["signals"]
        and "designer_critique" in sig["operator_message"]
    ):
        passed += 1
    else:
        failures.append(
            f"18: add_parallel_designer_review fixture — got "
            f"{sig['recommendation']} ready={sig['ready_count']} "
            f"avg={sig['avg_duration_sec']} longest={sig['longest_stage']} "
            f"signals={sig['signals']}"
        )

    # 19. Smoke run end-to-end with maturity signal — write_outputs creates
    #     factory_smoke_history.jsonl AND the report contains the
    #     "Factory Maturity Signal" section.
    total += 1
    repo = os.environ.get("LOCAL_RUNNER_REPO")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCAL_RUNNER_REPO"] = tmp
        try:
            run = SmokeRun(mode="observer-only", timeout_sec=0)
            run.started_at = "2026-05-01T00:00:00.000000Z"
            run.finished_at = "2026-05-01T00:10:00.000000Z"
            run.verdict = "PASS"
            write_outputs(run, factory_state={}, observer_classification=None)
            hp = Path(tmp) / ".runtime" / "factory_smoke_history.jsonl"
            rp = Path(tmp) / ".runtime" / "factory_smoke_report.md"
            ok = (
                hp.is_file()
                and rp.is_file()
                and "Factory Maturity Signal" in rp.read_text(encoding="utf-8")
                and "추천:" in rp.read_text(encoding="utf-8")
            )
            if ok:
                first = hp.read_text(encoding="utf-8").splitlines()[0]
                rec = json.loads(first)
                ok = (
                    rec["verdict"] == "PASS"
                    and rec["duration_sec"] == 600.0
                    and rec["human_action_required"] == 0
                    and "stage_durations" in rec
                )
            if ok:
                passed += 1
            else:
                failures.append(
                    "19: maturity history/report — "
                    f"history_exists={hp.is_file()} report_exists={rp.is_file()}"
                )
        finally:
            if repo is not None:
                os.environ["LOCAL_RUNNER_REPO"] = repo
            else:
                os.environ.pop("LOCAL_RUNNER_REPO", None)

    # 20A. PM HOLD with "SVG path" in reasons → next planner prompt has
    #      design_spec 우선 모드 + spec keyword list.
    total += 1
    if _cycle is not None:  # set in test 12
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".runtime").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / ".runtime" / "pm_decision.md").write_text(
                _PM_DECISION_FIXTURE_SPEC, encoding="utf-8"
            )
            (Path(tmp) / ".runtime" / "designer_final_review.md").write_text(
                _DESIGNER_FINAL_REVIEW_FIXTURE, encoding="utf-8"
            )
            saved_pm = _cycle.PM_DECISION_FILE
            saved_dr = _cycle.DESIGNER_FINAL_REVIEW_FILE
            _cycle.PM_DECISION_FILE = Path(tmp) / ".runtime" / "pm_decision.md"
            _cycle.DESIGNER_FINAL_REVIEW_FILE = (
                Path(tmp) / ".runtime" / "designer_final_review.md"
            )
            try:
                prompt = _cycle._build_product_planner_prompt("test goal")
                _, mode_active, kw = _cycle._load_pm_hold_rework_context(
                    return_spec_mode=True,
                )
            finally:
                _cycle.PM_DECISION_FILE = saved_pm
                _cycle.DESIGNER_FINAL_REVIEW_FILE = saved_dr
        if (
            mode_active
            and any("SVG" in k or "svg" in k for k in kw)
            and "design_spec" in prompt
            and "디자인 구현 명세 확정 모드" in prompt
            and "구현 명세 확정 사이클" in prompt
        ):
            passed += 1
        else:
            failures.append(
                f"20A: SVG path HOLD did not trigger spec-mode in planner prompt "
                f"(mode={mode_active}, keywords={kw})"
            )
    else:
        failures.append("20A: skipped — cycle import failed earlier")

    # 20B. design_spec.md with Tier 2 / Tier 3 numeric paths + 13
    #      titleLabels + 3 target files → _validate_design_spec returns
    #      [] (PM SHIP-equivalent gate passes).
    total += 1
    if _cycle is not None:
        fails_b = _cycle._validate_design_spec(_DESIGN_SPEC_FIXTURE_GOOD)
        target_files_b = _cycle._extract_design_spec_target_files(
            _DESIGN_SPEC_FIXTURE_GOOD
        )
        title_count_b = _cycle._extract_design_spec_titlelabel_count(
            _DESIGN_SPEC_FIXTURE_GOOD
        )
        svg_b = _cycle._extract_design_spec_svg_paths(_DESIGN_SPEC_FIXTURE_GOOD)
        if (
            not fails_b
            and len(target_files_b) >= 3
            and title_count_b >= 13
            and len(svg_b) >= 3
        ):
            passed += 1
        else:
            failures.append(
                f"20B: good design_spec did not pass — fails={fails_b[:2]} "
                f"files={len(target_files_b)} titles={title_count_b} svg={svg_b}"
            )
    else:
        failures.append("20B: skipped — cycle import failed earlier")

    # 20C. design_spec.md with fewer than 13 titleLabels → validator
    #      returns the titleLabel-count failure (PM stays in HOLD).
    total += 1
    if _cycle is not None:
        # Drop a titleLabel from the good fixture so the count is 12.
        truncated = _DESIGN_SPEC_FIXTURE_GOOD.replace(
            "- traveler_starter: 동네 탐험가\n", "", 1
        )
        fails_c = _cycle._validate_design_spec(truncated)
        title_count_c = _cycle._extract_design_spec_titlelabel_count(truncated)
        if (
            title_count_c < 13
            and any("titleLabel 13" in f for f in fails_c)
        ):
            passed += 1
        else:
            failures.append(
                f"20C: <13 titleLabel did not fail — count={title_count_c} "
                f"fails={fails_c[:3]}"
            )
    else:
        failures.append("20C: skipped — cycle import failed earlier")

    # 20D. design_spec.md provides 3+ target files → ticket extractor
    #      returns those exact files.
    total += 1
    if _cycle is not None:
        files_d = _cycle._extract_design_spec_target_files(
            _DESIGN_SPEC_FIXTURE_GOOD
        )
        expected_d = {
            "app/web/src/data/badges.js",
            "app/web/src/screens/Badges.jsx",
            "app/web/src/screens/Share.jsx",
        }
        if expected_d.issubset(set(files_d)):
            passed += 1
        else:
            failures.append(f"20D: ticket target_files extraction — got {files_d}")
    else:
        failures.append("20D: skipped — cycle import failed earlier")

    # 20E. Repeated HOLD verdicts → factory_smoke_report shows
    #      hold_repeat_count + same_reason_as_prev + next_action.
    total += 1
    history_e = [
        {
            "verdict": "HOLD", "pm_hold_spec_keywords": ["SVG path", "titleLabel"],
            "design_spec_status": "skipped",
            "design_spec_acceptance_passed": None,
        },
        {
            "verdict": "HOLD",
            "pm_hold_spec_keywords": ["SVG path", "titleLabel", "ShareCard"],
            "design_spec_status": "insufficient",
            "design_spec_acceptance_passed": False,
        },
    ]
    progress_e = compute_hold_progress(history_e)
    section_e = "\n".join(_build_hold_progress_section(progress_e))
    if (
        progress_e["hold_repeat_count"] == 2
        and progress_e["more_concrete"] is True
        and progress_e["same_reason_as_prev"] is False
        and progress_e["next_action"] == "design_spec 보완 필요"
        and "HOLD 반복 횟수" in section_e
        and "다음 행동" in section_e
    ):
        passed += 1
    else:
        failures.append(
            f"20E: hold_progress — repeat={progress_e['hold_repeat_count']} "
            f"more_concrete={progress_e['more_concrete']} "
            f"same_reason={progress_e['same_reason_as_prev']} "
            f"next={progress_e['next_action']!r}"
        )

    # 20F. design_spec NOT YET written but PM HOLD → claude_rework_prompt.md
    #      created and lists `design_spec.md` as the next deliverable;
    #      claude_repair_prompt.md remains absent.
    total += 1
    repo = os.environ.get("LOCAL_RUNNER_REPO")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCAL_RUNNER_REPO"] = tmp
        try:
            runtime = Path(tmp) / ".runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "pm_decision.md").write_text(
                _PM_DECISION_FIXTURE_SPEC, encoding="utf-8"
            )
            (runtime / "designer_final_review.md").write_text(
                _DESIGNER_FINAL_REVIEW_FIXTURE, encoding="utf-8"
            )
            run = SmokeRun(mode="local-cycle", timeout_sec=1800)
            run.started_at = _utc_now_iso()
            run.finished_at = _utc_now_iso()
            run.verdict = "HOLD"
            run.factory_status = "hold_for_rework"
            run.cycle_id = 1
            run.ticket_status = "skipped_hold"
            run.pm_decision_ship_ready = False
            run.changed_files_count = 0
            run.pm_hold_spec_keywords = ["SVG path", "titleLabel"]
            run.design_spec_status = "skipped"
            fs = {
                "status": "hold_for_rework", "cycle": 1,
                "pm_decision_status": "generated",
                "pm_decision_ship_ready": False,
                "implementation_ticket_status": "skipped_hold",
                "pm_decision_message": "HOLD",
                "pm_hold_spec_keywords": ["SVG path", "titleLabel"],
                "design_spec_status": "skipped",
                "design_spec_acceptance_passed": False,
            }
            write_outputs(run, factory_state=fs, observer_classification=None)
            rework_text = (runtime / "claude_rework_prompt.md").read_text(
                encoding="utf-8"
            )
            ok = (
                (runtime / "claude_rework_prompt.md").is_file()
                and not (runtime / "claude_repair_prompt.md").exists()
                and "design_spec" in rework_text
                and "design_spec 우선 모드" in rework_text
            )
            if ok:
                passed += 1
            else:
                failures.append(
                    "20F: rework prompt without design_spec — "
                    "missing design_spec mention or wrong artifact set"
                )
        finally:
            if repo is not None:
                os.environ["LOCAL_RUNNER_REPO"] = repo
            else:
                os.environ.pop("LOCAL_RUNNER_REPO", None)

    # 20G. design_spec acceptance passed + PM SHIP → ticket extractor
    #      pulls target_files from design_spec; status would be
    #      `generated` (we exercise the PURE extractor path so we don't
    #      need to spawn claude).
    total += 1
    if _cycle is not None:
        # Mock-write design_spec.md and call the same parsers cycle.py
        # uses inside stage_implementation_ticket. We don't need to
        # actually run the stage — we want to assert that target_files
        # come from the design_spec body when acceptance passes.
        files_g = _cycle._extract_design_spec_target_files(
            _DESIGN_SPEC_FIXTURE_GOOD
        )
        valid_g = _cycle._validate_design_spec(_DESIGN_SPEC_FIXTURE_GOOD)
        if not valid_g and len(files_g) >= 3:
            passed += 1
        else:
            failures.append(
                f"20G: PM-SHIP path with design_spec — files={files_g} "
                f"valid_failures={valid_g[:2]}"
            )
    else:
        failures.append("20G: skipped — cycle import failed earlier")

    return passed, total, failures


def _maturity_fixture_entry(
    *,
    verdict: str,
    duration_sec: float,
    stage_durations: dict[str, float] | None = None,
    failure_code: str | None = None,
) -> dict:
    """Build a synthetic history entry for the maturity self-tests.

    Mirrors `_build_history_entry` so the same shape goes through
    compute_maturity_signal as a real smoke run would produce.
    """
    return {
        "started_at": "2026-05-01T00:00:00.000000Z",
        "ended_at": "2026-05-01T00:00:00.000000Z",
        "duration_sec": duration_sec,
        "verdict": verdict,
        "failure_code": failure_code,
        "current_stage": None,
        "last_stage": None,
        "stage_durations": stage_durations or {},
        "changed_files_count": 0,
        "qa_status": None,
        "pm_decision_ship_ready": None,
        "implementation_ticket_status": None,
        "human_action_required": _human_action_count_for(verdict),
        "repeated_failure_count": 0,
    }


# ---------------------------------------------------------------------------
# Acceptance fixtures (kept inline so factory_smoke remains stdlib-only)
# ---------------------------------------------------------------------------


_PM_DECISION_FIXTURE = """\
# Stampport PM Decision

## 출하 결정
hold (재작업 후 다음 사이클)

## 결정 이유
총점 19점으로 출하 기준 24점에 5점 미달이며, Visual Desire(3) 와 Share(3) 두 필수
게이트가 동시에 미달이다. selectedTitle 이 string 이라 Share.jsx 에서 level 파악
방식이 없고, 잠금 조건 progress === 0 이 Lv2/Lv3 잠금 슬롯 설계와 충돌한다.

## 출하 단위 (가장 작은)
- badges.js 에 level 필드 추가 후 Lv 분기
- ShareCard 칭호 라인을 share-foot 외부로 이동
- 원형/방패/왕관 SVG 3종 컴포넌트

## 다음 단계 담당
- 디자이너: 원형/방패/왕관 SVG 3종을 단일 컴포넌트로 설계.
- 기획자: selectedTitle string 문제 해결 방식 제안 (selectedTitleId 또는 titleLabel→id 역매핑).
- 프론트/백엔드: N/A

## QA가 추가로 점검할 것
- Lv.3 ShareCard gold serif 칭호가 iOS Safari 390px 에서 Lv.1 과 육안으로 구분되는지.
- 카페 1곳 방문 후 Badges 에서 cafe_lover/cafe_master 가 잠금 슬롯으로 노출되는지.
"""


_DESIGNER_FINAL_REVIEW_FIXTURE = """\
# Stampport Designer Final Review

## 첫인상
방향은 맞다.

## 욕구 점수표

| 축 | 점수 (1~5) | 이유 |
|---|---|---|
| Collection Score | 3 | 카테고리 하나에만 적용되어 반쪽이다 |
| Share Score | 3 | gold serif 방향은 맞으나 칭호가 share-foot 에 묻힘 |
| Progression Score | 4 | 진행 바 + 모달 조합은 명확 |
| Rarity Score | 2 | 잠금 조건이 progress === 0 과 충돌 |
| Revisit Score | 4 | 진행 바가 재방문 임계값을 낮춤 |
| Visual Desire Score | 3 | SVG 미결로 모바일 390px 렌더링 보장 X |

## 약점
selectedTitle 은 string 이라 Share.jsx 에서 level 파악 방식이 없다.
잠금 조건 progress === 0 은 Lv2/Lv3 잠금 슬롯 설계와 충돌한다.
ShareCard 칭호 라인이 share-foot 하단에 묻혀 카드 위계를 바꾸지 못한다.

## 개선 지침
- ShareCard 칭호 라인을 share-foot 외부 share-note 아래 독립 블록으로.
- 원형/방패/왕관 SVG 3종을 size prop 단일 컴포넌트로.

## 최종 판단
revise — 세 구멍이 막히지 않으면 MVP 구현 시 버그 + 시각 효과 미달 동시 발생.
"""


# A canonical-form planner report fixture used to test that the gate
# accepts both heading variants. Intentionally compact but covers all
# REQUIRED sections in `_validate_planner_report`. We split the
# multiline string with %s placeholders for the candidate-detail section
# so test 12 can swap headings without touching the rest of the body.
_STAMPPORT_PLANNER_FIXTURE = """\
# Stampport Product Planner Report

## 제품 방향
Stampport 는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스다.

## 사용자가 가진 욕구 중 가장 약한 곳
재방문 욕구가 가장 약하다. 방문 후 다시 열 이유가 없다.

## 현재 제품의 한계
`app/web/src/screens/MyPassport.jsx:42` 에서 다음 방문 신호가 부재.

## 이번 사이클의 가장 큰 병목
사용자가 다음 방문을 만들 시각/보상 신호가 없어 재방문 동기가 약하다. 근거: `app/web/src/screens/MyPassport.jsx:42`.

## 신규 기능 아이디어 후보

| 기능 | 자극하는 욕구(2개 이상) | 사용자 가치 | 구현 난이도 | 제품 임팩트 | 리스크 |
|---|---|---|---|---|---|
| 후보1 | 수집욕 + 과시욕 | 동네 정복 시각 증거 | 낮 | 중 | 카테고리 1개로 좁음 |
| 후보2 | 성장욕 + 재방문 | 칭호 진화 헤더 | 중 | 중 | 데이터 임계값 튜닝 필요 |
| 후보3 | 재방문 + 희소성 | 추천 슬롯 | 낮 | 중 | 추천이 강요로 보일 위험 |

## 후보 상세

### 후보 1: Local Visa 배지
- 사용자 욕구: 수집욕 + 과시욕 (동네별 시각 자산을 얻고 친구에게 자랑)
- 핵심 루프: 방문 → 도장 → Visa 발급 → ShareCard 공유
- MVP 구현 범위:
  - Visa 배지 1종
  - 자동 발급 룰 (dong_code 3회)
  - ShareCard 노출
- 기대 행동 변화: 같은 동네 재방문률 상승, ShareCard 열람 증가
- 디자이너에게 던질 질문:
  1. 도장과 어떻게 시각적으로 구분?
  2. 발급 모먼트가 충분히 emotional 한가?
  3. 카드 위 위치는 어디가 적절?

### 후보 2: Taste Title 진화
- 사용자 욕구: 성장욕 + 재방문 (단계별 진화 + 다음 단계 자극)
- 핵심 루프: 카테고리 누적 → 칭호 진화 → 헤더 갱신 → 친구 공유
- MVP 구현 범위:
  - 카테고리 1종
  - 3단계 임계값
  - 헤더 갱신
- 기대 행동 변화: 카테고리 집중 방문 비율 상승
- 디자이너에게 던질 질문:
  1. 진화 단계 시각언어?
  2. 진화 모멘트 모달?
  3. 칭호 폰트?

### 후보 3: Passport 발급 대기 슬롯
- 사용자 욕구: 재방문 + 희소성 (추천 슬롯이 다음 방문을 만든다)
- 핵심 루프: 추천 → 방문 → 도장 → 슬롯 갱신
- MVP 구현 범위:
  - 슬롯 3개
  - 룰 기반 추천
  - 갱신 표시
- 기대 행동 변화: 미방문 동네 방문률 증가
- 디자이너에게 던질 질문:
  1. 추천 톤?
  2. 슬롯 형태?
  3. 갱신 애니메이션?

## 이번 사이클 선정 기능
Local Visa 배지

## 선정 이유
가장 작은 변경 범위에서 수집/과시 욕구를 동시에 자극하고 BE 변경이 없다.
다른 후보를 채택하지 않은 이유:
- 후보2: 진화 단계 모달이 한 사이클 범위를 넘는다.
- 후보3: 추천 정확도 기준이 필요해 LLM 의존 발생.

## 사용자 시나리오
사용자는 단골 동네 카페 두 곳을 며칠에 걸쳐 방문해 도장을 찍는다. 세 번째 방문에서 Visa 가 자동 발급되고 MyPassport 헤더에 노출된다.

## 해결 방식 (자체 판단)
- 핵심 패턴: 방문 카운팅 + 임계값 기반 자동 발급
- 근거: 클라이언트 LocalStorage 만으로 결정론적으로 동작.

## LLM 필요 여부
- 불필요
- 이유: 결정론적 룰 기반.
- 입력: dong_code 카운터.
- 출력 JSON schema: { "visa": "string" }
- fallback 방식: 룰 기반 그대로.

## 데이터 저장 필요 여부
- 필요
- 클라이언트 LocalStorage 에 dong_code 별 카운터.

## 외부 연동 필요 여부
- 불필요
- 외부 SNS 게시는 다음 사이클.

## 프론트 변경 범위
- `app/web/src/screens/MyPassport.jsx` — Visa 헤더 노출
- `app/web/src/components/ShareCard.jsx` — Visa 일러스트 추가
- `app/web/src/components/VisaBadge.jsx` (신규)

## 백엔드 변경 범위
- 백엔드 변경 불필요 — 사유: 클라이언트 LocalStorage 기반 룰만 사용.

## 이번 사이클 MVP 범위
- Visa 배지 1종
- 자동 발급 룰
- ShareCard 노출

## 이번 사이클에서 하지 않을 것
- 서버 sync
- 카테고리 확장
- 외부 SNS 게시

## 디자이너에게 던질 질문
- 이 도장/뱃지/카드가 정말 갖고 싶게 보이는가?
- 이 카드가 인스타 스토리에 자랑하고 싶게 보이는가?
- 이 슬롯/장치가 다음 방문 욕구를 만드는가?

## 성공 기준
1. 동일 dong_code 도장 3회 누적 시 Visa 자동 발급
2. MyPassport / ShareCard 두 화면에 Visa 시각적 노출
3. ShareCard caption 에 Visa 라벨 포함
"""


# PM HOLD that explicitly cites SVG path / titleLabel / ShareCard / 좌표
# / locked — every keyword we want spec-mode to detect. Used by tests
# 20A / 20F.
_PM_DECISION_FIXTURE_SPEC = """\
# Stampport PM Decision

## 출하 결정
hold (재작업 후 다음 사이클)

## 결정 이유
디자이너가 제안한 SVG path 의 좌표가 비어 있고 (tier-2 방패 / tier-3 왕관 모두
숫자 없음), titleLabel 13개 최종 목록이 확정되지 않았다. ShareCard 의
\"영사까지 1곳 남음\" 보조 텍스트 조건도 미확정 — relatedBadge 가 null 일 때
렌더 여부가 결정되지 않아 layout 이 깨질 수 있다. selectedTitle 이 string
인 문제와 locked 슬롯의 progress === 0 충돌 문제가 그대로 남았다.

## 출하 단위 (가장 작은)
- badges.js 에 level / titleLabel 추가
- ShareCard 보조 텍스트 렌더 조건 명시
- SVG 3종 좌표 확정

## 다음 단계 담당
- 디자이너: design_spec.md 작성. SVG path 좌표 / titleLabel / ShareCard layout 모두 숫자/문자열로 확정.
- 기획자: 위 design_spec 의 각 항목을 후보 MVP 구현 범위에 미리 적어 둘 것.
- 프론트/백엔드: N/A

## QA가 추가로 점검할 것
- 390×560 카드 안에서 SVG 3종이 모두 의도한 위치/크기로 렌더되는가.
- relatedBadge null 일 때 보조 텍스트가 렌더되지 않는가.
"""


# A "good" design_spec.md — every required section, numeric SVG paths
# for tiers 2/3, 13+ titleLabels, 5 target_files.
_DESIGN_SPEC_FIXTURE_GOOD = """\
# Stampport Design Implementation Spec

## 구현 대상 기능
- 기능명: 칭호 진화 카드 (TitleSeal)
- 관련 PM HOLD 사유: SVG path 좌표 부재, titleLabel 미확정, ShareCard layout 미결.

## SVG Path 명세

### Tier 1 원형
- viewBox: 0 0 80 80
- 정의: <circle cx=40 cy=40 r=30 stroke="#c9a23a" fill="#fff" stroke-width="2"/>
- stroke / fill: gold / paper

### Tier 2 방패
- viewBox: 0 0 80 80
- path: M10,8 L70,8 L70,48 C70,62 56,72 40,76 C24,72 10,62 10,48 Z
- stroke / fill / stroke-width: gold / paper / 2

### Tier 3 왕관
- viewBox: 0 0 80 80
- path: M12,58 L18,24 L32,42 L40,16 L48,42 L62,24 L68,58 Z
- stroke / fill / stroke-width: gold / cream / 2

## titleLabel 최종 목록
- cafe_starter: 카페 입문자
- cafe_lover: 카페 영사
- cafe_master: 카페 대사
- bakery_starter: 베이커리 견습관
- bakery_lover: 베이커리 영사
- bakery_master: 베이커리 대사
- restaurant_starter: 미식 입문자
- restaurant_lover: 미식 영사
- restaurant_master: 미식 대사
- dessert_starter: 디저트 입문자
- dessert_lover: 디저트 영사
- dessert_master: 디저트 대사
- traveler_starter: 동네 탐험가

## badges.js 스키마 변경
- level: 1 / 2 / 3
- tier: starter / lover / master
- titleLabel: 위 목록의 한 항목
- lockedUntilLevel: number
- currentTitleLevel = max(unlocked.level)

## ShareCard 레이아웃 명세
- share-title-seal: share-note 블록 바로 아래 독립 블록
- share-foot 의 기존 Lv 텍스트: 제거
- "<title>까지 N곳 남음" 보조 텍스트:
  - relatedBadge 가 있을 때만 렌더
  - relatedBadge 가 null 이면 미렌더
- share-canvas: max-width 390 / max-height 560 / overflow hidden / share-note line-clamp 3

## 수정 대상 파일
- app/web/src/data/badges.js
- app/web/src/screens/Badges.jsx
- app/web/src/screens/Share.jsx
- app/web/src/components/TitleSeal.jsx
- app/web/src/components/TitleEvolveModal.jsx

## QA 기준
- iOS Safari 390px 에서 Lv1 / Lv3 카드 육안 구분
- share-note 100자 이상에서 share-canvas 가 560px 를 넘지 않는다
- relatedBadge null 시 보조 텍스트 미렌더
- Lv1 사용자가 tier-2 badge 선택 시 잠금 슬롯 스타일이 렌더된다
"""


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
    if run.verdict == "HOLD":
        print(f"  rework=.runtime/claude_rework_prompt.md")


if __name__ == "__main__":
    raise SystemExit(main())
