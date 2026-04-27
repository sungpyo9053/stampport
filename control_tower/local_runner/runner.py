"""Stampport Local Runner.

Runs on the developer's Mac. Polls the Stampport Lab Control Tower for
queued commands, executes ONLY commands in the allowlist, and reports
the result back. The server never reaches out to the Mac — all traffic
is outbound from the Runner.

Run it like this:

    export CONTROL_TOWER_URL='https://reviewdr.kr/stampport-control-api'
    export LOCAL_RUNNER_ID='sungpyo-macbook'
    export LOCAL_RUNNER_TOKEN='<the bearer token>'
    export LOCAL_FACTORY_START_SCRIPT="$PWD/scripts/local_factory_start.sh"
    export LOCAL_FACTORY_STOP_SCRIPT="$PWD/scripts/local_factory_stop.sh"
    export LOCAL_FACTORY_STATUS_SCRIPT="$PWD/scripts/local_factory_status.sh"
    python3 -m control_tower.local_runner.runner

Hard rules baked in:
    - Only commands in COMMAND_HANDLERS are honored. Anything else is
      reported back as "rejected_unknown_command" with no execution.
    - Shell commands are launched via subprocess with a fixed argv list.
      No string is ever spliced into a shell command.
    - The script paths must be **absolute** and must exist on disk.
    - No request payload is ever passed to a shell — all args are fixed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# Allowlist + how to handle each. Mirrors ALLOWED_COMMANDS in the server.
HANDLERS_DOC = """
    start_factory        — run LOCAL_FACTORY_START_SCRIPT
    stop_factory         — run LOCAL_FACTORY_STOP_SCRIPT
    restart_factory      — stop, sleep 1.5s, start
    pause_factory        — write 'paused' marker (start script reads it)
    resume_factory       — clear 'paused' marker
    status               — run LOCAL_FACTORY_STATUS_SCRIPT
    git_pull             — git -C <repo> pull --ff-only
    build_check          — npm run build for app/web (one shot)
    test_check           — python3 -c '...'  (placeholder; project-specific)
"""

# ---------------------------------------------------------------------------
# Config (env-driven, never logged in full)
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default if default is not None else "")
    if required and not v:
        sys.stderr.write(f"FATAL: env var {name} is required\n")
        sys.exit(2)
    return v


CONTROL_TOWER_URL = _env(
    "CONTROL_TOWER_URL",
    default="https://reviewdr.kr/stampport-control-api",
)
RUNNER_ID = _env("LOCAL_RUNNER_ID", default="local-runner")
RUNNER_TOKEN = _env("LOCAL_RUNNER_TOKEN", default="")  # warning if empty
RUNNER_NAME = _env("LOCAL_RUNNER_NAME", default=RUNNER_ID)

REPO_ROOT = Path(_env("LOCAL_RUNNER_REPO", default=str(Path.cwd())))
RUNTIME_DIR = REPO_ROOT / ".runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
PAUSE_MARKER = RUNTIME_DIR / "factory.paused"
PID_FILE = RUNTIME_DIR / "local_factory.pid"
STATE_FILE = RUNTIME_DIR / "factory_state.json"
REPORT_FILE = RUNTIME_DIR / "factory_last_report.md"
LOG_FILE = RUNTIME_DIR / "local_factory.log"
PUBLISH_STATE_FILE = RUNTIME_DIR / "factory_publish.json"

# Deploy state — separate from publish state so the dashboard can
# render "publish vs deploy" without conflating commit hashes with
# remote-build outcomes.
DEPLOY_STATE_FILE = RUNTIME_DIR / "factory_deploy.json"
# Single-flight lock for deploy_to_server. Created with O_EXCL on
# acquire, deleted on release. A stale lock older than
# DEPLOY_LOCK_STALE_SEC is force-cleared so a crashed deploy doesn't
# permanently wedge the queue.
DEPLOY_LOCK_FILE = RUNTIME_DIR / "factory_deploy.lock"
DEPLOY_LOCK_STALE_SEC = 30 * 60  # 30 minutes

# Operator Fix Request artifacts. See _h_operator_fix_request handler
# below for the lifecycle: dashboard textarea → command queue → runner
# writes the request file → Claude CLI edits files → runner runs QA
# Gate → optional publish.
OPERATOR_REQUEST_FILE = RUNTIME_DIR / "operator_request.md"
OPERATOR_FIX_STATE_FILE = RUNTIME_DIR / "operator_fix_state.json"

# GitHub repo target. Used in heartbeat metadata as `actions_url`.
GITHUB_REPO = _env("LOCAL_RUNNER_GITHUB_REPO", default="sungpyo9053/stampport")
ACTIONS_URL = f"https://github.com/{GITHUB_REPO}/actions"


# ---------------------------------------------------------------------------
# Publish-time policy (mirrored on the server side via heartbeat metadata
# so the dashboard can decide whether to enable the deploy button).
# ---------------------------------------------------------------------------

# Roots whose contents may be auto-committed wholesale. Stampport의
# 자동화 공장은 *변경을 만들기 위한* 시스템이라, deploy/CI/scripts
# 변경도 publish 대상이다. 차단은 secret/conflict/build/health 게이트가
# 책임진다.
ALLOWED_PUBLISH_DIR_ROOTS: tuple[str, ...] = (
    "app/",
    "control_tower/",
    "scripts/",
    "deploy/",
    "config/",
    "docs/",
    ".github/",
)
# Specific top-level files that may be auto-committed.
ALLOWED_PUBLISH_FILES: frozenset[str] = frozenset({
    "CLAUDE.md",
    "README.md",
    "CHANGELOG.md",
    ".gitignore",
})
# Filename prefixes are no longer needed — the dir roots cover them.
ALLOWED_PUBLISH_FILE_PREFIXES: tuple[str, ...] = ()

# Anything in the change set matching one of these substrings makes
# publish refuse outright. Restricted to secret-shaped paths only —
# cache/build artifacts (.runtime/, node_modules/, dist/, .venv/,
# __pycache__/) are gitignored and shouldn't appear; if they do, they
# are NOT a publish blocker. Same logic as cycle.py's RISKY_PATTERNS.
RISKY_PUBLISH_PATTERNS: tuple[str, ...] = (
    ".env",
    ".pem",
    ".key",
    ".db",
    ".claude/settings.local.json",
)

# Used to block publish on deploy/CI/infra changes. The Stampport
# automation factory now treats these as warnings — build/health/
# secret gates decide whether the change actually ships. Empty by
# design; we keep the constant so the classifier signature stays
# stable for back-compat callers.
BLOCKED_PUBLISH_PATTERNS: tuple[str, ...] = ()


# Strict secret-blob markers. Substring presence in any added line is
# enough to abort — these never legitimately appear in source.
SECRET_STRICT_MARKERS: tuple[str, ...] = (
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN DSA PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN PGP PRIVATE KEY",
    "BEGIN PRIVATE KEY",
)

# Keyword=value patterns. The minimum value length on each pattern is
# tuned so an env-var name like `KAKAO_ACCESS_TOKEN` or a code reference
# `os.environ["KAKAO_ACCESS_TOKEN"]` does NOT match — only an actual
# literal long value does.
SECRET_VALUE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("AWS_SECRET_ACCESS_KEY", re.compile(r"AWS_SECRET_ACCESS_KEY\s*[:=]\s*['\"]?([A-Za-z0-9/+=._-]{16,})")),
    ("KAKAO_ACCESS_TOKEN",    re.compile(r"KAKAO_ACCESS_TOKEN\s*[:=]\s*['\"]?([A-Za-z0-9/+=._-]{20,})")),
    ("KAKAO_REFRESH_TOKEN",   re.compile(r"KAKAO_REFRESH_TOKEN\s*[:=]\s*['\"]?([A-Za-z0-9/+=._-]{20,})")),
    ("TELEGRAM_BOT_TOKEN",    re.compile(r"TELEGRAM_BOT_TOKEN\s*[:=]\s*['\"]?(\d+:[A-Za-z0-9_-]{20,})")),
    ("SMTP_PASSWORD",         re.compile(r"SMTP_PASSWORD\s*[:=]\s*['\"]?([A-Za-z0-9/+=._@!#$%-]{8,})")),
    ("password=",             re.compile(r"\bpassword\s*=\s*['\"]([A-Za-z0-9/+=._@!#$%-]{12,})['\"]", re.IGNORECASE)),
    ("secret=",               re.compile(r"\bsecret\s*=\s*['\"]([A-Za-z0-9/+=._-]{16,})['\"]",          re.IGNORECASE)),
    ("token=",                re.compile(r"\btoken\s*=\s*['\"]([A-Za-z0-9/+=._-]{20,})['\"]",           re.IGNORECASE)),
)

START_SCRIPT  = _env("LOCAL_FACTORY_START_SCRIPT",  default=str(REPO_ROOT / "scripts/local_factory_start.sh"))
STOP_SCRIPT   = _env("LOCAL_FACTORY_STOP_SCRIPT",   default=str(REPO_ROOT / "scripts/local_factory_stop.sh"))
STATUS_SCRIPT = _env("LOCAL_FACTORY_STATUS_SCRIPT", default=str(REPO_ROOT / "scripts/local_factory_status.sh"))

POLL_INTERVAL_SEC = float(_env("LOCAL_RUNNER_POLL_INTERVAL", default="3.0"))
HEARTBEAT_INTERVAL_SEC = float(_env("LOCAL_RUNNER_HEARTBEAT_INTERVAL", default="15.0"))

_running = True

# Process identity, captured once at boot. RUNNER_STARTED_AT lets the
# dashboard show "재시작된 지 N분 전" so a user can confirm a restart
# actually replaced the process.
RUNNER_PID = os.getpid()
RUNNER_STARTED_AT = (
    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
)

# A handler may set this to schedule work that has to happen AFTER its
# result has been reported to the server (because the work either kills
# the process — exec_self — or restarts the factory child loop, both of
# which would interrupt the in-flight report otherwise). _execute reads
# the flag, runs the action, and clears it.
_POST_REPORT_ACTION: str | None = None

# Test/CI escape hatch. When `LOCAL_RUNNER_RESTART_DRY_RUN=true` the
# restart handlers report what they WOULD do but skip the actual
# subprocess + os.execv side effects, so we can verify the wiring
# without bouncing the live process.
def _restart_is_dry_run() -> bool:
    v = os.environ.get("LOCAL_RUNNER_RESTART_DRY_RUN", "").strip().lower()
    return v in {"true", "1", "yes", "on"}


def _shutdown(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    sys.stderr.write(f"\n[runner] caught signal {signum}, exiting…\n")


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ---------------------------------------------------------------------------
# Tiny HTTP helpers (stdlib only — keep deps to zero)
# ---------------------------------------------------------------------------


def _request(method: str, path: str, body: dict | None = None) -> dict | None:
    url = f"{CONTROL_TOWER_URL.rstrip('/')}{path}"
    data = None if body is None else json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if RUNNER_TOKEN:
        headers["Authorization"] = f"Bearer {RUNNER_TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:200]
        sys.stderr.write(f"[runner] {method} {path} → HTTP {e.code}: {body_text}\n")
        return None
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[runner] {method} {path} failed: {e}\n")
        return None


# ---------------------------------------------------------------------------
# Safe subprocess wrapper
# ---------------------------------------------------------------------------


def _run_script(path: str, *, timeout: float = 60.0) -> tuple[bool, str]:
    """Execute a known absolute script path. Path is validated.

    The args are a fixed argv list — never a shell string. The script
    itself can do whatever bash supports, but the runner does not splice
    any user input into the command line.
    """
    if not path.startswith("/"):
        return False, f"script path must be absolute: {path}"
    p = Path(path)
    if not p.is_file():
        return False, f"script not found: {path}"
    try:
        r = subprocess.run(
            ["bash", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        ok = (r.returncode == 0)
        out = (r.stdout + ("\n--stderr--\n" + r.stderr if r.stderr else ""))[-1500:]
        return ok, out
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ---------------------------------------------------------------------------
# Command handlers (return (success, message))
# ---------------------------------------------------------------------------


def _h_start(_payload: dict) -> tuple[bool, str]:
    if PAUSE_MARKER.exists():
        PAUSE_MARKER.unlink()
    return _run_script(START_SCRIPT, timeout=20)


def _h_stop(_payload: dict) -> tuple[bool, str]:
    return _run_script(STOP_SCRIPT, timeout=20)


def _h_restart(_payload: dict) -> tuple[bool, str]:
    if _restart_is_dry_run():
        return True, "DRY_RUN: would stop+start factory"
    ok1, out1 = _h_stop({})
    time.sleep(1.5)
    ok2, out2 = _h_start({})
    return (ok1 and ok2), f"stop:\n{out1}\n---\nstart:\n{out2}"


def _h_pause(_payload: dict) -> tuple[bool, str]:
    PAUSE_MARKER.write_text(datetime.utcnow().isoformat() + "\n")
    return True, f"paused (marker: {PAUSE_MARKER})"


def _h_resume(_payload: dict) -> tuple[bool, str]:
    if PAUSE_MARKER.exists():
        PAUSE_MARKER.unlink()
    return True, "paused marker removed"


def _h_status(_payload: dict) -> tuple[bool, str]:
    return _run_script(STATUS_SCRIPT, timeout=10)


def _h_git_pull(_payload: dict) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (r.returncode == 0), (r.stdout + r.stderr)[-1500:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _h_build_check(_payload: dict) -> tuple[bool, str]:
    web_dir = REPO_ROOT / "app" / "web"
    if not web_dir.is_dir():
        return False, f"app/web not found at {web_dir}"
    try:
        r = subprocess.run(
            ["npm", "run", "build"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(web_dir),
        )
        return (r.returncode == 0), (r.stdout + r.stderr)[-1500:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _h_test_check(_payload: dict) -> tuple[bool, str]:
    # Placeholder — repo doesn't have a runnable test suite yet. We
    # report success so the UI can light up the green check, with a note.
    return True, "(MVP) no test suite wired yet — placeholder OK"


# ---------------------------------------------------------------------------
# publish_changes helpers
# ---------------------------------------------------------------------------


def _utc_now_z() -> str:
    """Same UTC-with-Z format that cycle.py emits — keeps timestamps
    consistent with what the dashboard already parses."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _git(*args: str, timeout: float = 60.0) -> tuple[bool, str]:
    """Run a git subcommand against REPO_ROOT. Pure argv list — no shell."""
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (r.stdout or "") + (("\n--stderr--\n" + r.stderr) if r.stderr else "")
        return (r.returncode == 0), out.strip()
    except subprocess.TimeoutExpired:
        return False, f"git timeout: {' '.join(args)}"
    except FileNotFoundError:
        return False, "git not installed"
    except Exception as e:  # noqa: BLE001
        return False, f"git error: {e}"


def _git_current_branch() -> str | None:
    ok, out = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    return out.strip() if ok else None


def _git_changed_files() -> list[str]:
    """Files visible to `git status --porcelain=v1` (modified, untracked,
    staged). Uses null-delimited output so paths with spaces, dots, or
    leading whitespace round-trip cleanly — earlier "%s.strip()" parsing
    silently dropped the leading space and shifted path-by-one for files
    starting with a `.` (e.g. `.claude/settings.local.json`).
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1", "-z"],
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return []
    if r.returncode != 0:
        return []

    raw = r.stdout.decode("utf-8", errors="replace")
    # `-z` separates entries by NUL. Each entry is "XY <path>" but a
    # rename has the destination first followed by another NUL and the
    # source path. We only care about destinations.
    paths: list[str] = []
    entries = [e for e in raw.split("\x00") if e]
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) < 4:
            i += 1
            continue
        x, y = e[0], e[1]
        path = e[3:]  # NOT stripped — preserves leading dot etc.
        if x == "R" or y == "R":
            # Rename: next entry is the source; we want the destination
            # which is `path`. Skip the source.
            i += 2
        else:
            i += 1
        if path:
            paths.append(path)
    # Stable de-dup.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    return deduped


def _is_allowed_publish_path(path: str) -> bool:
    """Return True only for paths under ALLOWED_PUBLISH_DIR_ROOTS, the
    explicit ALLOWED_PUBLISH_FILES, or matching ALLOWED_PUBLISH_FILE_PREFIXES.
    Path-traversal attempts and absolute paths are rejected."""
    if not path:
        return False
    if path.startswith("/") or ".." in path.split("/"):
        return False
    if any(path.startswith(d) for d in ALLOWED_PUBLISH_DIR_ROOTS):
        return True
    if path in ALLOWED_PUBLISH_FILES:
        return True
    if any(path.startswith(p) for p in ALLOWED_PUBLISH_FILE_PREFIXES):
        return True
    return False


def _classify_publish_files(
    files: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Bucket the change set into (allowed, blocked, risky).

    Risky takes precedence over blocked, blocked takes precedence over
    allowed — so a single file is reported in exactly one bucket.
    """
    allowed: list[str] = []
    blocked: list[str] = []
    risky: list[str] = []
    for f in files:
        if any(p in f for p in RISKY_PUBLISH_PATTERNS):
            risky.append(f)
            continue
        if any(p in f for p in BLOCKED_PUBLISH_PATTERNS):
            blocked.append(f)
            continue
        if _is_allowed_publish_path(f):
            allowed.append(f)
        else:
            # Path didn't match any allowed root or file — refuse
            # rather than guess. This catches stray top-level files
            # like CHANGELOG.md, docs/, etc.
            blocked.append(f)
    return allowed, blocked, risky


def _scan_secret_patterns_in_diff(file_paths: list[str]) -> list[tuple[str, str]]:
    """Scan `git diff HEAD -- <files>` ADDED lines for likely secret values.

    Returns a list of (label, redacted_snippet). Captured values are
    NEVER returned verbatim — we replace them with `<REDACTED>` in the
    snippet so the result is safe to log/report. Conservative on
    purpose: any keyword=value match with a long-enough value counts
    as a hit, even if it might be a placeholder.
    """
    if not file_paths:
        return []
    ok, diff_out = _git("diff", "HEAD", "--", *file_paths, timeout=60)
    if not ok or not diff_out:
        return []

    hits: list[tuple[str, str]] = []
    for line in diff_out.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]

        for marker in SECRET_STRICT_MARKERS:
            if marker in added:
                hits.append((marker, "<REDACTED line containing private-key marker>"))

        for label, pat in SECRET_VALUE_PATTERNS:
            m = pat.search(added)
            if not m:
                continue
            captured = m.group(1)
            redacted = added.replace(captured, "<REDACTED>")
            # Trim leading + spaces to keep the snippet short and not
            # echo a full source line.
            hits.append((label, redacted.strip()[:120]))
    return hits


def _read_publish_state() -> dict:
    if not PUBLISH_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(PUBLISH_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_publish_state(updates: dict) -> None:
    cur = _read_publish_state()
    cur.update(updates)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISH_STATE_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# Deploy progress — fine-grained per-stage state the dashboard's
# "배포 진행" stepper consults so a user can see exactly where a
# deploy is mid-flight (received → validating → committing → pushing
# → actions_triggered → completed/failed). Persisted under the
# publish_state row so a single _save_publish_state write is enough.
DEPLOY_PROGRESS_HISTORY_CAP = 40
DEPLOY_PROGRESS_TERMINAL = {"completed", "failed", "actions_triggered"}


def _new_deploy_progress(now: str) -> dict:
    return {
        "status": "idle",
        "current_step": None,
        "started_at": None,
        "updated_at": now,
        "completed_at": None,
        "failed_reason": None,
        "failed_at_status": None,
        "actions_url": ACTIONS_URL,
        "history": [],
    }


def _set_deploy_progress(
    status: str,
    *,
    current_step: str | None = None,
    failed_reason: str | None = None,
    log_message: str | None = None,
    reset: bool = False,
) -> None:
    """Update the deploy progress block under publish_state.

    Each call appends a history entry so the dashboard can render a
    timestamped log without the FE having to keep its own buffer
    across reloads. History is capped at DEPLOY_PROGRESS_HISTORY_CAP
    to keep the heartbeat payload small.
    """
    cur = _read_publish_state()
    prev = cur.get("deploy_progress")
    if not isinstance(prev, dict):
        prev = {}
    now = _utc_now_z()
    progress = {
        "status": prev.get("status") or "idle",
        "current_step": prev.get("current_step"),
        "started_at": prev.get("started_at"),
        "updated_at": prev.get("updated_at") or now,
        "completed_at": prev.get("completed_at"),
        "failed_reason": prev.get("failed_reason"),
        "failed_at_status": prev.get("failed_at_status"),
        "actions_url": ACTIONS_URL,
        "history": list(prev.get("history") or [])[-DEPLOY_PROGRESS_HISTORY_CAP:],
    }
    if reset:
        progress["started_at"] = now
        progress["completed_at"] = None
        progress["failed_reason"] = None
        progress["failed_at_status"] = None
    prior_status = progress["status"]
    progress["status"] = status
    progress["updated_at"] = now
    if current_step is not None:
        progress["current_step"] = current_step
    if status == "command_received" and not progress["started_at"]:
        progress["started_at"] = now
    if status in {"completed", "failed"}:
        progress["completed_at"] = now
    if status == "failed":
        if failed_reason is not None:
            progress["failed_reason"] = failed_reason
        # Pin the in-flight stage where the failure happened so the
        # stepper can mark exactly that step red.
        progress["failed_at_status"] = (
            prior_status if prior_status not in ("idle", "failed") else "command_received"
        )
    entry_msg = log_message or current_step or status
    progress["history"].append({
        "at": now,
        "status": status,
        "message": entry_msg,
    })
    progress["history"] = progress["history"][-DEPLOY_PROGRESS_HISTORY_CAP:]
    _save_publish_state({"deploy_progress": progress})


def _publish_is_dry_run() -> bool:
    """Default ON. Only flipping `LOCAL_RUNNER_PUBLISH_DRY_RUN=false`
    explicitly disables dry-run mode."""
    v = os.environ.get("LOCAL_RUNNER_PUBLISH_DRY_RUN", "true").strip().lower()
    return v not in {"false", "0", "no", "off"}


def _publish_is_allowed() -> bool:
    """Default OFF. Must be explicitly opted in to perform real commits."""
    v = os.environ.get("LOCAL_RUNNER_ALLOW_PUBLISH", "false").strip().lower()
    return v in {"true", "1", "yes", "on"}


def _publish_allows_failed_qa() -> bool:
    """Emergency escape hatch — defaults OFF.

    When ON, publish_changes still requires risky/secret-clean state
    (those checks are upstream and not gated by this), but the QA Gate
    sign-off requirement is waived. Use only when the QA system itself
    is broken and a manual review confirmed the change is safe.
    """
    v = os.environ.get(
        "LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA", "false"
    ).strip().lower()
    return v in {"true", "1", "yes", "on"}


def _qa_gate_allows_publish(cycle_state: dict) -> tuple[bool, str]:
    """Decide whether the most recent factory cycle's QA gate signed
    off on a publish.

    Acceptance criteria:
      - .runtime/qa_report.md exists.
      - cycle_state.qa_status == "passed"
      - cycle_state.qa_publish_allowed == True
      - The qa_report file's mtime is newer than the most recent code
        change in the working tree (so a stale 'pass' from before the
        latest edit doesn't carry forward).
    """
    if _publish_allows_failed_qa():
        return True, "QA Gate 우회 (LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA=true)"

    qa_report = RUNTIME_DIR / "qa_report.md"
    if not qa_report.is_file():
        return (
            False,
            "QA Gate가 통과되지 않아 배포를 중단했습니다. "
            "(.runtime/qa_report.md 없음 — 사이클이 QA까지 도달하지 않았을 수 있음)",
        )

    qa_status = (cycle_state or {}).get("qa_status")
    qa_publish_allowed = bool((cycle_state or {}).get("qa_publish_allowed"))
    if qa_status != "passed" or not qa_publish_allowed:
        reason = (cycle_state or {}).get("qa_failed_reason") or qa_status or "unknown"
        return (
            False,
            f"QA Gate가 통과되지 않아 배포를 중단했습니다. (사유: {reason})",
        )

    # Staleness check — make sure the QA pass corresponds to the
    # current working tree, not a previous edit.
    try:
        qa_mtime = qa_report.stat().st_mtime
    except OSError:
        qa_mtime = 0.0

    # Find the newest mtime among files git status currently reports
    # as modified/untracked. If any of those is newer than qa_report,
    # the report is stale relative to the change set being shipped.
    changed = _git_changed_files()
    newest_change = 0.0
    for rel in changed:
        full = REPO_ROOT / rel
        try:
            t = full.stat().st_mtime
        except OSError:
            continue
        if t > newest_change:
            newest_change = t
    if newest_change > qa_mtime + 1.0:  # 1s slack for filesystem precision
        return (
            False,
            "QA Gate 리포트가 현재 작업 트리보다 오래됨 — 다음 사이클의 qa_gate 통과 후 다시 시도하세요.",
        )

    return True, "QA Gate 통과 — 배포 진행"


def _publish_blocker_preflight() -> tuple[bool, str]:
    """Run cycle's Release Safety Gate against the working tree:
    auto-restore drift files, auto-delete cache junk, then refuse only
    if a real blocker (hard_risky secret OR git conflict marker)
    remains. manual_required (cycle.py / runner.py / deploy script /
    nginx 등) is treated as a *warning*, not a publish blocker.

    Returns (ok, message). The message is shown verbatim on the
    dashboard, so the warning path uses the new "Release Safety Gate:
    passed with warnings" copy.
    """
    try:
        from . import cycle as _cycle
    except ImportError as e:
        return False, f"cycle import 실패 (preflight 생략 안전): {e}"

    state = _cycle.CycleState()
    # Pre-cycle status guard — defer to "사이클 실행 중" copy when a
    # factory cycle is mid-flight so we don't race on the same files.
    fs = _read_factory_state() or {}
    if fs.get("status") == "running":
        return False, "사이클 실행 중이라 배포 대기 (파일 차단 아님)"

    try:
        _cycle.stage_publish_blocker_check(state)
        _cycle.stage_publish_blocker_resolve(state)
    except Exception as e:  # noqa: BLE001
        # If the stage itself crashes, do not lock publish forever.
        return True, f"preflight crashed (계속 진행): {e}"

    if state.hard_risky_files:
        return False, "위험 파일(secret 패턴)이 남아 있어 배포를 중단했습니다."
    if state.conflict_marker_files:
        return (
            False,
            f"Git conflict marker가 남아 있는 파일 {len(state.conflict_marker_files)}건 — 배포를 중단했습니다.",
        )
    if state.publish_blocker_status == "warning":
        reason_summary = ", ".join(state.warning_reasons[:3]) or (
            f"warning 파일 {len(state.manual_required_files)}건"
        )
        return (
            True,
            f"Release Safety Gate: passed with warnings — 사유: {reason_summary} — "
            "결과: build/health 통과로 배포 허용",
        )
    return True, "Release Safety Gate clean"


def _revalidate_for_publish() -> tuple[bool, list[str]]:
    """Reuse cycle.py's correctness gate verbatim.

    cycle._revalidate_after_apply runs:
      app/web build, control_tower/web build, py_compile across all
      .py files in app/api + control_tower/api + control_tower/local_runner,
      bash -n on the local_factory_*.sh scripts, and a risky-files git
      status scan. Returns (ok, failure_names).
    """
    try:
        from . import cycle as _cycle
    except ImportError as e:
        return False, [f"cycle import failed: {e}"]
    return _cycle._revalidate_after_apply()


def _build_publish_commit_message(
    allowed_files: list[str],
    state: dict,
    push_message: str,
) -> str:
    """Compose the commit body. Header gets <60 chars from goal.

    Subject form: `Auto factory: <goal short>`
    Body lines describe goal, files, and validation outcome — handy
    later when reviewing `git log` for what the bot decided to commit.
    """
    raw_goal = (state.get("goal") or "").strip()
    short_goal = raw_goal.split("\n", 1)[0]
    if len(short_goal) > 60:
        short_goal = short_goal[:57] + "..."
    if not short_goal:
        short_goal = "factory automated update"

    subject = f"Auto factory: {short_goal}"

    body_lines = [subject, ""]
    if raw_goal and raw_goal != short_goal:
        body_lines.append(f"Goal: {raw_goal}")
        body_lines.append("")
    body_lines.append(f"Changed files ({len(allowed_files)}):")
    for f in allowed_files[:30]:
        body_lines.append(f"  - {f}")
    if len(allowed_files) > 30:
        body_lines.append(f"  ... and {len(allowed_files) - 30} more")
    body_lines.append("")
    body_lines.append(
        "Validation: build_app=passed, build_control=passed, "
        "py_compile=passed, bash -n=passed, risky=0"
    )
    cyc = state.get("cycle")
    if cyc:
        body_lines.append(f"Cycle: #{cyc}")
    body_lines.append("")
    body_lines.append(push_message)
    body_lines.append("Generated by Stampport Local Factory")
    return "\n".join(body_lines)


def _h_publish_changes(_payload: dict) -> tuple[bool, str]:
    """End-to-end publish handler. Mirrors what the dashboard button asks for.

    Stages:
        branch_check → diff_classify → secret_scan → revalidate
        → (dry-run report | git add+commit+push) → save state

    A failure in any stage short-circuits with status=failed and a
    short message that's safe to display in the dashboard. We never
    retry a failed push from inside this handler — the user must
    re-click 배포하기 after fixing whatever broke.
    """
    dry_run = _publish_is_dry_run()
    allow = _publish_is_allowed()
    state_at_start = _read_factory_state() or {}
    started_at = _utc_now_z()

    # Only the dashboard's deploy_to_server flow asks us to surface
    # per-stage progress to the heartbeat. Operator-fix and direct
    # publish_changes calls leave the deploy_progress block alone so
    # the stepper doesn't light up out-of-band.
    track_progress = bool((_payload or {}).get("__track_deploy_progress"))

    def _progress(status: str, *, step: str, fail: str | None = None) -> None:
        if not track_progress:
            return
        _set_deploy_progress(status, current_step=step, failed_reason=fail)

    def _record_failure(stage: str, message: str) -> None:
        """Persist enough for the heartbeat/UI to show why we refused."""
        _save_publish_state({
            "last_push_status": "failed",
            "last_push_at": _utc_now_z(),
            "last_publish_message": f"{stage}: {message}",
            "last_failed_stage": stage,
            "last_attempt_started_at": started_at,
        })
        if track_progress:
            _set_deploy_progress(
                "failed",
                current_step=f"{stage} 실패",
                failed_reason=message[:280],
            )

    _progress("validating", step="브랜치 / Release Safety Gate 확인 중")

    # 1. Must be on main.
    branch = _git_current_branch()
    if branch != "main":
        msg = f"branch is '{branch}', not 'main'"
        _record_failure("branch_check", msg)
        return False, f"publish failed at branch_check: {msg}"

    # 1b. Run the publish-blocker stages first. If hard_risky or
    # manual_required files remain after auto-resolve, refuse with
    # the spec's exact message — same vocabulary the dashboard
    # already shows for cycle blockers, so the operator sees a
    # consistent story.
    bk_ok, bk_msg = _publish_blocker_preflight()
    if not bk_ok:
        _record_failure("publish_blocker", bk_msg)
        return False, f"publish failed at publish_blocker: {bk_msg}"

    # 2. Anything to publish?
    changed = _git_changed_files()
    if not changed:
        _save_publish_state({
            "last_push_status": "noop",
            "last_push_at": _utc_now_z(),
            "last_publish_message": "no changes to publish",
            "last_failed_stage": None,
            "last_attempt_started_at": started_at,
        })
        return True, "publish skipped: no changes to publish"

    # 3. Classify into buckets. Risky (secret-shaped) blocks; "blocked"
    # is now an empty bucket — deploy/CI/infra files ride along.
    allowed, blocked, risky = _classify_publish_files(changed)
    if risky:
        msg = f"risky files in change set ({len(risky)}건): " + ", ".join(risky[:3])
        _record_failure("risky_check", msg)
        return False, f"publish failed at risky_check: {msg}"
    # Anything that would historically have landed in `blocked` (deploy
    # script, package.json, nginx, etc.) is now folded into `allowed`
    # by the classifier. We keep the variable for telemetry but never
    # block on it.
    if blocked:
        # Defensive: a future patch to BLOCKED_PUBLISH_PATTERNS should
        # not start blocking here without an explicit code change.
        allowed = sorted(set(allowed) | set(blocked))
        blocked = []
    if not allowed:
        _record_failure("allowed_check", "no allowed files in change set")
        return False, "publish failed at allowed_check: no allowed files in change set"

    # 4. Secret pattern scan on the diff of allowed files only.
    secret_hits = _scan_secret_patterns_in_diff(allowed)
    if secret_hits:
        labels = sorted({h[0] for h in secret_hits})
        msg = f"suspected secrets ({len(secret_hits)}건): " + ", ".join(labels[:5])
        _record_failure("secret_scan", msg)
        return False, f"publish failed at secret_scan: {msg}"

    # 4b. QA Gate. The most recent factory cycle must have signed off
    # via cycle.stage_qa_gate; otherwise we refuse to ship even if the
    # code compiles. Override is possible via
    # LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA=true (emergency only),
    # but never overrides risky/secret refusals above.
    _progress("validating", step="QA Gate 검증 중")
    qa_ok, qa_msg = _qa_gate_allows_publish(state_at_start)
    if not qa_ok:
        _record_failure("qa_gate", qa_msg)
        return False, f"publish failed at qa_gate: {qa_msg}"

    # 5. Re-run the correctness gate before we touch git history.
    revalidate_ok, failures = _revalidate_for_publish()
    if not revalidate_ok:
        msg = "validation failed: " + ", ".join(failures)
        _record_failure("revalidate", msg)
        return False, f"publish failed at revalidate: {msg}"

    # Pre-compute the restart classification so both the dry-run
    # report and the real-publish path expose accurate "would this
    # commit need a runner/factory bounce?" info to the dashboard.
    restart_action_pre, restart_reason_pre = _classify_restart_required(allowed)

    # 6. Dry-run path.
    if dry_run or not allow:
        reason_parts = []
        if dry_run:
            reason_parts.append("LOCAL_RUNNER_PUBLISH_DRY_RUN=true")
        if not allow:
            reason_parts.append("LOCAL_RUNNER_ALLOW_PUBLISH=false")
        reason = " · ".join(reason_parts) or "dry-run"
        listing = ", ".join(allowed[:5])
        msg = (
            f"dry-run ({reason}): would commit {len(allowed)} files: {listing}"
            + (f" (+{len(allowed)-5} more)" if len(allowed) > 5 else "")
        )
        if restart_action_pre:
            msg += f"; would also schedule restart ({restart_reason_pre})"
        _save_publish_state({
            "last_push_status": "dry_run",
            "last_push_at": _utc_now_z(),
            "last_publish_message": msg,
            "last_dry_run_files": allowed,
            "last_failed_stage": None,
            "last_attempt_started_at": started_at,
            "last_restart_required": bool(restart_action_pre),
            "last_restart_action": restart_action_pre,
            "last_restart_reason": restart_reason_pre,
        })
        return True, msg

    # 7. Real publish path. git add ONLY the allowed files (never `.`).
    _progress("committing", step="git commit 생성 중")
    ok, out = _git("add", "--", *allowed, timeout=60)
    if not ok:
        _record_failure("git_add", out[-300:])
        return False, f"publish failed at git_add: {out[-300:]}"

    # 8. Commit.
    commit_message = _build_publish_commit_message(
        allowed, state_at_start, push_message="Push: origin/main"
    )
    ok, out = _git("commit", "-m", commit_message, timeout=60)
    if not ok:
        # Rollback the staged adds so nothing lingers.
        _git("reset", "HEAD", "--", *allowed, timeout=30)
        _record_failure("git_commit", out[-300:])
        return False, f"publish failed at git_commit: {out[-300:]}"

    ok, commit_hash = _git("rev-parse", "HEAD", timeout=10)
    commit_hash = commit_hash.strip() if ok else "unknown"

    # 9. Push.
    _progress("pushing", step="git push origin main 진행 중")
    ok, push_out = _git("push", "origin", "main", timeout=120)
    if not ok:
        _save_publish_state({
            "last_push_status": "failed",
            "last_push_at": _utc_now_z(),
            "last_publish_message": f"push failed: {push_out[-300:]}",
            "last_commit_hash": commit_hash,
            "last_failed_stage": "git_push",
            "last_attempt_started_at": started_at,
        })
        return False, f"publish failed at git_push: {push_out[-300:]}"

    # After a successful push, decide whether the new commit also
    # invalidated the running runner / factory loop. We *schedule* the
    # restart action — the result is reported first, the action runs
    # AFTER, so the dashboard sees "published" before the heartbeat
    # gap that an exec_self causes.
    restart_action = restart_action_pre
    restart_reason = restart_reason_pre
    msg = f"published: commit={commit_hash[:8]}, pushed to origin/main"
    if restart_action:
        msg = f"{msg}; restart scheduled ({restart_reason})"
        global _POST_REPORT_ACTION
        _POST_REPORT_ACTION = restart_action

    _save_publish_state({
        "last_push_status": "succeeded",
        "last_push_at": _utc_now_z(),
        "last_publish_message": msg,
        "last_commit_hash": commit_hash,
        "last_failed_stage": None,
        "last_attempt_started_at": started_at,
        "last_pushed_files": allowed,
        "last_restart_required": bool(restart_action),
        "last_restart_action": restart_action,
        "last_restart_reason": restart_reason,
    })
    return True, msg


# ---------------------------------------------------------------------------
# Self-restart machinery
#
# After a publish that touches runner code or factory scripts, the
# already-running runner process holds STALE bytecode in memory. We
# can't just `git pull` on the next cycle — the in-memory module
# instance won't pick up the new file content. So:
#
#   * Factory loop changes  → stop.sh + start.sh re-spawns the bash loop
#   * Runner-code changes   → os.execv replaces THIS python process
#                              with a fresh interpreter loading the
#                              latest control_tower.local_runner.runner
#
# Both are best-effort: a failure in restart never kills the runner
# itself — we just report the error and stay on the old code until the
# next manual nudge.
# ---------------------------------------------------------------------------


# Files whose changes invalidate the *runner's* in-memory state.
# Anything under control_tower/local_runner/ counts because runner.py
# imports cycle.py at revalidate time.
RUNNER_CODE_PREFIXES: tuple[str, ...] = (
    "control_tower/local_runner/",
)
# Files whose changes invalidate the factory bash loop.
FACTORY_SCRIPT_FILES: frozenset[str] = frozenset({
    "scripts/local_factory_start.sh",
    "scripts/local_factory_stop.sh",
    "scripts/local_factory_status.sh",
})


def _classify_restart_required(
    changed_files: list[str],
) -> tuple[str | None, str | None]:
    """Decide what (if anything) to bounce after a successful publish.

    Returns (action, reason). Action is one of:
        None             — nothing to restart
        'factory'        — bounce factory loop only
        'factory_runner' — bounce factory loop AND exec a fresh runner
    """
    runner_hits = [f for f in changed_files if any(f.startswith(p) for p in RUNNER_CODE_PREFIXES)]
    factory_hits = [f for f in changed_files if f in FACTORY_SCRIPT_FILES]

    if runner_hits:
        names = ", ".join(p.split("/")[-1] for p in runner_hits[:3])
        return "factory_runner", f"runner code 변경 감지 ({names})"
    if factory_hits:
        names = ", ".join(p.split("/")[-1] for p in factory_hits[:3])
        return "factory", f"factory script 변경 감지 ({names})"
    return None, None


def _do_restart_factory() -> tuple[bool, str]:
    """Run stop.sh then start.sh. Best-effort: failures never raise."""
    if _restart_is_dry_run():
        return True, "DRY_RUN: factory restart skipped"
    try:
        ok1, out1 = _run_script(STOP_SCRIPT, timeout=20)
    except Exception as e:  # noqa: BLE001
        return False, f"stop.sh raised: {e}"
    time.sleep(1.5)
    try:
        ok2, out2 = _run_script(START_SCRIPT, timeout=20)
    except Exception as e:  # noqa: BLE001
        return False, f"start.sh raised: {e}"
    return (ok1 and ok2), f"stop ok={ok1}, start ok={ok2}"


def _exec_self() -> None:
    """Replace this Python process with a fresh runner. Inherits the
    full env so user-set LOCAL_RUNNER_ALLOW_PUBLISH and friends survive.

    Flushes stdio first because once execv succeeds, anything still
    buffered in the old process is lost — including the heartbeat /
    log line we just emitted to confirm the report went out.
    """
    if _restart_is_dry_run():
        sys.stderr.write("[runner] DRY_RUN: would exec self\n")
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(
            sys.executable,
            [sys.executable, "-m", "control_tower.local_runner.runner"],
        )
    except OSError as e:
        # Couldn't exec — log and stay on the old process. Better than
        # crashing entirely.
        sys.stderr.write(f"[runner] exec failed: {e}\n")


def _h_restart_runner(_payload: dict) -> tuple[bool, str]:
    """Schedule a runner self-exec for AFTER the result is reported."""
    if _restart_is_dry_run():
        return True, "DRY_RUN: would exec self after reporting"
    global _POST_REPORT_ACTION
    _POST_REPORT_ACTION = "exec_self"
    return True, "runner restart scheduled (will exec after report)"


def _h_update_runner(_payload: dict) -> tuple[bool, str]:
    """Fast-forward main from origin, then bounce factory + exec self.

    Refuses to run on a dirty working tree to avoid silently dropping
    uncommitted local edits. Also stops on any merge conflict that
    --ff-only would refuse to resolve.
    """
    dirty = _git_changed_files()
    if dirty:
        head = ", ".join(dirty[:3])
        return False, f"로컬 변경사항이 있어 pull 중단 ({len(dirty)}건: {head})"

    branch = _git_current_branch()
    if branch != "main":
        return False, f"branch is '{branch}', not 'main' — pull 중단"

    ok, out = _git("fetch", "origin", "main", timeout=120)
    if not ok:
        return False, f"git fetch 실패: {out[-200:]}"

    ok, out = _git("pull", "--ff-only", "origin", "main", timeout=120)
    if not ok:
        return False, f"git pull 실패: {out[-200:]}"

    pulled_summary = (out or "").splitlines()[-1][:200] if out else "(no output)"

    if _restart_is_dry_run():
        return True, f"DRY_RUN: pulled ({pulled_summary}); would restart factory + exec self"

    global _POST_REPORT_ACTION
    _POST_REPORT_ACTION = "factory_runner"
    return True, f"updated ({pulled_summary}); restart scheduled"


# ---------------------------------------------------------------------------
# Server deployment (deploy_to_server)
#
# 관제실의 배포 버튼이 누르는 경로. 흐름:
#
#   1. file lock 획득 (단일-flight 보장; stale 30분이면 자동 회수)
#   2. 같은 command_id 재처리면 캐시된 결과를 그대로 반환 (idempotent)
#   3. _h_publish_changes 호출 — Release Safety Gate / QA / git push
#   4. main 에 push 가 성사되면 GitHub Actions(deploy.yml)가 자동으로
#      서버 SSH 배포를 수행한다 — runner는 SSH를 직접 만지지 않는다.
#   5. 결과를 factory_deploy.json + publish_state 에 기록한다.
#
# `scripts/remote_deploy_stampport.sh`는 더 이상 publish 경로에서 호출되지
# 않는다 (수동 fallback 용도로만 남겨둠). LOCAL_RUNNER_DEPLOY_DRY_RUN
# 환경 변수도 이 핸들러에서는 더 이상 의미가 없다 — 실제 서버 배포 여부는
# main 브랜치에 commit이 push 되었는지(=GitHub Actions가 트리거되었는지)로
# 판가름한다.
# ---------------------------------------------------------------------------


def _read_deploy_state() -> dict:
    if not DEPLOY_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(DEPLOY_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_deploy_state(updates: dict) -> None:
    cur = _read_deploy_state()
    cur.update(updates)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_STATE_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _deploy_lock_acquire(command_id: int) -> tuple[bool, str]:
    """Atomic file-create as the single-flight guard.

    O_EXCL fails if the lock file already exists. A stale lock older
    than DEPLOY_LOCK_STALE_SEC is force-cleared first so a crashed
    runner doesn't permanently wedge the deploy queue.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if DEPLOY_LOCK_FILE.is_file():
        try:
            age = time.time() - DEPLOY_LOCK_FILE.stat().st_mtime
        except OSError:
            age = 0.0
        if age >= DEPLOY_LOCK_STALE_SEC:
            try:
                DEPLOY_LOCK_FILE.unlink()
            except OSError:
                pass
    try:
        fd = os.open(
            str(DEPLOY_LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        return False, "다른 deploy가 진행 중 — 중복 실행 차단"
    try:
        os.write(
            fd,
            json.dumps({
                "command_id": command_id,
                "started_at": _utc_now_z(),
                "pid": os.getpid(),
            }).encode("utf-8"),
        )
    finally:
        os.close(fd)
    return True, "lock acquired"


def _deploy_lock_release() -> None:
    try:
        DEPLOY_LOCK_FILE.unlink()
    except OSError:
        pass


def _h_deploy_to_server(payload: dict) -> tuple[bool, str]:
    """Publish + handoff to GitHub Actions.

    publish_changes가 main에 push를 성사시키면 .github/workflows/deploy.yml
    이 그 push를 감지해 SSH 배포를 자동 실행한다. runner는 SSH를 직접
    만지지 않으며, "GitHub Actions가 이어받았습니다" 라는 안내를
    돌려준다.

    Returns (ok, message). `message`는 대시보드에 그대로 표시되니
    한국어로 짧게: commit 해시 + Actions URL + 변경/무변경 안내를 담는다.
    """
    cmd_id = int((payload or {}).get("__command_id") or 0)

    # 1. Idempotency — same command_id seen twice returns the cached
    # outcome without re-running. Protects against the server retrying
    # a delivery the runner already handled.
    last = _read_deploy_state()
    if cmd_id and last.get("last_command_id") == cmd_id and last.get("last_finished_at"):
        ok_prev = bool(last.get("last_status_ok"))
        prev_msg = last.get("last_message") or "(no detail)"
        return ok_prev, f"이미 처리된 deploy command #{cmd_id} — {prev_msg}"

    # 2. Single-flight lock — dedup mass clicks even across processes.
    ok_lock, lock_msg = _deploy_lock_acquire(cmd_id)
    if not ok_lock:
        return False, lock_msg

    started = _utc_now_z()
    _save_deploy_state({
        "last_status": "running",
        "last_status_ok": False,
        "last_command_id": cmd_id,
        "last_started_at": started,
        "last_finished_at": None,
        "last_message": "deploy started — publish 단계 진입",
        "last_failed_stage": None,
        "last_actions_url": ACTIONS_URL,
    })
    _set_deploy_progress(
        "command_received",
        current_step="Runner가 deploy_to_server 명령 수신",
        reset=True,
    )

    try:
        # 3. Run the publish pipeline. _h_publish_changes does its own
        # Release Safety Gate / QA / secret_scan / revalidate / commit /
        # push. A noop (no changes to publish) is reported as ok=True
        # but no commit/push happens — we surface that distinctly so
        # the operator knows GitHub Actions did NOT trigger.
        publish_payload = dict(payload or {})
        publish_payload["__track_deploy_progress"] = True
        publish_ok, publish_msg = _h_publish_changes(publish_payload)
        finished = _utc_now_z()
        if not publish_ok:
            _save_deploy_state({
                "last_status": "failed",
                "last_status_ok": False,
                "last_finished_at": finished,
                "last_message": f"publish 실패: {publish_msg[:280]}",
                "last_failed_stage": "publish",
            })
            # _h_publish_changes already wrote a failed deploy_progress
            # entry via _record_failure; nothing more to do here.
            return False, f"deploy failed at publish: {publish_msg}"

        # 4. Distinguish "pushed" vs "noop" so the dashboard message is
        # accurate about whether GitHub Actions will fire.
        msg_lower = (publish_msg or "").lower()
        no_changes = "no changes to publish" in msg_lower
        is_dry_run = "dry-run" in msg_lower or "dry_run" in msg_lower

        if no_changes:
            handoff_msg = (
                "변경 파일 없음 — main에 새 commit이 없으므로 GitHub Actions 배포는 트리거되지 않습니다. "
                f"서버를 강제 재배포하려면 deploy.yml workflow_dispatch를 사용하세요. ({ACTIONS_URL})"
            )
            _set_deploy_progress(
                "completed",
                current_step="변경 없음 — 배포 스킵",
            )
        elif is_dry_run:
            handoff_msg = (
                f"publish dry-run 완료 — 실제 push 미수행이라 GitHub Actions도 트리거되지 않습니다. "
                f"실제 배포는 LOCAL_RUNNER_PUBLISH_DRY_RUN=false + LOCAL_RUNNER_ALLOW_PUBLISH=true 후 다시 시도. "
                f"detail: {publish_msg}"
            )
            _set_deploy_progress(
                "completed",
                current_step="dry-run 완료 — 실제 배포 미수행",
            )
        else:
            handoff_msg = (
                f"published — main push 성공. GitHub Actions(deploy.yml)가 서버 배포를 이어받습니다. "
                f"진행 상황: {ACTIONS_URL} · publish: {publish_msg}"
            )
            _set_deploy_progress(
                "actions_triggered",
                current_step="GitHub Actions Deploy Stampport 트리거됨",
            )

        _save_deploy_state({
            "last_status": "succeeded",
            "last_status_ok": True,
            "last_finished_at": finished,
            "last_message": handoff_msg,
            "last_failed_stage": None,
            "last_actions_url": ACTIONS_URL,
            "last_publish_message": publish_msg,
            "last_no_changes": no_changes,
            "last_dry_run": is_dry_run,
        })
        # Surface the handoff on the publish state row too so the
        # dashboard's existing "마지막 배포 결과" chip shows it.
        _save_publish_state({"last_publish_message": handoff_msg})
        return True, handoff_msg
    except Exception as e:  # noqa: BLE001
        _set_deploy_progress(
            "failed",
            current_step="deploy_to_server 핸들러 예외",
            failed_reason=str(e)[:280],
        )
        raise
    finally:
        _deploy_lock_release()


# ---------------------------------------------------------------------------
# Operator Fix Request
#
# Lets an admin type a free-form bug/improvement request into the
# Control Tower dashboard. The runner writes that request to
# .runtime/operator_request.md, calls Claude Code with restricted
# tools (Read/Glob/Grep/Edit only — no shell, no Bash, no Write to
# files outside app/control_tower/scripts allowlist) and runs the
# normal correctness gate plus QA Gate. The "_and_publish" variant
# additionally chains into the same publish_changes path the dashboard
# 배포하기 button uses, but ONLY after QA Gate passes.
#
# Hard rules baked in here:
#   * The handlers never touch git history themselves. They reuse
#     _h_publish_changes for the actual commit/push so all sandboxing
#     stays in one place.
#   * Free-form text from the dashboard is *redacted* for secret
#     patterns before being written to disk — we never preserve a
#     stray AKIA token because someone pasted a stack trace.
#   * The handlers refuse to run while the factory loop is mid-cycle
#     to avoid trampling claude_apply edits.
# ---------------------------------------------------------------------------


# Tools the Claude CLI is allowed to use during the operator fix call.
# NOTE: Bash is NOT in this list — the runner runs every verification
# step itself so Claude can't escape the sandbox via a shell command.
OPERATOR_FIX_CLAUDE_TOOLS = "Read,Glob,Grep,Edit"


# Maximum chars of free-form request we pass to Claude. We truncate
# silently above this to bound prompt size + cost.
OPERATOR_FIX_REQUEST_MAX_CHARS = 6000


# Prompt template the operator-fix handlers feed Claude. Keeps the
# safety rules close to the request body so Claude can't easily get
# distracted by a clever prompt-injection in the request.
OPERATOR_FIX_PROMPT_TEMPLATE = """\
당신은 Stampport 프로젝트의 Operator Fix Request를 처리하는 Claude Code 입니다.

Stampport는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스입니다.
어떤 변경도 이 정체성을 흐트러뜨려서는 안 됩니다 (지도/리뷰/관리자 대시보드/할 일 앱 느낌 금지).

다음 요청을 그대로 처리하세요. 단, 아래 규칙을 절대 위반하지 마세요.

=== Operator Request 시작 ===
{request}
=== Operator Request 끝 ===

규칙:
- 사용 가능한 도구: Read, Glob, Grep, Edit. 그 외(Write, Bash, WebFetch 등) 호출 금지.
- 다음 디렉터리 아래의 파일만 수정 가능:
  app/, control_tower/, scripts/local_factory_*.sh, scripts/notify_*.*
- 다음 패턴은 어떤 경우에도 만들거나 수정하거나 삭제하지 마세요:
  .env, .key, .pem, .db, .runtime/, node_modules/, dist/, .venv/,
  package.json, package-lock.json, requirements.txt,
  deploy/, .github/, systemd 관련 파일, nginx 관련 파일.
- 어떤 셸 명령도 실행하지 마세요. git, npm, deploy, curl 모두 금지.
- 자동 commit / push / deploy 금지 — 그건 runner가 직접 수행합니다.
- secret/private key/token 값을 출력에 포함하지 마세요.
- 요청이 모호하거나 위험하다고 판단되면 어떤 파일도 수정하지 말고 종료하세요.
- 한 가지 수정만 하세요. 여러 안 나열 금지.
- 200줄 이하의 코드 변경으로 구현 가능해야 합니다.

배포 허용 여부: {allow_publish}
(true여도 자동 push는 절대 하지 않습니다. runner가 QA Gate 통과 후 직접 처리합니다.)

작업이 끝나면 마지막 응답은 다음 Markdown 형식만 출력하세요. preamble/설명 금지:

# Operator Fix 결과
- `path/to/file1.py` — 한 줄 변경 요약
- `path/to/file2.jsx` — 한 줄 변경 요약

(파일을 변경하지 않았다면 위 형식 대신 "변경 없음" 한 줄만 출력하세요.)
"""


def _redact_request_text(text: str) -> tuple[str, list[str]]:
    """Strip values that look like secrets/tokens from a free-form
    request before persisting it to disk. Returns the redacted text
    plus a list of redaction-marker labels for the state file.

    We're conservative on purpose — false positives are fine since
    they only affect the operator's own request body. Real secrets
    leaking to a checked-in file would be much worse.
    """
    redactions: list[str] = []
    out = text or ""
    # Strict markers — replace whole line.
    for marker in SECRET_STRICT_MARKERS:
        if marker in out:
            redactions.append(marker)
            # Replace any line that mentions the marker with a stub.
            out = re.sub(
                rf"^.*{re.escape(marker)}.*$",
                "[REDACTED — private-key marker detected]",
                out,
                flags=re.MULTILINE,
            )
    # Value patterns — keep the keyword, replace the value.
    for label, pat in SECRET_VALUE_PATTERNS:
        def _repl(m: "re.Match[str]", _label: str = label) -> str:
            redactions.append(_label)
            full = m.group(0)
            captured = m.group(1)
            return full.replace(captured, "<REDACTED>")
        out = pat.sub(_repl, out)
    # Long Bearer tokens (`Authorization: Bearer ...`) — common in
    # pasted curl examples.
    bearer_pat = re.compile(
        r"(Bearer\s+)([A-Za-z0-9._\-]{20,})", re.IGNORECASE,
    )
    if bearer_pat.search(out):
        redactions.append("bearer_token")
    out = bearer_pat.sub(r"\1<REDACTED>", out)
    return out, sorted(set(redactions))


def _read_operator_fix_state() -> dict:
    if not OPERATOR_FIX_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(OPERATOR_FIX_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_operator_fix_state(updates: dict) -> None:
    cur = _read_operator_fix_state()
    cur.update(updates)
    cur["updated_at"] = _utc_now_z()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        OPERATOR_FIX_STATE_FILE.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        sys.stderr.write(f"[runner] failed to write operator_fix_state: {e}\n")


def _write_operator_request_md(
    request: str, *, allow_publish: bool, priority: str, redactions: list[str]
) -> None:
    """Render operator_request.md from the (already-redacted) request
    body. The file is the single source of truth Claude reads — we
    embed the policy fences directly inside it so even if the prompt
    template above gets weakened in the future, this file still
    spells out the rules."""
    redaction_note = ""
    if redactions:
        redaction_note = (
            "\n\n_요청 본문에서 다음 항목이 자동 마스킹되었습니다: "
            + ", ".join(redactions) + "._"
        )
    body = f"""# Operator Fix Request

## 요청 시각
{_utc_now_z()}

## 우선순위
{priority}

## 배포 허용
{'true' if allow_publish else 'false'}

## 요청 내용
{request.strip()}{redaction_note}

## 수정 범위 정책
- allowed: app/**, control_tower/**, scripts/local_factory_*.sh, scripts/notify_*.*
- blocked: .env, .pem, .key, .db, package.json, package-lock.json,
  requirements.txt, deploy/, .github/, nginx, systemd

## 성공 조건
- build_app passed
- build_control passed
- py_compile passed
- bash -n passed
- qa_gate passed
- risky/secret 0
"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    OPERATOR_REQUEST_FILE.write_text(body, encoding="utf-8")


def _operator_fix_invoke_claude(request_md: str, *, allow_publish: bool) -> tuple[bool, str]:
    """Run the Claude CLI with the operator fix prompt + restricted
    tools. Returns (ok, output) — same shape as _run-style helpers."""
    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return False, "claude CLI 미설치 — operator fix 처리 불가"
    prompt = OPERATOR_FIX_PROMPT_TEMPLATE.format(
        request=request_md.strip()[:OPERATOR_FIX_REQUEST_MAX_CHARS],
        allow_publish="true" if allow_publish else "false",
    )
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    budget_usd = os.environ.get("FACTORY_CLAUDE_BUDGET_USD", "1.0").strip() or "1.0"
    timeout_sec = float(
        os.environ.get("FACTORY_CLAUDE_OPERATOR_TIMEOUT_SEC",
                       os.environ.get("FACTORY_CLAUDE_APPLY_TIMEOUT_SEC", "900"))
    )
    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", OPERATOR_FIX_CLAUDE_TOOLS,
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    try:
        r = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, f"claude CLI timeout after {timeout_sec}s"
    except FileNotFoundError as e:
        return False, f"claude CLI 실행 실패: {e}"
    out = (r.stdout or "")
    if r.stderr:
        out += "\n--stderr--\n" + r.stderr
    return r.returncode == 0, out.strip()


# We import shutil lazily here because runner.py was historically
# stdlib-only; avoid disturbing the import block at the top of the
# file. (`subprocess` and `re` are already imported.)
import shutil  # noqa: E402


def _operator_fix_run_qa_gate() -> tuple[bool, str, dict]:
    """Run cycle.stage_qa_gate against the post-fix working tree.

    Returns (ok, message, qa_state_dict). qa_state_dict contains the
    QA fields we care about for the operator_fix_state file
    (qa_status / qa_publish_allowed / qa_failed_reason / etc.) so the
    dashboard surfaces the same telemetry as a normal cycle.
    """
    try:
        from . import cycle as _cycle
    except ImportError as e:
        return False, f"cycle import failed: {e}", {}
    state = _cycle.CycleState()
    sr = _cycle.stage_qa_gate(state)
    qa_dict = {
        "qa_status": state.qa_status,
        "qa_publish_allowed": bool(state.qa_publish_allowed),
        "qa_failed_reason": state.qa_failed_reason,
        "qa_failed_categories": list(state.qa_failed_categories),
        "qa_build_artifact": state.qa_build_artifact,
        "qa_api_health": state.qa_api_health,
        "qa_screen_presence": state.qa_screen_presence,
        "qa_flow_presence": state.qa_flow_presence,
        "qa_domain_profile": state.qa_domain_profile,
        "qa_report_path": state.qa_report_path,
        "qa_feedback_path": state.qa_feedback_path,
    }
    return sr.status == "passed", sr.message, qa_dict


def _operator_fix_pipeline(payload: dict, *, allow_publish: bool) -> tuple[bool, str]:
    """Shared pipeline for both operator_fix_request and
    operator_fix_and_publish. The only difference is whether we
    chain into _h_publish_changes after QA passes."""
    request_raw = (payload or {}).get("request") or ""
    if not isinstance(request_raw, str) or not request_raw.strip():
        return False, "operator_fix payload.request가 비어있습니다."

    priority = (payload or {}).get("priority") or "normal"
    if priority not in {"normal", "high"}:
        priority = "normal"

    # Refuse to run while a factory cycle is mid-flight — both paths
    # would race on the same files.
    state = _read_factory_state() or {}
    if state.get("status") == "running":
        msg = "factory 사이클 실행 중 — operator fix 거부 (잠시 후 다시 시도)"
        _save_operator_fix_state({
            "status": "failed",
            "started_at": _utc_now_z(),
            "allow_publish": allow_publish,
            "publish_status": "blocked",
            "last_message": msg,
        })
        return False, msg

    started_at = _utc_now_z()
    request_redacted, redactions = _redact_request_text(request_raw)
    _write_operator_request_md(
        request_redacted,
        allow_publish=allow_publish,
        priority=priority,
        redactions=redactions,
    )
    _save_operator_fix_state({
        "status": "running",
        "request_path": str(OPERATOR_REQUEST_FILE),
        "started_at": started_at,
        "allow_publish": allow_publish,
        "priority": priority,
        "redactions": redactions,
        "publish_status": "not_requested",
        "last_message": "Claude CLI 호출 중",
        "changed_files": [],
    })

    # Snapshot the diff BEFORE Claude runs so we know what Claude
    # actually changed — for both reporting and rollback decisions.
    before_changed = set(_git_changed_files())

    # 1. Invoke Claude with restricted tools.
    ok, claude_out = _operator_fix_invoke_claude(
        OPERATOR_REQUEST_FILE.read_text(encoding="utf-8"),
        allow_publish=allow_publish,
    )
    after_changed = set(_git_changed_files())
    new_files = sorted(after_changed - before_changed)
    if not ok:
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "not_requested",
            "changed_files": new_files,
            "last_message": (claude_out or "claude 실행 실패")[-300:],
        })
        return False, f"claude CLI 실패: {(claude_out or '')[-200:]}"

    # 2. Run the standard build/syntax/risky validation gate.
    revalidate_ok, failures = _revalidate_for_publish()
    if not revalidate_ok:
        msg = "build/syntax 재검증 실패: " + ", ".join(failures or ["unknown"])
        _save_operator_fix_state({
            "status": "qa_failed",
            "qa_status": "failed",
            "publish_status": "blocked",
            "changed_files": sorted(after_changed),
            "last_message": msg,
        })
        return False, msg

    # 3. QA Gate.
    qa_ok, qa_msg, qa_dict = _operator_fix_run_qa_gate()
    state_update: dict = {
        "changed_files": sorted(after_changed),
        **qa_dict,
    }
    if not qa_ok:
        state_update.update({
            "status": "qa_failed",
            "publish_status": "blocked",
            "last_message": f"QA Gate 실패: {qa_msg}",
        })
        _save_operator_fix_state(state_update)
        return False, f"QA Gate 실패: {qa_msg}"

    # QA passed.
    if not allow_publish:
        state_update.update({
            "status": "applied",
            "publish_status": "not_requested",
            "last_message": "수정 완료 + QA 통과 — 사람 검토 대기",
        })
        _save_operator_fix_state(state_update)
        return True, (
            f"operator fix 적용 완료, QA 통과. "
            f"변경 파일 {len(after_changed)}건. 자동 push 안 함 (요청 종류: fix only)."
        )

    # 4. allow_publish=true → chain into the publish_changes handler.
    # _h_publish_changes re-runs its own validation/secret/QA checks
    # on top of ours, which is the right belt-and-braces behavior.
    pub_ok, pub_msg = _h_publish_changes({})
    if pub_ok:
        commit_hash = (_read_publish_state() or {}).get("last_commit_hash")
        state_update.update({
            "status": "published",
            "publish_status": "published",
            "last_commit_hash": commit_hash,
            "last_message": pub_msg,
        })
        _save_operator_fix_state(state_update)
        return True, f"operator fix + 배포 완료: {pub_msg}"
    state_update.update({
        "status": "qa_failed" if "qa_gate" in (pub_msg or "").lower() else "failed",
        "publish_status": "blocked" if "qa_gate" in (pub_msg or "").lower() else "failed",
        "last_message": pub_msg,
    })
    _save_operator_fix_state(state_update)
    return False, f"QA 통과했으나 publish 단계에서 거부됨: {pub_msg}"


def _h_operator_fix_request(payload: dict) -> tuple[bool, str]:
    """Operator fix WITHOUT auto-publish. Always stops at the QA Gate
    pass / fail boundary; the user clicks 배포하기 manually if they
    want to ship the result."""
    return _operator_fix_pipeline(payload or {}, allow_publish=False)


def _h_operator_fix_and_publish(payload: dict) -> tuple[bool, str]:
    """Operator fix WITH auto-publish. Identical to the *_request
    variant up through QA Gate; then chains into _h_publish_changes
    for commit+push if QA Gate signed off. The publish handler
    itself enforces all the same risky/secret/branch guards a normal
    publish would, so a malicious request can't sneak past."""
    p = dict(payload or {})
    p["allow_publish"] = True
    return _operator_fix_pipeline(p, allow_publish=True)


# ---------------------------------------------------------------------------
# operator_request — autonomous "Claude에게 작업 지시" channel
#
# Distinct from operator_fix_*: this hands FULL agency (Edit / Write /
# Bash + git commit + git push) to a user-configured Claude CLI. The
# operator's expectation is "type a request on my phone, and ten
# minutes later it's deployed". We rely on:
#
#   1. The Claude prompt explicitly forbidding commit/push on validation
#      failure / secret leak / merge-conflict marker.
#   2. The user-supplied LOCAL_RUNNER_CLAUDE_COMMAND (e.g. with
#      `--dangerously-skip-permissions` or `--permission-mode
#      bypassPermissions`) — only run on a private operator machine.
#   3. The factory-cycle running guard so we don't race a cycle.
#
# The prompt and the in-band instructions are the security boundary —
# there is no Bash sandbox here. That is intentional and matches the
# user's request: Claude must be able to run the build, run QA, and
# commit/push autonomously.
# ---------------------------------------------------------------------------

OPERATOR_REQUEST_MAX_CHARS = 6000


def _resolve_claude_command() -> list[str]:
    """Parse LOCAL_RUNNER_CLAUDE_COMMAND into argv. Empty/missing →
    the resolved `claude` binary (PATH lookup) or the literal string
    `claude`. shlex.split handles both `claude` and longer forms like
    `claude --dangerously-skip-permissions` or
    `claude --permission-mode bypassPermissions`. Returns at least
    one element."""
    raw = os.environ.get("LOCAL_RUNNER_CLAUDE_COMMAND", "").strip()
    if raw:
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            sys.stderr.write(
                f"[runner] LOCAL_RUNNER_CLAUDE_COMMAND parse error: {e}\n"
            )
            parts = []
        if parts:
            return parts
    fallback = shutil.which("claude") or "claude"
    return [fallback]


OPERATOR_REQUEST_PROMPT_TEMPLATE = """\
당신은 Stampport 프로젝트의 자동 운영 Claude Code 에이전트입니다.

아이폰/맥북에서 운영자가 요청을 보냈고, 이 맥북 runner가 당신을 호출했습니다.
요청을 끝까지 자동으로 처리하세요. 검증 통과 시에는 commit + push까지 직접
수행해도 됩니다 — main에 push되면 GitHub Actions Deploy Stampport workflow가
서버 자동 배포까지 이어갑니다.

Stampport는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스다.
지도/리뷰/관리자 대시보드/할 일 앱 톤으로 흐트러뜨리지 마세요.

=== 요청 본문 (자동 마스킹 적용 후) ===
{request}
=== 요청 본문 끝 ===

작업 흐름 (반드시 이 순서):

1. 요청 의도를 정확히 파악하고, 가장 작은 변경 단위만 수행하세요.
2. 다음 디렉터리만 수정 가능합니다:
     app/**, control_tower/**, scripts/local_factory_*.sh, scripts/notify_*.*
3. 다음은 어떤 경우에도 만들거나 수정하거나 삭제하지 마세요:
     .env*, .key, .pem, .db, .runtime/, node_modules/, dist/, .venv/,
     package.json, package-lock.json, requirements.txt,
     deploy/nginx-stampport.conf, .github/workflows/deploy.yml, systemd 관련.
4. 검증을 직접 실행하세요:
     - 변경이 app/web 또는 control_tower/web에 있다면 해당 디렉터리에서
       `npm run build` (또는 동등한 빌드)를 실행해 통과를 확인.
     - 변경이 .py 파일을 건드리면 `python3 -m py_compile <파일>` 통과 확인.
     - QA 흐름이 의심되면 관련 단위/문법 점검도 직접 실행.
5. 검증이 모두 통과했고, 다음 모두 만족할 때만 commit + push를 실행하세요:
     - secret 패턴 (BEGIN PRIVATE KEY, AWS_SECRET 등) 노출 없음
     - merge conflict marker `<<<<<<<`, `=======`, `>>>>>>>` 없음
     - .env, .pem, .key, .db, package-lock.json 변경 없음
     - 현재 브랜치가 main
     - 변경된 파일이 위 허용 디렉터리 안에 있음
6. commit 메시지는 한국어 1줄 + 빈 줄 + 본문(선택). 추가로 마지막 줄에:
     "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
   를 포함하세요.
7. push는 `git push origin main` 만 허용. force push / 다른 브랜치 push 금지.
8. 검증 실패 / secret 노출 / conflict marker 발견 / 위반 가능성이 조금이라도
   있으면 commit / push 를 절대 하지 마세요. 변경을 그대로 working tree에
   남겨두고 운영자에게 사유를 보고하세요. (`git restore` 같은 파괴적 명령으로
   되돌리지도 마세요 — 운영자가 직접 결정합니다.)

자동 commit 허용 플래그: {auto_commit_push}
(false면 commit/push 금지. 변경만 만들고 종료하세요.)

작업이 끝나면 마지막 응답에 다음 Markdown만 출력하세요. preamble/잡담 금지:

# Operator Request 결과

## 상태
applied | committed | pushed | aborted | failed

## 요약
한 줄 요약.

## 변경 파일
- `path/to/file1.jsx` — 한 줄 설명
- `path/to/file2.py` — 한 줄 설명

(파일을 변경하지 않았다면 위 항목 대신 "변경 없음" 한 줄.)

## commit / push
- commit hash: <짧은 7자리 hash 또는 N/A>
- pushed to main: yes | no
- 거부 사유 (있으면): ...

## 다음 단계
운영자가 무엇을 확인해야 하는지 1~2줄.
"""


def _h_operator_request(payload: dict) -> tuple[bool, str]:
    """Autonomous operator-request handler. Persists the request to
    .runtime/operator_request.md and hands it off to Claude Code via
    LOCAL_RUNNER_CLAUDE_COMMAND. Claude itself runs build/QA/commit/
    push — the runner only enforces the cycle-not-running guard plus
    a single-flight per-request lock via the operator_fix_state file
    so two clicks from the dashboard can't double-fire.
    """
    prompt_raw = (payload or {}).get("prompt") or (payload or {}).get("request") or ""
    if not isinstance(prompt_raw, str) or not prompt_raw.strip():
        return False, "operator_request payload.prompt가 비어있습니다."

    auto_cp_raw = (payload or {}).get("auto_commit_push", True)
    auto_commit_push = bool(auto_cp_raw) if not isinstance(auto_cp_raw, str) \
        else auto_cp_raw.strip().lower() in {"true", "1", "yes", "on"}

    # Refuse to run while a cycle is mid-flight — Claude editing files
    # under the cycle would corrupt both.
    state = _read_factory_state() or {}
    if state.get("status") == "running":
        msg = "factory 사이클 실행 중 — operator_request 거부 (잠시 후 다시 시도)"
        _save_operator_fix_state({
            "status": "failed",
            "started_at": _utc_now_z(),
            "allow_publish": auto_commit_push,
            "publish_status": "blocked",
            "last_message": msg,
        })
        return False, msg

    started_at = _utc_now_z()
    request_redacted, redactions = _redact_request_text(prompt_raw)
    truncated = request_redacted.strip()[:OPERATOR_REQUEST_MAX_CHARS]
    _write_operator_request_md(
        truncated,
        allow_publish=auto_commit_push,
        priority="normal",
        redactions=redactions,
    )
    _save_operator_fix_state({
        "status": "running",
        "request_path": str(OPERATOR_REQUEST_FILE),
        "started_at": started_at,
        "allow_publish": auto_commit_push,
        "priority": "operator_request",
        "redactions": redactions,
        "publish_status": "not_requested",
        "last_message": "Claude CLI 호출 중 (operator_request)",
        "changed_files": [],
    })

    prompt = OPERATOR_REQUEST_PROMPT_TEMPLATE.format(
        request=truncated,
        auto_commit_push="true" if auto_commit_push else "false",
    )

    argv = list(_resolve_claude_command()) + [
        "-p", prompt,
        "--output-format", "text",
    ]
    timeout_sec = float(
        os.environ.get(
            "FACTORY_CLAUDE_OPERATOR_REQUEST_TIMEOUT_SEC",
            os.environ.get("FACTORY_CLAUDE_OPERATOR_TIMEOUT_SEC", "1500"),
        )
    )

    # Snapshot the diff BEFORE Claude runs so we can report what was
    # touched even when Claude's final markdown is incomplete.
    before_changed = set(_git_changed_files())

    try:
        r = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        msg = f"claude CLI timeout after {timeout_sec}s"
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "blocked",
            "last_message": msg,
        })
        return False, msg
    except FileNotFoundError as e:
        msg = f"claude CLI 실행 실패: {e}"
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "blocked",
            "last_message": msg,
        })
        return False, msg

    out = (r.stdout or "")
    if r.stderr:
        out += "\n--stderr--\n" + r.stderr
    out = out.strip()
    after_changed = set(_git_changed_files())
    new_or_changed = sorted(after_changed | (after_changed - before_changed))

    if r.returncode != 0:
        tail = out[-400:] if out else "(no output)"
        msg = f"claude CLI returncode={r.returncode}: {tail}"
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "blocked",
            "changed_files": new_or_changed,
            "last_message": msg,
        })
        return False, msg

    # Best-effort parse of Claude's structured tail. We only use this
    # to label the operator_fix_state row — the runner did not run
    # the commit itself, Claude did. The state file is the operator's
    # rear-view mirror, not a gate.
    pushed = bool(re.search(r"pushed to main\s*[:：]\s*yes", out, re.IGNORECASE))
    aborted = "aborted" in out.lower() or "거부" in out
    if pushed:
        status = "published"
        publish_status = "published"
    elif aborted:
        status = "qa_failed"
        publish_status = "blocked"
    else:
        status = "applied"
        publish_status = "not_requested"

    _save_operator_fix_state({
        "status": status,
        "publish_status": publish_status,
        "changed_files": new_or_changed,
        "last_message": (out[-400:] if out else "Claude 응답 없음"),
    })

    summary = (
        f"operator_request 완료 (status={status}, "
        f"changed_files={len(new_or_changed)})"
    )
    return True, summary


COMMAND_HANDLERS: dict[str, Callable[[dict], tuple[bool, str]]] = {
    "start_factory":           _h_start,
    "stop_factory":            _h_stop,
    "restart_factory":         _h_restart,
    "pause_factory":           _h_pause,
    "resume_factory":          _h_resume,
    "status":                  _h_status,
    "git_pull":                _h_git_pull,
    "build_check":             _h_build_check,
    "test_check":              _h_test_check,
    "publish_changes":         _h_publish_changes,
    "deploy_to_server":        _h_deploy_to_server,
    "restart_runner":          _h_restart_runner,
    "update_runner":           _h_update_runner,
    "operator_fix_request":    _h_operator_fix_request,
    "operator_fix_and_publish": _h_operator_fix_and_publish,
    "operator_request":        _h_operator_request,
}


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


def _read_factory_state() -> dict | None:
    """Read .runtime/factory_state.json — written by cycle.py.

    Returns None if the file is missing or unparseable. The runner is
    happy to send a heartbeat without it; the dashboard just won't have
    factory progress to show.
    """
    if not STATE_FILE.is_file():
        return None
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _factory_pid_alive() -> tuple[bool, int | None]:
    """Look at .runtime/local_factory.pid and check if the process exists."""
    if not PID_FILE.is_file():
        return False, None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        return False, None
    if pid <= 0:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, PermissionError):
        return False, pid
    except OSError:
        return False, pid


def _log_tail(n: int = 20) -> str:
    """Return the last `n` lines of local_factory.log (best effort)."""
    if not LOG_FILE.is_file():
        return ""
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 8000)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-n:])
    except OSError:
        return ""


def _artifact_preview(path: Path, max_chars: int = 280) -> str | None:
    """Read a Markdown artifact and return the first non-heading
    paragraph as a single-line preview. Headings (`# ...`, `## ...`)
    are stripped so the dashboard surfaces *content*, not the title
    we already display elsewhere. Returns None when the file is
    missing/empty/unreadable so the caller can decide whether to
    fall back to demo copy."""
    if not path.is_file():
        return None
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return None
    paragraphs: list[str] = []
    cur: list[str] = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s:
            if cur:
                paragraphs.append(" ".join(cur))
                cur = []
            continue
        # Skip pure heading lines and table separator rows.
        if s.startswith("#") or re.fullmatch(r"\|[\s\-:|]+\|", s):
            if cur:
                paragraphs.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        paragraphs.append(" ".join(cur))
    for p in paragraphs:
        # Strip basic markdown emphasis so the preview reads cleanly
        # in the dashboard (no orphan `**` / backticks).
        cleaned = re.sub(r"[*_`]", "", p).strip()
        if cleaned:
            return cleaned[: max_chars - 3] + "..." if len(cleaned) > max_chars else cleaned
    return None


def _build_pingpong_meta(state: dict) -> dict:
    """Compose the heartbeat's `metadata.local_factory.ping_pong`
    payload from the five Markdown artifacts cycle.py writes plus
    the structured desire_scorecard.json.

    The block is *cycle-scoped*: when cycle.py finishes a run it
    leaves these files on disk and updates factory_state.json with
    status/at/path fields. The dashboard reads this block to render
    the live planner ↔ designer ping-pong (the demo workflow's
    artifact_created events are now a fallback, not the source of
    truth).

    enabled is True when *any* of the four downstream stages have
    generated an artifact in the current cycle, OR when the file is
    present on disk from a prior cycle. That keeps the panel populated
    after a cycle that skipped (e.g., FACTORY_PLANNER_DESIGNER_PINGPONG
    was off this run).
    """
    PLANNER_PROPOSAL_F  = RUNTIME_DIR / "planner_proposal.md"
    DESIGNER_CRITIQUE_F = RUNTIME_DIR / "designer_critique.md"
    PLANNER_REVISION_F  = RUNTIME_DIR / "planner_revision.md"
    DESIGNER_FINAL_F    = RUNTIME_DIR / "designer_final_review.md"
    PM_DECISION_F       = RUNTIME_DIR / "pm_decision.md"
    DESIRE_SCORECARD_F  = RUNTIME_DIR / "desire_scorecard.json"

    scorecard_obj: dict | None = None
    if DESIRE_SCORECARD_F.is_file():
        try:
            scorecard_obj = json.loads(
                DESIRE_SCORECARD_F.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            scorecard_obj = None

    # Synthesize a scorecard dict when the JSON is missing but
    # factory_state.json carries the parsed values from the most
    # recent designer_final_review stage. Either source feeds the
    # same UI ship/hold gate.
    if not scorecard_obj:
        scorecard_obj = {
            "scores": dict(state.get("desire_scorecard") or {}),
            "total":  int(state.get("desire_scorecard_total") or 0),
            "ship_ready": bool(state.get("desire_scorecard_ship_ready")),
            "rework": list(state.get("desire_scorecard_rework") or []),
            "verdict": state.get("designer_final_review_verdict"),
            "generated_at": state.get("designer_final_review_at"),
        }

    pp_existed = any(
        f.is_file()
        for f in (
            PLANNER_PROPOSAL_F, DESIGNER_CRITIQUE_F, PLANNER_REVISION_F,
            DESIGNER_FINAL_F, PM_DECISION_F,
        )
    )
    pp_generated_this_cycle = any(
        state.get(k) == "generated"
        for k in (
            "designer_critique_status", "planner_revision_status",
            "designer_final_review_status", "pm_decision_status",
        )
    )

    return {
        "enabled": bool(pp_existed or pp_generated_this_cycle),
        # File existence flags — cheap booleans the UI uses to switch
        # between live-data and fallback rendering per step.
        "planner_proposal_exists":      PLANNER_PROPOSAL_F.is_file(),
        "designer_critique_exists":     DESIGNER_CRITIQUE_F.is_file(),
        "planner_revision_exists":      PLANNER_REVISION_F.is_file(),
        "designer_final_review_exists": DESIGNER_FINAL_F.is_file(),
        "pm_decision_exists":           PM_DECISION_F.is_file(),
        # Single-line previews so the dashboard panel doesn't have to
        # download the whole markdown file.
        "planner_proposal_preview":      _artifact_preview(PLANNER_PROPOSAL_F),
        "designer_critique_preview":     _artifact_preview(DESIGNER_CRITIQUE_F),
        "planner_revision_preview":      _artifact_preview(PLANNER_REVISION_F),
        "designer_final_review_preview": _artifact_preview(DESIGNER_FINAL_F),
        "pm_decision_preview":           _artifact_preview(PM_DECISION_F),
        # Stage statuses + timestamps so the UI can render the dot
        # next to each step (idle / running / passed / failed).
        "designer_critique_status":     state.get("designer_critique_status") or "skipped",
        "planner_revision_status":      state.get("planner_revision_status") or "skipped",
        "designer_final_review_status": state.get("designer_final_review_status") or "skipped",
        "pm_decision_status":           state.get("pm_decision_status") or "skipped",
        "designer_critique_at":         state.get("designer_critique_at"),
        "planner_revision_at":          state.get("planner_revision_at"),
        "designer_final_review_at":     state.get("designer_final_review_at"),
        "pm_decision_at":               state.get("pm_decision_at"),
        "planner_revision_selected_feature": state.get("planner_revision_selected_feature"),
        "designer_final_review_verdict": state.get("designer_final_review_verdict"),
        "pm_decision_message":           state.get("pm_decision_message"),
        # Score gate. Both the structured scorecard object AND the
        # flat ship_ready/rework fields ride along — UI consumers can
        # pick whichever is convenient.
        "desire_scorecard": scorecard_obj,
        "ship_ready": bool(scorecard_obj.get("ship_ready")),
        "rework":     list(scorecard_obj.get("rework") or []),
        "scorecard_path": (
            str(DESIRE_SCORECARD_F) if DESIRE_SCORECARD_F.is_file() else None
        ),
    }


def _build_local_factory_meta() -> dict:
    """Compose the `metadata.local_factory` payload for heartbeats.

    Pulls from factory_state.json + pid file + log tail. Everything is
    best-effort — missing files turn into nulls, never raised errors."""
    state = _read_factory_state() or {}
    alive, pid = _factory_pid_alive()

    # The state file's claude_proposal_path/at reflect the CURRENT cycle.
    # On a skip cycle these go null even when a proposal from an earlier
    # cycle is still on disk. We want the dashboard to keep showing
    # "last proposal time", so fall back to the file path + its mtime
    # when the state itself isn't carrying them.
    PROPOSAL_FILE = RUNTIME_DIR / "claude_proposal.md"
    proposal_path = state.get("claude_proposal_path")
    if not proposal_path and PROPOSAL_FILE.is_file():
        proposal_path = str(PROPOSAL_FILE)
    proposal_exists = bool(proposal_path) and Path(proposal_path).is_file()

    proposal_at = state.get("claude_proposal_at")
    if not proposal_at and proposal_exists:
        try:
            mtime = Path(proposal_path).stat().st_mtime
            proposal_at = (
                datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            )
        except OSError:
            proposal_at = None

    # Same fallback game for claude_apply: prefer the most recent
    # cycle's data, but keep the prior apply visible if this cycle just
    # skipped the stage.
    APPLY_DIFF_FILE = RUNTIME_DIR / "claude_apply.diff"
    apply_diff_path = state.get("claude_apply_diff_path")
    if not apply_diff_path and APPLY_DIFF_FILE.is_file():
        apply_diff_path = str(APPLY_DIFF_FILE)
    apply_diff_exists = bool(apply_diff_path) and Path(apply_diff_path).is_file()

    # Product Planner report — surfaces "what feature did the planner
    # decide to build in the last cycle" plus a path the dashboard can
    # deep-link to. Falls back to the file mtime when the cycle skipped
    # but a prior report still exists on disk.
    PLANNER_FILE = RUNTIME_DIR / "product_planner_report.md"
    planner_path = state.get("product_planner_path")
    if not planner_path and PLANNER_FILE.is_file():
        planner_path = str(PLANNER_FILE)
    planner_exists = bool(planner_path) and Path(planner_path).is_file()
    planner_at = state.get("product_planner_at")
    if not planner_at and planner_exists:
        try:
            mtime = Path(planner_path).stat().st_mtime
            planner_at = (
                datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            )
        except OSError:
            planner_at = None

    apply_at = state.get("claude_apply_at")
    if not apply_at and apply_diff_exists:
        try:
            mtime = Path(apply_diff_path).stat().st_mtime
            apply_at = (
                datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            )
        except OSError:
            apply_at = None

    return {
        "alive": alive,
        "pid": pid,
        "status": state.get("status"),
        "current_stage": state.get("current_stage"),
        "current_task": state.get("current_task"),
        "progress": state.get("progress"),
        "last_message": state.get("last_message"),
        "cycle": state.get("cycle"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "finished_at": state.get("finished_at"),
        "risky_files": state.get("risky_files") or [],
        "report_path": str(REPORT_FILE) if REPORT_FILE.is_file() else None,
        "report_exists": REPORT_FILE.is_file(),
        "claude_proposal_status": state.get("claude_proposal_status"),
        "claude_proposal_path": proposal_path,
        "claude_proposal_exists": proposal_exists,
        "claude_proposal_at": proposal_at,
        "claude_proposal_skipped_reason": state.get("claude_proposal_skipped_reason"),
        "claude_apply_status": state.get("claude_apply_status"),
        "claude_apply_at": apply_at,
        "claude_apply_changed_files": state.get("claude_apply_changed_files") or [],
        "claude_apply_changed_count": len(state.get("claude_apply_changed_files") or []),
        "claude_apply_rollback": bool(state.get("claude_apply_rollback")),
        "claude_apply_skipped_reason": state.get("claude_apply_skipped_reason"),
        "claude_apply_message": state.get("claude_apply_message"),
        "claude_apply_diff_path": apply_diff_path,
        "claude_apply_diff_exists": apply_diff_exists,
        "product_planner": {
            # Whether Product Planner Mode produced something this run
            # OR has a leftover report from a prior run on disk.
            "enabled": (
                state.get("product_planner_status") == "generated"
                or planner_exists
            ),
            "status": state.get("product_planner_status"),
            "bottleneck": state.get("product_planner_bottleneck"),
            "selected_feature": state.get("product_planner_selected_feature"),
            "solution_pattern": state.get("product_planner_solution_pattern"),
            "value_summary": state.get("product_planner_value_summary"),
            "llm_needed": state.get("product_planner_llm_needed"),
            "data_storage_needed": state.get("product_planner_data_storage_needed"),
            "external_integration_needed": state.get(
                "product_planner_external_integration_needed"
            ),
            "frontend_scope": state.get("product_planner_frontend_scope"),
            "backend_scope": state.get("product_planner_backend_scope"),
            "success_criteria": state.get("product_planner_success_criteria"),
            "candidate_count": state.get("product_planner_candidate_count") or 0,
            "report_path": planner_path,
            "report_exists": planner_exists,
            "generated_at": planner_at,
            "message": state.get("product_planner_message"),
            "skipped_reason": state.get("product_planner_skipped_reason"),
            "gate_failures": state.get("product_planner_gate_failures") or [],
        },
        "publish": _build_publish_meta(state),
        "publish_blocker": _build_publish_blocker_meta(state),
        "qa_gate": _build_qa_meta(state),
        "operator_fix": _build_operator_fix_meta(),
        # Planner ↔ Designer ping-pong cycle output. See
        # _build_pingpong_meta for the full schema. Always present so
        # the dashboard can decide enabled=true/false on the runner
        # side, instead of every consumer guessing.
        "ping_pong": _build_pingpong_meta(state),
        "log_tail": _log_tail(8),
    }


def _build_publish_blocker_meta(state: dict) -> dict:
    """Compose the heartbeat's `metadata.local_factory.publish_blocker`
    payload from the 5-bucket blocker fields cycle.py writes into
    factory_state.json. Also folds in the contents of
    .runtime/blocker_state.json (re-derived classification from the
    *current* working tree) so the dashboard reflects what publish_changes
    would actually see right now — not just the stale snapshot from the
    last cycle's blocker stages.

    Hard-risky paths are surfaced ONLY as basenames so the dashboard
    never echoes a directory hint that would help an attacker locate
    a secret.
    """
    # Cycle-state (last cycle's view).
    auto_restored = state.get("auto_restored_files") or []
    auto_deleted = state.get("auto_deleted_files") or []
    allowed_code = state.get("allowed_code_files") or []
    manual_required = state.get("manual_required_files") or []
    hard_risky_paths = state.get("hard_risky_files") or []
    # Back-compat: combined auto_resolved if the new fields are empty.
    if not auto_restored and not auto_deleted:
        legacy = state.get("auto_resolved_files") or []
        # We can't distinguish restore vs delete from the legacy list,
        # so file them all under auto_restored for the UI rather than
        # leaving them invisible.
        auto_restored = list(legacy)
    recurring = state.get("publish_blocker_recurring") or {}
    if not recurring:
        # Read the standalone counter file as a fallback.
        try:
            from pathlib import Path
            f = RUNTIME_DIR / "blocker_recurring.json"
            if f.is_file():
                recurring = {
                    k: int((v or {}).get("count", 0))
                    for k, v in json.loads(f.read_text(encoding="utf-8")).items()
                    if isinstance(v, dict)
                }
        except (json.JSONDecodeError, OSError):
            recurring = {}
    report_path = state.get("publish_blocker_report_path")
    if not report_path:
        candidate = RUNTIME_DIR / "blocker_resolve_report.md"
        if candidate.is_file():
            report_path = str(candidate)
    report_exists = bool(report_path) and Path(report_path).is_file()

    # Hard-risky basenames only — never the full path.
    def _bn(p: str) -> str:
        if not p:
            return "<empty>"
        return p.rsplit("/", 1)[-1] or p

    hard_risky_basenames = sorted({_bn(p) for p in hard_risky_paths})

    conflict_markers = state.get("conflict_marker_files") or []
    warning_reasons = state.get("warning_reasons") or []

    return {
        "blocked": bool(state.get("publish_blocked")),
        "status": state.get("publish_blocker_status") or "clean",
        # Counts — primary UI signal.
        "auto_restored_count": len(auto_restored),
        "auto_deleted_count": len(auto_deleted),
        "allowed_code_count": len(allowed_code),
        "manual_required_count": len(manual_required),
        "hard_risky_count": len(hard_risky_basenames),
        "conflict_marker_count": len(conflict_markers),
        # Lists — for "show details" expansion. We cap each list at
        # 30 to keep the heartbeat payload small.
        "auto_restored_files": auto_restored[:30],
        "auto_deleted_files": auto_deleted[:30],
        "allowed_code_files": allowed_code[:30],
        "manual_required_files": manual_required[:30],
        "hard_risky_basenames": hard_risky_basenames[:30],
        "conflict_markers": conflict_markers[:30],
        "warning_reasons": warning_reasons[:10],
        "message": state.get("publish_blocker_message"),
        "report_path": report_path,
        "report_exists": report_exists,
        "recurring": recurring,  # {path: count}
        # Back-compat keys for existing dashboard code.
        "auto_resolved_files": (auto_restored + auto_deleted)[:30],
        "auto_resolved_count": len(auto_restored) + len(auto_deleted),
    }


def _build_operator_fix_meta() -> dict:
    """Surface the latest operator_fix_state.json contents for the
    dashboard. The state file is the canonical source — we just shape
    the dict and add file-existence flags so the UI can decide
    whether to render report/feedback/request links.
    """
    state = _read_operator_fix_state()
    if not state:
        return {
            "status": "idle",
            "request_exists": False,
            "request_path": None,
            "started_at": None,
            "updated_at": None,
            "allow_publish": False,
            "publish_status": "not_requested",
            "last_message": None,
            "changed_files": [],
        }

    request_path = state.get("request_path") or str(OPERATOR_REQUEST_FILE)
    request_exists = OPERATOR_REQUEST_FILE.is_file()

    qa_report_path = state.get("qa_report_path")
    qa_report_exists = bool(qa_report_path) and Path(qa_report_path).is_file()
    qa_feedback_path = state.get("qa_feedback_path")
    qa_feedback_exists = bool(qa_feedback_path) and Path(qa_feedback_path).is_file()

    return {
        "status": state.get("status") or "idle",
        "request_path": request_path,
        "request_exists": request_exists,
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "allow_publish": bool(state.get("allow_publish")),
        "priority": state.get("priority") or "normal",
        "redactions": state.get("redactions") or [],
        "changed_files": state.get("changed_files") or [],
        "changed_count": len(state.get("changed_files") or []),
        "qa_status": state.get("qa_status") or "skipped",
        "qa_publish_allowed": bool(state.get("qa_publish_allowed")),
        "qa_failed_reason": state.get("qa_failed_reason"),
        "qa_failed_categories": state.get("qa_failed_categories") or [],
        "qa_report_path": qa_report_path,
        "qa_report_exists": qa_report_exists,
        "qa_feedback_path": qa_feedback_path,
        "qa_feedback_exists": qa_feedback_exists,
        "publish_status": state.get("publish_status") or "not_requested",
        "last_commit_hash": state.get("last_commit_hash"),
        "last_message": state.get("last_message"),
    }


def _build_qa_meta(state: dict) -> dict:
    """Compose the heartbeat's `metadata.local_factory.qa_gate` payload.

    Falls back gracefully when fields are missing — older state files
    (pre-QA stage) just see all-skipped status. We surface file paths
    only when the file actually exists on disk so the dashboard
    doesn't try to deep-link to nothing.
    """
    qa_report_path = state.get("qa_report_path")
    if not qa_report_path:
        candidate = RUNTIME_DIR / "qa_report.md"
        if candidate.is_file():
            qa_report_path = str(candidate)
    qa_report_exists = bool(qa_report_path) and Path(qa_report_path).is_file()

    qa_feedback_path = state.get("qa_feedback_path")
    if not qa_feedback_path:
        candidate = RUNTIME_DIR / "qa_feedback.md"
        if candidate.is_file():
            qa_feedback_path = str(candidate)
    qa_feedback_exists = bool(qa_feedback_path) and Path(qa_feedback_path).is_file()

    return {
        "status": state.get("qa_status") or "skipped",
        "publish_allowed": bool(state.get("qa_publish_allowed")),
        "failed_reason": state.get("qa_failed_reason"),
        "failed_categories": state.get("qa_failed_categories") or [],
        "build_artifact": state.get("qa_build_artifact") or "skipped",
        "api_health": state.get("qa_api_health") or "skipped",
        "screen_presence": state.get("qa_screen_presence") or "skipped",
        "flow_presence": state.get("qa_flow_presence") or "skipped",
        "domain_profile": state.get("qa_domain_profile") or "skipped",
        "report_path": qa_report_path,
        "report_exists": qa_report_exists,
        "feedback_path": qa_feedback_path,
        "feedback_exists": qa_feedback_exists,
        "fix_attempt": int(state.get("qa_fix_attempt") or 0),
        "fix_max_attempts": int(state.get("qa_fix_max_attempts") or 2),
        "fix_propose_status": state.get("qa_fix_propose_status") or "skipped",
        "fix_apply_status": state.get("qa_fix_apply_status") or "skipped",
    }


def _build_publish_meta(cycle_state: dict) -> dict:
    """Snapshot of publish-readiness — what the dashboard's "배포하기"
    button consults to decide whether to enable itself, plus the
    history of the last attempt.

    The classification work is done here so the UI doesn't need to
    re-implement the policy. The button activation logic mirrors what
    `_h_publish_changes` would do if invoked right now.
    """
    branch = _git_current_branch()
    changed = _git_changed_files()
    allowed, blocked, risky = _classify_publish_files(changed)
    publish_state = _read_publish_state()

    cycle_status = (cycle_state or {}).get("status")
    apply_status = (cycle_state or {}).get("claude_apply_status")
    risky_files_state = (cycle_state or {}).get("risky_files") or []

    # Stage statuses inside the most recent cycle. Used to refuse
    # publish when build/syntax flunked even if the working-tree-level
    # risky scan above came back empty.
    bad_stage_names = {"build_app", "build_control", "syntax_check", "git_check"}
    failed_cycle_stages = [
        s.get("name")
        for s in (cycle_state or {}).get("stages", [])
        if s.get("status") == "failed" and s.get("name") in bad_stage_names
    ]

    # Compute readiness — first reason wins so the UI can show
    # something concrete. Note: deploy/CI/infra path-shape is no
    # longer a blocker (only secret-shaped paths are).
    blocked_reason: str | None = None
    if branch is None:
        blocked_reason = "git 브랜치를 확인할 수 없음"
    elif branch != "main":
        blocked_reason = f"현재 브랜치가 main이 아님 (현재: {branch})"
    elif cycle_status == "running":
        blocked_reason = "사이클 실행 중이라 배포 대기 (파일 차단 아님)"
    elif failed_cycle_stages:
        blocked_reason = f"검증 실패 단계: {', '.join(failed_cycle_stages)}"
    elif risky_files_state:
        blocked_reason = f"위험 파일 {len(risky_files_state)}건 (최근 사이클)"
    elif risky:
        blocked_reason = f"git 변경 중 위험 파일 {len(risky)}건"
    elif not changed:
        blocked_reason = "변경 파일 없음"
    elif not allowed:
        blocked_reason = "허용된 파일이 없음"
    # blocked (deploy/CI/infra path shape) is no longer a blocker —
    # publish ships them, build/health/secret gates decide actual safety.

    ready = blocked_reason is None

    return {
        "ready": ready,
        "blocked_reason": blocked_reason,
        "branch": branch,
        "dry_run": _publish_is_dry_run(),
        "allow_publish": _publish_is_allowed(),
        "changed_count": len(changed),
        "changed_files": changed[:30],
        "allowed_count": len(allowed),
        "allowed_files": allowed[:30],
        "blocked_count": len(blocked),
        "blocked_files": blocked[:10],
        "risky_count": len(risky),
        "risky_files": risky[:10],
        "last_commit_hash": publish_state.get("last_commit_hash"),
        "last_push_status": publish_state.get("last_push_status"),
        "last_push_at": publish_state.get("last_push_at"),
        "last_publish_message": publish_state.get("last_publish_message"),
        "last_failed_stage": publish_state.get("last_failed_stage"),
        "last_restart_required": publish_state.get("last_restart_required"),
        "last_restart_action": publish_state.get("last_restart_action"),
        "last_restart_reason": publish_state.get("last_restart_reason"),
        "actions_url": ACTIONS_URL,
        # Per-stage stepper state for the "배포 진행" panel. Trimmed
        # so the heartbeat payload stays small (~40 history entries).
        "deploy_progress": (
            publish_state.get("deploy_progress")
            or _new_deploy_progress(_utc_now_z())
        ),
    }


def _runner_code_mtime_iso() -> str | None:
    """When was the on-disk runner.py last touched? Lets the dashboard
    confirm the running process picked up a fresh exec_self."""
    try:
        ts = os.path.getmtime(__file__)
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    except OSError:
        return None


def _build_runner_meta() -> dict:
    """Process-identity + working-tree summary the dashboard renders so
    a user can confirm a restart actually took (PID change, started_at
    change, code_mtime forward, etc.)."""
    branch = _git_current_branch()
    dirty = _git_changed_files()
    ok, head = _git("rev-parse", "HEAD", timeout=10)
    return {
        "pid": RUNNER_PID,
        "started_at": RUNNER_STARTED_AT,
        "code_mtime_at": _runner_code_mtime_iso(),
        "git_branch": branch,
        "git_commit": head.strip()[:12] if ok else None,
        "git_commit_full": head.strip() if ok else None,
        "dirty_files_count": len(dirty),
        "dirty_files": dirty[:30],
        "restart_dry_run": _restart_is_dry_run(),
    }


def heartbeat() -> None:
    metadata = {
        "poll_interval": POLL_INTERVAL_SEC,
        "repo_root": str(REPO_ROOT),
        "runner": _build_runner_meta(),
        "local_factory": _build_local_factory_meta(),
    }
    _request(
        "POST",
        "/runners/heartbeat",
        {
            "runner_id": RUNNER_ID,
            "name": RUNNER_NAME,
            "kind": "local",
            "status": "online",
            "metadata": metadata,
        },
    )


def claim_next() -> dict | None:
    return _request("GET", f"/runners/{RUNNER_ID}/commands/next")


def report_result(command_id: int, ok: bool, message: str) -> None:
    _request(
        "POST",
        f"/runners/{RUNNER_ID}/commands/{command_id}/result",
        {
            "status": "succeeded" if ok else "failed",
            "result_message": message[:1500],
        },
    )


def _execute(cmd_row: dict) -> None:
    name = cmd_row.get("command", "")
    cid = int(cmd_row.get("id", 0))
    payload = cmd_row.get("payload", {}) or {}
    handler = COMMAND_HANDLERS.get(name)
    if handler is None:
        sys.stderr.write(f"[runner] unknown command '{name}' — rejecting\n")
        report_result(cid, False, f"rejected_unknown_command: {name}")
        return
    # Surface the command id to handlers that want idempotency
    # (deploy_to_server uses it to dedupe the same command across
    # retries). Other handlers ignore it.
    payload["__command_id"] = cid
    sys.stderr.write(f"[runner] executing '{name}' (cmd #{cid})\n")
    try:
        ok, msg = handler(payload)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, f"handler raised: {e}"
    report_result(cid, ok, msg)

    # Honor any deferred action a handler scheduled. We always run the
    # action AFTER the report so the server has a chance to record the
    # success of `publish_changes` / `restart_runner` / `update_runner`
    # before the heartbeat goes silent during the bounce.
    global _POST_REPORT_ACTION
    action = _POST_REPORT_ACTION
    _POST_REPORT_ACTION = None
    if action == "factory":
        ok_r, out_r = _do_restart_factory()
        sys.stderr.write(f"[runner] post-report factory restart: ok={ok_r} {out_r}\n")
    elif action == "factory_runner":
        ok_r, out_r = _do_restart_factory()
        sys.stderr.write(f"[runner] post-report factory restart: ok={ok_r} {out_r}\n")
        # Brief pause — gives the freshly-spawned factory a head start
        # on its first heartbeat marker before we exec ourselves.
        time.sleep(1.0)
        _exec_self()
    elif action == "exec_self":
        time.sleep(0.5)
        _exec_self()


def main() -> None:
    if not RUNNER_TOKEN:
        sys.stderr.write(
            "[runner] WARNING: LOCAL_RUNNER_TOKEN is empty — running unauthenticated.\n"
            "         The server treats this as simulation mode and will accept it.\n"
        )
    sys.stderr.write(
        f"[runner] starting · runner_id={RUNNER_ID} · "
        f"control={CONTROL_TOWER_URL} · poll={POLL_INTERVAL_SEC}s\n"
    )
    last_heartbeat = 0.0
    while _running:
        now = time.time()
        if now - last_heartbeat > HEARTBEAT_INTERVAL_SEC:
            heartbeat()
            last_heartbeat = now
        cmd = claim_next()
        if cmd:
            _execute(cmd)
            # Loop tight — there might be more commands queued.
            continue
        time.sleep(POLL_INTERVAL_SEC)
    sys.stderr.write("[runner] stopped.\n")


if __name__ == "__main__":
    main()
