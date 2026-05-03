"""Stampport Auto Pilot Publish loop.

Drives the factory cycle from the Control Tower button — no terminal.
Each cycle runs `factory_smoke --mode local-cycle`, parses the verdict
from `factory_smoke_state.json`, then either:

  * commits + pushes (auto_publish), runs render smoke + production
    health, and continues to the next cycle, or
  * commits only (auto_commit), or
  * runs the cycle without touching git (safe_run), or
  * stops on FAIL / TIMEOUT / scope_mismatch / HOLD-with-dirty-tree.

State is persisted to `.runtime/autopilot_state.json` and a final
`.runtime/autopilot_report.md` is written on stop. Both are surfaced
through the runner's heartbeat so the dashboard can render the panel
without polling the runtime directory.

The module is stdlib-only by design — same constraint as factory_smoke
— so a runner that ships with only the stdlib can still execute it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths + tiny helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(os.environ.get("LOCAL_RUNNER_REPO", str(Path.cwd())))


def _runtime_dir() -> Path:
    return _repo_root() / ".runtime"


def _state_path() -> Path:
    return _runtime_dir() / "autopilot_state.json"


def _report_path() -> Path:
    return _runtime_dir() / "autopilot_report.md"


def _smoke_state_path() -> Path:
    return _runtime_dir() / "factory_smoke_state.json"


def _factory_state_path() -> Path:
    return _runtime_dir() / "factory_state.json"


def _log_path() -> Path:
    return _runtime_dir() / "autopilot.log"


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _read_json(path: Path) -> dict:
    try:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        sys.stderr.write(f"[autopilot] write_json failed: {exc}\n")


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[autopilot] write_text failed: {exc}\n")


def _log(line: str) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{_utc_now()}] {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Config / state types
# ---------------------------------------------------------------------------


VALID_MODES = ("safe_run", "auto_commit", "auto_publish")


@dataclass
class AutopilotConfig:
    autopilot_enabled: bool = True
    autopilot_mode: str = "safe_run"
    max_cycles: int = 5
    max_hours: float = 6.0
    stop_on_hold: bool = True
    require_scope_consistency: bool = True
    require_render_check: bool = True
    require_api_health: bool = True
    smoke_timeout_sec: int = 3600
    # Always-on policies (the spec calls them out as constants).
    stop_on_fail: bool = True
    stop_on_scope_mismatch: bool = True

    @classmethod
    def from_payload(cls, payload: dict | None) -> "AutopilotConfig":
        # Accept mode under either `autopilot_mode` (canonical) or `mode`
        # (defensive — both the dashboard and the autopilot --mode CLI
        # used to disagree on the field name; supporting both means a
        # rename in one place can never silently fall through to
        # safe_run again).
        payload = payload or {}
        raw_mode = (
            payload.get("autopilot_mode")
            if payload.get("autopilot_mode") is not None
            else payload.get("mode")
        )
        if raw_mode is None:
            # Distinct from "client said safe_run" — surface a real
            # error so the operator sees `start_autopilot` rejected
            # instead of a silent downgrade. Caller (_h_start_autopilot)
            # turns this into a False/message tuple.
            raise ValueError(
                "autopilot_mode missing from payload — refusing to default "
                "to safe_run silently. Send autopilot_mode = "
                f"{'/'.join(VALID_MODES)}."
            )
        mode = str(raw_mode).strip()
        if mode not in VALID_MODES:
            raise ValueError(
                f"autopilot_mode={mode!r} not in {VALID_MODES}"
            )
        try:
            max_cycles = int(payload.get("max_cycles") or 5)
        except (TypeError, ValueError):
            max_cycles = 5
        try:
            max_hours = float(payload.get("max_hours") or 6.0)
        except (TypeError, ValueError):
            max_hours = 6.0
        try:
            smoke_timeout = int(payload.get("smoke_timeout_sec") or 3600)
        except (TypeError, ValueError):
            smoke_timeout = 3600

        # Booleans need to round-trip JSON honestly. `bool(payload.get(k,
        # True))` is wrong when the client sends literal False — Python's
        # bool() of False is False, which IS what we want, but the prior
        # `payload.get(k, True)` form would also coerce a missing key to
        # True. Keep that "missing → True" behaviour for backward compat
        # but make the explicit-False path unambiguous.
        def _bool_with_default(key: str, default: bool) -> bool:
            v = payload.get(key, default)
            if isinstance(v, str):
                return v.strip().lower() in {"1", "true", "yes", "on"}
            return bool(v)

        return cls(
            autopilot_enabled=_bool_with_default("autopilot_enabled", True),
            autopilot_mode=mode,
            max_cycles=max(1, min(max_cycles, 50)),
            max_hours=max(0.1, min(max_hours, 48.0)),
            stop_on_hold=_bool_with_default("stop_on_hold", True),
            require_scope_consistency=_bool_with_default(
                "require_scope_consistency", True
            ),
            require_render_check=_bool_with_default("require_render_check", True),
            require_api_health=_bool_with_default("require_api_health", True),
            smoke_timeout_sec=max(60, min(smoke_timeout, 7200)),
        )


@dataclass
class CycleRecord:
    cycle: int
    started_at: str
    finished_at: str | None = None
    verdict: str | None = None
    failure_code: str | None = None
    factory_status: str | None = None
    qa_status: str | None = None
    scope_consistency_status: str | None = None
    changed_files_count: int = 0
    publish_action: str | None = None  # commit | push | skip | none
    commit_hash: str | None = None
    push_status: str | None = None     # succeeded | failed | skipped | noop
    health_status: str | None = None   # passed | failed | skipped
    render_status: str | None = None   # passed | failed | skipped
    note: str | None = None
    # HOLD loop breaker telemetry — every cycle records the per-stage
    # gate evidence so the autopilot report can show *why* a HOLD cycle
    # didn't reach claude_apply, instead of forcing the operator to
    # grep three different artifacts. These fields are mirrored from
    # factory_smoke_state.json + factory_state.json at record time.
    selected_feature: str | None = None
    selected_feature_id: str | None = None
    design_spec_feature_id: str | None = None
    implementation_ticket_feature_id: str | None = None
    apply_preflight_status: str | None = None
    run_id: str | None = None
    pm_verdict: str | None = None              # SHIP | HOLD | —
    hold_type: str | None = None               # soft | hard | None
    hold_type_reason: str | None = None
    design_spec_status: str | None = None
    design_spec_acceptance_passed: bool | None = None
    stale_design_spec_detected: bool = False
    implementation_ticket_status: str | None = None
    claude_propose_status: str | None = None
    claude_apply_status: str | None = None
    code_changed: bool = False
    active_rework_feature: str | None = None
    active_rework_hold_count: int = 0
    planner_feature_drift_detected: bool = False


@dataclass
class AutopilotState:
    status: str = "idle"  # idle | running | stopped | failed
    mode: str = "safe_run"
    started_at: str | None = None
    ended_at: str | None = None
    updated_at: str | None = None
    cycle_count: int = 0
    max_cycles: int = 5
    max_hours: float = 6.0
    stop_on_hold: bool = True
    stop_on_fail: bool = True
    stop_on_scope_mismatch: bool = True
    require_scope_consistency: bool = True
    require_render_check: bool = True
    require_api_health: bool = True
    last_verdict: str | None = None
    last_failure_code: str | None = None
    last_commit_hash: str | None = None
    last_push_status: str | None = None
    last_health_status: str | None = None
    last_render_status: str | None = None
    last_note: str | None = None
    stop_reason: str | None = None
    report_path: str | None = None
    history: list[dict] = field(default_factory=list)
    # Stuck-before-first-cycle diagnostic fields. The autopilot loop
    # writes these around every smoke spawn so the dashboard can tell
    # the difference between "loop is alive but blocked on a slow
    # smoke" and "loop is alive and stuck — first smoke never spawned".
    #   first_cycle_spawn_at      — set the first time the loop calls
    #                                _run_smoke_cycle. None until then.
    #   current_cycle_started_at  — set at the start of every smoke
    #                                spawn. UI compares vs finished_at
    #                                to detect a cycle in flight.
    #   current_cycle_finished_at — set when the smoke subprocess
    #                                returns. >= started_at when idle.
    first_cycle_spawn_at: str | None = None
    current_cycle_started_at: str | None = None
    current_cycle_finished_at: str | None = None
    # Active cycle markers. The dashboard's stuck-before-first-cycle
    # diagnostic was firing while a real smoke subprocess was alive
    # because we hadn't published the live cycle index. We now write
    # active_cycle_index = cycle_count + 1 the moment we spawn factory
    # smoke and clear it when the spawn returns; current_stage mirrors
    # the cycle-py stage name so the office scene can route the
    # working agent. live_report_path is `.runtime/autopilot_live_
    # report.md` while running so the UI can distinguish "current run
    # status" from "last final autopilot_report.md".
    active_cycle_index: int | None = None
    current_stage: str | None = None
    live_report_path: str | None = None
    # Claude Executor Contract — mirrored from
    # .runtime/claude_executor_state.json after every cycle so the UI
    # / report doesn't have to grep stderr files. claude_apply_retry_pending
    # is set when the previous cycle's CLI failure is retryable AND the
    # ticket+proposal artifacts are intact; the next _run_smoke_cycle
    # call exports FACTORY_APPLY_RETRY_ONLY=true so cycle.py skips
    # planning stages and re-runs claude_apply only.
    claude_executor_status: str | None = None
    claude_executor_failure_code: str | None = None
    claude_executor_failure_reason: str | None = None
    claude_executor_retryable: bool = False
    claude_executor_retry_count: int = 0
    claude_executor_stdout_path: str | None = None
    claude_executor_stderr_path: str | None = None
    claude_executor_command: str | None = None
    claude_executor_duration_sec: float | None = None
    claude_apply_retry_pending: bool = False
    # current_run_id — unique identifier for the autopilot run that
    # owns this state. Generated at run_loop start, propagated via the
    # FACTORY_RUN_ID env var to every cycle.py / factory_smoke spawn,
    # and recorded on every artifact those subprocesses write. Smoke /
    # observer / dashboard use it as the primary freshness key — same
    # cycle_id but different run_id == PREVIOUS RUN, never CURRENT.
    current_run_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AutopilotState":
        if not payload:
            return cls()
        kwargs = {}
        for f_name in cls.__dataclass_fields__:
            if f_name in payload:
                kwargs[f_name] = payload[f_name]
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Threading + state I/O
# ---------------------------------------------------------------------------


_LOCK = threading.RLock()
_THREAD: "threading.Thread | None" = None
_STOP_EVENT = threading.Event()
_STATE: AutopilotState = AutopilotState()

# Cadence for the smoke heartbeat thread. 20s matches the runner's
# heartbeat interval so the dashboard sees a fresh updated_at every
# heartbeat tick even when factory_smoke runs for an hour.
_SMOKE_HEARTBEAT_INTERVAL_SEC = 20.0


def _smoke_heartbeat(stop_event: threading.Event) -> None:
    """Background thread that mirrors factory_state.current_stage into
    autopilot_state.current_stage while a smoke subprocess blocks the
    main loop. Without this, a long product_planning / designer_critique
    leaves autopilot_state.current_stage frozen on "factory_smoke" and
    the dashboard can't say "Designer가 비평 중".

    Stops as soon as the parent loop sets stop_event.
    """
    while not stop_event.is_set():
        try:
            fs = _read_json(_factory_state_path()) or {}
            stage = fs.get("current_stage")
            with _LOCK:
                if stage:
                    _STATE.current_stage = str(stage)
                # `_save_state` always bumps updated_at — that alone is
                # enough to convince the dashboard the loop is alive.
            _save_state()
        except Exception:  # noqa: BLE001
            # Heartbeat is best-effort; never let a transient read
            # error kill the cycle loop.
            pass
        # Use Event.wait so a stop is acknowledged immediately; we
        # don't want to oversleep when the main loop calls .set()
        # right after the smoke subprocess returns.
        stop_event.wait(_SMOKE_HEARTBEAT_INTERVAL_SEC)


def _write_live_report(cycle: int, config: "AutopilotConfig",
                       last_smoke: dict | None = None) -> None:
    """Append a tiny markdown snapshot to .runtime/autopilot_live_
    report.md while running. The dashboard surfaces this path during
    a live run so the operator isn't reading the previous run's
    autopilot_report.md by mistake. Final autopilot_report.md is
    still written on stop by `_write_report` below.
    """
    try:
        path = _runtime_dir() / "autopilot_live_report.md"
        snap = _STATE.to_dict() if _STATE else {}
        lines = [
            f"# Auto Pilot Live Report (cycle {cycle})",
            "",
            f"- updated_at: `{snap.get('updated_at') or _utc_now()}`",
            f"- status: `{snap.get('status')}`",
            f"- mode: `{config.autopilot_mode}`",
            f"- active_cycle_index: `{snap.get('active_cycle_index')}`",
            f"- cycle_count: `{snap.get('cycle_count')}` / max `{config.max_cycles}`",
            f"- current_stage: `{snap.get('current_stage')}`",
            f"- current_cycle_started_at: `{snap.get('current_cycle_started_at')}`",
            f"- current_cycle_finished_at: `{snap.get('current_cycle_finished_at')}`",
            "",
            ("> 진행 중" if snap.get("active_cycle_index") else "> 사이클 완료 — 다음 사이클 대기"),
        ]
        if last_smoke:
            lines += [
                "",
                "## last smoke result",
                f"- verdict: `{last_smoke.get('verdict')}`",
                f"- failure_code: `{last_smoke.get('failure_code')}`",
            ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[autopilot] live_report write failed: {exc}\n")


def _save_state() -> None:
    with _LOCK:
        _STATE.updated_at = _utc_now()
        _write_json(_state_path(), _STATE.to_dict())


def load_state() -> dict:
    """Read the on-disk state. Used by heartbeat builders."""
    return _read_json(_state_path())


def is_running() -> bool:
    with _LOCK:
        return _STATE.status == "running" and not _STOP_EVENT.is_set()


def initial_state(payload: dict | None = None) -> dict:
    """Return the on-disk state if it exists, else an idle skeleton."""
    state = load_state()
    if state:
        return state
    return AutopilotState().to_dict()


# ---------------------------------------------------------------------------
# Smoke runner adapter
# ---------------------------------------------------------------------------


def _run_smoke_cycle(timeout_sec: int) -> dict:
    """Spawn `factory_smoke --mode local-cycle` and return the parsed
    smoke state. On subprocess errors we synthesize a FAIL verdict."""
    started = _utc_now()
    cmd = [
        sys.executable, "-m", "control_tower.local_runner.factory_smoke",
        "--mode", "local-cycle",
        "--timeout", str(int(timeout_sec)),
    ]
    repo = str(_repo_root())
    # Propagate the autopilot's run_id to factory_smoke / cycle.py so
    # every artifact written during this smoke cycle carries the same
    # id (and the freshness verdict survives cycle-counter collisions
    # across runs). Falls back to a one-shot id if the autopilot didn't
    # mint one — manual factory_smoke probes get their own per-process
    # run_id this way.
    env = os.environ.copy()
    rid = (_STATE.current_run_id or "").strip() if _STATE else ""
    if rid:
        env["FACTORY_RUN_ID"] = rid
    # Apply-only retry path. When the previous cycle hit a retryable
    # claude_apply CLI failure AND the ticket+proposal artifacts are
    # intact, run the next cycle in apply_retry_only mode so cycle.py
    # skips every planner/designer stage and re-runs claude_apply only.
    # The flag is cleared after the spawn so a subsequent unrelated
    # cycle is a normal full run.
    retry_pending = False
    with _LOCK:
        retry_pending = bool(_STATE.claude_apply_retry_pending)
        if retry_pending:
            _STATE.claude_apply_retry_pending = False
    if retry_pending:
        env["FACTORY_APPLY_RETRY_ONLY"] = "true"
        _log("smoke spawn — apply_retry_only=true (claude executor retry path)")
    _log(f"smoke spawn timeout={timeout_sec}s run_id={env.get('FACTORY_RUN_ID') or '—'} cmd={' '.join(cmd)}")
    try:
        # Wall-clock cap = smoke timeout + 5min buffer. The smoke runner
        # has its own internal deadline; we don't want to kill it early.
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 300,
            env=env,
        )
        rc = proc.returncode
        tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
        _log(f"smoke exit rc={rc} tail={tail[:200]}")
    except subprocess.TimeoutExpired as exc:
        _log(f"smoke wall-clock timeout: {exc}")
        return {
            "verdict": "FAIL",
            "failure_code": "smoke_timeout",
            "failure_reason": (
                f"factory_smoke wall-clock timeout ({timeout_sec + 300}s)"
            ),
            "started_at": started,
            "finished_at": _utc_now(),
        }
    except Exception as exc:  # noqa: BLE001
        _log(f"smoke spawn failed: {exc}")
        return {
            "verdict": "FAIL",
            "failure_code": "smoke_spawn_failed",
            "failure_reason": str(exc)[:400],
            "started_at": started,
            "finished_at": _utc_now(),
        }

    # The smoke runner writes the state file regardless of exit code.
    state = _read_json(_smoke_state_path())
    if not state:
        return {
            "verdict": "FAIL",
            "failure_code": "smoke_state_missing",
            "failure_reason": "factory_smoke_state.json absent after run",
            "started_at": started,
            "finished_at": _utc_now(),
            "cycle_subprocess_exit": rc,
        }
    # Normalize the subprocess exit so callers can see it.
    state.setdefault("cycle_subprocess_exit", rc)
    return state


# ---------------------------------------------------------------------------
# Pre-publish gate
# ---------------------------------------------------------------------------


READY_VERDICTS = frozenset({"READY_TO_REVIEW", "READY_TO_PUBLISH", "PASS"})
HOLD_VERDICTS = frozenset({"HOLD"})
FAIL_VERDICTS = frozenset({"FAIL"})

# After this many consecutive HOLD cycles with code_changed=False, the
# autopilot loop terminates with failure_code=no_progress_hold_loop. The
# operator should inspect why the rework loop is unable to ship — usually
# the planner or design_spec stage is mis-classifying soft HOLD inputs
# as hard.
NO_CHANGE_HOLD_STOP_THRESHOLD = 3
# The threshold past which we WARN (force the locked feature, force
# design_spec, force implementation_ticket) but do NOT yet stop. Lives
# in cycle.py via _save_active_rework_feature; the autopilot just
# tracks the same number for the report.
NO_CHANGE_HOLD_WARN_THRESHOLD = 2


def _populate_cycle_record_from_state(
    rec: CycleRecord, smoke: dict, factory_state: dict
) -> None:
    """Fill the CycleRecord HOLD-loop telemetry from the latest smoke +
    factory_state. Idempotent — the loop calls this whenever a smoke
    cycle returns so the stop-on-loop classifier has fresh data."""
    fs = factory_state or {}
    sm = smoke or {}
    rec.selected_feature = (
        fs.get("selected_feature")
        or fs.get("planner_revision_selected_feature")
        or fs.get("product_planner_selected_feature")
        or sm.get("selected_feature")
    )
    rec.selected_feature_id = fs.get("selected_feature_id") or sm.get(
        "selected_feature_id"
    )
    rec.design_spec_feature_id = fs.get("design_spec_feature_id") or sm.get(
        "design_spec_feature_id"
    )
    rec.implementation_ticket_feature_id = fs.get(
        "implementation_ticket_feature_id"
    ) or sm.get("implementation_ticket_feature_id")
    rec.apply_preflight_status = fs.get("apply_preflight_status") or sm.get(
        "apply_preflight_status"
    )
    rec.run_id = fs.get("run_id") or sm.get("run_id")
    pm_ship = fs.get("pm_decision_ship_ready")
    if pm_ship is True:
        rec.pm_verdict = "SHIP"
    elif pm_ship is False:
        rec.pm_verdict = "HOLD"
    else:
        rec.pm_verdict = None
    rec.hold_type = fs.get("pm_hold_type") or sm.get("pm_hold_type")
    rec.hold_type_reason = (
        fs.get("pm_hold_type_reason") or sm.get("pm_hold_type_reason")
    )
    rec.design_spec_status = fs.get("design_spec_status") or sm.get(
        "design_spec_status"
    )
    dsap = fs.get("design_spec_acceptance_passed")
    if dsap is None:
        dsap = sm.get("design_spec_acceptance_passed")
    if dsap is not None:
        rec.design_spec_acceptance_passed = bool(dsap)
    rec.stale_design_spec_detected = bool(
        fs.get("stale_design_spec_detected")
        or sm.get("stale_design_spec_detected")
    )
    rec.implementation_ticket_status = fs.get(
        "implementation_ticket_status"
    ) or sm.get("ticket_status")
    rec.claude_propose_status = fs.get("claude_proposal_status")
    rec.claude_apply_status = fs.get("claude_apply_status") or sm.get(
        "claude_apply_status"
    )
    rec.code_changed = bool(
        fs.get("code_changed")
        or sm.get("code_changed")
        or len(fs.get("claude_apply_changed_files") or []) > 0
    )
    rec.active_rework_feature = fs.get("active_rework_feature") or sm.get(
        "active_rework_feature"
    )
    arwc = fs.get("active_rework_hold_count")
    if not isinstance(arwc, int):
        arwc = sm.get("active_rework_hold_count") or 0
    try:
        rec.active_rework_hold_count = int(arwc)
    except (TypeError, ValueError):
        rec.active_rework_hold_count = 0
    rec.planner_feature_drift_detected = bool(
        fs.get("planner_feature_drift_detected")
        or sm.get("planner_feature_drift_detected")
    )


def _consecutive_no_change_holds(history: list[dict]) -> int:
    """Walk the history backwards and count consecutive HOLD cycles
    whose code_changed flag is False. Stops at the first cycle that is
    NOT a HOLD-no-change (READY/SHIP, FAIL, or HOLD with code_changed
    True)."""
    count = 0
    for h in reversed(history or []):
        verdict = (h.get("verdict") or "").upper()
        code_changed = bool(h.get("code_changed"))
        if verdict == "HOLD" and not code_changed:
            count += 1
            continue
        break
    return count


def _max_cycles_boundary_classification(
    history: list[dict], max_cycles: int
) -> str | None:
    """Decide whether reaching max_cycles should terminate the loop as
    `no_progress_hold_loop` instead of a benign `max_cycles reached` stop.

    Policy (intentionally narrower than the in-loop trigger so a single
    HOLD on a max_cycles=1 manual probe is NOT treated as a broken
    rework loop):

      * `max_cycles >= NO_CHANGE_HOLD_STOP_THRESHOLD` (>= 3) — short
        runs are operator probes, not loop diagnostics.
      * `len(history) >= NO_CHANGE_HOLD_STOP_THRESHOLD` — the run
        actually executed enough cycles to assess.
      * The last `NO_CHANGE_HOLD_STOP_THRESHOLD` cycles must ALL be
        `HOLD` with `code_changed=False`.

    Returns "no_progress_hold_loop" when the policy fires, else None.
    """
    if max_cycles < NO_CHANGE_HOLD_STOP_THRESHOLD:
        return None
    if len(history) < NO_CHANGE_HOLD_STOP_THRESHOLD:
        return None
    last = history[-NO_CHANGE_HOLD_STOP_THRESHOLD:]
    all_hold_no_change = all(
        (h.get("verdict") or "").upper() == "HOLD"
        and not bool(h.get("code_changed"))
        for h in last
    )
    return "no_progress_hold_loop" if all_hold_no_change else None


def _git_status_porcelain() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(_repo_root()), "status", "--porcelain"],
            capture_output=True, text=True, timeout=20,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _git_dirty() -> bool:
    return bool(_git_status_porcelain().strip())


def _git_branch() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(_repo_root()), "rev-parse",
             "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


# Secret-shaped path substrings that block publish from autopilot.
# Mirrors RISKY_PUBLISH_PATTERNS in runner.py — kept inline so the
# autopilot module doesn't import runner.py (circular).
RISKY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".pem",
    ".key",
    "/runtime/",
    "/node_modules/",
    "__pycache__",
    ".claude/settings.local.json",
)


def _scan_changed_files_for_risk(paths: list[str]) -> list[str]:
    hits = []
    for p in paths or []:
        low = p.lower()
        if any(pat in low for pat in RISKY_PATTERNS):
            hits.append(p)
    return hits


def _claude_executor_state_path() -> Path:
    return _runtime_dir() / "claude_executor_state.json"


def _resolve_claude_executor_state(factory_state: dict) -> dict:
    """Return the latest Claude Executor verdict.

    Prefers .runtime/claude_executor_state.json (the kernel artifact
    cycle.py / stage_claude_preflight write directly) so the autopilot
    retry policy is reading the same bytes the dashboard renders.
    Falls back to the claude_executor_* fields on factory_state.json if
    the dedicated file is missing.
    """
    payload = _read_json(_claude_executor_state_path())
    if payload:
        return payload
    fs = factory_state or {}
    if not fs.get("claude_executor_status"):
        return {}
    return {
        "status": fs.get("claude_executor_status"),
        "stage": fs.get("claude_executor_stage"),
        "command": fs.get("claude_executor_command"),
        "exit_code": fs.get("claude_executor_exit_code"),
        "timed_out": fs.get("claude_executor_timed_out"),
        "duration_sec": fs.get("claude_executor_duration_sec"),
        "failure_code": fs.get("claude_executor_failure_code"),
        "failure_reason": fs.get("claude_executor_failure_reason"),
        "stdout_path": fs.get("claude_executor_stdout_path"),
        "stderr_path": fs.get("claude_executor_stderr_path"),
        "retryable": fs.get("claude_executor_retryable"),
        "retry_count": fs.get("claude_executor_retry_count"),
    }


def _mirror_executor_into_state(executor: dict) -> None:
    """Mirror the executor verdict onto AutopilotState. The Control
    Tower UI reads autopilot_state.json directly for the executor
    panel — without this mirror the dashboard would have to load a
    second file."""
    if not executor:
        return
    with _LOCK:
        _STATE.claude_executor_status = executor.get("status") or _STATE.claude_executor_status
        _STATE.claude_executor_failure_code = executor.get("failure_code")
        _STATE.claude_executor_failure_reason = executor.get("failure_reason")
        _STATE.claude_executor_retryable = bool(executor.get("retryable"))
        try:
            _STATE.claude_executor_retry_count = int(executor.get("retry_count") or 0)
        except (TypeError, ValueError):
            _STATE.claude_executor_retry_count = 0
        _STATE.claude_executor_stdout_path = executor.get("stdout_path")
        _STATE.claude_executor_stderr_path = executor.get("stderr_path")
        _STATE.claude_executor_command = executor.get("command")
        _STATE.claude_executor_duration_sec = executor.get("duration_sec")


def _is_executor_failure_code(code: str | None) -> bool:
    if not code:
        return False
    code = str(code).strip()
    return code.startswith("claude_cli_") or code.startswith("claude_apply_")


def _resolve_pipeline_decision(factory_state: dict) -> dict:
    """Return the PipelineDecision contract for `factory_state`.

    Prefers the contract cycle.py wrote into factory_state.json directly
    so smoke / autopilot / observer all agree byte-for-byte. Falls back
    to recomputing via cycle.build_pipeline_decision when the field is
    missing — that path keeps older runtime files (written before the
    contract existed) working without forcing a re-run.
    """
    decision = factory_state.get("pipeline_decision")
    if isinstance(decision, dict) and decision:
        return decision
    try:
        # Local import — autopilot.py is stdlib-only as a hard rule but
        # cycle.py lives in the same package, so this stays import-safe.
        from . import cycle as _cycle
        return _cycle.build_pipeline_decision(factory_state)
    except Exception:  # noqa: BLE001
        return {
            "pipeline_status": "blocked",
            "can_commit": False,
            "can_push": False,
            "can_publish": False,
            "blocking_code": "pipeline_decision_unavailable",
            "blocking_reason": (
                "factory_state.pipeline_decision missing and "
                "build_pipeline_decision import failed"
            ),
            "checks": {},
            "evidence": {},
        }


def evaluate_publish_gate(
    smoke_state: dict, factory_state: dict, *, require_scope: bool
) -> tuple[bool, str | None, dict]:
    """Apply the auto-publish pre-conditions.

    Delegates the cycle-level publish/commit/push verdict to
    `pipeline_decision.can_publish` (the single source of truth) and
    layers autopilot-specific guards on top:
      * smoke verdict must be READY/PASS
      * no risky paths in the change set
      * git branch must be `main`

    `require_scope` is preserved for backwards compatibility but no
    longer fails the gate on its own when every other publish check
    passes — that legacy behaviour was the source of the
    "scope_consistency_status=null blocks publish" bug.

    Returns (ok, failure_reason, evidence_dict).
    """
    decision = _resolve_pipeline_decision(factory_state)
    changed_files = factory_state.get("claude_apply_changed_files") or []

    evidence: dict[str, Any] = {
        "factory_status": factory_state.get("status"),
        "qa_status": factory_state.get("qa_status"),
        "scope_consistency_status": factory_state.get("scope_consistency_status"),
        "scope_mismatch_reason": factory_state.get("scope_mismatch_reason"),
        "implementation_ticket_status": factory_state.get(
            "implementation_ticket_status"
        ),
        "claude_apply_status": factory_state.get("claude_apply_status"),
        "apply_preflight_status": factory_state.get("apply_preflight_status"),
        "design_spec_acceptance_passed": factory_state.get(
            "design_spec_acceptance_passed"
        ),
        "design_spec_status": factory_state.get("design_spec_status"),
        "changed_files_count": len(changed_files),
        "branch": _git_branch(),
        "pipeline_status": decision.get("pipeline_status"),
        "pipeline_blocking_code": decision.get("blocking_code"),
        "pipeline_blocking_reason": decision.get("blocking_reason"),
        "pipeline_checks": decision.get("checks") or {},
    }

    if not decision.get("can_publish"):
        code = decision.get("blocking_code") or "pipeline_not_ready"
        reason_text = decision.get("blocking_reason") or "pipeline gate not ready"
        return False, f"{code}: {reason_text}", evidence

    # Hard scope-mismatch override: even when can_publish=true, if the
    # legacy scope check explicitly reports `failed` and the operator
    # asked for scope enforcement, refuse — that's a real diff/spec
    # divergence, not the legacy null-field bug.
    if require_scope:
        scope = (factory_state.get("scope_consistency_status") or "").strip()
        if scope == "failed":
            reason = factory_state.get("scope_mismatch_reason") or "(no reason)"
            return False, (
                f"scope_consistency_status='failed' ({reason})"
            ), evidence

    risky = _scan_changed_files_for_risk(list(changed_files))
    if risky:
        evidence["risky_changed_files"] = risky[:10]
        return False, (
            f"risky paths in changed_files: {', '.join(risky[:3])}"
        ), evidence

    sv = (smoke_state.get("verdict") or "").strip()
    if sv not in READY_VERDICTS:
        return False, f"smoke verdict={sv} not READY/PASS", evidence

    branch = evidence["branch"]
    if branch and branch != "main":
        return False, f"branch={branch}, expected main", evidence

    return True, None, evidence


# ---------------------------------------------------------------------------
# Render smoke
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def render_smoke(
    *,
    web_dir: Path | None = None,
    extra_routes: tuple[str, ...] = ("/passport", "/share/demo"),
    overall_timeout_sec: float = 240.0,
) -> dict:
    """Build app/web and verify the bundle renders.

    Steps:
        1. npm run build (must exit 0)
        2. spin up `npx vite preview --host 127.0.0.1 --port <free>`
        3. poll http://127.0.0.1:<port>/ until 200 with <div id="root">
        4. probe extra SPA routes (hash-routed app: same payload — we
           only care about HTTP 200 + presence of the root element).
        5. tear down preview process.

    Returns a dict with keys: ok, status, build_ok, preview_ok,
    routes_ok, message, log_tail.
    """
    web = web_dir or (_repo_root() / "app" / "web")
    out: dict[str, Any] = {
        "ok": False,
        "status": "skipped",
        "build_ok": False,
        "preview_ok": False,
        "routes_ok": [],
        "message": "",
        "log_tail": "",
    }
    if not web.is_dir():
        out["message"] = f"web dir missing: {web}"
        out["status"] = "skipped"
        return out

    deadline = time.time() + overall_timeout_sec

    # 1. npm run build.
    try:
        r = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(web),
            capture_output=True, text=True,
            timeout=max(30.0, deadline - time.time()),
        )
        out["build_ok"] = (r.returncode == 0)
        out["log_tail"] = ((r.stdout or "")[-800:]
                           + (r.stderr or "")[-400:])
        if not out["build_ok"]:
            out["status"] = "failed"
            out["message"] = f"npm run build failed (rc={r.returncode})"
            return out
    except subprocess.TimeoutExpired:
        out["status"] = "failed"
        out["message"] = "npm run build timed out"
        return out
    except FileNotFoundError:
        out["status"] = "skipped"
        out["message"] = "npm not installed — skipping render smoke"
        return out
    except Exception as exc:  # noqa: BLE001
        out["status"] = "failed"
        out["message"] = f"npm run build error: {exc}"
        return out

    # 2. vite preview on a free port.
    port = _free_port()
    preview_cmd = [
        "npx", "--yes", "vite", "preview",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--strictPort",
    ]
    try:
        proc = subprocess.Popen(
            preview_cmd,
            cwd=str(web),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        out["status"] = "skipped"
        out["message"] = "npx not installed — skipping preview probe"
        return out
    except Exception as exc:  # noqa: BLE001
        out["status"] = "failed"
        out["message"] = f"vite preview spawn failed: {exc}"
        return out

    try:
        # 3. poll /.
        base = f"http://127.0.0.1:{port}"
        ok = False
        index_html = ""
        for _ in range(40):  # ~20s @ 0.5s
            if time.time() >= deadline:
                break
            time.sleep(0.5)
            status, body = _http_get(base + "/")
            if status == 200 and "id=\"root\"" in body:
                ok = True
                index_html = body
                break
        if not ok:
            out["status"] = "failed"
            out["message"] = (
                f"preview did not serve / (port={port}); "
                "either build artifact is missing or vite refused to bind"
            )
            return out
        out["preview_ok"] = True

        # 4. extra route probes — for a hash-routed SPA every path
        # returns the same index.html, so we just confirm 200.
        route_results: list[dict] = []
        for route in extra_routes:
            status, body = _http_get(base + route)
            route_results.append({
                "route": route,
                "status": status,
                "ok": status == 200 and "id=\"root\"" in body,
            })
        out["routes_ok"] = route_results
        all_routes_ok = all(r["ok"] for r in route_results) if route_results else True
        if not all_routes_ok:
            out["status"] = "failed"
            out["message"] = "one or more route probes failed: " + ", ".join(
                f"{r['route']}={r['status']}" for r in route_results if not r["ok"]
            )
            return out

        out["ok"] = True
        out["status"] = "passed"
        out["message"] = (
            f"render smoke passed (port={port}, "
            f"index_bytes={len(index_html)})"
        )
        return out
    finally:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Production health check
# ---------------------------------------------------------------------------


def production_health_check() -> dict:
    """Probe the operator-configured Control Tower / app / API URLs.

    Reads three optional env vars:
        AUTOPILOT_HEALTH_URL      — explicit one-off probe
        CONTROL_TOWER_URL         — falls back to <url>/health
        AUTOPILOT_PRODUCTION_API_URL — backend health endpoint
        AUTOPILOT_PRODUCTION_APP_URL — frontend index probe

    A probe is considered successful on HTTP 200. When NO URLs are
    configured the result is {ok: True, status: "skipped"} so an
    operator running the loop without prod can still cycle.
    """
    probes: list[dict] = []

    explicit = os.environ.get("AUTOPILOT_HEALTH_URL", "").strip()
    if explicit:
        probes.append({"label": "AUTOPILOT_HEALTH_URL", "url": explicit})

    ct = os.environ.get("CONTROL_TOWER_URL", "").strip()
    if ct:
        url = ct.rstrip("/") + "/health"
        probes.append({"label": "control_tower", "url": url})

    api_url = os.environ.get("AUTOPILOT_PRODUCTION_API_URL", "").strip()
    if api_url:
        probes.append({"label": "production_api", "url": api_url})

    app_url = os.environ.get("AUTOPILOT_PRODUCTION_APP_URL", "").strip()
    if app_url:
        probes.append({"label": "production_app", "url": app_url})

    if not probes:
        return {
            "ok": True,
            "status": "skipped",
            "message": "no health URLs configured — skipping probe",
            "probes": [],
        }

    failures: list[str] = []
    results: list[dict] = []
    for p in probes:
        status, _body = _http_get(p["url"], timeout=10.0)
        ok = status == 200
        results.append({**p, "http_status": status, "ok": ok})
        if not ok:
            failures.append(f"{p['label']}({p['url']})={status}")

    if failures:
        return {
            "ok": False,
            "status": "failed",
            "message": "; ".join(failures)[:600],
            "probes": results,
        }
    return {
        "ok": True,
        "status": "passed",
        "message": ", ".join(f"{r['label']}=200" for r in results),
        "probes": results,
    }


# ---------------------------------------------------------------------------
# Git commit / push (auto_commit / auto_publish modes)
# ---------------------------------------------------------------------------


def _git(*args: str, timeout: float = 60.0) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(_repo_root()), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode, out.strip()
    except subprocess.SubprocessError as exc:
        return -1, str(exc)
    except FileNotFoundError:
        return -2, "git not installed"


def _filter_addable(changed: list[str]) -> list[str]:
    """Subset of changed files we are willing to git-add. Mirrors the
    runner's ALLOWED_PUBLISH_DIR_ROOTS — keeps risky/ignored paths out.
    """
    keep: list[str] = []
    allowed_roots = ("app/", "control_tower/", "scripts/", "deploy/",
                     "config/", "docs/", ".github/")
    allowed_files = {"CLAUDE.md", "README.md", "CHANGELOG.md", ".gitignore"}
    for p in changed or []:
        low = p.lower()
        if any(pat in low for pat in RISKY_PATTERNS):
            continue
        if p in allowed_files:
            keep.append(p)
            continue
        if any(p.startswith(r) for r in allowed_roots):
            keep.append(p)
    return sorted(set(keep))


def _git_commit_only(message: str, files: list[str]) -> tuple[bool, str, str]:
    """Stage `files` and create a commit. Returns (ok, commit_hash, log)."""
    if not files:
        return False, "", "no addable files"
    rc, out = _git("add", "--", *files, timeout=60)
    if rc != 0:
        return False, "", f"git add failed: {out[-300:]}"
    rc, out = _git("commit", "-m", message, timeout=60)
    if rc != 0:
        # Roll back the staged adds so a partial state doesn't linger.
        _git("reset", "HEAD", "--", *files, timeout=30)
        return False, "", f"git commit failed: {out[-300:]}"
    rc, hsh = _git("rev-parse", "HEAD", timeout=10)
    return True, (hsh.strip() if rc == 0 else "unknown"), "ok"


def _git_push() -> tuple[bool, str]:
    rc, out = _git("push", "origin", "main", timeout=120)
    return rc == 0, out


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------


def _classify_failure(verdict: str, smoke: dict) -> str:
    code = (smoke.get("failure_code") or "").strip().lower()
    if code in {"smoke_timeout"}:
        return "TIMEOUT"
    if code == "scope_mismatch":
        return "scope_mismatch"
    if verdict == "FAIL":
        return code or "fail"
    return verdict


def _hold_loop_root_cause(state: AutopilotState) -> list[str]:
    """When the run terminated via max_cycles AND every recorded cycle
    verdict was HOLD, surface the root-cause signals from the last
    factory_state so the operator doesn't have to grep three artifacts
    to figure out why nothing shipped.

    Returns a list of markdown lines (empty when this is not a HOLD-loop
    termination — caller appends only when non-empty).
    """
    history = list(state.history or [])
    stop_reason = (state.stop_reason or "").lower()
    failure_code = (state.last_failure_code or "").lower()
    is_max_cycles = "max_cycles" in stop_reason
    is_explicit_loop = "no_progress_hold_loop" in stop_reason or failure_code == "no_progress_hold_loop"
    if not history or not (is_max_cycles or is_explicit_loop):
        return []
    verdicts = {(h.get("verdict") or "").upper() for h in history}
    if is_max_cycles and verdicts and verdicts != {"HOLD"}:
        return []

    heading = (
        "## HOLD 반복 종료 (no_progress_hold_loop)"
        if is_explicit_loop
        else "## HOLD 반복 종료 (root cause)"
    )
    lines: list[str] = ["", heading, ""]
    code_changed_count = sum(1 for h in history if h.get("code_changed"))
    lines.append(
        f"- 총 {len(history)}개 cycle 중 code_changed=true: {code_changed_count}개. "
        f"claude_apply 가 한 번도 실제 코드 변경을 만들지 못해 commit/push 가 발생할 수 없었습니다."
    )
    # Per-cycle skip diagnosis — show why each cycle didn't reach apply.
    lines.append("")
    lines.append("### Cycle별 skip 단계")
    lines.append("")
    lines.append("| # | hold_type | design_spec | impl_ticket | claude_propose | claude_apply | code_changed |")
    lines.append("|---|-----------|-------------|-------------|-----------------|---------------|--------------|")
    for h in history:
        lines.append(
            f"| {h.get('cycle')} "
            f"| {h.get('hold_type') or '—'} "
            f"| {h.get('design_spec_status') or '—'} "
            f"| {h.get('implementation_ticket_status') or '—'} "
            f"| {h.get('claude_propose_status') or '—'} "
            f"| {h.get('claude_apply_status') or '—'} "
            f"| {h.get('code_changed')} |"
        )
    lines.append("")

    fs_path = _factory_state_path()
    fs: dict = {}
    try:
        if fs_path.is_file():
            with open(fs_path, "r", encoding="utf-8") as fh:
                fs = json.load(fh) or {}
    except Exception:
        fs = {}

    gate_failures = fs.get("product_planner_gate_failures") or []
    if gate_failures:
        lines.append("- 마지막 사이클 기획 품질 가드 실패:")
        for f in list(gate_failures)[:5]:
            lines.append(f"  - {f}")

    skip_reason = fs.get("implementation_ticket_skipped_reason")
    ticket_status = fs.get("implementation_ticket_status")
    if ticket_status == "skipped_hold" or skip_reason == "pm_hold_for_rework":
        lines.append(
            "- implementation_ticket 가 매 cycle 마다 PM HOLD 게이트로 skip → "
            "claude_propose 도 자동 skip → claude_apply 변경 없음."
        )

    spec_passed = bool(fs.get("design_spec_acceptance_passed"))
    spec_status = fs.get("design_spec_status")
    if spec_passed and spec_status == "generated":
        lines.append(
            "- design_spec.md acceptance=passed 였으나 PM HOLD 로 막혔습니다 — "
            "spec_acceptance_bypass 이 동작하는 최신 cycle.py 인지 확인하세요."
        )
    elif spec_status and spec_status != "generated":
        lines.append(f"- design_spec_status={spec_status} (acceptance bypass 비활성)")

    pm_msg = fs.get("pm_decision_message") or fs.get("pm_decision_status")
    if pm_msg:
        lines.append(f"- 마지막 PM 결정 메시지: {pm_msg}")

    lines.append("")
    lines.append("**다음 cycle 권장 조치**:")
    lines.append(
        "- 직전 HOLD 사유를 후보 1개의 `사용자 문제` 로 옮긴 planner 출력을 만들거나, "
        "design_spec.md acceptance 를 통과시켜 spec_acceptance_bypass 를 발동시키세요."
    )
    lines.append(
        "- `cat .runtime/pm_decision.md`, `cat .runtime/designer_final_review.md`, "
        "`cat .runtime/product_planner_report.md` 순서로 확인하면 어디서 막혔는지 보입니다."
    )
    return lines


def _format_report(state: AutopilotState) -> str:
    # Pull the latest factory_state for the contract / freshness panels.
    fs = _read_json(_factory_state_path()) or {}
    contract_results = fs.get("contract_results") or []
    active_feature = (
        fs.get("selected_feature")
        or fs.get("implementation_ticket_selected_feature")
        or fs.get("design_spec_feature")
    )
    active_feature_id = (
        fs.get("selected_feature_id")
        or fs.get("implementation_ticket_feature_id")
        or fs.get("design_spec_feature_id")
    )
    lines = [
        "# Stampport Auto Pilot Report",
        "",
        f"- run_id: `{state.current_run_id or '—'}`",
        f"- 모드: `{state.mode}`",
        f"- 시작: `{state.started_at}`",
        f"- 종료: `{state.ended_at}`",
        f"- 종료 사유: `{state.stop_reason or '—'}`",
        f"- 실행한 cycle 수: `{state.cycle_count}` / 최대 `{state.max_cycles}`",
        f"- 최대 시간: `{state.max_hours}h`",
        f"- 활성 feature: `{active_feature or '—'}` "
        f"(feature_id=`{active_feature_id or '—'}`)",
        f"- selected_feature_source: `{fs.get('selected_feature_source') or '—'}`",
        f"- design_spec_feature: `{fs.get('design_spec_feature') or '—'}`",
        f"- implementation_ticket_feature: "
        f"`{fs.get('implementation_ticket_selected_feature') or '—'}`",
        f"- scope_consistency_status: `{fs.get('scope_consistency_status') or '—'}`",
        f"- apply_preflight_status: `{fs.get('apply_preflight_status') or '—'}`",
        f"- 마지막 verdict: `{state.last_verdict}` "
        f"({state.last_failure_code or '—'})",
        f"- 마지막 commit: `{state.last_commit_hash or '—'}`",
        f"- 마지막 push: `{state.last_push_status or '—'}`",
        f"- 마지막 health: `{state.last_health_status or '—'}`",
        f"- 마지막 render: `{state.last_render_status or '—'}`",
    ]
    if contract_results:
        lines += [
            "",
            "## Stage contract table",
            "",
            "| validator | ok | code | message |",
            "|-----------|----|------|---------|",
        ]
        for c in contract_results:
            msg = (c.get("message") or "—").replace("|", "/")
            if len(msg) > 80:
                msg = msg[:77] + "…"
            lines.append(
                f"| {c.get('name') or '—'} "
                f"| {'✅' if c.get('ok') else '❌'} "
                f"| {c.get('code') or '—'} "
                f"| {msg} |"
            )

    decision = _resolve_pipeline_decision(fs)
    lines += [
        "",
        "## Pipeline Decision",
        "",
        f"- pipeline_status: `{decision.get('pipeline_status') or '—'}`",
        f"- can_commit: `{decision.get('can_commit')}`",
        f"- can_push: `{decision.get('can_push')}`",
        f"- can_publish: `{decision.get('can_publish')}`",
        f"- blocking_code: `{decision.get('blocking_code') or '—'}`",
        f"- blocking_reason: {decision.get('blocking_reason') or '—'}",
    ]
    checks = decision.get("checks") or {}
    if checks:
        lines += [
            "",
            "| check | status |",
            "|-------|--------|",
        ]
        for name in (
            "planner", "ticket", "apply", "qa", "scope", "meaningful_change"
        ):
            if name in checks:
                lines.append(f"| {name} | `{checks[name]}` |")
    # Claude Executor section — what we know about CLI health, the
    # last failure classification, retry status, and where the forensic
    # logs live. Surfaces stdout/stderr paths so the operator can grep
    # them without hunting around .runtime/.
    executor = _resolve_claude_executor_state(fs)
    next_action = "—"
    exec_status = (executor.get("status") or "").strip()
    exec_code = (executor.get("failure_code") or "").strip()
    if exec_status == "passed":
        next_action = "정상 — claude_apply 정상 실행"
    elif _is_executor_failure_code(exec_code):
        if exec_code == "claude_cli_budget_exceeded":
            next_action = (
                "increase CLAUDE_*_MAX_COST_USD or reduce prompt scope — "
                "재시도해도 같은 cap에서 또 초과됨"
            )
        elif executor.get("retryable"):
            next_action = (
                "retryable 실패 — autopilot이 apply_retry_only 1회 재시도를 자동 수행"
            )
        elif exec_code in {"claude_cli_missing", "claude_cli_auth_failed"}:
            next_action = "수동 조치 필요 — claude CLI 설치/재로그인 후 autopilot 재시작"
        else:
            next_action = "재시도 한도 초과 — operator가 stderr 확인 후 재시작"
    lines += [
        "",
        "## Claude Executor",
        "",
        f"- status: `{exec_status or '—'}`",
        f"- command: `{(executor.get('command') or '—')[:200]}`",
        f"- failure_code: `{exec_code or '—'}`",
        f"- retryable: `{bool(executor.get('retryable'))}`",
        f"- retry_count: `{executor.get('retry_count') or 0}`",
        f"- duration_sec: `{executor.get('duration_sec') or '—'}`",
        f"- exit_code: `{executor.get('exit_code')}`",
        f"- timed_out: `{bool(executor.get('timed_out'))}`",
        f"- max_cost_usd: `{executor.get('max_cost_usd') or '—'}`",
        f"- cost_budget_source: `{executor.get('cost_budget_source') or '—'}`",
        f"- exceeded_budget: `{bool(executor.get('exceeded_budget'))}`",
        f"- stdout_path: `{executor.get('stdout_path') or '—'}`",
        f"- stderr_path: `{executor.get('stderr_path') or '—'}`",
        f"- failure_reason: {(executor.get('failure_reason') or '—')[:300]}",
        f"- next_action: {next_action}",
    ]

    lines += [
        "",
        "## Cycle log",
        "",
        "| # | verdict | failure | publish | commit | push | render | health |",
        "|---|---------|---------|---------|--------|------|--------|--------|",
    ]
    for h in state.history:
        lines.append(
            f"| {h.get('cycle')} "
            f"| {h.get('verdict') or '—'} "
            f"| {h.get('failure_code') or '—'} "
            f"| {h.get('publish_action') or '—'} "
            f"| {(h.get('commit_hash') or '—')[:8]} "
            f"| {h.get('push_status') or '—'} "
            f"| {h.get('render_status') or '—'} "
            f"| {h.get('health_status') or '—'} |"
        )

    # HOLD-loop diagnostic table — surfaces hold_type / design_spec /
    # impl_ticket / claude_propose / claude_apply / code_changed for
    # every recorded cycle. Stays empty when there are no cycles to
    # report, but otherwise always renders so the operator can see the
    # per-stage gate evidence even when the run finishes successfully.
    if state.history:
        lines += [
            "",
            "## HOLD loop telemetry",
            "",
            "| # | selected_feature | pm_verdict | hold_type | design_spec | "
            "ds_acceptance | stale_ds | impl_ticket | claude_propose | "
            "claude_apply | code_changed |",
            "|---|------------------|------------|-----------|-------------|"
            "---------------|----------|-------------|-----------------|"
            "---------------|--------------|",
        ]
        for h in state.history:
            sf = (h.get("selected_feature") or "—")
            sf = sf if len(sf) <= 28 else sf[:25] + "…"
            lines.append(
                f"| {h.get('cycle')} "
                f"| {sf} "
                f"| {h.get('pm_verdict') or '—'} "
                f"| {h.get('hold_type') or '—'} "
                f"| {h.get('design_spec_status') or '—'} "
                f"| {h.get('design_spec_acceptance_passed')} "
                f"| {h.get('stale_design_spec_detected')} "
                f"| {h.get('implementation_ticket_status') or '—'} "
                f"| {h.get('claude_propose_status') or '—'} "
                f"| {h.get('claude_apply_status') or '—'} "
                f"| {h.get('code_changed')} |"
            )

    lines.extend(_hold_loop_root_cause(state))

    lines += [
        "",
        "## 아침에 확인할 것",
        "",
        f"- `git log --oneline -n 20`",
        f"- `cat .runtime/autopilot_state.json`",
        f"- `tail -200 .runtime/autopilot.log`",
        f"- `cat .runtime/factory_smoke_report.md`",
    ]
    return "\n".join(lines) + "\n"


def _record_cycle(record: CycleRecord, *, save: bool = True) -> None:
    with _LOCK:
        _STATE.cycle_count = record.cycle
        _STATE.last_verdict = record.verdict
        _STATE.last_failure_code = record.failure_code
        if record.commit_hash:
            _STATE.last_commit_hash = record.commit_hash
        if record.push_status:
            _STATE.last_push_status = record.push_status
        if record.health_status:
            _STATE.last_health_status = record.health_status
        if record.render_status:
            _STATE.last_render_status = record.render_status
        if record.note:
            _STATE.last_note = record.note
        _STATE.history.append(asdict(record))
        # cap history size to avoid unbounded heartbeat metadata
        if len(_STATE.history) > 50:
            _STATE.history = _STATE.history[-50:]
        if save:
            _save_state()


def _stop(reason: str, *, status: str = "stopped") -> None:
    with _LOCK:
        _STATE.status = status
        _STATE.stop_reason = reason
        _STATE.ended_at = _utc_now()
        # Clear live-cycle markers — a stopped run must not leave
        # active_cycle_index / current_stage behind for the next
        # heartbeat, otherwise the dashboard would still claim a
        # cycle is in flight.
        _STATE.active_cycle_index = None
        _STATE.current_stage = None
        _STATE.live_report_path = None
        rep = _report_path()
        _write_text(rep, _format_report(_STATE))
        _STATE.report_path = str(rep)
        _save_state()
    _log(f"autopilot stop status={status} reason={reason}")


def run_loop(config: AutopilotConfig, stop_event: threading.Event) -> None:
    """Synchronous loop body. Run from a daemon thread."""
    started_wall = time.time()
    deadline = started_wall + max(60.0, config.max_hours * 3600.0)

    with _LOCK:
        _STATE.status = "running"
        _STATE.mode = config.autopilot_mode
        _STATE.started_at = _utc_now()
        _STATE.ended_at = None
        _STATE.cycle_count = 0
        _STATE.max_cycles = config.max_cycles
        _STATE.max_hours = config.max_hours
        _STATE.stop_on_hold = config.stop_on_hold
        _STATE.stop_on_fail = config.stop_on_fail
        _STATE.stop_on_scope_mismatch = config.stop_on_scope_mismatch
        _STATE.require_scope_consistency = config.require_scope_consistency
        _STATE.require_render_check = config.require_render_check
        _STATE.require_api_health = config.require_api_health
        _STATE.last_verdict = None
        _STATE.last_failure_code = None
        _STATE.last_commit_hash = None
        _STATE.last_push_status = None
        _STATE.last_health_status = None
        _STATE.last_render_status = None
        _STATE.last_note = None
        _STATE.stop_reason = None
        _STATE.history = []
        # Reset live-cycle markers from any prior run so the dashboard
        # starts each new run from a clean "no cycle yet" baseline.
        _STATE.first_cycle_spawn_at = None
        _STATE.current_cycle_started_at = None
        _STATE.current_cycle_finished_at = None
        _STATE.active_cycle_index = None
        _STATE.current_stage = None
        _STATE.live_report_path = None
        # Allocate a fresh run_id for this autopilot run. Every
        # downstream artifact (factory_smoke, cycle.py stages, autopilot
        # report) records this id so the UI's freshness verdict survives
        # cycle-counter resets across runs. The generator is uuid4 hex
        # truncated to 12 chars + a "r-" prefix so it sorts and greps
        # cleanly in artifact metadata.
        _STATE.current_run_id = "r-" + uuid.uuid4().hex[:12]
        _save_state()

    _log(
        f"autopilot start mode={config.autopilot_mode} "
        f"max_cycles={config.max_cycles} max_hours={config.max_hours}"
    )

    cycle = 0
    try:
        while not stop_event.is_set():
            if cycle >= config.max_cycles:
                # max_cycles boundary — surface a specific
                # no_progress_hold_loop failure ONLY when the policy in
                # `_max_cycles_boundary_classification` actually fires
                # (>= 3 cycles in history, >= 3 max_cycles, last 3 all
                # HOLD+code_changed=false). Short runs (max_cycles=1/2)
                # — typically manual probes — fall through to the
                # benign `max_cycles reached` stop so a single
                # legitimate HOLD isn't reported as a broken loop.
                classification = _max_cycles_boundary_classification(
                    _STATE.history or [], config.max_cycles,
                )
                if classification == "no_progress_hold_loop":
                    ncl = _consecutive_no_change_holds(_STATE.history or [])
                    _STATE.last_failure_code = classification
                    _stop(
                        f"no_progress_hold_loop: {ncl} consecutive HOLD "
                        f"cycles with code_changed=false (reached "
                        f"max_cycles={config.max_cycles}) — "
                        f"implementation never reached. Inspect "
                        f"autopilot_report.md cycle table.",
                        status="failed",
                    )
                    return
                _stop(f"max_cycles reached ({config.max_cycles})")
                return
            if time.time() >= deadline:
                _stop(f"max_hours reached ({config.max_hours}h)")
                return

            cycle += 1
            rec = CycleRecord(cycle=cycle, started_at=_utc_now())
            _log(f"cycle {cycle} starting")

            # ── Live state pre-spawn ────────────────────────────────
            # Publish the cycle index BEFORE we spawn factory_smoke so
            # the dashboard's stuck-before-first-cycle check sees an
            # active cycle marker the moment the subprocess is alive.
            # Without this, a long-running first cycle painted CYCLE
            # 0 / 5 + STUCK while real product_planning / designer_
            # critique work was happening.
            with _LOCK:
                now = _utc_now()
                if _STATE.first_cycle_spawn_at is None:
                    _STATE.first_cycle_spawn_at = now
                _STATE.current_cycle_started_at = now
                _STATE.current_cycle_finished_at = None
                _STATE.active_cycle_index = cycle
                _STATE.current_stage = "factory_smoke"
                _STATE.last_note = f"cycle {cycle} running"
                _STATE.live_report_path = str(_runtime_dir() / "autopilot_live_report.md")
            _save_state()
            _write_live_report(cycle, config)

            # ── Heartbeat thread ───────────────────────────────────
            # While factory_smoke blocks (subprocess.run), tail factory
            # _state.json every 20s and mirror current_stage into our
            # state so the UI shows "Designer가 비평 중" instead of a
            # frozen "factory_smoke" label.
            hb_stop = threading.Event()
            hb_thread = threading.Thread(
                target=_smoke_heartbeat,
                args=(hb_stop,),
                daemon=True,
                name="autopilot-smoke-hb",
            )
            hb_thread.start()

            try:
                smoke = _run_smoke_cycle(config.smoke_timeout_sec)
            finally:
                hb_stop.set()
                hb_thread.join(timeout=2.0)

            # ── Live state post-spawn ───────────────────────────────
            # The subprocess returned. Cycle index goes back to "no
            # active cycle" and cycle_count advances by one. Verdict
            # is recorded by the existing _record_cycle below.
            with _LOCK:
                _STATE.current_cycle_finished_at = _utc_now()
                _STATE.active_cycle_index = None
                _STATE.current_stage = None
                _STATE.last_note = (
                    f"cycle {cycle} finished verdict="
                    + (smoke.get("verdict") or "—")
                )
            _save_state()
            _write_live_report(cycle, config, last_smoke=smoke)
            verdict = (smoke.get("verdict") or "").strip()
            failure = (smoke.get("failure_code") or "").strip()
            rec.verdict = verdict
            rec.failure_code = failure or None
            rec.factory_status = smoke.get("factory_status")
            rec.qa_status = smoke.get("qa_status")
            rec.scope_consistency_status = smoke.get("scope_consistency_status")
            rec.changed_files_count = int(smoke.get("changed_files_count") or 0)

            factory_state = _read_json(_factory_state_path())
            _populate_cycle_record_from_state(rec, smoke, factory_state)

            # Mirror the Claude Executor verdict immediately so the UI
            # sees the latest CLI status even if the autopilot stops on
            # a non-executor reason.
            executor = _resolve_claude_executor_state(factory_state)
            _mirror_executor_into_state(executor)

            # FAIL / TIMEOUT / scope_mismatch — immediate stop UNLESS
            # the failure is a retryable Claude executor error AND the
            # implementation_ticket / claude_proposal artifacts are
            # still intact. In that case, schedule one apply-only retry
            # so we don't burn a fresh planner+designer pass on a
            # transient CLI hiccup.
            failure_norm = _classify_failure(verdict, smoke)
            if (
                verdict in FAIL_VERDICTS
                or failure_norm in {"TIMEOUT", "scope_mismatch"}
            ):
                exec_code = (executor.get("failure_code") or "").strip()
                exec_retryable = bool(executor.get("retryable"))
                try:
                    exec_retry_count = int(executor.get("retry_count") or 0)
                except (TypeError, ValueError):
                    exec_retry_count = 0
                ticket_ready = (
                    factory_state.get("implementation_ticket_status")
                    == "generated"
                )
                proposal_ready = (
                    factory_state.get("claude_proposal_status") == "generated"
                )
                cli_failed = (
                    factory_state.get("claude_apply_status") == "cli_failed"
                ) or _is_executor_failure_code(exec_code)
                can_retry = (
                    cli_failed
                    and exec_retryable
                    and exec_retry_count < 1
                    and ticket_ready
                    and proposal_ready
                    and cycle < config.max_cycles
                )
                if can_retry:
                    rec.finished_at = _utc_now()
                    rec.publish_action = "skip"
                    rec.note = (
                        f"claude executor retryable failure ({exec_code}) — "
                        f"scheduling apply-only retry"
                    )
                    _record_cycle(rec)
                    with _LOCK:
                        _STATE.claude_apply_retry_pending = True
                    _log(
                        f"executor retry scheduled — code={exec_code} "
                        f"retry_count={exec_retry_count} "
                        f"ticket_ready={ticket_ready} "
                        f"proposal_ready={proposal_ready}"
                    )
                    _save_state()
                    continue
                rec.finished_at = _utc_now()
                rec.publish_action = "skip"
                rec.note = f"halt on {failure_norm}"
                _record_cycle(rec)
                stop_reason = (
                    f"{failure_norm}: "
                    f"{(smoke.get('failure_reason') or failure_norm)[:200]}"
                )
                # Surface the precise executor code so the dashboard
                # / report shows e.g. claude_cli_auth_failed instead of
                # the generic FAIL/build_failed.
                if _is_executor_failure_code(exec_code):
                    _STATE.last_failure_code = exec_code
                    stop_reason = (
                        f"{exec_code}: "
                        f"{(executor.get('failure_reason') or exec_code)[:200]}"
                    )
                _stop(stop_reason, status="failed")
                return

            # HOLD path.
            if verdict in HOLD_VERDICTS:
                rec.publish_action = "skip"
                if _git_dirty():
                    rec.note = "HOLD with dirty tree → stop"
                    rec.finished_at = _utc_now()
                    _record_cycle(rec)
                    _stop("HOLD with dirty git tree", status="stopped")
                    return
                if config.stop_on_hold:
                    rec.note = "HOLD (stop_on_hold=true)"
                    rec.finished_at = _utc_now()
                    _record_cycle(rec)
                    _stop("HOLD (stop_on_hold=true)", status="stopped")
                    return
                # No-change HOLD loop breaker: if the rework loop has
                # produced N consecutive HOLD cycles WITHOUT any
                # claude_apply touching files, force a stop with a
                # specific failure_code. Without this, max_cycles
                # finishes "successfully" while shipping nothing.
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                ncl = _consecutive_no_change_holds(_STATE.history or [])
                _log(
                    f"HOLD cycle {rec.cycle} consecutive_no_change_holds={ncl} "
                    f"hold_type={rec.hold_type}"
                )
                if ncl >= NO_CHANGE_HOLD_STOP_THRESHOLD:
                    _STATE.last_failure_code = "no_progress_hold_loop"
                    _stop(
                        f"no_progress_hold_loop: {ncl} consecutive HOLD cycles "
                        f"with code_changed=false — implementation never "
                        f"reached. Inspect autopilot_report.md cycle table.",
                        status="failed",
                    )
                    return
                continue

            # READY / PASS — apply the gate.
            if verdict not in READY_VERDICTS:
                rec.note = f"unexpected verdict={verdict} — halt"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                _stop(f"unexpected smoke verdict {verdict}", status="failed")
                return

            ok, why, _evidence = evaluate_publish_gate(
                smoke, factory_state,
                require_scope=config.require_scope_consistency,
            )
            if not ok:
                rec.publish_action = "skip"
                rec.note = f"gate blocked: {why}"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                _stop(f"publish gate blocked: {why}", status="stopped")
                return

            # Render smoke (pre-push gate).
            render_status = "skipped"
            if config.require_render_check and config.autopilot_mode == "auto_publish":
                rs = render_smoke()
                render_status = rs.get("status") or ("passed" if rs.get("ok") else "failed")
                rec.render_status = render_status
                if not rs.get("ok"):
                    rec.publish_action = "skip"
                    rec.note = f"render smoke failed: {rs.get('message','')[:200]}"
                    rec.finished_at = _utc_now()
                    _record_cycle(rec)
                    _stop(
                        f"render smoke failed: {rs.get('message','')[:200]}",
                        status="stopped",
                    )
                    return
            else:
                rec.render_status = "skipped"

            # Mode-specific publish path.
            mode = config.autopilot_mode
            if mode == "safe_run":
                rec.publish_action = "none"
                rec.push_status = "skipped"
                rec.note = "safe_run — verdict ok, no commit/push"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                continue

            # auto_commit / auto_publish need a real diff in the tree.
            # If the cycle wrote files but they're now stashed by the
            # smoke preflight (.prev rename), the working tree is clean
            # again — surface as no-op rather than commit a phantom.
            if not _git_dirty():
                rec.publish_action = "noop"
                rec.push_status = "noop"
                rec.note = "tree clean — nothing to commit"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                continue

            # Build the addable file list from the cycle's claude_apply
            # records first (most precise), then fall back to git diff.
            changed = factory_state.get("claude_apply_changed_files") or []
            addable = _filter_addable(changed)
            if not addable:
                # If the change was outside our allowed roots, refuse.
                rec.publish_action = "skip"
                rec.note = (
                    "no addable files in change set — "
                    "refusing to commit"
                )
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                _stop(
                    "no addable files (everything filtered out by safety roots)",
                    status="stopped",
                )
                return

            commit_msg = (
                f"Auto factory cycle {cycle}: "
                f"{factory_state.get('selected_feature') or 'autopilot publish'}"
            )
            ok, commit_hash, log_msg = _git_commit_only(commit_msg, addable)
            if not ok:
                rec.publish_action = "skip"
                rec.note = f"commit failed: {log_msg}"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                _stop(f"commit failed: {log_msg}", status="failed")
                return
            rec.commit_hash = commit_hash
            rec.publish_action = "commit"

            if mode == "auto_commit":
                rec.push_status = "skipped"
                rec.note = f"auto_commit only — commit={commit_hash[:8]}"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                continue

            # mode == auto_publish → push + post-push checks.
            push_ok, push_log = _git_push()
            rec.push_status = "succeeded" if push_ok else "failed"
            rec.publish_action = "push"
            if not push_ok:
                rec.note = f"push failed: {push_log[-200:]}"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
                _stop(f"push failed: {push_log[-200:]}", status="failed")
                return

            # Post-push: production health (HTTP 200 across configured
            # URLs). Render check was pre-push. require_api_health=true
            # means we treat a failed health probe as STOP.
            if config.require_api_health:
                hc = production_health_check()
                rec.health_status = hc.get("status") or (
                    "passed" if hc.get("ok") else "failed"
                )
                if not hc.get("ok"):
                    rec.note = f"post-push health failed: {hc.get('message','')[:200]}"
                    rec.finished_at = _utc_now()
                    _record_cycle(rec)
                    _stop(
                        f"post-push health failed: {hc.get('message','')[:200]}",
                        status="failed",
                    )
                    return
            else:
                rec.health_status = "skipped"

            rec.note = (
                f"published commit={commit_hash[:8]} "
                f"render={rec.render_status} health={rec.health_status}"
            )
            rec.finished_at = _utc_now()
            _record_cycle(rec)
            # Continue to next cycle.

        # Stop event signalled.
        _stop("operator stop")
    except Exception as exc:  # noqa: BLE001
        _log(f"loop crashed: {exc}")
        _stop(f"loop exception: {exc}", status="failed")


# ---------------------------------------------------------------------------
# Public start/stop API
# ---------------------------------------------------------------------------


def start(config: AutopilotConfig) -> tuple[bool, str]:
    """Spawn the background autopilot loop. Idempotent."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False, "autopilot already running"
        _STOP_EVENT.clear()

        def _runner():
            try:
                run_loop(config, _STOP_EVENT)
            finally:
                # Clear thread reference so a subsequent start() can
                # spawn a fresh thread.
                with _LOCK:
                    global _THREAD
                    _THREAD = None

        t = threading.Thread(
            target=_runner,
            name="autopilot",
            daemon=True,
        )
        _THREAD = t
        t.start()
        return True, (
            f"autopilot started (mode={config.autopilot_mode}, "
            f"max_cycles={config.max_cycles}, max_hours={config.max_hours})"
        )


def stop(reason: str = "operator stop") -> tuple[bool, str]:
    """Signal the loop to stop. Returns immediately; the loop finalizes
    state on the next iteration boundary."""
    with _LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            # Make sure persisted state reflects "stopped" even if we
            # never started in this process — idempotent UX.
            if _STATE.status == "running":
                _STATE.status = "stopped"
                _STATE.stop_reason = reason
                _STATE.ended_at = _utc_now()
                _save_state()
            return False, "autopilot is not running"
        _STOP_EVENT.set()
        return True, f"autopilot stop signalled ({reason})"


# ---------------------------------------------------------------------------
# Self-tests — exercise verdict/gate/state without spawning subprocesses
# ---------------------------------------------------------------------------


def _self_test_with_smoke(
    smoke_state: dict,
    factory_state: dict,
    *,
    config: AutopilotConfig,
    git_dirty_override: bool | None = None,
    render_override: dict | None = None,
    health_override: dict | None = None,
    git_commit_override: tuple[bool, str, str] | None = None,
    git_push_override: tuple[bool, str] | None = None,
) -> dict:
    """Run a single iteration of the loop body using injected fixtures.

    Returns the resulting state dict + the recorded cycle entry.
    """
    # Reset module state.
    global _STATE, _THREAD
    _STATE = AutopilotState()
    _STOP_EVENT.clear()
    _THREAD = None

    # Patch helpers.
    real_smoke = globals()["_run_smoke_cycle"]
    real_dirty = globals()["_git_dirty"]
    real_render = globals()["render_smoke"]
    real_health = globals()["production_health_check"]
    real_commit = globals()["_git_commit_only"]
    real_push = globals()["_git_push"]
    real_factory_read = globals()["_read_json"]

    globals()["_run_smoke_cycle"] = lambda timeout_sec: smoke_state
    if git_dirty_override is not None:
        globals()["_git_dirty"] = lambda: bool(git_dirty_override)
    if render_override is not None:
        globals()["render_smoke"] = (
            lambda **kw: render_override  # type: ignore[arg-type]
        )
    if health_override is not None:
        globals()["production_health_check"] = lambda: health_override
    if git_commit_override is not None:
        globals()["_git_commit_only"] = (
            lambda message, files: git_commit_override
        )
    if git_push_override is not None:
        globals()["_git_push"] = lambda: git_push_override

    # The factory_state read should resolve to the injected fixture.
    def _read_json_patch(path):
        if path == _factory_state_path():
            return factory_state
        return real_factory_read(path)

    globals()["_read_json"] = _read_json_patch

    try:
        cfg = AutopilotConfig(
            autopilot_enabled=True,
            autopilot_mode=config.autopilot_mode,
            max_cycles=config.max_cycles or 1,
            max_hours=config.max_hours,
            stop_on_hold=config.stop_on_hold,
            require_scope_consistency=config.require_scope_consistency,
            require_render_check=config.require_render_check,
            require_api_health=config.require_api_health,
            smoke_timeout_sec=10,
        )
        run_loop(cfg, _STOP_EVENT)
        return _STATE.to_dict()
    finally:
        globals()["_run_smoke_cycle"] = real_smoke
        globals()["_git_dirty"] = real_dirty
        globals()["render_smoke"] = real_render
        globals()["production_health_check"] = real_health
        globals()["_git_commit_only"] = real_commit
        globals()["_git_push"] = real_push
        globals()["_read_json"] = real_factory_read


def self_test() -> tuple[int, int, list[str]]:
    """Acceptance fixtures A–K from the spec."""
    import tempfile
    passed = 0
    total = 0
    failures: list[str] = []

    repo_prev = os.environ.get("LOCAL_RUNNER_REPO")

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCAL_RUNNER_REPO"] = tmp
        Path(tmp, ".runtime").mkdir(parents=True, exist_ok=True)

        ready_smoke = {
            "verdict": "READY_TO_REVIEW",
            "failure_code": None,
            "factory_status": "ready_to_review",
            "qa_status": "passed",
            "scope_consistency_status": "passed",
            "changed_files_count": 1,
            "changed_files": ["app/web/src/screens/Foo.jsx"],
        }
        ready_factory = {
            "status": "succeeded",
            "qa_status": "passed",
            "scope_consistency_status": "passed",
            "apply_preflight_status": "passed",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "applied",
            "claude_apply_changed_files": ["app/web/src/screens/Foo.jsx"],
            "design_spec_acceptance_passed": True,
            "design_spec_status": "accepted",
            "selected_feature": "Auto pilot fixture A",
        }

        # --- A. READY + auto_publish + render passed → commit/push --------
        total += 1
        result = _self_test_with_smoke(
            ready_smoke, ready_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
            render_override={"ok": True, "status": "passed", "message": "ok"},
            git_commit_override=(True, "abcdef1234567890", "ok"),
            git_push_override=(True, "ok"),
        )
        last = (result.get("history") or [{}])[-1]
        if (
            last.get("publish_action") == "push"
            and last.get("push_status") == "succeeded"
            and last.get("commit_hash") == "abcdef1234567890"
        ):
            passed += 1
        else:
            failures.append(
                f"A: expected push success — got publish_action="
                f"{last.get('publish_action')}, push={last.get('push_status')}"
            )

        # --- B. READY + scope_mismatch → push 금지, stopped ---------------
        total += 1
        bad_scope_smoke = dict(ready_smoke,
                               verdict="FAIL",
                               failure_code="scope_mismatch",
                               scope_consistency_status="failed")
        bad_scope_factory = dict(ready_factory,
                                 scope_consistency_status="failed",
                                 scope_mismatch_reason="title 누락")
        result = _self_test_with_smoke(
            bad_scope_smoke, bad_scope_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
        )
        last = (result.get("history") or [{}])[-1]
        if (
            result.get("status") == "failed"
            and "scope_mismatch" in (result.get("stop_reason") or "")
            and last.get("publish_action") in {"skip", None}
        ):
            passed += 1
        else:
            failures.append(
                f"B: expected scope_mismatch stop — got "
                f"status={result.get('status')} reason={result.get('stop_reason')}"
            )

        # --- C. READY + render failed → push 금지, stopped ----------------
        total += 1
        result = _self_test_with_smoke(
            ready_smoke, ready_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
            render_override={"ok": False, "status": "failed",
                             "message": "vite preview died"},
        )
        last = (result.get("history") or [{}])[-1]
        if (
            result.get("status") == "stopped"
            and last.get("publish_action") == "skip"
            and last.get("render_status") == "failed"
        ):
            passed += 1
        else:
            failures.append(
                f"C: expected render-fail stop — got "
                f"status={result.get('status')} render={last.get('render_status')}"
            )

        # --- D1. max_cycles=1 + 1 HOLD+no-change → benign max_cycles
        # reached stop. Single-cycle manual probes must NOT be flagged
        # as no_progress_hold_loop — that's only a multi-cycle pattern.
        total += 1
        hold_smoke = {"verdict": "HOLD", "failure_code": None,
                      "factory_status": "hold_for_rework",
                      "scope_consistency_status": None}
        hold_factory = {"status": "hold_for_rework",
                        "qa_status": None,
                        "claude_apply_changed_files": []}
        result = _self_test_with_smoke(
            hold_smoke, hold_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=False,
        )
        if (
            result.get("status") == "stopped"
            and "max_cycles" in (result.get("stop_reason") or "")
            and (result.get("last_failure_code") or "")
                != "no_progress_hold_loop"
        ):
            passed += 1
        else:
            failures.append(
                f"D1: max_cycles=1 + 1 HOLD must be benign max_cycles "
                f"stop — got status={result.get('status')} "
                f"reason={result.get('stop_reason')} "
                f"code={result.get('last_failure_code')}"
            )

        # --- D2. max_cycles=3 + 3 HOLD+no-change cycles → failed /
        # no_progress_hold_loop (the in-loop threshold trigger fires at
        # the third cycle).
        total += 1
        result = _self_test_with_smoke(
            hold_smoke, hold_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=3, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=False,
        )
        hist_d2 = result.get("history") or []
        if (
            result.get("status") == "failed"
            and (result.get("last_failure_code") or "")
                == "no_progress_hold_loop"
            and "no_progress_hold_loop" in (result.get("stop_reason") or "")
            and len(hist_d2) == 3
        ):
            passed += 1
        else:
            failures.append(
                f"D2: max_cycles=3 + 3xHOLD must classify as "
                f"no_progress_hold_loop — got status={result.get('status')} "
                f"code={result.get('last_failure_code')} "
                f"history_len={len(hist_d2)}"
            )

        # --- D3. Mixed history (HOLD + READY/PASS) must NOT classify
        # as no_progress_hold_loop. Test the helpers directly because
        # _self_test_with_smoke uses a single fixture across all cycles.
        total += 1
        history_d3 = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "READY_TO_REVIEW", "code_changed": True},
            {"verdict": "HOLD", "code_changed": False},
        ]
        n_d3 = _consecutive_no_change_holds(history_d3)
        cls_d3 = _max_cycles_boundary_classification(history_d3, max_cycles=3)
        if (
            n_d3 == 1  # only the trailing HOLD counts
            and n_d3 < NO_CHANGE_HOLD_STOP_THRESHOLD
            and cls_d3 is None
        ):
            passed += 1
        else:
            failures.append(
                f"D3: mixed HOLD+READY history must not trip the loop "
                f"breaker — n={n_d3} cls={cls_d3!r}"
            )

        # --- D4. 3 HOLD cycles where ANY had code_changed=true must
        # NOT classify as no_progress_hold_loop.
        total += 1
        history_d4 = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": True},  # the saving cycle
            {"verdict": "HOLD", "code_changed": False},
        ]
        n_d4 = _consecutive_no_change_holds(history_d4)
        cls_d4 = _max_cycles_boundary_classification(history_d4, max_cycles=3)
        if (
            n_d4 == 1  # the trailing HOLD only
            and cls_d4 is None
        ):
            passed += 1
        else:
            failures.append(
                f"D4: HOLD-with-code-change must reset the loop counter "
                f"— n={n_d4} cls={cls_d4!r}"
            )

        # --- E. HOLD + git dirty → stopped --------------------------------
        total += 1
        result = _self_test_with_smoke(
            hold_smoke, hold_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
        )
        if (
            result.get("status") == "stopped"
            and "dirty" in (result.get("stop_reason") or "")
        ):
            passed += 1
        else:
            failures.append(
                f"E: expected dirty-tree HOLD stop — got "
                f"status={result.get('status')} reason={result.get('stop_reason')}"
            )

        # --- F. FAIL → stopped --------------------------------------------
        total += 1
        fail_smoke = {"verdict": "FAIL", "failure_code": "build_failed",
                      "failure_reason": "vite build error",
                      "factory_status": "failed"}
        result = _self_test_with_smoke(
            fail_smoke, {"status": "failed"},
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
            ),
            git_dirty_override=False,
        )
        if (
            result.get("status") == "failed"
            and (result.get("last_failure_code") or "") == "build_failed"
        ):
            passed += 1
        else:
            failures.append(
                f"F: expected FAIL stop — got "
                f"status={result.get('status')} code={result.get('last_failure_code')}"
            )

        # --- G. TIMEOUT → stopped -----------------------------------------
        total += 1
        timeout_smoke = {"verdict": "FAIL", "failure_code": "smoke_timeout",
                         "failure_reason": "wall-clock timeout"}
        result = _self_test_with_smoke(
            timeout_smoke, {"status": "running"},
            config=AutopilotConfig(autopilot_mode="auto_publish",
                                   max_cycles=1, max_hours=0.1),
        )
        if (
            result.get("status") == "failed"
            and "TIMEOUT" in (result.get("stop_reason") or "")
        ):
            passed += 1
        else:
            failures.append(
                f"G: expected TIMEOUT stop — got "
                f"reason={result.get('stop_reason')}"
            )

        # --- H. auto_commit → commit only, no push -----------------------
        total += 1
        result = _self_test_with_smoke(
            ready_smoke, ready_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_commit", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
            git_commit_override=(True, "deadbeefcafe1234", "ok"),
        )
        last = (result.get("history") or [{}])[-1]
        if (
            last.get("publish_action") == "commit"
            and last.get("push_status") == "skipped"
            and last.get("commit_hash") == "deadbeefcafe1234"
        ):
            passed += 1
        else:
            failures.append(
                f"H: expected commit-only — got "
                f"publish_action={last.get('publish_action')} "
                f"push={last.get('push_status')}"
            )

        # --- I. safe_run → no commit/push ---------------------------------
        total += 1
        result = _self_test_with_smoke(
            ready_smoke, ready_factory,
            config=AutopilotConfig(
                autopilot_mode="safe_run", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
        )
        last = (result.get("history") or [{}])[-1]
        if (
            last.get("publish_action") == "none"
            and last.get("push_status") == "skipped"
            and last.get("commit_hash") in (None, "")
        ):
            passed += 1
        else:
            failures.append(
                f"I: expected safe_run no-publish — got "
                f"action={last.get('publish_action')} "
                f"commit={last.get('commit_hash')}"
            )

        # --- J. autopilot_state.json + report.md created -----------------
        total += 1
        sp = Path(tmp) / ".runtime" / "autopilot_state.json"
        rp = Path(tmp) / ".runtime" / "autopilot_report.md"
        if sp.is_file() and rp.is_file():
            text = rp.read_text(encoding="utf-8")
            if "Stampport Auto Pilot Report" in text and "Cycle log" in text:
                passed += 1
            else:
                failures.append("J: report.md missing required headings")
        else:
            failures.append(
                f"J: state={sp.is_file()} report={rp.is_file()}"
            )

        # --- K. UI surfaces autopilot state via load_state() -------------
        total += 1
        snap = load_state()
        if (
            "status" in snap
            and "mode" in snap
            and "history" in snap
            and "last_verdict" in snap
        ):
            passed += 1
        else:
            failures.append(
                "K: load_state missing required keys "
                f"(got {list(snap.keys())[:8]})"
            )

        # --- L. _consecutive_no_change_holds counts only HOLD+no-change.
        total += 1
        history_l = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": False},
        ]
        n_l = _consecutive_no_change_holds(history_l)
        history_l_break = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "READY_TO_REVIEW", "code_changed": True},
            {"verdict": "HOLD", "code_changed": False},
        ]
        n_l_break = _consecutive_no_change_holds(history_l_break)
        history_l_apply = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": True},
            {"verdict": "HOLD", "code_changed": False},
        ]
        n_l_apply = _consecutive_no_change_holds(history_l_apply)
        if n_l == 3 and n_l_break == 1 and n_l_apply == 1:
            passed += 1
        else:
            failures.append(
                f"L: consecutive_no_change_holds — n={n_l} "
                f"break={n_l_break} apply={n_l_apply}"
            )

        # --- M. _populate_cycle_record_from_state mirrors hold telemetry.
        total += 1
        rec_m = CycleRecord(cycle=1, started_at=_utc_now())
        smoke_m = {
            "verdict": "HOLD",
            "pm_hold_type": "soft",
            "design_spec_status": "generated",
            "design_spec_acceptance_passed": True,
            "stale_design_spec_detected": False,
            "active_rework_feature": "TitleSeal",
            "active_rework_hold_count": 2,
        }
        fs_m = {
            "claude_apply_changed_files": ["app/web/src/foo.jsx"],
            "claude_apply_status": "applied",
            "claude_proposal_status": "generated",
            "implementation_ticket_status": "generated",
            "selected_feature": "TitleSeal 컴포넌트",
            "pm_decision_ship_ready": False,
            "pm_hold_type": "soft",
            "code_changed": True,
        }
        _populate_cycle_record_from_state(rec_m, smoke_m, fs_m)
        if (
            rec_m.hold_type == "soft"
            and rec_m.design_spec_status == "generated"
            and rec_m.implementation_ticket_status == "generated"
            and rec_m.claude_apply_status == "applied"
            and rec_m.code_changed is True
            and rec_m.active_rework_feature == "TitleSeal"
            and rec_m.active_rework_hold_count == 2
            and rec_m.pm_verdict == "HOLD"
            and rec_m.selected_feature == "TitleSeal 컴포넌트"
        ):
            passed += 1
        else:
            failures.append(
                f"M: populate_cycle_record — hold_type={rec_m.hold_type!r} "
                f"design_spec={rec_m.design_spec_status!r} "
                f"impl_ticket={rec_m.implementation_ticket_status!r} "
                f"apply={rec_m.claude_apply_status!r} "
                f"code_changed={rec_m.code_changed} "
                f"feature={rec_m.selected_feature!r}"
            )

        # --- N. _hold_loop_root_cause renders telemetry table when the
        # run terminated via no_progress_hold_loop.
        total += 1
        st_n = AutopilotState()
        st_n.stop_reason = (
            "no_progress_hold_loop: 3 consecutive HOLD cycles with "
            "code_changed=false — implementation never reached."
        )
        st_n.last_failure_code = "no_progress_hold_loop"
        st_n.history = [
            {
                "cycle": i, "verdict": "HOLD", "code_changed": False,
                "hold_type": "soft" if i % 2 else "hard",
                "design_spec_status": "skipped",
                "implementation_ticket_status": "skipped_hold",
                "claude_propose_status": "skipped",
                "claude_apply_status": "skipped",
            }
            for i in range(1, 4)
        ]
        out_n = _hold_loop_root_cause(st_n)
        joined_n = "\n".join(out_n)
        if (
            "no_progress_hold_loop" in joined_n
            and "Cycle별 skip 단계" in joined_n
            and "skipped_hold" in joined_n
            and "soft" in joined_n
            and "hard" in joined_n
        ):
            passed += 1
        else:
            failures.append(
                f"N: hold_loop_root_cause for no_progress_hold_loop — "
                f"snippet={joined_n[:300]!r}"
            )

        # --- O. _max_cycles_boundary_classification policy fires only
        # for max_cycles >= 3 + history >= 3 + last 3 all
        # HOLD+code_changed=false. Verifies the explicit boundary
        # helper end-to-end (the integration path is exercised by D1
        # / D2; this is the unit test for the boundary policy).
        total += 1
        all_hold_no_change = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": False},
        ]
        # max_cycles too small → policy declines.
        cls_o_small = _max_cycles_boundary_classification(
            all_hold_no_change, max_cycles=2,
        )
        # history too short → policy declines.
        cls_o_short = _max_cycles_boundary_classification(
            all_hold_no_change[:2], max_cycles=3,
        )
        # full match → policy fires.
        cls_o_full = _max_cycles_boundary_classification(
            all_hold_no_change, max_cycles=3,
        )
        # mixed history → declines.
        cls_o_mixed = _max_cycles_boundary_classification(
            [
                {"verdict": "HOLD", "code_changed": False},
                {"verdict": "READY_TO_REVIEW", "code_changed": True},
                {"verdict": "HOLD", "code_changed": False},
            ],
            max_cycles=3,
        )
        if (
            cls_o_small is None
            and cls_o_short is None
            and cls_o_full == "no_progress_hold_loop"
            and cls_o_mixed is None
        ):
            passed += 1
        else:
            failures.append(
                f"O: boundary classifier policy — small={cls_o_small!r} "
                f"short={cls_o_short!r} full={cls_o_full!r} "
                f"mixed={cls_o_mixed!r}"
            )

        # --- P. _consecutive_no_change_holds threshold: 3 consecutive
        # HOLD+no-change cycles must hit the in-loop trigger (verifies
        # the existing threshold-based path is not broken by the new
        # boundary classifier).
        total += 1
        history_p = [
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": False},
            {"verdict": "HOLD", "code_changed": False},
        ]
        if _consecutive_no_change_holds(history_p) >= NO_CHANGE_HOLD_STOP_THRESHOLD:
            passed += 1
        else:
            failures.append(
                f"P: 3xHOLD+no-change must hit "
                f"NO_CHANGE_HOLD_STOP_THRESHOLD (got n="
                f"{_consecutive_no_change_holds(history_p)})"
            )

        # --- PD-A. PipelineDecision: legacy scope null with everything else
        # green must NOT block can_publish. Regression for the
        # scope_consistency_status=null bug that froze auto-publish.
        from . import cycle as _cycle  # local import: stdlib-only at module level
        total += 1
        decision = _cycle.build_pipeline_decision({
            "status": "succeeded",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "applied",
            "qa_status": "passed",
            "apply_preflight_status": "passed",
            "claude_apply_changed_files": ["a.py", "b.py", "c.py", "d.py", "e.py"],
            "scope_consistency_status": None,
        })
        if (
            decision.get("can_publish") is True
            and decision.get("blocking_code") is None
            and decision.get("pipeline_status") == "ready_to_publish"
        ):
            passed += 1
        else:
            failures.append(
                "PD-A: scope_consistency_status=None must NOT block "
                f"can_publish — got decision={decision}"
            )

        # --- PD-B. ticket missing → blocking_code=missing_ticket_contract.
        total += 1
        decision = _cycle.build_pipeline_decision({
            "status": "succeeded",
            "implementation_ticket_status": "skipped",
            "claude_apply_status": "applied",
            "qa_status": "passed",
            "apply_preflight_status": "passed",
            "claude_apply_changed_files": ["a.py"],
        })
        if (
            decision.get("can_publish") is False
            and decision.get("blocking_code") == "missing_ticket_contract"
        ):
            passed += 1
        else:
            failures.append(
                "PD-B: ticket missing must blocking_code="
                f"missing_ticket_contract — got {decision}"
            )

        # --- PD-C. claude_apply_status=retry_required → apply_not_completed.
        total += 1
        decision = _cycle.build_pipeline_decision({
            "status": "running",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "retry_required",
            "qa_status": "skipped",
            "apply_preflight_status": "passed",
            "claude_apply_changed_files": [],
        })
        if (
            decision.get("can_publish") is False
            and decision.get("blocking_code") == "apply_not_completed"
        ):
            passed += 1
        else:
            failures.append(
                "PD-C: claude_apply_status=retry_required must blocking_code="
                f"apply_not_completed — got {decision}"
            )

        # --- PD-D. qa_status=failed → blocking_code=qa_failed.
        total += 1
        decision = _cycle.build_pipeline_decision({
            "status": "failed",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "applied",
            "qa_status": "failed",
            "qa_failed_reason": "build_artifact missing",
            "apply_preflight_status": "passed",
            "claude_apply_changed_files": ["a.py"],
        })
        if (
            decision.get("can_publish") is False
            and decision.get("blocking_code") == "qa_failed"
        ):
            passed += 1
        else:
            failures.append(
                "PD-D: qa_status=failed must blocking_code=qa_failed — "
                f"got {decision}"
            )

        # --- PD-E. failed_stage set → blocking_code=stage_failed.
        total += 1
        decision = _cycle.build_pipeline_decision({
            "status": "failed",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "applied",
            "qa_status": "passed",
            "apply_preflight_status": "passed",
            "claude_apply_changed_files": ["a.py"],
            "failed_stage": "claude_apply",
            "failed_reason": "build_app revalidation failed",
        })
        if (
            decision.get("can_publish") is False
            and decision.get("blocking_code") == "stage_failed"
            and "claude_apply" in (decision.get("blocking_reason") or "")
        ):
            passed += 1
        else:
            failures.append(
                "PD-E: failed_stage set must blocking_code=stage_failed — "
                f"got {decision}"
            )

        # --- PD-F. no changed files → blocking_code=no_meaningful_change.
        total += 1
        decision = _cycle.build_pipeline_decision({
            "status": "succeeded",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "applied",
            "qa_status": "passed",
            "apply_preflight_status": "passed",
            "claude_apply_changed_files": [],
            "changed_files_count": 0,
        })
        if (
            decision.get("can_publish") is False
            and decision.get("blocking_code") == "no_meaningful_change"
        ):
            passed += 1
        else:
            failures.append(
                "PD-F: changed_files=[] must blocking_code="
                f"no_meaningful_change — got {decision}"
            )

        # --- PD-G. Full happy path with scope_consistency_status=None →
        # autopilot must NOT halt with publish-gate-blocked. Regression
        # for the bug where legacy scope null froze auto-publish even
        # though every real gate had passed.
        total += 1
        happy_factory = {
            "status": "succeeded",
            "qa_status": "passed",
            "apply_preflight_status": "passed",
            "implementation_ticket_status": "generated",
            "claude_apply_status": "applied",
            "claude_apply_changed_files": ["app/web/src/screens/Foo.jsx"],
            "design_spec_acceptance_passed": True,
            "design_spec_status": "accepted",
            "selected_feature": "PD-G fixture",
            # scope_consistency_status intentionally omitted (legacy null)
        }
        happy_smoke = {
            "verdict": "READY_TO_REVIEW",
            "failure_code": None,
            "factory_status": "ready_to_review",
            "qa_status": "passed",
            "scope_consistency_status": None,
            "changed_files_count": 1,
            "changed_files": ["app/web/src/screens/Foo.jsx"],
        }
        result = _self_test_with_smoke(
            happy_smoke, happy_factory,
            config=AutopilotConfig(
                autopilot_mode="auto_publish", max_cycles=1, max_hours=0.1,
                stop_on_hold=False, require_scope_consistency=True,
                require_render_check=True, require_api_health=False,
            ),
            git_dirty_override=True,
            render_override={"ok": True, "status": "passed", "message": "ok"},
            git_commit_override=(True, "feedfacecafe1234", "ok"),
            git_push_override=(True, "ok"),
        )
        last = (result.get("history") or [{}])[-1]
        if (
            last.get("publish_action") == "push"
            and last.get("push_status") == "succeeded"
            and "publish gate blocked" not in (result.get("stop_reason") or "")
        ):
            passed += 1
        else:
            failures.append(
                "PD-G: happy path with scope_consistency=None must reach "
                f"push — got publish_action={last.get('publish_action')} "
                f"push={last.get('push_status')} reason={result.get('stop_reason')}"
            )

    # Restore env.
    if repo_prev is None:
        os.environ.pop("LOCAL_RUNNER_REPO", None)
    else:
        os.environ["LOCAL_RUNNER_REPO"] = repo_prev

    return passed, total, failures


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description="Stampport Auto Pilot Publish loop.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--mode", choices=VALID_MODES, default="safe_run")
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--max-hours", type=float, default=0.5)
    parser.add_argument("--stop-on-hold", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-health", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        passed, total, fails = self_test()
        print(f"[autopilot self-test] {passed}/{total} passed")
        for f in fails:
            print(f"  FAIL · {f}")
        return 0 if passed == total else 1

    cfg = AutopilotConfig(
        autopilot_enabled=True,
        autopilot_mode=args.mode,
        max_cycles=args.max_cycles,
        max_hours=args.max_hours,
        stop_on_hold=args.stop_on_hold,
        require_render_check=not args.no_render,
        require_api_health=not args.no_health,
    )
    ok, msg = start(cfg)
    print(f"[autopilot] {msg}")
    if not ok:
        return 1
    while is_running():
        time.sleep(2.0)
    snap = load_state()
    print(f"[autopilot] final status={snap.get('status')} "
          f"reason={snap.get('stop_reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
