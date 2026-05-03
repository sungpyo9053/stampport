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
    _log(f"smoke spawn timeout={timeout_sec}s cmd={' '.join(cmd)}")
    try:
        # Wall-clock cap = smoke timeout + 5min buffer. The smoke runner
        # has its own internal deadline; we don't want to kill it early.
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 300,
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


def evaluate_publish_gate(
    smoke_state: dict, factory_state: dict, *, require_scope: bool
) -> tuple[bool, str | None, dict]:
    """Apply the auto-publish pre-conditions enumerated in spec §4.

    Returns (ok, failure_reason, evidence_dict).
    """
    evidence: dict[str, Any] = {
        "factory_status": factory_state.get("status"),
        "qa_status": factory_state.get("qa_status"),
        "scope_consistency_status": factory_state.get("scope_consistency_status"),
        "scope_mismatch_reason": factory_state.get("scope_mismatch_reason"),
        "implementation_ticket_status": factory_state.get(
            "implementation_ticket_status"
        ),
        "claude_apply_status": factory_state.get("claude_apply_status"),
        "design_spec_acceptance_passed": factory_state.get(
            "design_spec_acceptance_passed"
        ),
        "design_spec_status": factory_state.get("design_spec_status"),
        "changed_files_count": len(
            factory_state.get("claude_apply_changed_files") or []
        ),
        "branch": _git_branch(),
    }

    # 1. factory_state.status must be terminal-success.
    f_status = (factory_state.get("status") or "").strip()
    if f_status not in {"succeeded", "completed", "ready_to_review",
                        "ready_to_publish"}:
        return False, f"factory_state.status='{f_status}' not terminal-success", evidence

    # 2. qa_status passed.
    qa = (factory_state.get("qa_status") or "").strip()
    if qa != "passed":
        return False, f"qa_status='{qa or 'missing'}', expected passed", evidence

    # 3. scope_consistency_status passed (if required).
    if require_scope:
        scope = (factory_state.get("scope_consistency_status") or "").strip()
        if scope != "passed":
            reason = factory_state.get("scope_mismatch_reason") or "(no reason)"
            return False, (
                f"scope_consistency_status='{scope or 'missing'}' "
                f"({reason})"
            ), evidence

    # 4. design_spec_acceptance — only enforce when the cycle is in
    # design-spec mode (design_spec_status reports anything non-empty).
    spec_status = (factory_state.get("design_spec_status") or "").strip()
    spec_passed = factory_state.get("design_spec_acceptance_passed")
    if spec_status and spec_status not in {"missing", "skipped"}:
        if spec_passed is False or spec_passed is None:
            return False, (
                f"design_spec_acceptance_passed={spec_passed} "
                f"(status={spec_status})"
            ), evidence

    # 5. implementation_ticket / claude_apply.
    ticket = (factory_state.get("implementation_ticket_status") or "").strip()
    if ticket != "generated":
        return False, f"implementation_ticket_status='{ticket}'", evidence
    apply_status = (factory_state.get("claude_apply_status") or "").strip()
    if apply_status != "applied":
        return False, f"claude_apply_status='{apply_status}'", evidence

    # 6. changed_files_count > 0 + risky scan.
    changed_files = factory_state.get("claude_apply_changed_files") or []
    if not changed_files:
        return False, "changed_files_count=0", evidence
    risky = _scan_changed_files_for_risk(changed_files)
    if risky:
        evidence["risky_changed_files"] = risky[:10]
        return False, (
            f"risky paths in changed_files: {', '.join(risky[:3])}"
        ), evidence

    # 7. Smoke verdict must be a ready/pass — caller normally pre-checks
    # but we re-verify here so the gate is composable.
    sv = (smoke_state.get("verdict") or "").strip()
    if sv not in READY_VERDICTS:
        return False, f"smoke verdict={sv} not READY/PASS", evidence

    # 8. Branch must be main for auto_publish.
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
    if not history or "max_cycles" not in stop_reason:
        return []
    verdicts = {(h.get("verdict") or "").upper() for h in history}
    if verdicts and verdicts != {"HOLD"}:
        return []

    lines: list[str] = ["", "## HOLD 반복 종료 (root cause)", ""]
    lines.append(
        f"- {len(history)}개 cycle 모두 HOLD 로 종료. claude_apply 가 한 번도 실행되지 않아 "
        f"commit/push 가 발생할 수 없었습니다."
    )

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
    lines = [
        "# Stampport Auto Pilot Report",
        "",
        f"- 모드: `{state.mode}`",
        f"- 시작: `{state.started_at}`",
        f"- 종료: `{state.ended_at}`",
        f"- 종료 사유: `{state.stop_reason or '—'}`",
        f"- 실행한 cycle 수: `{state.cycle_count}` / 최대 `{state.max_cycles}`",
        f"- 최대 시간: `{state.max_hours}h`",
        f"- 마지막 verdict: `{state.last_verdict}` "
        f"({state.last_failure_code or '—'})",
        f"- 마지막 commit: `{state.last_commit_hash or '—'}`",
        f"- 마지막 push: `{state.last_push_status or '—'}`",
        f"- 마지막 health: `{state.last_health_status or '—'}`",
        f"- 마지막 render: `{state.last_render_status or '—'}`",
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
        _save_state()

    _log(
        f"autopilot start mode={config.autopilot_mode} "
        f"max_cycles={config.max_cycles} max_hours={config.max_hours}"
    )

    cycle = 0
    try:
        while not stop_event.is_set():
            if cycle >= config.max_cycles:
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

            # FAIL / TIMEOUT / scope_mismatch — immediate stop.
            failure_norm = _classify_failure(verdict, smoke)
            if (
                verdict in FAIL_VERDICTS
                or failure_norm in {"TIMEOUT", "scope_mismatch"}
            ):
                rec.finished_at = _utc_now()
                rec.publish_action = "skip"
                rec.note = f"halt on {failure_norm}"
                _record_cycle(rec)
                _stop(
                    f"{failure_norm}: "
                    f"{(smoke.get('failure_reason') or failure_norm)[:200]}",
                    status="failed",
                )
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
                rec.note = "HOLD → carry rework prompt to next cycle"
                rec.finished_at = _utc_now()
                _record_cycle(rec)
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
            max_cycles=1,
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

        # --- D. HOLD + stop_on_hold=false → continue (no stop_reason) ----
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
        # max_cycles=1 means we'll naturally stop after one HOLD cycle —
        # but the stop reason should be `max_cycles reached` (loop fell
        # through to next iteration), NOT a HOLD-specific halt.
        if (
            result.get("status") == "stopped"
            and "max_cycles" in (result.get("stop_reason") or "")
        ):
            passed += 1
        else:
            failures.append(
                f"D: expected continue-on-HOLD then max_cycles — got "
                f"status={result.get('status')} reason={result.get('stop_reason')}"
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
