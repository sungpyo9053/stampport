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

# QA gate diagnostic artifacts. qa_diagnostics.json is written every
# time a deploy makes a QA decision (cached pass / on-demand run /
# crash) so the dashboard can show *why* the gate behaved the way it
# did even when qa_report.md is missing. command_diagnostics.json
# stores the most recent dispatched-command's structured failure (so
# the UI can render `last_command / status / failed_stage /
# diagnostic_code / suggested_action`).
QA_DIAGNOSTICS_FILE = RUNTIME_DIR / "qa_diagnostics.json"
COMMAND_DIAGNOSTICS_FILE = RUNTIME_DIR / "factory_command_diagnostics.json"

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

# Marker placed alongside PAUSE_MARKER when the runner pauses the bash
# factory loop on behalf of the API (continuous_mode=false / desired=
# paused). Lets the runner know it owns this pause and can lift it
# automatically when the API flips the flag back. Operator-written
# PAUSE_MARKER files (manual pause) leave this file absent and the
# runner refuses to clear them.
CONTINUOUS_PAUSE_MARKER = RUNTIME_DIR / "factory.continuous_paused"

# Factory Watchdog state file. Written by the watchdog thread, read by
# heartbeat builders. Off by default — must be opted in via
# FACTORY_WATCHDOG_ENABLED=true.
WATCHDOG_STATE_FILE = RUNTIME_DIR / "factory_watchdog.json"
# How many entries the watchdog keeps in its rolling event log on disk.
WATCHDOG_LOG_CAP = 30
# Minimum gap between auto-triggered smoke tests. The smoke test
# commits + pushes a single line to docs/factory-smoke-test.md so the
# operator can confirm "git push works end-to-end" without invoking
# Claude.
WATCHDOG_SMOKE_TEST_COOLDOWN_SEC = 30 * 60

_running = True

# Process identity, captured once at boot. RUNNER_STARTED_AT lets the
# dashboard show "재시작된 지 N분 전" so a user can confirm a restart
# actually replaced the process.
RUNNER_PID = os.getpid()
RUNNER_STARTED_AT = (
    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
)
# code_mtime + git head captured *at boot*. We compare against the
# current on-disk values to detect "the operator edited runner.py
# after the process started" (stale_runner) — a frequent root cause
# of "fix shipped but symptom unchanged" reports.
try:
    _RUNNER_CODE_MTIME_AT_START = os.path.getmtime(__file__)
except OSError:
    _RUNNER_CODE_MTIME_AT_START = 0.0

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


def _admin_request(method: str, path: str, body: dict | None = None) -> dict | None:
    """Same as _request but uses LOCAL_RUNNER_ADMIN_TOKEN for the
    factory-mutation endpoints (POST /factory/continuous, /factory/stop,
    /factory/desired). Falls back silently when unset — the API
    treats CONTROL_TOWER_ADMIN_TOKEN-unset as simulation mode and lets
    the call through, which is the normal local-development case.
    """
    url = f"{CONTROL_TOWER_URL.rstrip('/')}{path}"
    data = None if body is None else json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    admin_token = os.environ.get("LOCAL_RUNNER_ADMIN_TOKEN", "").strip()
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    elif RUNNER_TOKEN:
        # Better than nothing — the request still goes through, and the
        # API logs a "simulation mode" warning if no admin token is set
        # on its side.
        headers["Authorization"] = f"Bearer {RUNNER_TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:200]
        sys.stderr.write(f"[runner] admin {method} {path} → HTTP {e.code}: {body_text}\n")
        return None
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[runner] admin {method} {path} failed: {e}\n")
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
#
# Each deploy click gets a fresh `attempt_id` (e.g.
# "deploy_20260427_022412_cmd_31") and the prior attempt is moved into
# `previous_attempts` (cap 3) when the new one resets. This is what
# lets the dashboard show "이번 시도 진행 중" vs "이전 배포 시도"
# without smearing a stale failure on top of an in-flight retry.
DEPLOY_PROGRESS_HISTORY_CAP = 40
DEPLOY_PROGRESS_PREVIOUS_CAP = 3
DEPLOY_PROGRESS_TERMINAL = {"completed", "failed", "actions_triggered"}
# Statuses where a click is still meaningfully in flight. Anything not
# in this set with is_active=false is treated as "settled" — the
# dashboard re-enables the button.
DEPLOY_PROGRESS_ACTIVE_STATUSES = {
    "queued",
    "command_received",
    "validating",
    "committing",
    "pushing",
    "actions_triggered",
    "deploying",
}


def _make_attempt_id(command_id: int | None, now_iso: str) -> str:
    """Build a stable attempt id: deploy_<YYYYMMDD>_<HHMMSS>_cmd_<cid>.

    Falls back to "cmd_local" when the command_id is unknown (operator-
    fix path, manual publish_changes call) so the UI still has a key
    to keep history rows distinct.
    """
    try:
        # Strip the trailing "Z" so fromisoformat accepts it.
        ts = datetime.strptime(now_iso[:19], "%Y-%m-%dT%H:%M:%S")
        stamp = ts.strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    cid_token = f"cmd_{int(command_id)}" if command_id else "cmd_local"
    return f"deploy_{stamp}_{cid_token}"


def _new_deploy_progress(now: str) -> dict:
    return {
        # Identity. attempt_id flips on reset; command_id matches the
        # runner queue's command row id for the *current* attempt.
        "attempt_id": None,
        "command_id": None,
        # Lifecycle.
        "status": "idle",
        "is_active": False,
        "current_step": None,
        "started_at": None,
        "updated_at": now,
        "completed_at": None,
        "ended_at": None,
        "failed_at": None,
        # Failure detail (mirrored into history but kept top-level so
        # the FE doesn't have to rescan the array).
        "failed_stage": None,
        "failed_reason": None,
        "suggested_action": None,
        "failed_at_status": None,
        # Static metadata.
        "actions_url": ACTIONS_URL,
        # Per-attempt timeline.
        "history": [],
        # Archive of the last DEPLOY_PROGRESS_PREVIOUS_CAP attempts.
        # Each entry is a snapshot of a settled attempt.
        "previous_attempts": [],
    }


def _archive_attempt(prev: dict) -> dict | None:
    """Snapshot a settled deploy_progress so a new attempt's reset can
    drop it into `previous_attempts`. Returns None when the prior
    block has nothing worth archiving — e.g. the very first deploy of
    a runner session, or a brand-new runner with only a default idle
    placeholder."""
    if not prev:
        return None
    status = prev.get("status") or "idle"
    if status == "idle" and not prev.get("history"):
        return None
    if not prev.get("attempt_id") and status == "idle":
        return None
    return {
        "attempt_id": prev.get("attempt_id"),
        "command_id": prev.get("command_id"),
        "status": status,
        "is_active": False,
        "current_step": prev.get("current_step"),
        "started_at": prev.get("started_at"),
        "updated_at": prev.get("updated_at"),
        "completed_at": prev.get("completed_at"),
        "ended_at": prev.get("ended_at") or prev.get("completed_at"),
        "failed_at": prev.get("failed_at"),
        "failed_stage": prev.get("failed_stage"),
        "failed_reason": prev.get("failed_reason"),
        "suggested_action": prev.get("suggested_action"),
        "failed_at_status": prev.get("failed_at_status"),
        # Keep only the tail of the history — snapshots stay small.
        "history": list(prev.get("history") or [])[-12:],
    }


def _set_deploy_progress(
    status: str,
    *,
    current_step: str | None = None,
    failed_reason: str | None = None,
    failed_stage: str | None = None,
    suggested_action: str | None = None,
    log_message: str | None = None,
    reset: bool = False,
    command_id: int | None = None,
    attempt_id: str | None = None,
) -> None:
    """Update the deploy progress block under publish_state.

    Each call appends a history entry so the dashboard can render a
    timestamped log without the FE having to keep its own buffer
    across reloads. History is capped at DEPLOY_PROGRESS_HISTORY_CAP
    to keep the heartbeat payload small.

    `reset=True` (used when a fresh `deploy_to_server` command lands)
    archives the prior attempt into `previous_attempts` and starts a
    new one — fresh attempt_id, fresh history, fresh timestamps.
    """
    cur = _read_publish_state()
    prev = cur.get("deploy_progress")
    if not isinstance(prev, dict):
        prev = {}
    now = _utc_now_z()

    if reset:
        archived = _archive_attempt(prev)
        prior_archive = list(prev.get("previous_attempts") or [])
        if archived is not None:
            prior_archive.append(archived)
        # FIFO drop — keep at most DEPLOY_PROGRESS_PREVIOUS_CAP.
        prior_archive = prior_archive[-DEPLOY_PROGRESS_PREVIOUS_CAP:]
        progress = _new_deploy_progress(now)
        progress["previous_attempts"] = prior_archive
        progress["attempt_id"] = attempt_id or _make_attempt_id(command_id, now)
        progress["command_id"] = int(command_id) if command_id else None
        progress["started_at"] = now
    else:
        progress = {
            "attempt_id": prev.get("attempt_id"),
            "command_id": prev.get("command_id"),
            "status": prev.get("status") or "idle",
            "is_active": bool(prev.get("is_active")),
            "current_step": prev.get("current_step"),
            "started_at": prev.get("started_at"),
            "updated_at": prev.get("updated_at") or now,
            "completed_at": prev.get("completed_at"),
            "ended_at": prev.get("ended_at"),
            "failed_at": prev.get("failed_at"),
            "failed_stage": prev.get("failed_stage"),
            "failed_reason": prev.get("failed_reason"),
            "suggested_action": prev.get("suggested_action"),
            "failed_at_status": prev.get("failed_at_status"),
            "actions_url": ACTIONS_URL,
            "history": list(prev.get("history") or [])[-DEPLOY_PROGRESS_HISTORY_CAP:],
            "previous_attempts": list(prev.get("previous_attempts") or [])[
                -DEPLOY_PROGRESS_PREVIOUS_CAP:
            ],
        }

    prior_status = progress["status"]
    progress["status"] = status
    progress["updated_at"] = now
    if command_id is not None:
        progress["command_id"] = int(command_id)
    if attempt_id is not None and reset is False:
        progress["attempt_id"] = attempt_id

    if current_step is not None:
        progress["current_step"] = current_step

    if status == "command_received" and not progress["started_at"]:
        progress["started_at"] = now

    is_terminal = status in {"completed", "failed"}
    progress["is_active"] = (status in DEPLOY_PROGRESS_ACTIVE_STATUSES) and not is_terminal

    if is_terminal:
        progress["ended_at"] = now
    if status == "completed":
        progress["completed_at"] = now
    if status == "failed":
        progress["failed_at"] = now
        if failed_reason is not None:
            progress["failed_reason"] = failed_reason
        if failed_stage is not None:
            progress["failed_stage"] = failed_stage
        if suggested_action is not None:
            progress["suggested_action"] = suggested_action
        # Pin the in-flight stage where the failure happened so the
        # stepper can mark exactly that step red.
        progress["failed_at_status"] = (
            prior_status if prior_status not in ("idle", "failed") else "command_received"
        )
        # Once the user has hit failed, the deploy is no longer
        # "active" — the button must be allowed to re-enable so the
        # operator can retry without restarting the runner.
        if not progress["current_step"] or progress["current_step"].endswith("실행 중"):
            progress["current_step"] = "배포 실패"

    # Stamp the attempt id into the history line so the System Log
    # picks up "Deploy #<cid>" markers via the existing classifier.
    cid_label = (
        f"Deploy #{progress['command_id']}"
        if progress.get("command_id")
        else (
            f"Deploy [{progress['attempt_id']}]"
            if progress.get("attempt_id") else "Deploy"
        )
    )
    raw_msg = log_message or current_step or status
    entry_msg = f"{cid_label} · {status} · {raw_msg}"
    progress["history"].append({
        "at": now,
        "status": status,
        "attempt_id": progress.get("attempt_id"),
        "command_id": progress.get("command_id"),
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


# ---------------------------------------------------------------------------
# QA gate diagnostics
#
# Every deploy click goes through the QA gate exactly once. The
# *result* gets persisted to factory_state.json (qa_status, …) for the
# next deploy click to look at, but the *decision trail* — "we ran
# QA because the report was missing, the build_app step exited 1, the
# stderr tail says X" — needs to land somewhere even when
# qa_report.md never gets written. That's qa_diagnostics.json.
#
# diagnostic_code values map 1:1 to the user-facing failure classifier:
#
#   qa_passed_cached            previous cycle's qa_report.md is fresh
#                               and covers the working tree.
#   qa_passed_after_run         on-demand QA executed and passed.
#   qa_skipped_bypass           LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA.
#   qa_not_run                  publish path exited before reaching QA
#                               (e.g. branch_check refused first).
#   qa_report_missing_before_run  no qa_report.md on disk → on-demand
#                                 was needed.
#   qa_report_missing_after_run no qa_report.md after stage_qa_gate
#                               returned (rare — usually means the
#                               stage crashed mid-write).
#   qa_report_path_mismatch     stage_qa_gate wrote the report under a
#                               different RUNTIME path than the runner
#                               is reading. Usually a REPO_ROOT env
#                               mismatch.
#   qa_command_failed           one of the targeted commands
#                               (npm build / py_compile / …) exited
#                               non-zero. Carries failed_command +
#                               exit_code + stderr_tail.
#   qa_exception_before_report  the QA function itself raised. Carries
#                               exception_message + traceback tail.
#   stale_runner                the runner.py on disk has been edited
#                               since the running process started.
#   stale_command               a duplicate deploy command landed and
#                               was answered from cache / lock-rejected.
#   stale_metadata              cycle_state says qa passed but
#                               qa_report.md is missing or older than
#                               the working tree.
#   unknown                     fallback — the raw detail blob is still
#                               attached so an operator can debug.
# ---------------------------------------------------------------------------


_QA_SUGGESTION = {
    "qa_passed_cached":
        "이전 사이클의 QA 결과 사용 — 별도 조치 필요 없음",
    "qa_passed_after_run":
        "on-demand QA 통과 — 별도 조치 필요 없음",
    "qa_skipped_bypass":
        "LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA 가 켜져 있음 — 비상시에만 사용",
    "qa_not_run":
        "QA 단계가 실행되지 않음 — 이전 단계(branch_check / publish_blocker / secret_scan) 실패 메시지 확인",
    "qa_report_missing_before_run":
        "on-demand QA 실행 중 — 결과를 기다리세요",
    "qa_report_missing_after_run":
        "QA 실행 후에도 .runtime/qa_report.md 가 생성되지 않음 — runner 와 cycle 의 RUNTIME 경로가 다른지 확인 (LOCAL_RUNNER_REPO env)",
    "qa_report_path_mismatch":
        "stage_qa_gate 가 다른 경로에 report 를 기록했어요 — LOCAL_RUNNER_REPO / REPO_ROOT 환경변수를 일치시키세요",
    "qa_command_failed":
        "검증 명령이 실패했어요 — 위 stderr tail 의 오류를 수정한 뒤 다시 배포",
    "qa_exception_before_report":
        "QA 실행 중 예외 발생 — exception_message 확인 후 cycle.py / runner.py 수정",
    "stale_runner":
        "runner.py 가 부팅 이후 수정됐어요 — `restart runner` 또는 `update runner` 후 다시 배포",
    "stale_command":
        "이전 deploy 명령이 처리 중 / 캐시됨 — 30초 후 다시 시도하거나 runner 재시작",
    "stale_metadata":
        "factory_state.json 의 qa 상태와 실제 파일 상태가 불일치 — on-demand QA 가 자동으로 재실행합니다",
    "unknown":
        "원인 분류 실패 — qa_diagnostics.json 의 raw_detail 확인",
}


def _qa_diagnostic_suggested(code: str) -> str:
    return _QA_SUGGESTION.get(code, _QA_SUGGESTION["unknown"])


def _tail_text(text: str | None, lines: int = 12, max_chars: int = 1600) -> str:
    """Last `lines` lines, capped at `max_chars` characters total. Used
    so stdout_tail/stderr_tail in heartbeat metadata stay small."""
    if not text:
        return ""
    s = "\n".join(text.splitlines()[-lines:]).strip()
    if len(s) > max_chars:
        s = s[-max_chars:]
    return s


def _save_qa_diagnostics(payload: dict) -> None:
    """Crash-safe writer for qa_diagnostics.json. Even if the QA path
    later raises, this file is the only durable evidence of WHY the
    deploy was about to be blocked."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        QA_DIAGNOSTICS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to write qa_diagnostics: {e}\n")


def _read_qa_diagnostics() -> dict:
    if not QA_DIAGNOSTICS_FILE.is_file():
        return {}
    try:
        return json.loads(QA_DIAGNOSTICS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_command_diagnostics(payload: dict) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        COMMAND_DIAGNOSTICS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to write command_diagnostics: {e}\n")


def _read_command_diagnostics() -> dict:
    if not COMMAND_DIAGNOSTICS_FILE.is_file():
        return {}
    try:
        return json.loads(COMMAND_DIAGNOSTICS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _log_event(line: str) -> None:
    """Append a line to .runtime/local_factory.log so _log_tail (and
    the dashboard System Log panel that consumes it) picks it up
    regardless of whether the cycle is mid-flight. Best-effort: log
    failures never block the QA flow."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{_utc_now_z()}] {line}\n")
    except OSError as e:
        sys.stderr.write(f"[runner] log_event failed: {e}\n")


def _set_continuous_pause(reason: str) -> bool:
    """Apply runner-managed pause: write both PAUSE_MARKER (read by
    the bash factory loop) and CONTINUOUS_PAUSE_MARKER (so the runner
    knows it owns this pause). No-op if both already exist. Returns
    True when the pause state changed."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        changed = False
        if not PAUSE_MARKER.is_file():
            PAUSE_MARKER.write_text(
                f"runner-managed pause at {_utc_now_z()}: {reason}\n",
                encoding="utf-8",
            )
            changed = True
        if not CONTINUOUS_PAUSE_MARKER.is_file():
            CONTINUOUS_PAUSE_MARKER.write_text(
                f"continuous_off at {_utc_now_z()}: {reason}\n",
                encoding="utf-8",
            )
            changed = True
        return changed
    except OSError as e:
        sys.stderr.write(f"[runner] continuous pause write failed: {e}\n")
        return False


def _clear_continuous_pause() -> bool:
    """Lift runner-managed pause. Only acts when CONTINUOUS_PAUSE_MARKER
    is present — otherwise the PAUSE_MARKER was set by the operator and
    we leave it alone. Returns True when the pause state changed."""
    if not CONTINUOUS_PAUSE_MARKER.is_file():
        return False
    changed = False
    try:
        if PAUSE_MARKER.is_file():
            PAUSE_MARKER.unlink()
            changed = True
    except OSError:
        pass
    try:
        CONTINUOUS_PAUSE_MARKER.unlink()
        changed = True
    except OSError:
        pass
    return changed


_LAST_FACTORY_RECONCILE_AT: float = 0.0
FACTORY_RECONCILE_INTERVAL_SEC = 30.0


def _reconcile_continuous_mode() -> None:
    """Bridge API factory state → local bash-loop pause marker.

    Pulls GET /factory/status and translates the result into
    factory.paused presence/absence so flipping continuous_mode on the
    dashboard actually halts (or releases) the local cycle loop.

    Throttled to FACTORY_RECONCILE_INTERVAL_SEC so we don't hammer the
    API every poll. Best-effort — a 5xx / network error just leaves
    the local state untouched until next tick.
    """
    global _LAST_FACTORY_RECONCILE_AT
    now = time.time()
    if now - _LAST_FACTORY_RECONCILE_AT < FACTORY_RECONCILE_INTERVAL_SEC:
        return
    _LAST_FACTORY_RECONCILE_AT = now

    snap = _request("GET", "/factory/status")
    if not isinstance(snap, dict):
        return

    continuous_mode = bool(snap.get("continuous_mode"))
    desired = (snap.get("desired_status") or "").strip().lower()
    actual = (snap.get("status") or "").strip().lower()

    # The dashboard's intent: if Continuous is OFF, the operator wants
    # the loop to stop after the current cycle. desired_status reflects
    # the same intent more directly when the operator explicitly
    # paused/stopped.
    want_paused = (not continuous_mode) or desired in {"paused", "idle"}

    if want_paused:
        if _set_continuous_pause(
            reason=f"continuous_mode={continuous_mode} desired={desired} actual={actual}"
        ):
            _log_event(
                f"factory bridge · pause applied (continuous={continuous_mode}, "
                f"desired={desired})"
            )
    else:
        if _clear_continuous_pause():
            _log_event(
                f"factory bridge · pause lifted (continuous={continuous_mode}, "
                f"desired={desired})"
            )


def _expected_qa_report_paths() -> tuple[Path, Path | None]:
    """Return (runner_path, cycle_path). Cycle path is None when the
    import fails. Used to detect qa_report_path_mismatch — if the
    cycle module computed RUNTIME from a different REPO_ROOT than the
    runner did, stage_qa_gate writes to a different file than the
    runner reads from."""
    runner_path = RUNTIME_DIR / "qa_report.md"
    try:
        from . import cycle as _cycle
        cycle_path = Path(getattr(_cycle, "QA_REPORT_FILE", runner_path))
        return runner_path, cycle_path
    except ImportError:
        return runner_path, None


def _runner_is_stale_now() -> tuple[bool, dict]:
    """Did the on-disk runner.py change since this process started?
    That's the strongest "you're running an older runner than the
    repo" signal we have without re-execing the interpreter."""
    detail: dict = {
        "code_mtime_at_start": _RUNNER_CODE_MTIME_AT_START,
        "code_mtime_now": None,
    }
    try:
        now = os.path.getmtime(__file__)
    except OSError:
        return False, detail
    detail["code_mtime_now"] = now
    if not _RUNNER_CODE_MTIME_AT_START:
        return False, detail
    # 1 second slack so a freshly-started runner whose own startup
    # touched the .pyc doesn't trip the alarm.
    return now > _RUNNER_CODE_MTIME_AT_START + 1.0, detail


def _qa_gate_status_from_state(cycle_state: dict) -> tuple[str, str]:
    """Inspect the persisted cycle state + qa_report.md and classify
    the QA Gate without running it.

    Returns (kind, reason). `kind` is one of:
      - "pass"    — qa_report.md exists, status=passed, publish_allowed,
                    and the report is newer than the working tree.
      - "fail"    — qa_status is failed (or publish_allowed is false).
      - "missing" — qa_report.md does not exist on disk.
      - "skipped" — qa_report.md exists but qa_status is "skipped"/empty,
                    e.g. the cycle exited before stage_qa_gate ran.
      - "stale"   — qa_report.md exists and last marked passed, but a
                    file in the working tree changed after the report
                    was written so the pass no longer covers the diff.

    `_h_publish_changes` treats "missing" / "skipped" / "stale" as
    "run the QA Gate on-demand and decide from THAT result", instead
    of refusing publish outright.
    """
    qa_report = RUNTIME_DIR / "qa_report.md"
    qa_status = ((cycle_state or {}).get("qa_status") or "").strip()
    qa_publish_allowed = bool((cycle_state or {}).get("qa_publish_allowed"))

    if not qa_report.is_file():
        return (
            "missing",
            "QA Gate가 아직 실행되지 않았습니다 (.runtime/qa_report.md 없음)",
        )

    if qa_status in {"", "skipped"}:
        return (
            "skipped",
            f"QA Gate가 아직 실행되지 않았습니다 (qa_status={qa_status or 'unset'})",
        )

    if qa_status != "passed" or not qa_publish_allowed:
        reason = (cycle_state or {}).get("qa_failed_reason") or qa_status
        return "fail", reason or "unknown"

    # Staleness check — make sure the recorded "pass" still covers the
    # current working tree.
    try:
        qa_mtime = qa_report.stat().st_mtime
    except OSError:
        qa_mtime = 0.0
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
        return "stale", "qa_report 가 작업 트리보다 오래됨"

    return "pass", "QA Gate 통과 (이전 사이클)"


def _merge_factory_state_qa(qa_dict: dict) -> None:
    """Read-modify-write the QA-related fields in factory_state.json so
    the heartbeat builder picks up an on-demand QA result without
    waiting for a fresh cycle. Best-effort: any I/O failure is logged
    to stderr but does not abort the publish flow."""
    try:
        cur = _read_factory_state() or {}
        cur.update(qa_dict)
        cur["qa_on_demand_at"] = _utc_now_z()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to merge on-demand QA state: {e}\n")


def _qa_targeted_command_plan(changed_files: list[str]) -> list[dict]:
    """Decide which validation commands to run for an on-demand QA
    Gate based on what actually changed in the working tree.

    Returns a list of step dicts: [{key, label, argv, cwd, env, category}].
    Each step is run sequentially; the first failure stops the gate
    and gets surfaced with its stderr tail in the deploy_progress
    history.

    The plan is *targeted* by design — running every npm build + every
    py_compile on every deploy click wastes minutes when only one
    folder changed.
    """
    import shutil as _shutil

    npm = _shutil.which("npm")
    py = sys.executable

    def _has_prefix(prefix: str) -> bool:
        return any(p.startswith(prefix) for p in changed_files)

    plan: list[dict] = []

    # 1. app/web build — fired only when the SPA tree was touched.
    if _has_prefix("app/web/") and npm:
        plan.append({
            "key": "build_app",
            "label": "app/web npm run build",
            "argv": [npm, "run", "build"],
            "cwd": REPO_ROOT / "app" / "web",
            "env": {"CI": "1"},
            "category": "Build Artifact",
        })

    # 2. control_tower/web build.
    if _has_prefix("control_tower/web/") and npm:
        plan.append({
            "key": "build_control",
            "label": "control_tower/web npm run build",
            "argv": [npm, "run", "build"],
            "cwd": REPO_ROOT / "control_tower" / "web",
            "env": {"CI": "1"},
            "category": "Build Artifact",
        })

    # 3. local_runner py_compile — runner.py + cycle.py only,
    # mirroring the user's explicit minimum.
    if _has_prefix("control_tower/local_runner/"):
        for rel in (
            "control_tower/local_runner/runner.py",
            "control_tower/local_runner/cycle.py",
        ):
            target = REPO_ROOT / rel
            if target.is_file():
                plan.append({
                    "key": f"py_compile_{Path(rel).stem}",
                    "label": f"py_compile {rel}",
                    "argv": [py, "-m", "py_compile", str(target)],
                    "cwd": REPO_ROOT,
                    "env": None,
                    "category": "Local Runner",
                })

    # 4. control_tower/api py_compile — main.py only.
    if _has_prefix("control_tower/api/"):
        target = REPO_ROOT / "control_tower" / "api" / "main.py"
        if target.is_file():
            plan.append({
                "key": "py_compile_control_api",
                "label": "py_compile control_tower/api/main.py",
                "argv": [py, "-m", "py_compile", str(target)],
                "cwd": REPO_ROOT,
                "env": None,
                "category": "API Health",
            })

    # 5. app/api py_compile — prefer the project venv if present,
    # fall back to the system python. We don't `source .venv/bin/activate`
    # (subprocess can't), but invoking the venv binary directly has
    # the same import effect.
    if _has_prefix("app/api/"):
        target = REPO_ROOT / "app" / "api" / "app" / "main.py"
        if target.is_file():
            venv_py = REPO_ROOT / "app" / "api" / ".venv" / "bin" / "python"
            py_bin = str(venv_py) if venv_py.is_file() else py
            plan.append({
                "key": "py_compile_app_api",
                "label": "py_compile app/api/app/main.py",
                "argv": [py_bin, "-m", "py_compile", str(target)],
                "cwd": REPO_ROOT,
                "env": None,
                "category": "API Health",
            })

    return plan


def _stderr_tail(output: str, lines: int = 12) -> str:
    """Pull the trailing N lines from a captured subprocess output —
    cycle._run returns stdout + '\\n--stderr--\\n' + stderr; for the
    failure copy in the dashboard we want the *last* lines, which
    are almost always the actionable error lines from stderr."""
    if not output:
        return ""
    tail = "\n".join(output.splitlines()[-lines:])
    return tail.strip()


def _run_qa_step(step: dict, timeout: float = 300.0) -> dict:
    """Run a single QA step (npm build / py_compile / …) directly via
    subprocess so we can preserve `returncode`, stdout, and stderr in
    isolation. cycle._run merges them into a single string with a
    `--stderr--` divider, which is fine for the cycle log but loses
    the structure the diagnostic classifier needs.

    Returns a dict the caller can attach to step_results without
    further shaping.
    """
    argv = step["argv"]
    cwd = step.get("cwd")
    env_override = step.get("env")
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    base = {
        "key": step["key"],
        "label": step["label"],
        "category": step["category"],
        "command": " ".join(shlex.quote(a) for a in argv),
        "cwd": str(cwd) if cwd else None,
        "ok": False,
        "exit_code": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "timed_out": False,
        "tool_missing": False,
        "exception_message": None,
    }
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        base["ok"] = r.returncode == 0
        base["exit_code"] = r.returncode
        base["stdout_tail"] = _tail_text(r.stdout, 12)
        base["stderr_tail"] = _tail_text(r.stderr, 12)
        return base
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired carries partial stdout/stderr as bytes when
        # capture_output=True. Decode best-effort.
        def _maybe_decode(x: object) -> str:
            if isinstance(x, bytes):
                return x.decode("utf-8", errors="replace")
            return x or ""
        base["ok"] = False
        base["timed_out"] = True
        base["exception_message"] = f"timeout after {timeout}s"
        base["stdout_tail"] = _tail_text(_maybe_decode(e.stdout), 12)
        base["stderr_tail"] = _tail_text(_maybe_decode(e.stderr), 12)
        return base
    except FileNotFoundError as e:
        base["ok"] = False
        base["tool_missing"] = True
        base["exception_message"] = f"missing tool: {e}"
        return base
    except Exception as e:  # noqa: BLE001
        base["ok"] = False
        base["exception_message"] = f"subprocess error: {e}"[:300]
        return base


def _run_targeted_qa_gate(
    changed_files: list[str],
) -> tuple[bool, str, dict, list[dict]]:
    """Run only the validations relevant to `changed_files`, then run
    cycle.stage_qa_gate to lock in screen_presence / flow_presence /
    domain_profile. Returns (passed, message, qa_dict, step_results).

    `step_results` is a list of {key, label, ok, stderr_tail, command,
    cwd, category} so the caller can render exact "여기서 실패" detail
    in the deploy progress UI.

    On any targeted-step failure we stop early — there's no point
    running domain_profile when the build is broken.
    """
    try:
        from . import cycle as _cycle
    except ImportError as e:
        msg = f"cycle 모듈 import 실패: {e}"
        qa_dict = {
            "qa_status": "failed",
            "qa_publish_allowed": False,
            "qa_failed_reason": msg,
            "qa_failed_categories": ["On-Demand"],
            "qa_build_artifact": "skipped",
            "qa_api_health": "skipped",
            "qa_screen_presence": "skipped",
            "qa_flow_presence": "skipped",
            "qa_domain_profile": "skipped",
            "qa_report_path": None,
            "qa_feedback_path": None,
        }
        _merge_factory_state_qa(qa_dict)
        return False, msg, qa_dict, []

    plan = _qa_targeted_command_plan(changed_files)
    step_results: list[dict] = []

    for step in plan:
        record = _run_qa_step(step, timeout=300.0)
        step_results.append(record)
        if not record["ok"]:
            cat_set: list[str] = []
            for r in step_results:
                if not r["ok"] and r["category"] not in cat_set:
                    cat_set.append(r["category"])
            qa_dict = {
                "qa_status": "failed",
                "qa_publish_allowed": False,
                "qa_failed_reason": (
                    f"{step['label']} 실패 — "
                    + (record["stderr_tail"][:200] or "오류 출력 없음")
                ),
                "qa_failed_categories": cat_set or ["Build/Syntax"],
                "qa_build_artifact": (
                    "failed"
                    if any(
                        r["key"].startswith("build_") and not r["ok"]
                        for r in step_results
                    )
                    else "skipped"
                ),
                "qa_api_health": (
                    "failed"
                    if any(
                        r["key"].startswith("py_compile_") and not r["ok"]
                        for r in step_results
                    )
                    else "skipped"
                ),
                "qa_screen_presence": "skipped",
                "qa_flow_presence": "skipped",
                "qa_domain_profile": "skipped",
                "qa_report_path": None,
                "qa_feedback_path": None,
            }
            _merge_factory_state_qa(qa_dict)
            return (
                False,
                f"{step['label']} 실패",
                qa_dict,
                step_results,
            )

    # Targeted commands all passed (or there were none for this diff —
    # docs-only change). Hand off to stage_qa_gate so the
    # screen_presence / flow_presence / domain_profile invariants
    # still get checked and qa_report.md gets written.
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
    _merge_factory_state_qa(qa_dict)
    return sr.status == "passed", sr.message, qa_dict, step_results


def _run_on_demand_qa_gate() -> tuple[bool, str, dict]:
    """Back-compat wrapper for callers (operator_fix flow) that don't
    have a changed-files list handy. Delegates to the targeted runner
    using the current `git status` diff and discards the per-step
    detail — operator_fix surfaces its own QA telemetry."""
    ok, msg, qa_dict, _steps = _run_targeted_qa_gate(_git_changed_files())
    return ok, msg, qa_dict


def _format_qa_failure_detail(qa_dict: dict, qa_msg: str) -> dict:
    """Pull a structured failure summary out of the QA result so the
    System Log entry / deploy_progress card surfaces what the operator
    needs to fix: which category failed, the first failed file when we
    can extract one from the reason text, and a suggested next action.
    """
    cats = list(qa_dict.get("qa_failed_categories") or [])
    reason = qa_msg or qa_dict.get("qa_failed_reason") or "unknown"
    m = re.search(r"([\w./-]+\.(?:py|jsx|js|tsx|ts|md|json|sh|html|css))", reason)
    failed_file = m.group(1) if m else None
    if cats == ["Build/Syntax"]:
        suggested = "npm run build / py_compile 실패 → 빌드 오류 수정 후 다시 배포 시도"
    elif cats:
        suggested = "qa_feedback.md 확인 → 해당 카테고리의 파일 수정 후 다시 배포 시도"
    else:
        suggested = "qa_report.md 확인 → 원인 식별 후 다시 배포 시도"
    return {
        "categories": cats,
        "file": failed_file,
        "reason": reason,
        "suggested_action": suggested,
    }


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

    def _progress(
        status: str,
        *,
        step: str,
        fail: str | None = None,
        log_message: str | None = None,
    ) -> None:
        if not track_progress:
            return
        _set_deploy_progress(
            status,
            current_step=step,
            failed_reason=fail,
            log_message=log_message,
        )

    def _record_failure(
        stage: str,
        message: str,
        suggested_action: str | None = None,
    ) -> None:
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
                failed_stage=stage,
                suggested_action=suggested_action,
                log_message=f"publish blocked at {stage}: {message[:200]}",
            )
        # Mirror the failure into command_diagnostics so the dashboard
        # always has a structured reason — including `qa_not_run` for
        # the cases where publish gave up before the QA gate (e.g.
        # branch_check, secret_scan, publish_blocker). The QA-stage
        # path overwrites this with its own richer diagnostic_code.
        if stage != "qa_gate":
            _save_command_diagnostics({
                "last_command": "deploy_to_server",
                "status": "failed",
                "failed_stage": stage,
                "diagnostic_code": "qa_not_run",
                "failed_reason": message[:600],
                "suggested_action": (
                    suggested_action
                    or _qa_diagnostic_suggested("qa_not_run")
                ),
                "occurred_at": _utc_now_z(),
            })
            _log_event(f"deploy blocked by {stage}: {message[:200]}")

    _progress("validating", step="브랜치 / Release Safety Gate 확인 중")

    # 0. Anything to publish?
    #
    # Hoist the no-changes check ahead of every other gate. When git
    # status is clean we have nothing to ship, and forcing the request
    # through branch_check / publish_blocker_preflight / QA Gate would
    # only produce noisy failure rows in deploy_progress / command_
    # diagnostics that the dashboard later mis-reports as "QA Gate
    # blocked the deploy". Surfacing this as `no_changes_to_deploy`
    # lets the Pipeline Recovery Orchestrator treat it as "배포할 변경
    # 없음" instead of "qa_gate failed".
    changed = _git_changed_files()
    if not changed:
        _save_publish_state({
            "last_push_status": "noop",
            "last_push_at": _utc_now_z(),
            "last_publish_message": "no changes to publish",
            "last_failed_stage": None,
            "last_attempt_started_at": started_at,
        })
        # Make sure deploy_progress reflects "completed without push" —
        # never "failed" — so a previous QA failure row doesn't haunt
        # the next operator click. The deploy_to_server caller will
        # later overwrite this with `actions_triggered` only when
        # something actually shipped.
        if track_progress:
            _set_deploy_progress(
                "completed",
                current_step="변경 없음 — 배포 스킵",
                log_message="no changes to deploy — deploy_progress cleared",
            )
        # Mirror to command_diagnostics so the panel reads "no_changes_
        # to_deploy" instead of falling back to a stale qa_gate row.
        _save_command_diagnostics({
            "last_command": "deploy_to_server",
            "status": "noop",
            "failed_stage": None,
            "diagnostic_code": "no_changes_to_deploy",
            "failed_reason": None,
            "suggested_action": (
                "변경 파일이 없어 push 하지 않습니다 — 새 작업을 commit 한 뒤 다시 시도하세요."
            ),
            "occurred_at": _utc_now_z(),
        })
        _log_event("publish skipped: no changes to publish (no_changes_to_deploy)")
        return True, "publish skipped: no changes to publish"

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

    # 4b. QA Gate.
    #
    # Goals:
    #   * Never refuse purely on "qa_report.md is missing" — first run
    #     the gate on-demand and decide from THAT result.
    #   * Persist enough evidence (qa_diagnostics.json + heartbeat
    #     metadata + System Log) that the operator never has to ask
    #     "did QA actually run? where did the output go?"
    #   * Crash-safe: even if the QA function raises an exception, we
    #     still write a diagnostic so the UI shows *why*.
    #
    # On every path we end up calling either _qa_finalize_pass or
    # _qa_finalize_fail (defined below) which write the qa_diagnostics
    # blob, persist failure context, and emit a System Log line.
    revalidate_already_ran = False
    qa_msg = ""

    runner_report_path, cycle_report_path = _expected_qa_report_paths()
    qa_required_reason = "no decision yet"

    def _read_qa_state_now() -> dict:
        """Re-read factory_state.json so the diagnostic blob always
        reflects what stage_qa_gate just wrote (vs the snapshot we
        took at publish entry)."""
        return _read_factory_state() or {}

    def _build_diag_base(stage_label: str) -> dict:
        existed = runner_report_path.is_file()
        return {
            "stage_label": stage_label,
            "decided_at": _utc_now_z(),
            "report_path": str(runner_report_path),
            "cycle_report_path": (
                str(cycle_report_path) if cycle_report_path else None
            ),
            "report_exists_before": existed,
            "report_exists_after": existed,
            "changed_files": list(allowed),
            "qa_required_reason": qa_required_reason,
            "qa_kind": None,
            "qa_status": None,
            "publish_allowed": False,
            "diagnostic_code": "unknown",
            "failed_command": None,
            "exit_code": None,
            "stdout_tail": None,
            "stderr_tail": None,
            "exception_message": None,
            "step_results": [],
            "stale_runner": _runner_is_stale_now()[0],
        }

    def _classify_after_run(
        diag: dict,
        ok: bool,
        ondemand_msg: str,
        step_results: list[dict],
    ) -> str:
        """After an on-demand run, decide the diagnostic_code from the
        evidence collected. Order matters — we report the most
        specific cause first."""
        # Path mismatch: stage_qa_gate (cycle) wrote to a different
        # path than the runner reads from. We detect it by checking
        # the cycle path exists *but* the runner path doesn't.
        if cycle_report_path and (
            str(cycle_report_path) != str(runner_report_path)
            and cycle_report_path.is_file()
            and not runner_report_path.is_file()
        ):
            return "qa_report_path_mismatch"
        # Targeted step failed (build/py_compile non-zero exit).
        failed_step = next((r for r in step_results if not r["ok"]), None)
        if failed_step is not None:
            return "qa_command_failed"
        # No targeted failure but stage_qa_gate decided fail (screen/
        # flow/domain). qa_report.md should still have been written.
        if not ok and runner_report_path.is_file():
            return "qa_command_failed"
        # We declared not-ok but no report shows for it on disk.
        if not runner_report_path.is_file():
            return "qa_report_missing_after_run"
        return "unknown"

    def _qa_finalize_fail(
        diag: dict,
        diagnostic_code: str,
        failed_msg: str,
    ) -> tuple[bool, str]:
        """Save the diagnostic blob, persist failure context, and emit
        the deploy_progress + System Log entries. Returns the publish
        return value the caller should bubble out."""
        diag["report_exists_after"] = runner_report_path.is_file()
        diag["diagnostic_code"] = diagnostic_code
        diag["suggested_action"] = _qa_diagnostic_suggested(diagnostic_code)
        _save_qa_diagnostics(diag)
        _save_command_diagnostics({
            "last_command": "deploy_to_server",
            "status": "failed",
            "failed_stage": "qa_gate",
            "diagnostic_code": diagnostic_code,
            "failed_reason": failed_msg[:600],
            "suggested_action": diag["suggested_action"],
            "occurred_at": _utc_now_z(),
        })
        _log_event(
            f"QA Gate failed → deploy blocked "
            f"({diagnostic_code}): {failed_msg.splitlines()[0][:200]}"
        )
        _record_failure("qa_gate", failed_msg[:1200], suggested_action=diag["suggested_action"])
        return False, f"publish failed at qa_gate: {failed_msg[:1200]}"

    def _qa_finalize_pass(diag: dict, message: str) -> None:
        diag["report_exists_after"] = runner_report_path.is_file()
        if not diag.get("diagnostic_code") or diag["diagnostic_code"] == "unknown":
            diag["diagnostic_code"] = (
                "qa_passed_after_run" if diag.get("qa_kind") in (None, "missing", "skipped", "stale")
                else "qa_passed_cached"
            )
        diag["suggested_action"] = _qa_diagnostic_suggested(diag["diagnostic_code"])
        _save_qa_diagnostics(diag)
        _save_command_diagnostics({
            "last_command": "deploy_to_server",
            "status": "running",
            "failed_stage": None,
            "diagnostic_code": diag["diagnostic_code"],
            "failed_reason": None,
            "suggested_action": diag["suggested_action"],
            "occurred_at": _utc_now_z(),
        })
        _log_event(f"QA Gate passed ({diag['diagnostic_code']}): {message[:200]}")

    if _publish_allows_failed_qa():
        qa_msg = "QA Gate 우회 (LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA=true)"
        _progress(
            "validating",
            step="QA Gate 우회 (LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA)",
            log_message=qa_msg,
        )
        diag = _build_diag_base("QA Gate bypassed")
        diag["qa_kind"] = "bypass"
        diag["diagnostic_code"] = "qa_skipped_bypass"
        diag["suggested_action"] = _qa_diagnostic_suggested("qa_skipped_bypass")
        diag["publish_allowed"] = True
        _save_qa_diagnostics(diag)
        _log_event("QA Gate bypassed (LOCAL_RUNNER_ALLOW_PUBLISH_WITH_FAILED_QA)")
    else:
        qa_kind, qa_reason = _qa_gate_status_from_state(state_at_start)
        qa_required_reason = f"{qa_kind}: {qa_reason}"
        diag = _build_diag_base("QA Gate decision")
        diag["qa_kind"] = qa_kind
        diag["qa_required_reason"] = qa_required_reason

        if qa_kind == "pass":
            qa_msg = qa_reason
            diag["qa_status"] = "passed"
            diag["publish_allowed"] = True
            _progress(
                "validating",
                step="QA Gate 검증 중 (이전 사이클 통과)",
                log_message=f"QA Gate passed (이전 사이클): {qa_reason}",
            )
            _qa_finalize_pass(diag, qa_msg)
        elif qa_kind == "fail":
            blocked_msg = f"QA Gate failed (사유: {qa_reason})"
            diag["qa_status"] = "failed"
            diag["publish_allowed"] = False
            # The cycle ran QA before us and persisted the failure.
            # Surface it as qa_command_failed (real failed step) when
            # we have feedback evidence on disk; otherwise stale_metadata.
            existing_state = state_at_start or {}
            if existing_state.get("qa_failed_categories"):
                diag["diagnostic_code"] = "qa_command_failed"
            elif runner_report_path.is_file():
                diag["diagnostic_code"] = "qa_command_failed"
            else:
                diag["diagnostic_code"] = "stale_metadata"
            return _qa_finalize_fail(diag, diag["diagnostic_code"], blocked_msg)
        else:
            # missing / skipped / stale → run on-demand QA Gate.
            #
            # We map the qa_kind into the diagnostic vocabulary:
            #   missing  → qa_report_missing_before_run
            #   skipped  → qa_not_run (factory_state says qa never ran)
            #   stale    → stale_metadata (report is older than tree)
            pre_code = {
                "missing": "qa_report_missing_before_run",
                "skipped": "qa_not_run",
                "stale": "stale_metadata",
            }.get(qa_kind, "qa_report_missing_before_run")
            diag["diagnostic_code"] = pre_code
            diag["suggested_action"] = _qa_diagnostic_suggested(pre_code)
            # Snapshot the "before" state immediately so an exception
            # mid-run still leaves us with this evidence.
            _save_qa_diagnostics(diag)
            _log_event(
                f"QA Gate missing before deploy "
                f"({pre_code}): {qa_reason}"
            )
            _progress(
                "validating",
                step="QA Gate 미실행 — 즉시 검증 중",
                log_message=(
                    f"QA Gate missing — 즉시 검증 시작 "
                    f"({pre_code}: {qa_reason})."
                ),
            )
            plan = _qa_targeted_command_plan(allowed)
            plan_summary = ", ".join(s["label"] for s in plan) or "screen/flow/domain only"
            _log_event(f"QA Gate on-demand started: {plan_summary}")
            _progress(
                "validating",
                step="QA Gate started — 검증 명령 실행 중",
                log_message=f"QA Gate started: {plan_summary}",
            )

            try:
                ok, ondemand_msg, qa_dict, step_results = _run_targeted_qa_gate(allowed)
            except Exception as e:  # noqa: BLE001
                # The QA path itself raised — write a fallback report
                # + diagnostic so the UI doesn't claim "QA never ran".
                import traceback as _tb
                tb = _tb.format_exc()
                diag["diagnostic_code"] = "qa_exception_before_report"
                diag["exception_message"] = str(e)[:400]
                diag["stderr_tail"] = _tail_text(tb, 12)
                diag["report_exists_after"] = runner_report_path.is_file()
                if not runner_report_path.is_file():
                    # Best-effort: leave a minimal report so the
                    # dashboard's report_exists flag flips on and the
                    # operator has *something* to read.
                    try:
                        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                        runner_report_path.write_text(
                            "# QA Gate — exception before report\n\n"
                            f"- 발생 시각: {_utc_now_z()}\n"
                            f"- 예외: {e}\n\n"
                            "## traceback (tail)\n\n"
                            f"```\n{_tail_text(tb, 20)}\n```\n",
                            encoding="utf-8",
                        )
                    except OSError:
                        pass
                failure_lines = [
                    "QA Gate raised an exception before writing a report.",
                    f"diagnostic_code: qa_exception_before_report",
                    f"exception: {e}",
                    "traceback tail:",
                    _tail_text(tb, 12) or "(no traceback captured)",
                    f"권장 조치: {_qa_diagnostic_suggested('qa_exception_before_report')}",
                ]
                return _qa_finalize_fail(
                    diag,
                    "qa_exception_before_report",
                    "\n".join(failure_lines),
                )

            revalidate_already_ran = True
            diag["step_results"] = [
                {
                    "key": r["key"], "label": r["label"], "ok": r["ok"],
                    "exit_code": r.get("exit_code"),
                    "command": r.get("command"),
                    "cwd": r.get("cwd"),
                    "stderr_tail": r.get("stderr_tail", "")[:600],
                    "stdout_tail": (r.get("stdout_tail") or "")[:300],
                    "timed_out": bool(r.get("timed_out")),
                    "tool_missing": bool(r.get("tool_missing")),
                    "exception_message": r.get("exception_message"),
                }
                for r in step_results
            ]
            diag["qa_status"] = qa_dict.get("qa_status")
            diag["publish_allowed"] = bool(qa_dict.get("qa_publish_allowed"))

            if ok:
                qa_msg = ondemand_msg
                _progress(
                    "validating",
                    step="검증 통과 — commit 단계로 진행",
                    log_message=(
                        f"QA Gate passed → publish continued — "
                        f"{ondemand_msg or 'all targeted checks ok'}"
                    ),
                )
                diag["diagnostic_code"] = "qa_passed_after_run"
                _qa_finalize_pass(diag, qa_msg)
            else:
                diagnostic_code = _classify_after_run(
                    diag, ok, ondemand_msg, step_results,
                )
                failed_step = next(
                    (r for r in step_results if not r["ok"]),
                    None,
                )
                if failed_step is not None:
                    diag["failed_command"] = failed_step.get("command")
                    diag["exit_code"] = failed_step.get("exit_code")
                    diag["stdout_tail"] = failed_step.get("stdout_tail")
                    diag["stderr_tail"] = failed_step.get("stderr_tail")
                    diag["exception_message"] = failed_step.get("exception_message")
                detail = _format_qa_failure_detail(qa_dict, ondemand_msg)
                cats_text = ", ".join(detail["categories"]) or "unknown"
                cmd_text = (
                    failed_step["command"] if failed_step else "(stage_qa_gate)"
                )
                cwd_text = (
                    failed_step["cwd"] if failed_step and failed_step.get("cwd")
                    else str(REPO_ROOT)
                )
                stderr_text = (
                    failed_step.get("stderr_tail") if failed_step
                    else (detail["reason"] or "")
                )
                file_text = detail["file"] or "(파일 미식별)"
                failure_lines = [
                    "QA Gate failed → publish blocked",
                    f"diagnostic_code: {diagnostic_code}",
                    f"실패 단계: qa_gate ({failed_step['key'] if failed_step else 'stage_qa_gate'})",
                    f"실패 명령: {cmd_text}",
                    f"실행 위치: {cwd_text}",
                    f"실패 카테고리: {cats_text}",
                    f"실패 파일: {file_text}",
                    f"exit_code: {failed_step.get('exit_code') if failed_step else 'n/a'}",
                    "stderr tail:",
                    (stderr_text or "(no stderr captured)"),
                    f"report_exists_before={diag['report_exists_before']}, "
                    f"report_exists_after={runner_report_path.is_file()}",
                    f"권장 조치: {_qa_diagnostic_suggested(diagnostic_code)}",
                ]
                failed_msg = "\n".join(failure_lines)
                return _qa_finalize_fail(diag, diagnostic_code, failed_msg)

    # 5. Re-run the correctness gate before we touch git history.
    # Skipped when an on-demand QA Gate already executed the same
    # build/syntax suite earlier in this call.
    if not revalidate_already_ran:
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
    qa_summary = qa_msg or "QA Gate passed"
    msg = (
        f"QA Gate passed → publish continued — "
        f"published: commit={commit_hash[:8]}, pushed to origin/main "
        f"({qa_summary})"
    )
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
    # a delivery the runner already handled. We surface the dedupe as
    # `stale_command` in command_diagnostics so the UI can explain
    # *why* a click looked like a no-op.
    last = _read_deploy_state()
    if cmd_id and last.get("last_command_id") == cmd_id and last.get("last_finished_at"):
        ok_prev = bool(last.get("last_status_ok"))
        prev_msg = last.get("last_message") or "(no detail)"
        _save_command_diagnostics({
            "last_command": "deploy_to_server",
            "status": "stale",
            "failed_stage": None,
            "diagnostic_code": "stale_command",
            "failed_reason": (
                f"같은 command_id #{cmd_id} 가 이미 처리됨 — 캐시된 결과 반환"
            ),
            "suggested_action": _qa_diagnostic_suggested("stale_command"),
            "occurred_at": _utc_now_z(),
        })
        _log_event(
            f"stale deploy command rejected: cmd #{cmd_id} 이미 처리됨 — "
            f"prev_ok={ok_prev}"
        )
        return ok_prev, f"이미 처리된 deploy command #{cmd_id} — {prev_msg}"

    # 2. Single-flight lock — dedup mass clicks even across processes.
    ok_lock, lock_msg = _deploy_lock_acquire(cmd_id)
    if not ok_lock:
        _save_command_diagnostics({
            "last_command": "deploy_to_server",
            "status": "stale",
            "failed_stage": None,
            "diagnostic_code": "stale_command",
            "failed_reason": lock_msg,
            "suggested_action": _qa_diagnostic_suggested("stale_command"),
            "occurred_at": _utc_now_z(),
        })
        _log_event(f"stale deploy command rejected: {lock_msg}")
        return False, lock_msg

    # 2b. stale_runner — the runner.py on disk has been edited since
    # this process started. The deploy will probably miss whatever the
    # operator just patched. We *don't* abort — the deploy may still
    # succeed — but we surface the warning so the operator can decide.
    is_stale, stale_detail = _runner_is_stale_now()
    if is_stale:
        _save_command_diagnostics({
            "last_command": "deploy_to_server",
            "status": "warn",
            "failed_stage": None,
            "diagnostic_code": "stale_runner",
            "failed_reason": (
                "runner.py 가 부팅 이후 수정됐어요. "
                "이 프로세스는 옛 코드를 실행 중일 수 있습니다."
            ),
            "suggested_action": _qa_diagnostic_suggested("stale_runner"),
            "occurred_at": _utc_now_z(),
            "stale_detail": stale_detail,
        })
        _log_event(
            "stale_runner detected at deploy_to_server entry — "
            "code_mtime_now > code_mtime_at_start"
        )

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
        command_id=cmd_id,
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
    # Mirror the result into factory_state.json so the heartbeat (and
    # a follow-up publish click that re-enters _h_publish_changes)
    # sees the QA Gate as already passed for the current diff and does
    # not waste cycles running it on-demand a second time.
    _merge_factory_state_qa(qa_dict)
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

# How long _h_operator_request will pause the bash factory loop and
# wait for an in-flight cycle to wrap before giving up. Operator
# requests have priority over cycles, but we still respect any cycle
# that's mid-stage so the working tree doesn't tear.
OPERATOR_REQUEST_PAUSE_WAIT_SEC = float(
    os.environ.get("LOCAL_RUNNER_OPERATOR_PAUSE_WAIT_SEC", "300") or "300"
)
# Cap on the operator_fix_state log array so heartbeat metadata stays
# small. The newest WATCHDOG_LOG_CAP-sized window is what the dashboard
# renders.
OPERATOR_REQUEST_LOG_CAP = 30


def _op_emit(
    *,
    kind: str,
    message: str,
    severity: str = "info",
    diagnostic_code: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append a structured event to operator_fix_state.json.log[] and
    drop a parallel line into local_factory.log so System Log via
    log_tail picks it up immediately. Each entry's `message` is
    keyword-classifier-friendly (e.g. "Claude command started",
    "validation passed", "git push completed") so the dashboard
    automatically buckets it into Claude / Build / Git / Error.
    """
    cur = _read_operator_fix_state() or {}
    log = list(cur.get("log") or [])
    entry = {
        "at": _utc_now_z(),
        "kind": kind,
        "message": message,
        "severity": severity,
        "diagnostic_code": diagnostic_code,
    }
    if extra:
        entry["payload"] = extra
    log.append(entry)
    cur["log"] = log[-OPERATOR_REQUEST_LOG_CAP:]
    _save_operator_fix_state(cur)
    _log_event(f"operator_request · {kind} · {message}")


def _op_save_diagnostics(
    diagnostic_code: str,
    *,
    failed_stage: str | None,
    failed_reason: str,
    suggested_action: str,
    extra: dict | None = None,
) -> None:
    """Mirror command_diagnostics.json with the structured codes the
    spec calls out (factory_running_blocked / claude_not_started /
    claude_process_failed / validation_failed / git_push_failed /
    no_changes). The dashboard's command_diagnostics block already
    surfaces these without further changes."""
    payload = {
        "last_command": "operator_request",
        "status": "failed" if diagnostic_code != "no_changes" else "warning",
        "failed_stage": failed_stage,
        "diagnostic_code": diagnostic_code,
        "failed_reason": failed_reason,
        "suggested_action": suggested_action,
        "occurred_at": _utc_now_z(),
    }
    if extra:
        payload.update(extra)
    _save_command_diagnostics(payload)


def _wait_for_factory_idle(timeout_sec: float) -> tuple[bool, str]:
    """Poll factory_state.json until status leaves "running" or the
    timeout expires. Returns (ok, last_status). The caller has
    already written PAUSE_MARKER so the bash loop won't start a new
    cycle — we're only waiting for the current one to wrap.

    Returns ok=True even when status is "skipped"/"docs_only"/"failed":
    those are all "cycle wrapped, working tree settled". Only a
    timeout returns False.
    """
    poll_interval = 5.0
    waited = 0.0
    last_status = ""
    while waited < timeout_sec:
        st = (_read_factory_state() or {}).get("status") or ""
        last_status = st
        if st != "running":
            return True, st
        time.sleep(poll_interval)
        waited += poll_interval
    return False, last_status


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
    """Autonomous operator-request handler.

    Operator requests are user-driven commands and have priority over
    the autonomous factory cycle. Flow:

      1. Single-flight check (refuse a parallel request).
      2. If a cycle is RUNNING, write the runner-managed pause marker,
         flip API continuous_mode=false, and wait up to
         OPERATOR_REQUEST_PAUSE_WAIT_SEC for the cycle to wrap. Only
         report `factory_running_blocked` if the wait times out — never
         simply because a cycle is in flight.
      3. Persist the request, invoke Claude Code, and emit lifecycle
         events to operator_fix_state.json.log[] at every step
         (operator_request_received → claude_command_started →
         claude_command_completed / failed → validation_started →
         validation_passed / failed → commit_created → push_completed).
      4. On any failure path, write structured command_diagnostics with
         a diagnostic_code from {factory_running_blocked,
         claude_not_started, claude_process_failed, validation_failed,
         git_push_failed, no_changes}.
    """
    prompt_raw = (payload or {}).get("prompt") or (payload or {}).get("request") or ""
    if not isinstance(prompt_raw, str) or not prompt_raw.strip():
        return False, "operator_request payload.prompt가 비어있습니다."

    auto_cp_raw = (payload or {}).get("auto_commit_push", True)
    auto_commit_push = bool(auto_cp_raw) if not isinstance(auto_cp_raw, str) \
        else auto_cp_raw.strip().lower() in {"true", "1", "yes", "on"}

    # Single-flight: refuse a second operator_request while the first
    # is still in-flight. A double-click on the dashboard would
    # otherwise queue two Claude CLI invocations against the same
    # working tree — the second would race the first's edits.
    prior_fix = _read_operator_fix_state() or {}
    if prior_fix.get("status") == "running":
        msg = (
            "이미 operator_request 가 실행 중입니다 — already_running. "
            "이전 요청이 끝난 뒤 다시 시도하세요."
        )
        _save_operator_fix_state({"last_message": msg})
        return False, msg

    started_at = _utc_now_z()
    request_redacted, redactions = _redact_request_text(prompt_raw)
    truncated = request_redacted.strip()[:OPERATOR_REQUEST_MAX_CHARS]

    # Reset operator_fix_state to "running" with an empty event log so
    # the dashboard renders a fresh timeline for this request.
    _save_operator_fix_state({
        "status": "running",
        "request_path": str(OPERATOR_REQUEST_FILE),
        "started_at": started_at,
        "allow_publish": auto_commit_push,
        "priority": "operator_request",
        "redactions": redactions,
        "publish_status": "not_requested",
        "last_message": "operator_request 수신",
        "changed_files": [],
        "log": [],
    })
    _op_emit(
        kind="operator_request_received",
        message="operator request received — Claude 작업 지시 수신",
        extra={
            "auto_commit_push": auto_commit_push,
            "redactions": redactions,
            "request_chars": len(truncated),
        },
    )

    # Step A — make sure the factory cycle is not currently writing to
    # the working tree. operator_request has priority over the cycle:
    # we pause the bash loop AND flip API continuous_mode off, then
    # wait for any in-flight cycle to wrap. Only a timeout earns the
    # factory_running_blocked diagnostic — a cycle that wraps within
    # the wait is treated as a clean handoff.
    state = _read_factory_state() or {}
    if state.get("status") == "running":
        _op_emit(
            kind="factory_pause_requested",
            message="factory 사이클 실행 중 — pause + continuous_mode=false 적용 후 대기",
            severity="warn",
        )
        _set_continuous_pause(reason="operator_request priority")
        _admin_request("POST", "/factory/continuous", {"enabled": False})
        ok_wait, last_status = _wait_for_factory_idle(
            OPERATOR_REQUEST_PAUSE_WAIT_SEC
        )
        if not ok_wait:
            msg = (
                f"factory 사이클이 {int(OPERATOR_REQUEST_PAUSE_WAIT_SEC)}s 안에 "
                f"끝나지 않음 (last_status={last_status}) — operator_request 보류"
            )
            _op_save_diagnostics(
                "factory_running_blocked",
                failed_stage="pause_and_wait",
                failed_reason=msg,
                suggested_action=(
                    "factory 사이클이 끝난 뒤 다시 시도하거나, 운영자가 직접 "
                    "factory를 stop 하세요."
                ),
            )
            _op_emit(
                kind="operator_request_blocked",
                message=msg,
                severity="error",
                diagnostic_code="factory_running_blocked",
            )
            _save_operator_fix_state({
                "status": "failed",
                "publish_status": "blocked",
                "last_message": msg,
            })
            return False, msg
        _op_emit(
            kind="factory_pause_confirmed",
            message=f"factory 사이클 wrap 완료 (status={last_status}) — operator_request 진행",
        )

    # Step B — write request md and prepare prompt.
    _write_operator_request_md(
        truncated,
        allow_publish=auto_commit_push,
        priority="normal",
        redactions=redactions,
    )

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

    # Snapshot before — diff against this to know what Claude touched.
    before_changed = set(_git_changed_files())

    _op_emit(
        kind="claude_command_started",
        message=f"Claude command started (timeout={int(timeout_sec)}s)",
        extra={"argv0": argv[0] if argv else None},
    )

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
        _op_save_diagnostics(
            "claude_process_failed",
            failed_stage="claude_subprocess",
            failed_reason=msg,
            suggested_action=(
                "FACTORY_CLAUDE_OPERATOR_REQUEST_TIMEOUT_SEC 를 늘리거나, "
                "요청 본문을 더 작게 쪼개서 다시 시도"
            ),
        )
        _op_emit(
            kind="claude_command_failed",
            message=f"Claude command failed — {msg}",
            severity="error",
            diagnostic_code="claude_process_failed",
        )
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "blocked",
            "last_message": msg,
        })
        return False, msg
    except FileNotFoundError as e:
        msg = f"claude CLI 실행 실패: {e}"
        _op_save_diagnostics(
            "claude_not_started",
            failed_stage="claude_subprocess",
            failed_reason=msg,
            suggested_action=(
                "claude 바이너리가 PATH에 있는지 확인하거나, "
                "LOCAL_RUNNER_CLAUDE_COMMAND 환경변수에 절대 경로를 지정하세요."
            ),
        )
        _op_emit(
            kind="claude_command_failed",
            message=f"Claude command failed — {msg}",
            severity="error",
            diagnostic_code="claude_not_started",
        )
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
        _op_save_diagnostics(
            "claude_process_failed",
            failed_stage="claude_subprocess",
            failed_reason=msg,
            suggested_action=(
                "Claude 출력 끝부분의 오류 메시지를 확인하고, 권한/디스크/네트워크 "
                "오류면 해당 항목 수정 후 재시도"
            ),
            extra={"stderr_tail": _tail_text(r.stderr, 12)},
        )
        _op_emit(
            kind="claude_command_failed",
            message=f"Claude command failed — returncode={r.returncode}",
            severity="error",
            diagnostic_code="claude_process_failed",
            extra={"stderr_tail": _tail_text(r.stderr, 12)},
        )
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "blocked",
            "changed_files": new_or_changed,
            "last_message": msg,
        })
        return False, msg

    _op_emit(
        kind="claude_command_completed",
        message=f"Claude command completed — 변경 파일 {len(new_or_changed)}개",
        extra={"changed_files": new_or_changed[:30]},
    )

    # Step C — runner-side validation. Cheap: py_compile on changed
    # *.py files. Web/JS validation stays Claude's responsibility (full
    # npm build is too slow to run synchronously here).
    _op_emit(
        kind="validation_started",
        message="validation started — runner py_compile pass",
    )
    py_targets = [f for f in new_or_changed if f.endswith(".py")]
    validation_failed_files: list[str] = []
    validation_message = ""
    for rel in py_targets:
        abs_path = REPO_ROOT / rel
        if not abs_path.is_file():
            continue
        try:
            r_v = subprocess.run(
                [sys.executable, "-m", "py_compile", str(abs_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            validation_failed_files.append(rel)
            validation_message += f"\n{rel}: validation invocation failed — {e}"
            continue
        if r_v.returncode != 0:
            validation_failed_files.append(rel)
            validation_message += (
                f"\n{rel}:\n"
                f"{_tail_text((r_v.stdout or '') + (r_v.stderr or ''), 6)}"
            )

    if validation_failed_files:
        _op_save_diagnostics(
            "validation_failed",
            failed_stage="post_claude_validation",
            failed_reason=(
                f"py_compile 실패 ({len(validation_failed_files)}개): "
                + ", ".join(validation_failed_files[:5])
            ),
            suggested_action="해당 파일의 syntax 오류를 수정한 뒤 다시 operator_request 실행",
            extra={"failed_files": validation_failed_files},
        )
        _op_emit(
            kind="validation_failed",
            message=(
                f"validation failed — py_compile 실패 "
                f"{len(validation_failed_files)}개"
            ),
            severity="error",
            diagnostic_code="validation_failed",
            extra={"failed_files": validation_failed_files},
        )
        _save_operator_fix_state({
            "status": "failed",
            "publish_status": "blocked",
            "changed_files": new_or_changed,
            "last_message": (
                f"runner-side py_compile 실패 ({len(validation_failed_files)}개)"
                + (validation_message[:400] if validation_message else "")
            ),
        })
        return False, "validation_failed"

    _op_emit(
        kind="validation_passed",
        message=(
            f"validation passed — py_compile {len(py_targets)}개 파일 통과"
            if py_targets
            else "validation passed — Python 변경 없음, 추가 검증 생략"
        ),
        severity="success",
    )

    # Step D — parse Claude's structured tail. Claude itself drives
    # commit/push; we observe the markdown report and emit
    # commit_created / push_completed events accordingly.
    pushed = bool(re.search(r"pushed to main\s*[:：]\s*yes", out, re.IGNORECASE))
    commit_match = re.search(
        r"commit\s+hash\s*[:：]\s*([0-9a-fA-F]{7,40})", out
    )
    commit_short = commit_match.group(1)[:8] if commit_match else None

    # Strict status parser. The earlier code did a bare substring match
    # on "aborted" against the whole output, which falsely tripped when
    # Claude echoed the prompt template's enum
    # (`applied | committed | pushed | aborted | failed`) verbatim. We
    # instead pull the FIRST non-blank line after the `## 상태` heading
    # and reject any pipe-delimited enum line.
    parsed_status = ""
    parsed_summary = ""
    m_status = re.search(r"##\s*상태\s*\n([^\n]+)", out)
    if m_status:
        line = m_status.group(1).strip().lower()
        # If the line still contains the prompt's enum separator, ignore it.
        if "|" in line and len(line) > 20:
            line = ""
        # Strip leading punctuation Claude sometimes leaves
        # (e.g. "- applied" or "* applied").
        line = re.sub(r"^[\-\*\•\s]+", "", line).strip()
        parsed_status = line
    m_summary = re.search(r"##\s*요약\s*\n([^\n#]+(?:\n[^\n#]+)?)", out)
    if m_summary:
        parsed_summary = m_summary.group(1).strip()

    # Detect "this request was a no-op smoke / ping by design" — checks
    # both the original request text AND Claude's summary line. Useful
    # so a "테스트 해볼게요" message doesn't get classified as
    # qa_failed when Claude correctly answered "no code change needed".
    NOOP_SIGNALS = (
        "테스트", "확인용", "동작 확인", "smoke", "ping", "alive",
        "수신 확인", "수신·해석", "코드 변경 없이", "응답 확인",
        "no-op", "noop", "작업 없음", "작업 안 함",
    )
    request_lc = (prompt_raw or "").lower()
    summary_lc = parsed_summary.lower()
    noop_intent = (
        any(sig.lower() in request_lc for sig in NOOP_SIGNALS)
        or any(sig.lower() in summary_lc for sig in NOOP_SIGNALS)
    )
    aborted_strict = parsed_status in {"aborted", "거부"}

    if commit_short:
        _op_emit(
            kind="commit_created",
            message=f"commit created — {commit_short}",
            severity="success",
            extra={"commit_hash": commit_short},
        )
    if pushed:
        _op_emit(
            kind="push_completed",
            message="git push completed — origin/main",
            severity="success",
        )

    # Step E — final classification. New status model:
    #   noop_success          — Claude understood the test/ping and
    #                            intentionally produced no code change
    #   no_changes            — Claude returned with no file change but
    #                            no clear noop intent (info, not failure)
    #   no_code_change_failed — operator asked for a code change but no
    #                            changed_files materialised
    #   applied               — code changes landed without a push
    #   published             — code changes landed AND pushed to main
    #   qa_failed             — Claude itself reported aborted (e.g.
    #                            QA / safety gate refused)
    #   git_push_failed       — auto_commit_push=true but no push happened
    #                            and Claude didn't say aborted
    if not new_or_changed:
        if noop_intent or parsed_status in {"applied", "noop", "noop_success"}:
            # Spec rule A: test/ping requests with no code change must
            # not land as qa_failed. Mark them noop_success so the
            # watchdog skips the failure-loop branch entirely.
            _op_save_diagnostics(
                "operator_noop_success",
                failed_stage=None,
                failed_reason=None,
                suggested_action="조치 필요 없음 — 요청은 정상 수신/해석되었습니다.",
            )
            _op_emit(
                kind="operator_request_noop_success",
                message="operator request no-op completed — 코드 변경 불필요 요청 정상 처리",
                severity="info",
                diagnostic_code="operator_noop_success",
            )
            publish_status = "not_requested"
            final_status = "noop_success"
        elif aborted_strict:
            # Claude's `## 상태` line literally says aborted/거부 AND we
            # don't see a noop intent — treat as a real refusal.
            _op_save_diagnostics(
                "operator_aborted",
                failed_stage="claude_decision",
                failed_reason=(
                    parsed_summary
                    or "Claude 가 요청을 거부했습니다 (## 상태 = aborted)."
                ),
                suggested_action=(
                    "Claude 응답의 ## 거부 사유 확인 후 요청 본문을 보강해 재시도"
                ),
            )
            _op_emit(
                kind="operator_request_aborted",
                message="operator request aborted — Claude 가 요청을 거부",
                severity="error",
                diagnostic_code="operator_aborted",
            )
            publish_status = "blocked"
            final_status = "qa_failed"
        else:
            # Spec rule B: real code change was expected but none landed.
            _op_save_diagnostics(
                "operator_no_code_change",
                failed_stage="claude_apply",
                failed_reason="Claude 가 working tree 를 변경하지 않았습니다.",
                suggested_action=(
                    "실제 수정 대상 파일과 기대 변경을 포함해 다시 요청하세요. "
                    "테스트/확인 메시지였다면 요청 본문에 명시하세요."
                ),
            )
            _op_emit(
                kind="operator_request_no_code_change_failed",
                message="operator request no code change failed — 변경 요청이 실제 코드 변경으로 이어지지 못함",
                severity="warning",
                diagnostic_code="operator_no_code_change",
            )
            publish_status = "blocked"
            final_status = "no_code_change_failed"
    elif aborted_strict:
        publish_status = "blocked"
        final_status = "qa_failed"
    elif auto_commit_push and not pushed:
        # Claude was authorized to push but didn't. Treat as a soft
        # git_push_failed signal so the operator knows the change is
        # sitting in the working tree without being committed.
        _op_save_diagnostics(
            "git_push_failed",
            failed_stage="claude_commit_push",
            failed_reason=(
                "auto_commit_push=true 였지만 Claude 가 main 으로 push 하지 않았습니다."
            ),
            suggested_action=(
                "Claude 응답의 거부 사유를 확인하고 publish_changes 로 직접 push "
                "하거나, 변경을 수동 검토"
            ),
        )
        _op_emit(
            kind="git_push_failed",
            message="git push failed — Claude 가 push 하지 않음",
            severity="error",
            diagnostic_code="git_push_failed",
        )
        publish_status = "blocked"
        final_status = "push_failed"
    elif pushed:
        publish_status = "published"
        final_status = "published"
    else:
        publish_status = "not_requested"
        final_status = "applied"

    _save_operator_fix_state({
        "status": final_status,
        "publish_status": publish_status,
        "changed_files": new_or_changed,
        "last_commit_hash": commit_short,
        "last_message": (out[-400:] if out else "Claude 응답 없음"),
    })

    summary = (
        f"operator_request 완료 (status={final_status}, "
        f"changed_files={len(new_or_changed)}, "
        f"commit={commit_short or '—'}, push={'yes' if pushed else 'no'})"
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


# ---------------------------------------------------------------------------
# Per-command-name dedupe — short-circuit duplicate test_check /
# build_check / deploy_to_server requests that pile up from rapid
# clicking. The runner is single-threaded so true concurrency isn't an
# issue, but a queue full of the same command wastes the polling slot.
#
# DEDUPE_NAMES are the commands we actively dedupe. Other commands
# (start_factory / pause_factory / restart_runner / operator_*) are
# left alone — they're either rare or have their own idempotency.
# ---------------------------------------------------------------------------

DEDUPE_WINDOW_SEC = 30.0
DEDUPE_NAMES: frozenset[str] = frozenset({
    "build_check",
    "test_check",
    "deploy_to_server",
})

_LAST_COMMAND_RESULT: dict[str, dict] = {}
_INFLIGHT_COMMAND: dict | None = None


def _is_duplicate_recent_command(name: str, cid: int) -> bool:
    if name not in DEDUPE_NAMES:
        return False
    # Same command name finished within DEDUPE_WINDOW_SEC and reported
    # success → treat the new claim as a duplicate. Failures don't
    # dedupe (the operator might be retrying after fixing whatever
    # broke).
    prev = _LAST_COMMAND_RESULT.get(name)
    if not prev:
        return False
    if prev.get("cid") == cid:
        # Same exact command id — that's the deploy idempotency case;
        # let the handler answer with its own cached result.
        return False
    if prev.get("ok") is not True:
        return False
    finished = float(prev.get("at") or 0.0)
    return (time.time() - finished) < DEDUPE_WINDOW_SEC


def _mark_command_inflight(name: str, cid: int) -> None:
    global _INFLIGHT_COMMAND
    _INFLIGHT_COMMAND = {"name": name, "cid": cid, "started_at": time.time()}


def _record_command_result(name: str, cid: int, ok: bool, message: str) -> None:
    global _INFLIGHT_COMMAND
    _LAST_COMMAND_RESULT[name] = {
        "cid": cid,
        "ok": ok,
        "message": message,
        "at": time.time(),
    }
    _INFLIGHT_COMMAND = None


# ---------------------------------------------------------------------------
# Factory Watchdog
#
# Periodic self-monitor that reads the same state files the dashboard
# reads (factory_state.json, factory_publish.json, operator_fix_state.json,
# qa_diagnostics.json, command_diagnostics.json), classifies the factory
# health into a diagnostic_code, and — when the operator opted in via
# FACTORY_WATCHDOG_ENABLED=true — performs a small set of *safe* auto
# repairs (pause factory, clear stale deploy lock, reset deploy_progress,
# rerun smoke test).
#
# Bounded by:
#   - per-diagnostic cooldown (FACTORY_WATCHDOG_REPAIR_COOLDOWN_SEC,
#     default 600s) so the same fix doesn't loop every tick.
#   - max-repeat (FACTORY_WATCHDOG_MAX_REPEAT, default 3) — after that
#     the watchdog status flips to "broken" and refuses further auto
#     repairs until an operator clears it.
#   - smoke-test cooldown (WATCHDOG_SMOKE_TEST_COOLDOWN_SEC, 30 min) so
#     the runner can't pile commits onto the repo.
#
# Hard exclusions (never auto-run, only suggested to the operator):
#   git reset --hard, git clean, force push, .env / nginx / systemd /
#   DB / production-file edits, arbitrary code edits.
# ---------------------------------------------------------------------------

import threading

_WATCHDOG_THREAD: "threading.Thread | None" = None
_WATCHDOG_STOP = threading.Event()
_WATCHDOG_LAST_REPAIR_AT: dict[str, float] = {}
_WATCHDOG_REPEAT_COUNT: dict[str, int] = {}
_WATCHDOG_LAST_SMOKE_AT: float = 0.0
_WATCHDOG_LOG_LOCK = threading.Lock()


def _watchdog_env_enabled() -> bool:
    v = os.environ.get("FACTORY_WATCHDOG_ENABLED", "false").strip().lower()
    return v in {"true", "1", "yes", "on"}


def _watchdog_interval_sec() -> float:
    try:
        return max(15.0, float(os.environ.get("FACTORY_WATCHDOG_INTERVAL_SEC", "120")))
    except ValueError:
        return 120.0


def _watchdog_stuck_command_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("FACTORY_WATCHDOG_STUCK_COMMAND_SEC", "600")))
    except ValueError:
        return 600.0


def _watchdog_repair_cooldown_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("FACTORY_WATCHDOG_REPAIR_COOLDOWN_SEC", "600")))
    except ValueError:
        return 600.0


def _watchdog_max_repeat() -> int:
    try:
        return max(1, int(os.environ.get("FACTORY_WATCHDOG_MAX_REPEAT", "3")))
    except ValueError:
        return 3


def _read_watchdog_state() -> dict:
    if not WATCHDOG_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(WATCHDOG_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_watchdog_state(state: dict) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        WATCHDOG_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to write watchdog state: {e}\n")


def _watchdog_log_event(
    state: dict,
    *,
    kind: str,
    message: str,
    severity: str = "info",
    diagnostic_code: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Append a watchdog event to both .runtime/local_factory.log (for
    operators tailing the file) and the in-memory log array on the state
    dict. The state dict is the caller's working copy — they persist
    after this returns."""
    now = _utc_now_z()
    entry = {
        "at": now,
        "kind": kind,
        "message": message,
        "severity": severity,
        "diagnostic_code": diagnostic_code,
    }
    if extra:
        entry["payload"] = extra
    log = list(state.get("log") or [])
    log.append(entry)
    state["log"] = log[-WATCHDOG_LOG_CAP:]
    # Also drop into local_factory.log so System Log via _log_tail picks
    # it up even before the heartbeat reaches the API.
    _log_event(f"watchdog · {kind} · {message}")
    return entry


# --- Diagnosis helpers ------------------------------------------------------


def _watchdog_command_inflight_age_sec() -> float | None:
    """Returns seconds the current command has been running, or None if
    no command is in flight."""
    cur = _INFLIGHT_COMMAND
    if not cur:
        return None
    started = float(cur.get("started_at") or 0.0)
    if started <= 0:
        return None
    return max(0.0, time.time() - started)


def _watchdog_recent_deploy_failures(publish_state: dict) -> int:
    dp = publish_state.get("deploy_progress") or {}
    prev = list(dp.get("previous_attempts") or [])
    failed = sum(1 for a in prev if (a or {}).get("status") == "failed")
    if (dp.get("status") == "failed"):
        failed += 1
    return failed


def _watchdog_recent_operator_failures(operator_state: dict) -> int:
    """The runner only persists the *last* operator_request outcome, so
    we can't count history straight from disk. Use the in-memory
    _LAST_COMMAND_RESULT for the operator_request slot when present, and
    treat a current `failed` row as 1."""
    n = 0
    if (operator_state.get("status") in {"failed", "qa_failed"}):
        n += 1
    last = _LAST_COMMAND_RESULT.get("operator_request")
    if last and last.get("ok") is False:
        # Don't double-count if it's the same as operator_state — but we
        # can't tell for sure. One fail is enough to surface a warning.
        pass
    return n


def _watchdog_cycle_loop_count(cycle_state: dict, kinds: set[str]) -> int:
    """How many of the trailing cycle_log entries match `kinds`."""
    log = cycle_state.get("cycle_log") or []
    if not isinstance(log, list):
        return 0
    n = 0
    for entry in reversed(log):
        if not isinstance(entry, dict):
            break
        if entry.get("kind") in kinds:
            n += 1
        else:
            break
    return n


def _watchdog_factory_idle_sec(cycle_state: dict) -> float | None:
    """Seconds since the factory state was last updated. Returns None
    when there's no timestamp to compare against."""
    iso = cycle_state.get("updated_at") or cycle_state.get("finished_at")
    if not iso or not isinstance(iso, str):
        return None
    try:
        ts = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return max(0.0, (datetime.utcnow() - ts).total_seconds())


def _watchdog_diagnose() -> dict:
    """Read the on-disk state files and classify the factory's health
    into one diagnostic_code. Returns the structured result the spec
    requires:

      {
        "diagnostic_code": str,
        "severity": "info" | "warning" | "error",
        "root_cause": str,
        "evidence": list[str],
        "safe_auto_fix_available": bool,
        "suggested_action": str,
      }
    """
    cycle_state = _read_factory_state() or {}
    publish_state = _read_publish_state() or {}
    operator_state = _read_operator_fix_state() or {}
    qa_diag = _read_qa_diagnostics() or {}
    cmd_diag = _read_command_diagnostics() or {}

    evidence: list[str] = []

    # 1. current_command_stuck — the runner is sitting on a single
    # command well past the stuck threshold. Detected only when the
    # watchdog runs in a separate thread; in main-loop mode, the global
    # is None while _execute() blocks.
    age = _watchdog_command_inflight_age_sec()
    if age is not None and age > _watchdog_stuck_command_sec():
        cur = _INFLIGHT_COMMAND or {}
        return {
            "diagnostic_code": "current_command_stuck",
            "severity": "error",
            "root_cause": (
                f"명령 `{cur.get('name')}` (cid={cur.get('cid')}) 가 "
                f"{int(age)}s 째 진행 중 — stuck threshold 초과"
            ),
            "evidence": [f"inflight_age_sec={int(age)}"],
            "safe_auto_fix_available": True,
            "suggested_action": (
                "deploy_progress 를 failed 로 정리하고 deploy lock 해제. "
                "필요 시 운영자가 runner 재시작."
            ),
        }

    # 2. duplicate_deploy_commands — same deploy_to_server cid landing
    # within the dedupe window OR multiple recent attempts piling up.
    dp = publish_state.get("deploy_progress") or {}
    prev_attempts = list(dp.get("previous_attempts") or [])
    if dp.get("is_active") and len(prev_attempts) >= 2:
        recent_active_age = (
            time.time()
            - (datetime.strptime(
                (dp.get("started_at") or _utc_now_z())[:19],
                "%Y-%m-%dT%H:%M:%S",
            ).timestamp())
        ) if dp.get("started_at") else 0
        if recent_active_age > _watchdog_stuck_command_sec():
            evidence.append(f"deploy_progress.is_active=true age={int(recent_active_age)}s")
            evidence.append(f"previous_attempts={len(prev_attempts)}")
            return {
                "diagnostic_code": "duplicate_deploy_commands",
                "severity": "warning",
                "root_cause": (
                    "deploy_progress 가 활성 상태로 멈춰 있고 직전 시도가 "
                    f"{len(prev_attempts)}건 — 큐에 중복 명령이 끼었을 가능성"
                ),
                "evidence": evidence,
                "safe_auto_fix_available": True,
                "suggested_action": "deploy lock 해제 + deploy_progress 초기화",
            }

    # 3. deploy_failed_repeatedly — last 3 deploy attempts failed.
    failed_recent = _watchdog_recent_deploy_failures(publish_state)
    if failed_recent >= 3:
        evidence.append(f"recent_deploy_failures={failed_recent}")
        evidence.append(
            f"last_failed_reason={dp.get('failed_reason') or 'unknown'}"
        )
        return {
            "diagnostic_code": "deploy_failed_repeatedly",
            "severity": "error",
            "root_cause": (
                f"최근 deploy 시도 {failed_recent}건 실패 — 사이클을 일시정지하고 "
                "운영자가 원인 확인 필요"
            ),
            "evidence": evidence,
            "safe_auto_fix_available": True,
            "suggested_action": "factory pause + deploy lock 해제. 위험 조치는 운영자 확인 필요.",
        }

    # 4. operator_request health.
    #
    # Failure states (qa_failed / validation_failed / no_code_change_failed
    # / push_failed / git_failed / failed) deserve a watchdog response.
    # noop_success / no_changes / applied / published are healthy and
    # must NOT trigger the operator_request_failed_repeatedly loop.
    #
    # We also detect stale_state_mismatch — operator_status=qa_failed
    # while last_message clearly says "상태 applied / noop / published".
    # That used to come from the bare-substring "aborted" misclassifier
    # that's now fixed at the source, but the watchdog still needs to
    # heal already-persisted bad state files without manual intervention.
    OPERATOR_FAILURE_STATES = {
        "failed",
        "qa_failed",
        "validation_failed",
        "no_code_change_failed",
        "push_failed",
        "git_failed",
    }
    op_status = (operator_state.get("status") or "").strip()
    last_msg = operator_state.get("last_message") or ""
    last_msg_lc = last_msg.lower()
    healthy_keywords = (
        "상태 applied", "상태 noop", "상태 published",
        "noop completed", "noop_success",
        "코드 변경 없이",
    )
    indicates_healthy = any(k in last_msg_lc for k in healthy_keywords)

    if op_status in OPERATOR_FAILURE_STATES and indicates_healthy:
        # The status row says failed, but the last_message body shows
        # the request actually succeeded as a no-op. That's the exact
        # bug pattern the spec is fixing. Surface as stale_state_mismatch
        # so the safe-repair branch can normalize the row.
        evidence.append(f"operator_status={op_status}")
        evidence.append(f"last_message_excerpt={last_msg[:160]}")
        return {
            "diagnostic_code": "stale_state_mismatch",
            "severity": "info",
            "root_cause": (
                f"operator_request 상태는 {op_status} 인데 마지막 메시지는 "
                "정상 처리(no-op / applied)로 보임 — stale 상태 정리 필요."
            ),
            "evidence": evidence,
            "safe_auto_fix_available": True,
            "suggested_action": "operator_fix_state.status 를 noop_success 로 정규화",
        }

    if op_status in OPERATOR_FAILURE_STATES:
        evidence.append(f"operator_status={op_status}")
        evidence.append(f"operator_last_message={last_msg[:120]}")
        # Detect "claude not started" — a sub-case worth its own code.
        if (
            "claude" in last_msg_lc
            and ("실행 실패" in last_msg or "FileNotFound" in last_msg
                 or "not found" in last_msg_lc)
        ):
            return {
                "diagnostic_code": "claude_not_started",
                "severity": "error",
                "root_cause": "Claude CLI 가 실행되지 않음 — 환경 변수 / PATH 확인 필요",
                "evidence": evidence,
                "safe_auto_fix_available": False,
                "suggested_action": (
                    "LOCAL_RUNNER_CLAUDE_COMMAND 환경 변수 / claude CLI 설치 상태 확인. "
                    "smoke test 로 commit/push 파이프라인은 별도 검증 가능."
                ),
            }
        return {
            "diagnostic_code": "operator_request_failed_repeatedly",
            "severity": "warning",
            "root_cause": (
                f"operator_request 가 최근 {op_status} 상태로 남아 있음"
            ),
            "evidence": evidence,
            "safe_auto_fix_available": True,
            "suggested_action": "smoke test 실행으로 commit/push 파이프라인 점검",
        }

    # 5. qa_gate_stuck — qa_diagnostics says the gate is broken in a
    # specific way (path mismatch / report missing after run).
    qa_code = (qa_diag.get("diagnostic_code") or "").strip()
    if qa_code in {
        "qa_report_missing_after_run",
        "qa_report_path_mismatch",
        "qa_exception_before_report",
    }:
        evidence.append(f"qa_diagnostic_code={qa_code}")
        if qa_diag.get("failed_command"):
            evidence.append(f"failed_command={qa_diag.get('failed_command')}")
        return {
            "diagnostic_code": "qa_gate_stuck",
            "severity": "error",
            "root_cause": f"QA Gate diagnostic={qa_code}",
            "evidence": evidence,
            "safe_auto_fix_available": False,
            "suggested_action": (
                qa_diag.get("suggested_action")
                or "QA Gate 경로/환경 점검 — runner.py 와 cycle.py 의 RUNTIME 경로 일치 여부 확인"
            ),
        }

    # 6. qa_report_missing_repeatedly — QA decided multiple times that
    # the report was missing before run.
    if qa_code == "qa_report_missing_before_run":
        evidence.append(f"qa_diagnostic_code={qa_code}")
        return {
            "diagnostic_code": "qa_report_missing_repeatedly",
            "severity": "warning",
            "root_cause": "QA report가 매번 누락 — cycle.py 의 stage_qa_gate 가 실행되지 않거나 경로 불일치",
            "evidence": evidence,
            "safe_auto_fix_available": False,
            "suggested_action": "factory_state.json 의 qa_status 확인, 필요 시 cycle 재시작",
        }

    # 7. planning_only_loop / no_code_change_loop — cycle_log shows
    # repeated planning-only or no-code-change cycles.
    planning_n = _watchdog_cycle_loop_count(cycle_state, {"cycle_planning_only"})
    if planning_n >= 3:
        evidence.append(f"trailing_planning_only_cycles={planning_n}")
        return {
            "diagnostic_code": "planning_only_loop",
            "severity": "warning",
            "root_cause": f"최근 사이클 {planning_n}회 연속 planning_only — 코드 변경이 발생하지 않음",
            "evidence": evidence,
            "safe_auto_fix_available": True,
            "suggested_action": "factory pause + 운영자가 product_planner_report.md / pm_decision.md 확인",
        }
    nochange_n = _watchdog_cycle_loop_count(
        cycle_state,
        {"cycle_produced_no_code_change", "cycle_produced_docs_only"},
    )
    if nochange_n >= 3:
        evidence.append(f"trailing_no_code_change_cycles={nochange_n}")
        return {
            "diagnostic_code": "no_code_change_loop",
            "severity": "warning",
            "root_cause": f"최근 사이클 {nochange_n}회 연속 코드 변경 없음",
            "evidence": evidence,
            "safe_auto_fix_available": True,
            "suggested_action": "factory pause + 운영자가 implementation_ticket / claude_apply 결과 확인",
        }

    # 8. github_actions_not_triggered — last deploy reached
    # actions_triggered but failed_at_status indicates push didn't go
    # through.
    if dp.get("status") == "failed" and dp.get("failed_stage") == "actions_dispatch":
        evidence.append("deploy_progress.failed_stage=actions_dispatch")
        return {
            "diagnostic_code": "github_actions_not_triggered",
            "severity": "warning",
            "root_cause": "deploy push 는 됐지만 GitHub Actions trigger 가 실패",
            "evidence": evidence,
            "safe_auto_fix_available": False,
            "suggested_action": "GitHub repo 의 Actions 탭 확인 — 워크플로우 활성 상태 / 최근 실행 점검",
        }

    # 9. git_dirty_unpublished — working tree dirty but no recent push.
    dirty = _git_changed_files()
    if dirty:
        last_push_at_iso = publish_state.get("last_push_at")
        last_push_age = None
        if last_push_at_iso:
            try:
                ts = datetime.strptime(last_push_at_iso[:19], "%Y-%m-%dT%H:%M:%S")
                last_push_age = (datetime.utcnow() - ts).total_seconds()
            except ValueError:
                last_push_age = None
        if last_push_age is None or last_push_age > 6 * 3600:
            evidence.append(f"dirty_files={len(dirty)}")
            evidence.append(f"last_push_age_sec={int(last_push_age) if last_push_age else 'never'}")
            return {
                "diagnostic_code": "git_dirty_unpublished",
                "severity": "info",
                "root_cause": (
                    f"working tree 에 {len(dirty)}개 변경 파일이 있고 최근 push 가 없음"
                ),
                "evidence": evidence,
                "safe_auto_fix_available": False,
                "suggested_action": "운영자가 변경 내용 검토 후 publish_changes 실행 — 자동 git 조작 금지",
            }

    # 10. factory_idle_too_long — cycle hasn't moved in a long time
    # and is not actively running.
    idle_sec = _watchdog_factory_idle_sec(cycle_state)
    if (
        cycle_state.get("status") not in {"running", "paused"}
        and idle_sec is not None
        and idle_sec > 6 * 3600
    ):
        evidence.append(f"idle_sec={int(idle_sec)}")
        return {
            "diagnostic_code": "factory_idle_too_long",
            "severity": "info",
            "root_cause": f"factory 가 {int(idle_sec/60)}분간 idle — 운영자가 시작 명령 필요",
            "evidence": evidence,
            "safe_auto_fix_available": False,
            "suggested_action": "수동으로 start_factory 실행 또는 사이클 점검",
        }

    # 11. command diagnostics surface a recent failure we haven't
    # already classified above.
    if cmd_diag.get("status") == "failed":
        evidence.append(f"last_command={cmd_diag.get('last_command')}")
        evidence.append(f"failed_stage={cmd_diag.get('failed_stage')}")
        return {
            "diagnostic_code": cmd_diag.get("diagnostic_code") or "unknown",
            "severity": "warning",
            "root_cause": cmd_diag.get("failed_reason") or "최근 명령이 실패",
            "evidence": evidence,
            "safe_auto_fix_available": False,
            "suggested_action": cmd_diag.get("suggested_action")
            or "운영자가 command_diagnostics.json 의 상세 내용 확인",
        }

    return {
        "diagnostic_code": "healthy",
        "severity": "info",
        "root_cause": "factory healthy",
        "evidence": [],
        "safe_auto_fix_available": False,
        "suggested_action": "조치 필요 없음",
    }


# --- Safe repair actions ----------------------------------------------------


def _watchdog_action_pause_factory(reason: str) -> str:
    """Pause the factory at every level we can reach:

    1. Local bash factory loop — write factory.paused marker (the loop
       short-circuits before launching the next cycle.py).
    2. Control Tower API — flip continuous_mode=false so the API
       watchdog stops auto-restarting the orchestrator.
    3. Control Tower API — request_stop so a currently-running cycle
       wraps at the next checkpoint.

    Best-effort: any individual leg failing leaves the others intact.
    Returns a short status line for the watchdog event log.
    """
    parts: list[str] = []
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_MARKER.write_text(
            f"watchdog auto-pause at {_utc_now_z()}: {reason}\n",
            encoding="utf-8",
        )
        # Mark this as runner-managed so the continuous-mode bridge can
        # later lift the pause if the operator turns Continuous back on.
        CONTINUOUS_PAUSE_MARKER.write_text(
            f"watchdog auto-pause at {_utc_now_z()}: {reason}\n",
            encoding="utf-8",
        )
        parts.append("pause marker written")
    except OSError as e:
        parts.append(f"pause marker write failed: {e}")

    cont = _admin_request("POST", "/factory/continuous", {"enabled": False})
    if isinstance(cont, dict):
        parts.append("api continuous_mode=false")
    else:
        parts.append("api continuous_mode flip failed")

    stop = _admin_request("POST", "/factory/stop")
    if isinstance(stop, dict):
        parts.append("api stop requested")
    else:
        parts.append("api stop request failed")

    return " · ".join(parts)


def _watchdog_action_release_deploy_lock() -> str:
    if not DEPLOY_LOCK_FILE.is_file():
        return "deploy lock 없음 — skip"
    try:
        DEPLOY_LOCK_FILE.unlink()
        return "deploy lock 해제"
    except OSError as e:
        return f"deploy lock 해제 실패: {e}"


def _watchdog_action_reset_deploy_progress(reason: str) -> str:
    try:
        _set_deploy_progress(
            "failed",
            failed_stage="watchdog_reset",
            failed_reason=reason,
            suggested_action="운영자가 원인 확인 후 다시 deploy",
            log_message=f"watchdog 자동 리셋: {reason}",
        )
        return "deploy_progress 를 failed 로 정리"
    except Exception as e:  # noqa: BLE001
        return f"deploy_progress 정리 실패: {e}"


def _watchdog_action_smoke_test() -> tuple[bool, str]:
    """Append a heartbeat line to docs/factory-smoke-test.md, then
    commit + push. Verifies the repo's git pipeline works without
    invoking Claude. Bounded by WATCHDOG_SMOKE_TEST_COOLDOWN_SEC.
    """
    global _WATCHDOG_LAST_SMOKE_AT
    if (time.time() - _WATCHDOG_LAST_SMOKE_AT) < WATCHDOG_SMOKE_TEST_COOLDOWN_SEC:
        return False, "smoke test cooldown 중"

    smoke_path = REPO_ROOT / "docs" / "factory-smoke-test.md"
    try:
        smoke_path.parent.mkdir(parents=True, exist_ok=True)
        existing = smoke_path.read_text(encoding="utf-8") if smoke_path.is_file() else (
            "# Factory Smoke Test\n\n"
            "Watchdog 가 commit/push 파이프라인이 살아 있는지 확인하기 위해 "
            "이 파일에 한 줄씩 timestamp 를 기록합니다.\n\n"
        )
        line = f"- watchdog smoke test at {_utc_now_z()}\n"
        smoke_path.write_text(existing + line, encoding="utf-8")
    except OSError as e:
        return False, f"smoke test 파일 쓰기 실패: {e}"

    rel_path = "docs/factory-smoke-test.md"
    ok_add, out_add = _git("add", rel_path, timeout=10)
    if not ok_add:
        return False, f"git add 실패: {_tail_text(out_add)}"
    ok_commit, out_commit = _git(
        "commit", "-m", "Watchdog smoke test", timeout=20,
    )
    if not ok_commit and "nothing to commit" not in (out_commit or ""):
        return False, f"git commit 실패: {_tail_text(out_commit)}"
    ok_push, out_push = _git("push", "origin", "main", timeout=30)
    if not ok_push:
        return False, f"git push 실패: {_tail_text(out_push)}"

    _WATCHDOG_LAST_SMOKE_AT = time.time()
    return True, "smoke test 통과 (commit + push 성공)"


def _watchdog_apply_safe_repair(diag: dict, state: dict) -> list[str]:
    """Run the safe auto-fix actions appropriate for this diagnostic.
    Returns a list of human-readable action labels for the state file."""
    code = diag["diagnostic_code"]
    actions: list[str] = []

    if code in {"planning_only_loop", "no_code_change_loop", "deploy_failed_repeatedly"}:
        msg = _watchdog_action_pause_factory(reason=code)
        actions.append(f"pause_factory · {msg}")

    if code in {
        "duplicate_deploy_commands",
        "current_command_stuck",
        "deploy_failed_repeatedly",
    }:
        msg_l = _watchdog_action_release_deploy_lock()
        actions.append(f"release_deploy_lock · {msg_l}")
        msg_r = _watchdog_action_reset_deploy_progress(reason=code)
        actions.append(f"reset_deploy_progress · {msg_r}")

    if code == "stale_state_mismatch":
        # Heal the operator_fix_state row whose status disagrees with
        # its last_message body. Same heuristic as the orchestrator's
        # normalize_operator_state action — kept inline here so the
        # legacy watchdog path repairs without depending on a pipeline
        # tick landing first.
        try:
            cur = _read_operator_fix_state() or {}
            prior = cur.get("status")
            msg_body = (cur.get("last_message") or "").lower()
            if "상태 published" in msg_body or "pushed to main: yes" in msg_body:
                new_status = "published"
            elif "상태 applied" in msg_body or "코드 변경 없이" in msg_body:
                new_status = "noop_success"
            else:
                new_status = "noop_success"
            _op_emit(
                kind="operator_request_state_mismatch_detected",
                message=(
                    f"operator_request state mismatch detected — "
                    f"prior={prior}, message indicates {new_status}"
                ),
                severity="warn",
            )
            _save_operator_fix_state({
                "status": new_status,
                "publish_status": (
                    "published" if new_status == "published"
                    else "not_requested"
                ),
                "last_message": (
                    f"[normalize] prior={prior} → {new_status} (watchdog stale 정리). "
                    f"원본: {(cur.get('last_message') or '')[:300]}"
                ),
            })
            _op_emit(
                kind="operator_request_stale_failure_cleared",
                message=(
                    f"operator_request stale failure cleared — "
                    f"{prior} → {new_status}"
                ),
                severity="info",
            )
            _op_emit(
                kind="operator_request_health_recovered",
                message="operator_request health recovered — Watchdog 정상화 완료",
                severity="success",
            )
            actions.append(
                f"normalize_operator_state · {prior} → {new_status}"
            )
        except Exception as e:  # noqa: BLE001
            actions.append(f"normalize_operator_state (skip: {e})")

    if code == "operator_request_failed_repeatedly":
        ok, msg_s = _watchdog_action_smoke_test()
        actions.append(f"smoke_test · ({'ok' if ok else 'skip/fail'}) {msg_s}")
        # If smoke test confirmed commit/push works, the persisted
        # qa_failed / no_code_change_failed row from a prior buggy run
        # is stale by definition. Normalize it so the watchdog stops
        # rediscovering the same "failure" on the next tick.
        if ok:
            try:
                cur = _read_operator_fix_state() or {}
                prior = cur.get("status")
                if prior in {
                    "failed", "qa_failed", "validation_failed",
                    "no_code_change_failed", "push_failed", "git_failed",
                }:
                    msg_body = (cur.get("last_message") or "").lower()
                    if "상태 published" in msg_body or "pushed to main: yes" in msg_body:
                        new_status = "published"
                    elif "상태 applied" in msg_body or "코드 변경 없이" in msg_body:
                        new_status = "noop_success"
                    else:
                        new_status = "noop_success"
                    _save_operator_fix_state({
                        "status": new_status,
                        "publish_status": (
                            "published" if new_status == "published"
                            else "not_requested"
                        ),
                        "last_message": (
                            f"[smoke-clear] prior={prior} → {new_status} "
                            f"(commit/push 파이프라인 healthy 확인)."
                        ),
                    })
                    _op_emit(
                        kind="operator_request_stale_failure_cleared",
                        message=(
                            f"operator_request stale failure cleared — "
                            f"{prior} → {new_status} (smoke test 통과)"
                        ),
                        severity="info",
                    )
                    _op_emit(
                        kind="operator_request_health_recovered",
                        message="operator_request health recovered — smoke test 후 정상화",
                        severity="success",
                    )
                    actions.append(
                        f"operator_state_clear · {prior} → {new_status}"
                    )
            except Exception as e:  # noqa: BLE001
                actions.append(f"operator_state_clear (skip: {e})")

    for label in actions:
        _watchdog_log_event(
            state,
            kind="watchdog_auto_repair_step",
            message=label,
            severity="info" if "실패" not in label else "warning",
            diagnostic_code=code,
        )

    return actions


# --- Tick orchestration -----------------------------------------------------


def _watchdog_tick() -> None:
    """One pass of the watchdog: diagnose, decide, repair, persist."""
    state = _read_watchdog_state()
    if not isinstance(state, dict):
        state = {}

    enabled = _watchdog_env_enabled()
    state["enabled"] = enabled
    if not enabled:
        # Persist a "disabled" marker once so the dashboard can show
        # "Watchdog OFF" without staring at last week's healthy row.
        if state.get("status") != "disabled":
            state["status"] = "disabled"
            state["last_checked_at"] = _utc_now_z()
            _watchdog_log_event(
                state,
                kind="watchdog_disabled",
                message="FACTORY_WATCHDOG_ENABLED=false — 자동 감시 비활성",
                severity="info",
            )
            _save_watchdog_state(state)
        return

    _watchdog_log_event(
        state, kind="watchdog_check_started", message="watchdog tick start"
    )
    state["last_checked_at"] = _utc_now_z()
    state["status"] = "watching"

    # Pipeline Recovery Orchestrator runs first — it owns the
    # stage-aware classification (planner → designer → ... → deploy).
    # Its decision/result is mirrored into the watchdog log so the
    # System Log shows "Pipeline recovery started / Stage failed /
    # Repair action started" entries the dashboard can group with the
    # legacy watchdog events.
    pipeline_outcome: dict | None = None
    try:
        pipeline_outcome = _pipeline_tick()
    except Exception as e:  # noqa: BLE001
        _watchdog_log_event(
            state,
            kind="pipeline_tick_failed",
            message=f"pipeline orchestrator raised: {e}",
            severity="error",
        )

    if pipeline_outcome:
        po_diag = pipeline_outcome.get("diagnosis") or {}
        po_decision = pipeline_outcome.get("decision")
        po_result = pipeline_outcome.get("result") or {}
        po_code = po_diag.get("diagnostic_code") or "unknown"
        po_failed_stage = po_diag.get("failed_stage")
        if po_code != "healthy":
            _watchdog_log_event(
                state,
                kind="pipeline_stage_failed",
                message=(
                    f"Stage failed — {po_failed_stage} ({po_code}): "
                    f"{(po_diag.get('root_cause') or '')[:200]}"
                ),
                severity=po_diag.get("severity") or "warning",
                diagnostic_code=po_code,
                extra={"failed_stage": po_failed_stage},
            )
        if po_decision and po_decision.get("rollback_to"):
            _watchdog_log_event(
                state,
                kind="pipeline_rollback",
                message=(
                    f"Rollback to stage — {po_decision['rollback_to']} "
                    f"(code={po_code})"
                ),
                severity="warning",
                diagnostic_code=po_code,
            )
        if po_result.get("applied"):
            _watchdog_log_event(
                state,
                kind="pipeline_repair_started",
                message=(
                    f"Repair action started — {', '.join(po_result['applied'])[:200]}"
                ),
                severity="info",
                diagnostic_code=po_code,
            )
            _watchdog_log_event(
                state,
                kind="pipeline_repair_completed",
                message=(
                    f"Repair action completed — applied "
                    f"{len(po_result['applied'])}건"
                ),
                severity="info",
                diagnostic_code=po_code,
            )
        if po_result.get("operator_required"):
            _watchdog_log_event(
                state,
                kind="pipeline_operator_required",
                message=(
                    f"Operator required — {(po_result.get('next_action') or po_code)[:200]}"
                ),
                severity="error",
                diagnostic_code=po_code,
            )
        if po_code == "no_changes_to_deploy":
            _watchdog_log_event(
                state,
                kind="pipeline_no_changes",
                message="No changes to validate — 배포 없이 종료",
                severity="info",
                diagnostic_code=po_code,
            )

    # Forward Progress Detector — heartbeat ≠ progress. Runs after the
    # orchestrator and emits its own System Log events. The detector is
    # cheap (file mtime reads + small JSON), and its judgments feed
    # directly back into the orchestrator on the *next* tick because
    # they share DIAGNOSTIC_REPAIR_MAP entries (current_stage_stuck,
    # planning_only_loop, no_progress_despite_heartbeat, …).
    try:
        fp_meta = _forward_progress_diagnose()
    except Exception as e:  # noqa: BLE001
        fp_meta = None
        _watchdog_log_event(
            state,
            kind="forward_progress_failed",
            message=f"Forward progress diagnose raised: {e}",
            severity="error",
        )
    if fp_meta:
        fp_status = fp_meta.get("status")
        fp_code = fp_meta.get("diagnostic_code") or "unknown"
        _watchdog_log_event(
            state,
            kind="forward_progress_check_started",
            message="Forward progress check started",
            severity="info",
            diagnostic_code=fp_code,
        )
        if fp_status == "blocked":
            _watchdog_log_event(
                state,
                kind="forward_progress_blocked",
                message=(
                    f"Forward progress blocked — {fp_code}: "
                    f"{(fp_meta.get('blocking_reason') or '')[:200]}"
                ),
                severity="warning",
                diagnostic_code=fp_code,
            )
            if fp_code == "required_output_missing":
                _watchdog_log_event(
                    state,
                    kind="forward_progress_required_output_missing",
                    message=(
                        f"Required output missing — {fp_meta.get('required_output')} "
                        f"(stage={fp_meta.get('current_stage')})"
                    ),
                    severity="warning",
                    diagnostic_code=fp_code,
                )
        elif fp_status == "stuck":
            _watchdog_log_event(
                state,
                kind="forward_progress_stuck",
                message=(
                    f"Current stage stuck — {fp_meta.get('current_stage')} "
                    f"({fp_meta.get('current_stage_elapsed_sec')}s, "
                    f"timeout={fp_meta.get('stage_timeout_sec')}s)"
                ),
                severity="error",
                diagnostic_code=fp_code,
            )
        elif fp_status == "planning_only":
            _watchdog_log_event(
                state,
                kind="forward_progress_planning_only",
                message=(
                    f"Planning only loop detected — {fp_code}: "
                    f"streak={fp_meta.get('planning_only_streak') or fp_meta.get('no_code_change_streak')}"
                ),
                severity="warning",
                diagnostic_code=fp_code,
            )
            _watchdog_log_event(
                state,
                kind="forward_progress_no_code_change",
                message="No code change detected — 산출물만 생성, 코드 변경 없음",
                severity="warning",
                diagnostic_code=fp_code,
            )
            # Continuous OFF directly — pipeline orchestrator's
            # planning_only_loop entry chains the same action, but
            # firing it from here makes the cause-effect explicit in
            # the System Log.
            note = _watchdog_action_pause_factory(reason=f"forward_progress·{fp_code}")
            _watchdog_log_event(
                state,
                kind="forward_progress_continuous_stopped",
                message=f"Continuous stopped due to no progress — {note}",
                severity="warning",
                diagnostic_code=fp_code,
            )
        elif fp_status == "no_progress":
            _watchdog_log_event(
                state,
                kind="forward_progress_no_progress",
                message=(
                    f"No progress despite heartbeat — {fp_code}: "
                    f"{(fp_meta.get('blocking_reason') or '')[:200]}"
                ),
                severity="warning" if fp_code == "no_changes_to_validate" else "error",
                diagnostic_code=fp_code,
            )
        elif fp_status == "operator_required":
            _watchdog_log_event(
                state,
                kind="forward_progress_operator_required",
                message=(
                    f"Operator required — {fp_code}: "
                    f"{(fp_meta.get('next_action') or '')[:200]}"
                ),
                severity="error",
                diagnostic_code=fp_code,
            )

    # Agent Supervisor — the meta-agent. Runs after FP so the
    # accountability report reflects the latest meaningful_change /
    # operator_required signals. We always emit a "review started"
    # marker so the System Log shows the supervisor is active.
    sup_report = None
    _watchdog_log_event(
        state,
        kind="supervisor_review_started",
        message="Agent Supervisor review started",
        severity="info",
    )
    try:
        sup_report = _supervisor_run()
    except Exception as e:  # noqa: BLE001
        _watchdog_log_event(
            state,
            kind="supervisor_review_failed",
            message=f"Agent Supervisor raised: {e}",
            severity="error",
        )

    if sup_report:
        sup_overall = sup_report.get("overall_status") or "unknown"
        sup_blocking = sup_report.get("blocking_agent")
        sup_meaningful = bool(sup_report.get("meaningful_change"))
        sup_ticket_ok = bool(sup_report.get("implementation_ticket_exists"))

        if sup_overall == "pass":
            _watchdog_log_event(
                state,
                kind="supervisor_pass",
                message="Agent accountability passed — 모든 에이전트 기준 통과 + 의미 있는 변경",
                severity="success",
            )
        else:
            _watchdog_log_event(
                state,
                kind="supervisor_fail",
                message=(
                    f"Agent accountability failed — overall={sup_overall} "
                    f"blocking={sup_blocking or '—'}: "
                    f"{(sup_report.get('blocking_reason') or '')[:200]}"
                ),
                severity=(
                    "warning" if sup_overall in {"retry_required", "planning_only"}
                    else "error"
                ),
            )
            if not sup_meaningful:
                _watchdog_log_event(
                    state,
                    kind="supervisor_meaningful_missing",
                    message="Meaningful change missing — 산출물만 있고 실제 코드 변경이 없음",
                    severity="warning",
                )
            if not sup_ticket_ok:
                _watchdog_log_event(
                    state,
                    kind="supervisor_ticket_missing",
                    message="Implementation Ticket missing — PM 단계 재실행 필요",
                    severity="warning",
                )

        # Per-agent reject markers — only the failed ones, to keep the
        # System Log compact. retry_prompts ride on the agent row in
        # heartbeat metadata, not the log entries.
        REJECT_KIND = {
            "planner":  ("supervisor_planner_rejected",  "Planner rejected"),
            "designer": ("supervisor_designer_rejected", "Designer rejected"),
            "pm":       ("supervisor_pm_rejected",       "PM rejected"),
            "frontend": ("supervisor_frontend_rejected", "Frontend rejected"),
            "backend":  ("supervisor_backend_rejected",  "Backend rejected"),
            "ai":       ("supervisor_ai_rejected",       "AI rejected"),
            "qa":       ("supervisor_qa_rejected",       "QA rejected"),
            "deploy":   ("supervisor_deploy_rejected",   "Deploy rejected"),
        }
        for name, row in (sup_report.get("agents") or {}).items():
            if not isinstance(row, dict):
                continue
            if not row.get("required_retry"):
                continue
            kind, label = REJECT_KIND.get(name, ("supervisor_agent_rejected", "Agent rejected"))
            first_problem = (row.get("problems") or [""])[0]
            _watchdog_log_event(
                state,
                kind=kind,
                message=f"{label} — {first_problem[:160]}",
                severity="warning",
            )

        if sup_blocking:
            _watchdog_log_event(
                state,
                kind="supervisor_cycle_blocked",
                message=(
                    f"Cycle blocked by agent — {sup_blocking}: "
                    f"{(sup_report.get('blocking_reason') or '')[:200]}"
                ),
                severity="warning",
            )

        _watchdog_log_event(
            state,
            kind="supervisor_review_completed",
            message=(
                f"Agent Supervisor review completed — overall={sup_overall} "
                f"meaningful={sup_meaningful}"
            ),
            severity="info",
        )

    # Control State aggregation — single source of truth. Runs after
    # pipeline / forward_progress / supervisor have all written their
    # sub-states so the verdict reflects the latest tick.
    cs_state = _control_state_aggregate(_build_runner_meta())
    if cs_state:
        cs_status = cs_state.get("status") or "unknown"
        _watchdog_log_event(
            state,
            kind="control_state_aggregated",
            message=(
                f"Control state · {cs_status} — "
                f"{(cs_state.get('summary') or '')[:160]}"
            ),
            severity=(
                "success" if cs_status in {"running", "completed", "idle"}
                else "warning" if cs_status in {"blocked"}
                else "error" if cs_status in {"failed", "operator_required"}
                else "info"
            ),
            diagnostic_code=cs_state.get("diagnostic_code"),
        )
        # Drive continuous-stop directly from the unified verdict.
        # This is the "blocked / failed / operator_required → continuous
        # OFF" rule the spec calls out.
        if cs_state.get("should_stop_continuous"):
            note = _watchdog_action_pause_factory(
                reason=f"control_state·{cs_state.get('diagnostic_code') or cs_status}"
            )
            _watchdog_log_event(
                state,
                kind="control_state_continuous_stopped",
                message=(
                    f"Continuous stopped by control_state — {cs_status}: "
                    f"{note[:160]}"
                ),
                severity="warning",
                diagnostic_code=cs_state.get("diagnostic_code"),
            )

    try:
        diag = _watchdog_diagnose()
    except Exception as e:  # noqa: BLE001
        diag = {
            "diagnostic_code": "unknown",
            "severity": "error",
            "root_cause": f"diagnose raised: {e}",
            "evidence": [],
            "safe_auto_fix_available": False,
            "suggested_action": "운영자가 runner 로그 확인 필요",
        }

    code = diag["diagnostic_code"]
    state["last_diagnostic_code"] = code
    state["severity"] = diag["severity"]
    state["root_cause"] = diag["root_cause"]
    state["evidence"] = diag.get("evidence") or []
    state["suggested_actions"] = (
        [diag["suggested_action"]] if diag.get("suggested_action") else []
    )

    if code == "healthy":
        # Orchestrator may still hold an open operator-required signal
        # (e.g. retry budget exhausted, claude_repair gated). Reflect
        # that in the watchdog status so the WatchdogPanel doesn't
        # claim "healthy" when the PipelineRecoveryPanel is asking for
        # human intervention.
        po_op_required = bool(
            ((pipeline_outcome or {}).get("result") or {}).get("operator_required")
        )
        po_code = ((pipeline_outcome or {}).get("diagnosis") or {}).get(
            "diagnostic_code"
        ) or "healthy"
        if po_op_required and po_code not in {"healthy", "no_changes_to_deploy"}:
            state["status"] = "degraded"
            state["auto_repair_blocked_reason"] = (
                f"pipeline orchestrator escalated ({po_code})"
            )
            _watchdog_log_event(
                state,
                kind="watchdog_check_completed",
                message="watchdog tick done — orchestrator escalation in effect",
                severity="warning",
            )
        else:
            state["status"] = "healthy"
            state["repeat_count"] = 0
            state["auto_repair_blocked_reason"] = None
            state["safe_actions_taken"] = []
            _watchdog_log_event(
                state, kind="watchdog_healthy", message="공장 healthy", severity="info",
            )
            _watchdog_log_event(
                state, kind="watchdog_check_completed", message="watchdog tick done",
            )
        _save_watchdog_state(state)
        return

    _watchdog_log_event(
        state,
        kind="watchdog_detected_issue",
        message=f"{code} — {diag['root_cause']}",
        severity=diag["severity"],
        diagnostic_code=code,
        extra={"evidence": diag.get("evidence")},
    )

    # Cooldown / repeat tracking.
    cooldown_sec = _watchdog_repair_cooldown_sec()
    max_repeat = _watchdog_max_repeat()
    last_repair_at = _WATCHDOG_LAST_REPAIR_AT.get(code, 0.0)
    cooldown_remaining = max(0.0, cooldown_sec - (time.time() - last_repair_at))
    repeat_count = _WATCHDOG_REPEAT_COUNT.get(code, 0)
    state["repeat_count"] = repeat_count
    state["cooldown_until"] = (
        _utc_now_z() if cooldown_remaining == 0
        else (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
              + f"+{int(cooldown_remaining)}s")
    )

    in_flight = _INFLIGHT_COMMAND is not None
    if in_flight:
        state["status"] = "watching"
        state["auto_repair_blocked_reason"] = "command in flight — heavy repair skip"
        _watchdog_log_event(
            state,
            kind="watchdog_auto_repair_skipped",
            message="명령 실행 중이라 heavy repair skip",
            severity="info",
            diagnostic_code=code,
        )
        _watchdog_log_event(
            state, kind="watchdog_check_completed", message="watchdog tick done",
        )
        _save_watchdog_state(state)
        return

    if repeat_count >= max_repeat:
        state["status"] = "broken"
        state["auto_repair_blocked_reason"] = (
            f"같은 진단 {repeat_count}회 반복 — 자동 복구 중단"
        )
        _watchdog_log_event(
            state,
            kind="watchdog_escalated",
            message=f"{code} 가 {repeat_count}회 반복 — 운영자 확인 필요",
            severity="error",
            diagnostic_code=code,
        )
        _watchdog_log_event(
            state, kind="watchdog_check_completed", message="watchdog tick done",
        )
        _save_watchdog_state(state)
        return

    if cooldown_remaining > 0:
        state["status"] = "watching"
        state["auto_repair_blocked_reason"] = (
            f"cooldown 중 ({int(cooldown_remaining)}s 남음)"
        )
        _watchdog_log_event(
            state,
            kind="watchdog_auto_repair_skipped",
            message=f"cooldown 중이라 자동 복구 skip ({int(cooldown_remaining)}s 남음)",
            severity="info",
            diagnostic_code=code,
        )
        _watchdog_log_event(
            state, kind="watchdog_check_completed", message="watchdog tick done",
        )
        _save_watchdog_state(state)
        return

    if not diag.get("safe_auto_fix_available"):
        state["status"] = "degraded"
        state["auto_repair_blocked_reason"] = "safe_auto_fix_available=false"
        _watchdog_log_event(
            state,
            kind="watchdog_escalated",
            message=f"{code} — 자동 복구 불가, 운영자 확인 필요",
            severity="warning",
            diagnostic_code=code,
        )
        _watchdog_log_event(
            state, kind="watchdog_check_completed", message="watchdog tick done",
        )
        _save_watchdog_state(state)
        return

    # Apply safe repair.
    state["status"] = "repairing"
    _watchdog_log_event(
        state,
        kind="watchdog_auto_repair_started",
        message=f"{code} 자동 복구 시작",
        severity="info",
        diagnostic_code=code,
    )
    actions = _watchdog_apply_safe_repair(diag, state)
    state["safe_actions_taken"] = actions
    state["last_repair_at"] = _utc_now_z()
    _WATCHDOG_LAST_REPAIR_AT[code] = time.time()
    _WATCHDOG_REPEAT_COUNT[code] = repeat_count + 1
    state["repeat_count"] = _WATCHDOG_REPEAT_COUNT[code]
    state["auto_repair_blocked_reason"] = None
    state["status"] = "watching"
    _watchdog_log_event(
        state,
        kind="watchdog_auto_repair_completed",
        message=f"{code} 자동 복구 완료 ({len(actions)}건)",
        severity="info",
        diagnostic_code=code,
    )
    _watchdog_log_event(
        state, kind="watchdog_check_completed", message="watchdog tick done",
    )
    _save_watchdog_state(state)


def _watchdog_loop() -> None:
    """Daemon-thread entrypoint. Sleeps for the configured interval
    between ticks and exits when _WATCHDOG_STOP is set."""
    sys.stderr.write(
        f"[runner] watchdog thread started · interval={_watchdog_interval_sec()}s · "
        f"enabled={_watchdog_env_enabled()}\n"
    )
    # Run the first tick almost immediately so the dashboard has data.
    initial_delay = 5.0
    if _WATCHDOG_STOP.wait(initial_delay):
        return
    while not _WATCHDOG_STOP.is_set():
        try:
            _watchdog_tick()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[runner] watchdog tick raised: {e}\n")
        # Re-read interval each loop so an env var change is picked up
        # at the next tick (the user can flip enabled=true at runtime).
        _WATCHDOG_STOP.wait(_watchdog_interval_sec())


def _start_watchdog_thread() -> None:
    global _WATCHDOG_THREAD
    if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
        return
    _WATCHDOG_STOP.clear()
    t = threading.Thread(
        target=_watchdog_loop,
        daemon=True,
        name="factory-watchdog",
    )
    _WATCHDOG_THREAD = t
    t.start()


def _build_watchdog_meta() -> dict:
    """Heartbeat metadata block under `local_factory.watchdog`. Always
    present so the dashboard can decide enabled/disabled rendering on
    its own, without inferring from absence."""
    state = _read_watchdog_state()
    if not state:
        return {
            "enabled": _watchdog_env_enabled(),
            "status": "disabled" if not _watchdog_env_enabled() else "watching",
            "last_checked_at": None,
            "last_repair_at": None,
            "last_diagnostic_code": None,
            "severity": "info",
            "root_cause": None,
            "evidence": [],
            "safe_actions_taken": [],
            "suggested_actions": [],
            "repeat_count": 0,
            "cooldown_until": None,
            "auto_repair_blocked_reason": None,
            "log": [],
            "interval_sec": _watchdog_interval_sec(),
            "stuck_command_sec": _watchdog_stuck_command_sec(),
            "repair_cooldown_sec": _watchdog_repair_cooldown_sec(),
            "max_repeat": _watchdog_max_repeat(),
        }
    return {
        "enabled": bool(state.get("enabled")),
        "status": state.get("status") or "watching",
        "last_checked_at": state.get("last_checked_at"),
        "last_repair_at": state.get("last_repair_at"),
        "last_diagnostic_code": state.get("last_diagnostic_code"),
        "severity": state.get("severity") or "info",
        "root_cause": state.get("root_cause"),
        "evidence": state.get("evidence") or [],
        "safe_actions_taken": state.get("safe_actions_taken") or [],
        "suggested_actions": state.get("suggested_actions") or [],
        "repeat_count": int(state.get("repeat_count") or 0),
        "cooldown_until": state.get("cooldown_until"),
        "auto_repair_blocked_reason": state.get("auto_repair_blocked_reason"),
        "log": list(state.get("log") or [])[-WATCHDOG_LOG_CAP:],
        "interval_sec": _watchdog_interval_sec(),
        "stuck_command_sec": _watchdog_stuck_command_sec(),
        "repair_cooldown_sec": _watchdog_repair_cooldown_sec(),
        "max_repeat": _watchdog_max_repeat(),
    }


# ---------------------------------------------------------------------------
# Pipeline Recovery Orchestrator
#
# Stage-aware sibling of the Watchdog. Where the Watchdog answers
# "is the factory healthy", the Orchestrator answers:
#
#   "What pipeline stage are we in? Which stage failed? What is the
#    smallest safe action that gets us back on the rails?"
#
# Stages (in order):
#   1. planner_proposal
#   2. designer_review
#   3. pm_decision
#   4. implementation_ticket
#   5. claude_apply
#   6. validation_qa
#   7. git_commit
#   8. git_push
#   9. github_actions
#  10. server_verification
#  11. browser_cache_verification
#
# State persisted to .runtime/pipeline_state.json (current diagnosis +
# retry counters + last action) and .runtime/recovery_history.json
# (append-only audit trail of every recovery decision).
#
# Repair actions are looked up from DIAGNOSTIC_REPAIR_MAP, which keeps
# the policy (rollback_to / max_retry / action / description) declarative
# rather than hidden inside if/elif chains. Auto-execution is bounded by:
#   - per-stage max_retry (broken once exceeded)
#   - FACTORY_WATCHDOG_ALLOW_CLAUDE_REPAIR for the claude_repair action
#   - existing safe-pause/lock-release primitives for queue cleanup
# ---------------------------------------------------------------------------

PIPELINE_STATE_FILE = RUNTIME_DIR / "pipeline_state.json"
RECOVERY_HISTORY_FILE = RUNTIME_DIR / "recovery_history.json"
PIPELINE_HISTORY_CAP = 30
PIPELINE_RECOVERY_CAP = 30

# Pipeline contracts — declarative, not function references. Each entry
# captures what the stage produces, what counts as failure, and the
# rollback target the orchestrator should jump to when the stage fails.
PIPELINE_CONTRACTS: list[dict] = [
    {
        "stage": "planner_proposal",
        "input_required": [],
        "output_required": ["product_planner_report.md"],
        "diagnostic_codes": ["planner_output_missing", "planner_output_low_quality"],
        "rollback_to": None,
        "max_retry": 2,
    },
    {
        "stage": "designer_review",
        "input_required": ["planner_proposal"],
        "output_required": ["designer_critique.md", "designer_final_review.md"],
        "diagnostic_codes": [
            "designer_output_missing", "designer_output_not_actionable",
        ],
        "rollback_to": "planner_proposal",
        "max_retry": 2,
    },
    {
        "stage": "pm_decision",
        "input_required": ["designer_review"],
        "output_required": ["pm_decision.md"],
        "diagnostic_codes": ["pm_decision_missing"],
        "rollback_to": "designer_review",
        "max_retry": 2,
    },
    {
        "stage": "implementation_ticket",
        "input_required": ["pm_decision"],
        "output_required": ["implementation_ticket.md"],
        "diagnostic_codes": [
            "implementation_ticket_missing", "implementation_ticket_invalid",
        ],
        "rollback_to": "pm_decision",
        "max_retry": 2,
    },
    {
        "stage": "claude_apply",
        "input_required": ["implementation_ticket"],
        "output_required": [],  # diff, not a single file
        "diagnostic_codes": [
            "claude_apply_skipped", "claude_not_started",
            "claude_process_failed", "no_code_change",
            "docs_only_change", "frontend_change_missing",
            "backend_change_missing",
        ],
        "rollback_to": "implementation_ticket",
        "max_retry": 2,
    },
    {
        "stage": "validation_qa",
        "input_required": ["claude_apply"],
        "output_required": ["qa_report.md"],
        "diagnostic_codes": [
            "no_changes_to_validate", "qa_not_run",
            "qa_report_missing_before_run", "qa_report_missing_after_run",
            "qa_command_failed", "qa_exception_before_report",
            "validation_failed",
        ],
        "rollback_to": "claude_apply",
        "max_retry": 2,
    },
    {
        "stage": "git_commit",
        "input_required": ["validation_qa"],
        "output_required": [],
        "diagnostic_codes": [
            "git_dirty_unpublished", "git_commit_failed",
            "no_changes_to_commit",
        ],
        "rollback_to": "claude_apply",
        "max_retry": 1,
    },
    {
        "stage": "git_push",
        "input_required": ["git_commit"],
        "output_required": [],
        "diagnostic_codes": ["git_push_failed", "non_fast_forward"],
        "rollback_to": "git_commit",
        "max_retry": 1,
    },
    {
        "stage": "github_actions",
        "input_required": ["git_push"],
        "output_required": [],
        "diagnostic_codes": [
            "github_actions_not_triggered", "github_actions_failed",
        ],
        "rollback_to": "git_push",
        "max_retry": 1,
    },
    {
        "stage": "server_verification",
        "input_required": ["github_actions"],
        "output_required": [],
        "diagnostic_codes": ["server_not_updated", "deploy_script_failed"],
        "rollback_to": None,
        "max_retry": 1,
    },
    {
        "stage": "browser_cache_verification",
        "input_required": ["server_verification"],
        "output_required": [],
        "diagnostic_codes": ["browser_cache_suspected"],
        "rollback_to": None,
        "max_retry": 1,
    },
]
PIPELINE_STAGE_ORDER: list[str] = [c["stage"] for c in PIPELINE_CONTRACTS]
PIPELINE_CONTRACT_BY_STAGE: dict[str, dict] = {
    c["stage"]: c for c in PIPELINE_CONTRACTS
}


# Diagnostic → repair recipe. `action` is one of:
#   noop, noop_clear_failed, operator_required, claude_repair,
#   release_deploy_lock, reset_deploy_progress, pause_factory,
#   cancel_inflight.
# Multiple actions can be chained via a list under `actions`.
DIAGNOSTIC_REPAIR_MAP: dict[str, dict] = {
    # 기획/디자인/PM
    "planner_output_missing": {
        "stage": "planner_proposal",
        "actions": ["operator_required"],
        "description": "planner 산출물이 비어 있습니다 — 운영자 확인 필요.",
    },
    "planner_output_low_quality": {
        "stage": "planner_proposal",
        "actions": ["operator_required"],
        "description": "planner 품질 낮음 — 운영자가 product goal / 입력 확인 필요.",
    },
    "designer_output_missing": {
        "stage": "designer_review",
        "actions": ["operator_required"],
        "description": "designer 산출물 누락 — planner 단계로 되돌아가 재검토 필요.",
    },
    "designer_output_not_actionable": {
        "stage": "designer_review",
        "rollback_to": "designer_review",
        "actions": ["operator_required"],
        "description": "designer 산출물이 구체적이지 않음 — 운영자가 UI 파일/레이아웃 지시를 보강해 재요청 필요.",
    },
    "pm_decision_missing": {
        "stage": "pm_decision",
        "actions": ["operator_required"],
        "description": "PM 결정 미작성 — 운영자가 PM 단계 확인 후 재실행.",
    },
    # 구현
    "implementation_ticket_missing": {
        "stage": "implementation_ticket",
        "rollback_to": "pm_decision",
        "actions": ["claude_repair"],
        "description": "Implementation Ticket 누락 — PM 단계로 되돌아가 ticket 재생성 요청.",
    },
    "implementation_ticket_invalid": {
        "stage": "implementation_ticket",
        "rollback_to": "pm_decision",
        "actions": ["claude_repair"],
        "description": "Implementation Ticket 의 수정 대상 파일이 비어 있음 — 재작성 요청.",
    },
    "claude_apply_skipped": {
        "stage": "claude_apply",
        "rollback_to": "implementation_ticket",
        "actions": ["claude_repair"],
        "description": "claude_apply 가 skipped — Implementation Ticket 기반으로 직접 재실행.",
    },
    "claude_not_started": {
        "stage": "claude_apply",
        "actions": ["operator_required"],
        "description": "Claude CLI 가 실행되지 않음 — PATH / LOCAL_RUNNER_CLAUDE_COMMAND 확인 필요.",
    },
    "claude_process_failed": {
        "stage": "claude_apply",
        "actions": ["operator_required"],
        "description": "Claude 프로세스가 실패 — stderr tail 확인 후 재시도.",
    },
    "no_code_change": {
        "stage": "claude_apply",
        "actions": ["claude_repair"],
        "description": "Claude 가 변경하지 않음 — 실제 app/web/src 또는 control_tower/web/src 파일을 수정하라고 재요청.",
    },
    "docs_only_change": {
        "stage": "claude_apply",
        "actions": ["claude_repair"],
        "description": "docs/config 만 변경 — 실제 화면 코드 변경을 요청.",
    },
    "frontend_change_missing": {
        "stage": "claude_apply",
        "actions": ["claude_repair"],
        "description": "프론트 변경이 필요한 ticket 인데 변경이 없음 — 재요청.",
    },
    "backend_change_missing": {
        "stage": "claude_apply",
        "actions": ["claude_repair"],
        "description": "백엔드 변경이 필요한 ticket 인데 변경이 없음 — 재요청.",
    },
    # 검증
    "no_changes_to_validate": {
        "stage": "validation_qa",
        "actions": ["noop_clear_failed"],
        "description": "변경 파일 없음 — QA 실패로 처리하지 않고 배포 없음으로 종료.",
    },
    "no_changes_to_deploy": {
        "stage": "validation_qa",
        "actions": ["noop_clear_failed"],
        "description": "배포할 변경이 없음 — deploy_progress 를 정상 종료 처리.",
    },
    "qa_not_run": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "QA 단계가 실행되지 않음 — 이전 단계 실패 메시지 확인.",
    },
    "qa_report_missing_before_run": {
        "stage": "validation_qa",
        "actions": ["noop"],
        "description": "변경이 있으면 on-demand QA 가 자동 실행됩니다 — 결과 대기.",
    },
    "qa_report_missing_after_run": {
        "stage": "validation_qa",
        "actions": ["claude_repair"],
        "description": "QA 실행 후에도 report 가 없음 — runner/cycle RUNTIME 경로 점검 + Claude Repair.",
    },
    "qa_command_failed": {
        "stage": "validation_qa",
        "actions": ["claude_repair"],
        "description": "QA 검증 명령이 실패 — failed_command + stderr_tail 기반 코드 수정 요청.",
    },
    "qa_exception_before_report": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "QA 실행 중 예외 발생 — runner/cycle 코드 수정 후 재시도 (운영자 확인).",
    },
    "validation_failed": {
        "stage": "validation_qa",
        "actions": ["claude_repair"],
        "description": "runner-side 검증 실패 — stderr tail 기반 코드 수정 요청.",
    },
    # Git
    "git_dirty_unpublished": {
        "stage": "git_commit",
        "actions": ["operator_required"],
        "description": "working tree 에 미배포 변경 — 운영자가 검토 후 publish.",
    },
    "git_commit_failed": {
        "stage": "git_commit",
        "actions": ["operator_required"],
        "description": "git commit 실패 — git status / pre-commit hook 확인.",
    },
    "no_changes_to_commit": {
        "stage": "git_commit",
        "actions": ["noop_clear_failed"],
        "description": "commit 할 변경이 없음 — failure 로 처리하지 않음.",
    },
    "git_push_failed": {
        "stage": "git_push",
        "actions": ["operator_required"],
        "description": "git push 실패 — branch / ahead-behind 확인 후 안전한 재시도 또는 운영자 처리.",
    },
    "non_fast_forward": {
        "stage": "git_push",
        "actions": ["operator_required"],
        "description": "non-fast-forward — 운영자가 직접 origin/main 과 정합 후 재시도.",
    },
    # 배포
    "github_actions_not_triggered": {
        "stage": "github_actions",
        "actions": ["operator_required"],
        "description": "최근 commit + Actions URL 확인 — workflow 가 활성 상태인지 점검.",
    },
    "github_actions_failed": {
        "stage": "github_actions",
        "actions": ["operator_required"],
        "description": "Actions 워크플로우 실패 — Actions 탭에서 stderr 확인 후 재시도.",
    },
    "server_not_updated": {
        "stage": "server_verification",
        "actions": ["operator_required"],
        "description": "서버 검증이 실패 — 검증 스크립트 또는 서버 상태 확인 필요.",
    },
    "deploy_script_failed": {
        "stage": "server_verification",
        "actions": ["operator_required"],
        "description": "deploy 스크립트 실패 — 운영자가 SSH 로그 확인 필요.",
    },
    "browser_cache_suspected": {
        "stage": "browser_cache_verification",
        "actions": ["operator_required"],
        "description": "브라우저 캐시로 인한 차이 의심 — hard reload + asset 해시 확인.",
    },
    # 운영
    "current_command_stuck": {
        "stage": "validation_qa",
        "actions": ["release_deploy_lock", "reset_deploy_progress"],
        "description": "명령이 stuck 임 — deploy lock 해제 + deploy_progress 정리.",
    },
    "duplicate_command_queue": {
        "stage": "validation_qa",
        "actions": ["release_deploy_lock", "reset_deploy_progress"],
        "description": "중복 큐 — stale 항목 정리.",
    },
    "duplicate_deploy_commands": {
        "stage": "validation_qa",
        "actions": ["release_deploy_lock", "reset_deploy_progress"],
        "description": "중복 deploy — lock 해제 후 정리.",
    },
    "deploy_failed_repeatedly": {
        "stage": "github_actions",
        "actions": ["pause_factory"],
        "description": "deploy 가 반복 실패 — factory pause 후 운영자 확인 필요.",
    },
    "continuous_loop_no_code_change": {
        "stage": "claude_apply",
        "actions": ["pause_factory"],
        "description": "사이클이 코드 변경 없이 반복 — continuous OFF + 운영자 확인.",
    },
    "planning_only_loop": {
        "stage": "claude_apply",
        "actions": ["pause_factory"],
        "description": "기획만 반복 — claude_apply 가 진행되지 않음, factory pause.",
    },
    "no_code_change_loop": {
        "stage": "claude_apply",
        "actions": ["pause_factory"],
        "description": "코드 변경 없이 반복 — factory pause.",
    },
    "operator_request_failed_repeatedly": {
        "stage": "claude_apply",
        "actions": ["operator_required"],
        "description": "operator_request 가 반복 실패 — 운영자 확인 필요.",
    },
    "stale_state_mismatch": {
        "stage": "claude_apply",
        "actions": ["normalize_operator_state"],
        "description": (
            "operator_request 의 status 와 last_message 가 어긋남 (status=qa_failed "
            "이지만 메시지는 applied/no-op). status 를 정규화합니다."
        ),
    },
    "operator_no_code_change": {
        "stage": "claude_apply",
        "actions": ["operator_required"],
        "description": (
            "operator_request 가 코드 변경을 요구했지만 changed_files=0 — "
            "수정 대상 파일과 기대 변경을 포함해 다시 요청 필요."
        ),
    },
    "operator_noop_success": {
        "stage": "claude_apply",
        "actions": ["noop"],
        "description": "operator_request 는 정상 no-op — 추가 조치 불요.",
    },
    "operator_aborted": {
        "stage": "claude_apply",
        "actions": ["operator_required"],
        "description": "Claude 가 요청을 거부 — 거부 사유 확인 후 재요청.",
    },
    "stale_runner": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "runner 가 부팅 이후 수정됨 — restart_runner / update_runner 후 재시도.",
    },
    "runner_offline": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "runner offline — Mac 에서 runner 재시작 필요.",
    },
    "git_dirty_unpublished_loop": {
        "stage": "git_commit",
        "actions": ["operator_required"],
        "description": "git working tree 변경이 오래도록 push 되지 않음.",
    },
    "qa_gate_stuck": {
        "stage": "validation_qa",
        "actions": ["claude_repair"],
        "description": "QA Gate 가 정상 동작하지 않음 — runner 와 cycle 의 RUNTIME 경로 점검.",
    },
    "factory_idle_too_long": {
        "stage": "planner_proposal",
        "actions": ["operator_required"],
        "description": "factory idle 시간 초과 — 운영자가 시작 명령 필요.",
    },
    # Forward Progress signals — heartbeat OK 이지만 진행이 멈춘 경우.
    "current_stage_stuck": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "현재 stage 가 timeout 을 초과한 채 멈춤 — 운영자 확인 필요.",
    },
    "required_output_missing": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "stage 의 required output 이 없습니다 — 다음 단계로 진행 불가.",
    },
    "no_progress_despite_heartbeat": {
        "stage": "claude_apply",
        "actions": ["pause_factory"],
        "description": "heartbeat 정상이지만 stage / output / commit 변화 없음 — factory pause.",
    },
    "no_changes_to_validate": {
        "stage": "validation_qa",
        "actions": ["noop_clear_failed"],
        "description": "변경 파일이 0개 — QA 실패가 아니라 검증할 변경사항 없음.",
    },
    "unknown": {
        "stage": "validation_qa",
        "actions": ["operator_required"],
        "description": "원인 분류 실패 — 수동 점검 필요.",
    },
}

# Diagnostic codes Claude Repair is allowed to attempt. These are the
# code-bug-shaped failures where a bounded Claude prompt can plausibly
# fix the underlying file.
CLAUDE_REPAIR_ELIGIBLE_CODES: frozenset[str] = frozenset({
    "qa_report_missing_after_run",
    "qa_command_failed",
    "claude_apply_skipped",
    "no_code_change",
    "docs_only_change",
    "frontend_change_missing",
    "backend_change_missing",
    "implementation_ticket_invalid",
    "implementation_ticket_missing",
    "validation_failed",
    "qa_gate_stuck",
})


# --- Pipeline state I/O ---------------------------------------------------


def _read_pipeline_state() -> dict:
    if not PIPELINE_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(PIPELINE_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_pipeline_state(state: dict) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _utc_now_z()
        PIPELINE_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to write pipeline_state: {e}\n")


def _read_recovery_history() -> list[dict]:
    if not RECOVERY_HISTORY_FILE.is_file():
        return []
    try:
        data = json.loads(RECOVERY_HISTORY_FILE.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _append_recovery_history(entry: dict) -> None:
    cur = _read_recovery_history()
    cur.append(entry)
    cur = cur[-PIPELINE_RECOVERY_CAP:]
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        RECOVERY_HISTORY_FILE.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to write recovery_history: {e}\n")


# --- Diagnosis ------------------------------------------------------------


def _pipeline_classify_stages(cycle_state: dict) -> tuple[str | None, str | None]:
    """Walk the pipeline contracts in order and return
    (last_success_stage, current_or_failed_stage) based on what cycle.py
    persisted to factory_state.json. The orchestrator uses this as the
    backbone for "where in the pipeline are we right now".
    """
    last_success: str | None = None
    current: str | None = None

    def _gen(key: str) -> bool:
        # Accept both the strict LLM-generated state and the fallback
        # report state — both produce a valid file on disk.
        return (cycle_state.get(key) or "") in {"generated", "fallback_generated"}

    # File-presence fallback so a successful prior cycle whose state
    # got reset (e.g. cycle.py crashed mid-write) doesn't show up as
    # planner_required_output_missing. Both filenames are accepted —
    # cycle.py writes both per planner_proposal protocol.
    def _planner_artifact_present() -> bool:
        return any(
            (RUNTIME_DIR / name).is_file()
            for name in ("product_planner_report.md", "planner_proposal.md")
        )

    def _designer_artifact_present() -> bool:
        return any(
            (RUNTIME_DIR / name).is_file()
            for name in ("designer_critique.md", "designer_final_review.md")
        )

    def _pm_artifact_present() -> bool:
        return (RUNTIME_DIR / "pm_decision.md").is_file()

    # 1. planner_proposal
    if _gen("product_planner_status") or _planner_artifact_present():
        last_success = "planner_proposal"
    else:
        return last_success, "planner_proposal"

    # 2. designer_review — pingpong stages produce designer_critique +
    # designer_final_review. Either being generated OR the file present
    # is enough.
    if (
        _gen("designer_critique_status")
        or _gen("designer_final_review_status")
        or _designer_artifact_present()
    ):
        last_success = "designer_review"
    else:
        return last_success, "designer_review"

    # 3. pm_decision
    if _gen("pm_decision_status") or _pm_artifact_present():
        last_success = "pm_decision"
    else:
        return last_success, "pm_decision"

    # 4. implementation_ticket
    if _gen("implementation_ticket_status"):
        last_success = "implementation_ticket"
    else:
        return last_success, "implementation_ticket"

    # 5. claude_apply — applied (changes), skipped (didn't run), failed
    apply_status = cycle_state.get("claude_apply_status") or "skipped"
    apply_changed = list(cycle_state.get("claude_apply_changed_files") or [])
    if apply_status == "applied" and apply_changed:
        last_success = "claude_apply"
    elif apply_status == "applied" and not apply_changed:
        # Edge: applied label but no diff — treat as still-on-stage.
        return last_success, "claude_apply"
    else:
        return last_success, "claude_apply"

    # 6. validation_qa
    qa_status = cycle_state.get("qa_status") or "skipped"
    if qa_status == "passed":
        last_success = "validation_qa"
    elif qa_status == "failed":
        return last_success, "validation_qa"
    else:
        # skipped / empty — pipeline hasn't reached QA yet.
        return last_success, "validation_qa"

    # 7-11 require deploy state, surfaced separately.
    current = "git_commit"
    return last_success, current


def _pipeline_diagnose() -> dict:
    """Walk on-disk state, decide (last_success_stage, current_stage,
    failed_stage, diagnostic_code). Mirrors the watchdog diagnose but
    is stage-aware.

    Returns:
      {
        "current_stage": ...,
        "last_success_stage": ...,
        "failed_stage": ... | None,
        "diagnostic_code": ... | "healthy",
        "severity": "info" | "warning" | "error",
        "evidence": [...],
        "root_cause": "...",
      }
    """
    cycle_state = _read_factory_state() or {}
    publish_state = _read_publish_state() or {}
    qa_diag = _read_qa_diagnostics() or {}
    cmd_diag = _read_command_diagnostics() or {}

    last_success, current_stage = _pipeline_classify_stages(cycle_state)
    evidence: list[str] = []

    # Honor explicit "no_changes_to_deploy" recorded by deploy_to_server.
    if (cmd_diag.get("diagnostic_code") == "no_changes_to_deploy"):
        return {
            "current_stage": "validation_qa",
            "last_success_stage": last_success,
            "failed_stage": None,
            "diagnostic_code": "no_changes_to_deploy",
            "severity": "info",
            "root_cause": "변경 파일 없음 — 배포할 것이 없습니다.",
            "evidence": ["command_diagnostics.diagnostic_code=no_changes_to_deploy"],
        }

    # Stale runner trumps everything — operator should restart first.
    is_stale, _ = _runner_is_stale_now()
    if is_stale:
        return {
            "current_stage": current_stage,
            "last_success_stage": last_success,
            "failed_stage": current_stage,
            "diagnostic_code": "stale_runner",
            "severity": "warning",
            "root_cause": "runner.py 가 부팅 이후 수정됨",
            "evidence": ["runner code mtime > start"],
        }

    # implementation_ticket missing/invalid — high-leverage failure.
    ticket_status = cycle_state.get("implementation_ticket_status") or "skipped"
    if (
        current_stage == "implementation_ticket"
        and ticket_status in {"missing", "skipped"}
        and last_success in {"pm_decision", "designer_review"}
    ):
        code = "implementation_ticket_missing"
        if ticket_status == "skipped":
            evidence.append(f"ticket_status=skipped reason="
                            f"{cycle_state.get('implementation_ticket_skipped_reason')}")
        else:
            evidence.append(
                f"ticket_status=missing — 수정 대상 파일 0개"
            )
        return {
            "current_stage": current_stage,
            "last_success_stage": last_success,
            "failed_stage": current_stage,
            "diagnostic_code": code,
            "severity": "error",
            "root_cause": "Implementation Ticket 의 수정 대상 파일이 비어 있어 다음 단계로 넘어가지 못했습니다.",
            "evidence": evidence,
        }

    # claude_apply skipped — loop signal.
    apply_status = cycle_state.get("claude_apply_status") or "skipped"
    apply_changed = list(cycle_state.get("claude_apply_changed_files") or [])
    if current_stage == "claude_apply" and apply_status == "skipped":
        return {
            "current_stage": current_stage,
            "last_success_stage": last_success,
            "failed_stage": current_stage,
            "diagnostic_code": "claude_apply_skipped",
            "severity": "warning",
            "root_cause": (
                cycle_state.get("claude_apply_skipped_reason")
                or "claude_apply 가 실행되지 않았습니다."
            ),
            "evidence": [f"claude_apply_status=skipped"],
        }

    # docs_only_change
    if cycle_state.get("docs_only"):
        return {
            "current_stage": "claude_apply",
            "last_success_stage": last_success,
            "failed_stage": "claude_apply",
            "diagnostic_code": "docs_only_change",
            "severity": "warning",
            "root_cause": "변경이 docs/config 만이라 사용자 영향이 없습니다.",
            "evidence": [f"changed_files={len(apply_changed)}"],
        }

    # no_code_change
    if (
        current_stage == "claude_apply"
        and apply_status in {"applied", "no_changes"}
        and not apply_changed
    ):
        return {
            "current_stage": current_stage,
            "last_success_stage": last_success,
            "failed_stage": current_stage,
            "diagnostic_code": "no_code_change",
            "severity": "warning",
            "root_cause": "Claude 가 변경하지 않았습니다.",
            "evidence": [f"claude_apply_status={apply_status}"],
        }

    # QA Gate paths
    qa_code = (qa_diag.get("diagnostic_code") or "").strip()
    if qa_code in {
        "qa_report_missing_after_run",
        "qa_command_failed",
        "qa_exception_before_report",
        "qa_report_path_mismatch",
    }:
        evidence.append(f"qa_diagnostic_code={qa_code}")
        if qa_diag.get("failed_command"):
            evidence.append(f"failed_command={qa_diag.get('failed_command')}")
        return {
            "current_stage": "validation_qa",
            "last_success_stage": last_success,
            "failed_stage": "validation_qa",
            "diagnostic_code": (
                "qa_gate_stuck" if qa_code == "qa_report_path_mismatch" else qa_code
            ),
            "severity": "error",
            "root_cause": qa_diag.get("suggested_action") or qa_code,
            "evidence": evidence,
        }
    if qa_code == "qa_report_missing_before_run":
        return {
            "current_stage": "validation_qa",
            "last_success_stage": last_success,
            "failed_stage": "validation_qa",
            "diagnostic_code": "qa_report_missing_before_run",
            "severity": "info",
            "root_cause": "QA 가 미실행 — on-demand 가 트리거됩니다.",
            "evidence": ["qa_report.md 부재"],
        }

    # deploy_progress
    dp = publish_state.get("deploy_progress") or {}
    failed_recent = sum(
        1 for a in (dp.get("previous_attempts") or [])
        if (a or {}).get("status") == "failed"
    )
    if dp.get("status") == "failed":
        failed_recent += 1
    if failed_recent >= 3:
        return {
            "current_stage": "github_actions",
            "last_success_stage": "validation_qa",
            "failed_stage": "github_actions",
            "diagnostic_code": "deploy_failed_repeatedly",
            "severity": "error",
            "root_cause": f"최근 deploy 시도 {failed_recent}건 실패",
            "evidence": [f"deploy_failed_recent={failed_recent}"],
        }

    # command-level diagnostics surface a recent failure we haven't
    # classified yet.
    if cmd_diag.get("status") == "failed" and cmd_diag.get("diagnostic_code"):
        code = cmd_diag.get("diagnostic_code")
        return {
            "current_stage": current_stage,
            "last_success_stage": last_success,
            "failed_stage": current_stage,
            "diagnostic_code": code,
            "severity": "warning",
            "root_cause": cmd_diag.get("failed_reason") or code,
            "evidence": [
                f"last_command={cmd_diag.get('last_command')}",
                f"failed_stage={cmd_diag.get('failed_stage')}",
            ],
        }

    # Healthy
    return {
        "current_stage": current_stage,
        "last_success_stage": last_success,
        "failed_stage": None,
        "diagnostic_code": "healthy",
        "severity": "info",
        "root_cause": "pipeline healthy",
        "evidence": [],
    }


# --- Recovery dispatch ----------------------------------------------------


def _watchdog_allow_claude_repair() -> bool:
    v = os.environ.get("FACTORY_WATCHDOG_ALLOW_CLAUDE_REPAIR", "false").strip().lower()
    return v in {"true", "1", "yes", "on"}


CLAUDE_REPAIR_PROMPT_TEMPLATE = """\
당신은 Stampport Pipeline Recovery Orchestrator 가 호출한 자동 수리 Claude Code 에이전트입니다.

운영자가 자리를 비운 상태에서 공장이 멈췄고, Watchdog 이 다음 stage 에서 실패를 감지했습니다.
당신의 임무는 *가장 작은 안전한 변경* 으로 이 stage 를 풀어내는 것입니다.

=== 진단 ===
diagnostic_code: {code}
failed_stage: {stage}
root_cause: {root_cause}
evidence:
{evidence}
=== 진단 끝 ===

기대되는 출력 (expected_output):
- 코드 변경이 필요하면 변경을 만들고, 검증이 통과하면 commit + push 까지 직접 수행
- 변경이 필요 없다고 판단되면 변경 없이 종료하고, 사유를 결과 markdown 에 명시

수정 대상 파일 후보:
- {targets}

수정 가능 디렉터리: app/**, control_tower/**, scripts/local_factory_*.sh, scripts/notify_*.*
금지: .env*, .key, .pem, .db, .runtime/, node_modules/, dist/, .venv/,
      package.json, package-lock.json, requirements.txt,
      deploy/nginx-stampport.conf, .github/workflows/deploy.yml, systemd 관련.

검증 명령:
- 변경이 app/web 또는 control_tower/web 에 있다면 해당 디렉터리에서 npm run build
- .py 파일이 변경되면 python3 -m py_compile <파일>
- 검증 실패 시 commit/push 절대 금지 — working tree 에 변경만 남기고 결과 markdown 에 거부 사유 명시.

commit/push 조건:
- 검증 통과 + secret/conflict 없음 + 현재 브랜치 main → `git push origin main` 만 허용.
- 그 외 force push / 다른 브랜치 push 절대 금지.

작업이 끝나면 마지막 응답은 OPERATOR_REQUEST_PROMPT_TEMPLATE 의 형식 (# Operator Request 결과) 으로만 출력하세요.
"""


def _build_claude_repair_prompt(diag: dict) -> str:
    code = diag.get("diagnostic_code") or "unknown"
    stage = diag.get("failed_stage") or diag.get("current_stage") or "unknown"
    root_cause = diag.get("root_cause") or ""
    evidence_lines = "\n".join(f"- {e}" for e in (diag.get("evidence") or [])) or "- (no evidence)"
    cycle_state = _read_factory_state() or {}
    targets = (
        cycle_state.get("implementation_ticket_target_files")
        or cycle_state.get("claude_apply_changed_files")
        or []
    )
    targets_line = ", ".join(targets[:10]) or "(자동 결정)"
    return CLAUDE_REPAIR_PROMPT_TEMPLATE.format(
        code=code,
        stage=stage,
        root_cause=root_cause[:400],
        evidence=evidence_lines,
        targets=targets_line,
    )


def _pipeline_decide_recovery(diag: dict) -> dict:
    """Map the diagnosis to a recovery decision: a stage rollback target,
    a list of action labels, an operator-readable description, and a
    next_action sentence the dashboard can render verbatim."""
    code = diag.get("diagnostic_code") or "unknown"
    recipe = DIAGNOSTIC_REPAIR_MAP.get(code) or DIAGNOSTIC_REPAIR_MAP["unknown"]
    contract = PIPELINE_CONTRACT_BY_STAGE.get(diag.get("failed_stage") or "") or {}
    rollback_to = recipe.get("rollback_to") or contract.get("rollback_to")
    return {
        "diagnostic_code": code,
        "actions": list(recipe.get("actions") or []),
        "description": recipe.get("description") or "",
        "rollback_to": rollback_to,
        "max_retry": int(contract.get("max_retry") or 1),
    }


def _pipeline_apply_recovery(
    diag: dict,
    decision: dict,
    pipeline_state: dict,
) -> dict:
    """Execute the safe actions described by the decision. Returns a
    result dict {applied: [...], skipped: [...], operator_required: bool,
    next_action: str}."""
    applied: list[str] = []
    skipped: list[str] = []
    operator_required = False

    for action in decision.get("actions") or []:
        if action == "noop":
            skipped.append("noop")
        elif action == "noop_clear_failed":
            # Clear deploy_progress.failed if there's no actual change
            # behind the failure.
            try:
                ps = _read_publish_state() or {}
                dp = ps.get("deploy_progress") or {}
                if dp.get("status") == "failed":
                    _set_deploy_progress(
                        "completed",
                        current_step="변경 없음 — 자동 정리",
                        log_message="pipeline orchestrator cleared stale failed deploy",
                    )
                    applied.append("clear_failed_deploy_progress")
                else:
                    skipped.append("clear_failed_deploy_progress (no failed row)")
            except Exception as e:  # noqa: BLE001
                skipped.append(f"clear_failed_deploy_progress ({e})")
        elif action == "operator_required":
            operator_required = True
            applied.append("escalated_to_operator")
        elif action == "release_deploy_lock":
            try:
                if DEPLOY_LOCK_FILE.is_file():
                    DEPLOY_LOCK_FILE.unlink()
                    applied.append("release_deploy_lock")
                else:
                    skipped.append("release_deploy_lock (no lock)")
            except OSError as e:
                skipped.append(f"release_deploy_lock ({e})")
        elif action == "reset_deploy_progress":
            try:
                _set_deploy_progress(
                    "failed",
                    failed_stage="orchestrator_reset",
                    failed_reason=diag.get("root_cause"),
                    suggested_action=decision.get("description"),
                    log_message="orchestrator reset deploy_progress",
                )
                applied.append("reset_deploy_progress")
            except Exception as e:  # noqa: BLE001
                skipped.append(f"reset_deploy_progress ({e})")
        elif action == "pause_factory":
            note = _watchdog_action_pause_factory(
                reason=f"orchestrator·{diag.get('diagnostic_code')}"
            )
            applied.append(f"pause_factory · {note}")
        elif action == "normalize_operator_state":
            # Heal a stale qa_failed row when the message body says
            # the request actually completed as no-op / applied.
            try:
                cur = _read_operator_fix_state() or {}
                prior = cur.get("status")
                msg = (cur.get("last_message") or "").lower()
                # Pick the "true" status from the message body. Default
                # to noop_success which is the most common pattern.
                if "상태 published" in msg or "pushed to main: yes" in msg:
                    new_status = "published"
                elif "상태 applied" in msg:
                    new_status = "applied"
                else:
                    new_status = "noop_success"
                _op_emit(
                    kind="operator_request_state_mismatch_detected",
                    message=(
                        f"operator_request state mismatch detected — "
                        f"prior={prior}, message indicates {new_status}"
                    ),
                    severity="warn",
                )
                _save_operator_fix_state({
                    "status": new_status,
                    "publish_status": (
                        "published" if new_status == "published"
                        else "not_requested"
                    ),
                    "last_message": (
                        f"[normalize] prior={prior} → {new_status} (watchdog "
                        f"stale 정리). 원본: {(cur.get('last_message') or '')[:300]}"
                    ),
                })
                _op_emit(
                    kind="operator_request_stale_failure_cleared",
                    message=(
                        f"operator_request stale failure cleared — "
                        f"{prior} → {new_status}"
                    ),
                    severity="info",
                )
                _op_emit(
                    kind="operator_request_health_recovered",
                    message="operator_request health recovered — Watchdog 정상화 완료",
                    severity="success",
                )
                applied.append(
                    f"normalize_operator_state · {prior} → {new_status}"
                )
            except Exception as e:  # noqa: BLE001
                skipped.append(f"normalize_operator_state ({e})")
        elif action == "claude_repair":
            if not _watchdog_allow_claude_repair():
                skipped.append("claude_repair (FACTORY_WATCHDOG_ALLOW_CLAUDE_REPAIR=false)")
                operator_required = True
                continue
            if diag.get("diagnostic_code") not in CLAUDE_REPAIR_ELIGIBLE_CODES:
                skipped.append("claude_repair (code not eligible)")
                operator_required = True
                continue
            if _INFLIGHT_COMMAND is not None:
                skipped.append("claude_repair (runner busy)")
                continue
            # Refuse if working tree is in a state we don't trust.
            dirty = _git_changed_files()
            if dirty:
                skipped.append(
                    f"claude_repair (git dirty — {len(dirty)}건; 운영자 검토 후 재시도)"
                )
                operator_required = True
                continue
            prompt = _build_claude_repair_prompt(diag)
            try:
                ok_repair, msg_repair = _h_operator_request({
                    "prompt": prompt,
                    "auto_commit_push": True,
                })
            except Exception as e:  # noqa: BLE001
                skipped.append(f"claude_repair (raised: {e})")
                operator_required = True
            else:
                if ok_repair:
                    applied.append(f"claude_repair · {msg_repair[:160]}")
                else:
                    skipped.append(f"claude_repair (failed: {msg_repair[:160]})")
                    operator_required = True
        elif action == "cancel_inflight":
            # We don't actually cancel inflight commands from here —
            # the runner is single-threaded so the call would race.
            # Surface the request as escalation instead.
            skipped.append("cancel_inflight (escalated)")
            operator_required = True
        else:
            skipped.append(f"{action} (unknown)")

    next_action = (
        decision.get("description")
        or "조치 필요 없음"
    )
    return {
        "applied": applied,
        "skipped": skipped,
        "operator_required": operator_required,
        "next_action": next_action,
    }


def _pipeline_tick() -> dict:
    """One pass of the orchestrator. Updates pipeline_state.json and
    appends an entry to recovery_history.json. Returns the decision +
    result so the watchdog can mirror them into its own log."""
    pipeline_state = _read_pipeline_state()
    if not isinstance(pipeline_state, dict):
        pipeline_state = {}

    diag = _pipeline_diagnose()
    code = diag["diagnostic_code"]

    cycle_state = _read_factory_state() or {}
    pipeline_state["cycle_id"] = cycle_state.get("cycle")
    pipeline_state["current_stage"] = diag.get("current_stage")
    pipeline_state["last_success_stage"] = diag.get("last_success_stage")
    pipeline_state["failed_stage"] = diag.get("failed_stage")
    pipeline_state["diagnostic_code"] = code
    pipeline_state["severity"] = diag.get("severity")
    pipeline_state["root_cause"] = diag.get("root_cause")
    pipeline_state["evidence"] = diag.get("evidence") or []

    history = list(pipeline_state.get("stage_history") or [])
    history.append({
        "at": _utc_now_z(),
        "stage": diag.get("current_stage"),
        "diagnostic_code": code,
        "severity": diag.get("severity"),
    })
    pipeline_state["stage_history"] = history[-PIPELINE_HISTORY_CAP:]

    if code == "healthy" or code == "no_changes_to_deploy":
        # Clear retry counters for the *previously failed* stage when we
        # transition to healthy / noop — the pipeline has caught up.
        pipeline_state["operator_required"] = False
        pipeline_state["next_action"] = (
            "조치 필요 없음" if code == "healthy"
            else "변경 파일 없음 — 배포할 것이 없습니다."
        )
        pipeline_state["last_decision"] = {"diagnostic_code": code, "actions": []}
        # If the diagnosis is no_changes_to_deploy, still execute the
        # noop_clear_failed action so any stale failed deploy_progress
        # gets cleaned up.
        if code == "no_changes_to_deploy":
            decision = _pipeline_decide_recovery(diag)
            result = _pipeline_apply_recovery(diag, decision, pipeline_state)
            pipeline_state["last_decision"] = decision
            pipeline_state["last_result"] = result
            _append_recovery_history({
                "at": _utc_now_z(),
                "failed_stage": diag.get("failed_stage"),
                "diagnostic_code": code,
                "repair_action": ",".join(decision.get("actions") or []),
                "result": "success" if result["applied"] else "skipped",
                "next_stage": decision.get("rollback_to"),
            })
        _save_pipeline_state(pipeline_state)
        return {"diagnosis": diag, "decision": None, "result": None}

    decision = _pipeline_decide_recovery(diag)

    # Retry budget — if exceeded, force operator_required.
    retries = dict(pipeline_state.get("retry_count_by_stage") or {})
    stage = diag.get("failed_stage") or diag.get("current_stage") or "unknown"
    cur_retry = int(retries.get(stage) or 0)
    max_retry = int(decision.get("max_retry") or 1)

    if cur_retry >= max_retry:
        result = {
            "applied": [],
            "skipped": [f"retry_exceeded ({cur_retry}/{max_retry})"],
            "operator_required": True,
            "next_action": (
                f"같은 실패가 {cur_retry}회 반복되어 운영자 확인이 필요합니다."
            ),
        }
    else:
        # Bump retry count BEFORE applying so a crash inside doesn't
        # leave us stuck in an infinite-retry loop.
        retries[stage] = cur_retry + 1
        pipeline_state["retry_count_by_stage"] = retries
        _save_pipeline_state(pipeline_state)
        result = _pipeline_apply_recovery(diag, decision, pipeline_state)

    pipeline_state["last_decision"] = decision
    pipeline_state["last_result"] = result
    pipeline_state["operator_required"] = bool(result.get("operator_required"))
    pipeline_state["next_action"] = result.get("next_action") or decision.get("description")

    _append_recovery_history({
        "at": _utc_now_z(),
        "failed_stage": diag.get("failed_stage"),
        "diagnostic_code": code,
        "repair_action": ",".join(decision.get("actions") or []),
        "result": (
            "success" if result["applied"]
            else ("skipped" if result["skipped"] else "noop")
        ),
        "next_stage": decision.get("rollback_to"),
    })
    _save_pipeline_state(pipeline_state)
    return {"diagnosis": diag, "decision": decision, "result": result}


def _build_pipeline_recovery_meta() -> dict:
    """Heartbeat metadata block under `local_factory.pipeline_recovery`.
    Always present so the dashboard can render the orchestrator panel
    even before the first tick lands."""
    state = _read_pipeline_state()
    history = _read_recovery_history()
    return {
        "cycle_id": state.get("cycle_id"),
        "current_stage": state.get("current_stage"),
        "last_success_stage": state.get("last_success_stage"),
        "failed_stage": state.get("failed_stage"),
        "diagnostic_code": state.get("diagnostic_code"),
        "severity": state.get("severity"),
        "root_cause": state.get("root_cause"),
        "evidence": state.get("evidence") or [],
        "retry_count_by_stage": state.get("retry_count_by_stage") or {},
        "stage_history": list(state.get("stage_history") or [])[-PIPELINE_HISTORY_CAP:],
        "operator_required": bool(state.get("operator_required")),
        "next_action": state.get("next_action"),
        "last_decision": state.get("last_decision"),
        "last_result": state.get("last_result"),
        "stage_order": list(PIPELINE_STAGE_ORDER),
        "claude_repair_allowed": _watchdog_allow_claude_repair(),
        "recovery_history": history[-PIPELINE_RECOVERY_CAP:],
    }


# ---------------------------------------------------------------------------
# Forward Progress Detector
#
# Liveness ≠ progress. heartbeat ok + factory.status=running can sit on
# the same cycle for an hour without producing a single line of code,
# and that's the failure mode this module is built for.
#
# The detector reads the same on-disk evidence the Pipeline Recovery
# Orchestrator reads, then asks ONE additional question at every level:
#
#   "Has the work actually moved forward since we last looked?"
#
# Inputs:
#   - pipeline_state.current_stage (where we are in the pipeline)
#   - cycle_state.* (artifact statuses + per-stage timestamps)
#   - publish_state.last_commit_at / last_push_at (release motion)
#   - file mtimes for required outputs
#   - cycle_state.cycle_log[] (count consecutive planning_only / no_code_change)
#
# Outputs (heartbeat metadata.local_factory.forward_progress):
#   status   — progressing | blocked | stuck | planning_only | no_progress | operator_required
#   plus all the supporting fields the dashboard uses to render the
#   "RUNNING but no progress" banner.
# ---------------------------------------------------------------------------

FORWARD_PROGRESS_STATE_FILE = RUNTIME_DIR / "forward_progress_state.json"
FORWARD_PROGRESS_LOG_CAP = 30
# How many trailing cycles of "planning only" / "no code change" we need
# before flipping status to planning_only_loop.
FORWARD_PROGRESS_PLANNING_LOOP_THRESHOLD = 2

# Per-stage timeouts in seconds. Mirrors the spec exactly.
FORWARD_PROGRESS_STAGE_TIMEOUTS: dict[str, float] = {
    "planner_proposal":           300,
    "designer_review":            300,
    "pm_decision":                300,
    "implementation_ticket":      300,
    "claude_apply":               600,
    "validation_qa":              600,
    "git_commit":                 180,
    "git_push":                   180,
    "github_actions":             600,
    "server_verification":        600,
    "browser_cache_verification": 600,
}

# Per-stage required output check. Each entry returns
# (label, exists_bool, last_at_iso_or_none) so the heartbeat shape
# stays consistent regardless of whether the artifact is a file or a
# state field.
def _fp_required_output(
    stage: str, cycle_state: dict, publish_state: dict
) -> tuple[str, bool, str | None]:
    state = cycle_state or {}
    pub = publish_state or {}

    if stage == "planner_proposal":
        path = RUNTIME_DIR / "planner_proposal.md"
        path2 = RUNTIME_DIR / "product_planner_report.md"
        any_exists = path.is_file() or path2.is_file()
        last_at = state.get("product_planner_at") or _file_mtime_iso(
            path if path.is_file() else path2
        )
        return ("product_planner_report.md", any_exists, last_at)

    if stage == "designer_review":
        c = RUNTIME_DIR / "designer_critique.md"
        f = RUNTIME_DIR / "designer_final_review.md"
        any_exists = c.is_file() or f.is_file()
        last_at = (
            state.get("designer_final_review_at")
            or state.get("designer_critique_at")
            or _file_mtime_iso(f if f.is_file() else c)
        )
        return ("designer_critique.md or designer_final_review.md", any_exists, last_at)

    if stage == "pm_decision":
        path = RUNTIME_DIR / "pm_decision.md"
        return ("pm_decision.md", path.is_file(),
                state.get("pm_decision_at") or _file_mtime_iso(path))

    if stage == "implementation_ticket":
        path = RUNTIME_DIR / "implementation_ticket.md"
        ticket_status = state.get("implementation_ticket_status") or "skipped"
        exists = path.is_file() and ticket_status == "generated"
        return (
            "implementation_ticket.md (status=generated)",
            exists,
            state.get("implementation_ticket_at") or _file_mtime_iso(path),
        )

    if stage == "claude_apply":
        changed = list(state.get("claude_apply_changed_files") or [])
        # Required output = at least one product-code file change.
        product_code_changed = any(
            f.startswith("app/") or f.startswith("control_tower/web/src/")
            for f in changed
        )
        return (
            "claude_apply: changed_files_count > 0 (product code path)",
            len(changed) > 0 and product_code_changed,
            state.get("claude_apply_at"),
        )

    if stage == "validation_qa":
        rep = RUNTIME_DIR / "qa_report.md"
        diag = RUNTIME_DIR / "qa_diagnostics.json"
        exists = rep.is_file() or diag.is_file()
        last_at = _file_mtime_iso(rep if rep.is_file() else diag)
        return ("qa_report.md or qa_diagnostics.json", exists, last_at)

    if stage == "git_commit":
        commit_hash = pub.get("last_commit_hash")
        return (
            "publish_state.last_commit_hash",
            bool(commit_hash),
            pub.get("last_push_at"),
        )

    if stage == "git_push":
        return (
            "publish_state.last_push_status == succeeded",
            (pub.get("last_push_status") in {"ok", "succeeded"}),
            pub.get("last_push_at"),
        )

    if stage == "github_actions":
        dp = pub.get("deploy_progress") or {}
        ok = dp.get("status") in {"actions_triggered", "completed"}
        return ("deploy_progress.status in {actions_triggered, completed}",
                ok, dp.get("updated_at"))

    if stage in {"server_verification", "browser_cache_verification"}:
        # No automated signal yet — flag as "operator owns this".
        return (f"{stage} (operator-verified)", False, None)

    return ("(unknown stage)", False, None)


def _file_mtime_iso(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return (
            datetime.utcfromtimestamp(path.stat().st_mtime)
            .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        )
    except OSError:
        return None


def _read_forward_progress_state() -> dict:
    if not FORWARD_PROGRESS_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(FORWARD_PROGRESS_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_forward_progress_state(state: dict) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _utc_now_z()
        FORWARD_PROGRESS_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError) as e:
        sys.stderr.write(f"[runner] failed to write forward_progress_state: {e}\n")


def _fp_count_trailing_cycle_log_kinds(
    cycle_state: dict,
    kinds: set[str],
) -> int:
    """How many of the trailing cycle_log entries match `kinds`. Used
    to decide planning_only_loop after multiple cycles in a row."""
    log = cycle_state.get("cycle_log") or []
    if not isinstance(log, list):
        return 0
    n = 0
    for entry in reversed(log):
        if not isinstance(entry, dict):
            break
        if entry.get("kind") in kinds:
            n += 1
        else:
            break
    return n


def _forward_progress_diagnose() -> dict:
    """Compute the forward_progress block. Persists running state to
    forward_progress_state.json so we can compute current_stage_elapsed_sec
    across watchdog ticks. Returns the metadata dict the heartbeat
    publishes verbatim."""
    pipeline_state = _read_pipeline_state() or {}
    cycle_state = _read_factory_state() or {}
    publish_state = _read_publish_state() or {}
    saved = _read_forward_progress_state() or {}

    now = time.time()
    now_iso = _utc_now_z()

    current_stage = pipeline_state.get("current_stage") or "planner_proposal"
    last_seen_stage = saved.get("current_stage")
    last_stage_changed_at = saved.get("last_stage_changed_at") or now_iso
    last_stage_changed_epoch = float(saved.get("_last_stage_changed_epoch") or now)

    if current_stage != last_seen_stage:
        last_stage_changed_at = now_iso
        last_stage_changed_epoch = now

    elapsed_sec = max(0.0, now - last_stage_changed_epoch)

    # Required output for the *current* stage.
    req_label, req_exists, req_last_at = _fp_required_output(
        current_stage, cycle_state, publish_state
    )

    # Convenience signals copied through to the heartbeat.
    apply_status = cycle_state.get("claude_apply_status") or "skipped"
    apply_changed = list(cycle_state.get("claude_apply_changed_files") or [])
    changed_count = len(apply_changed)
    ticket_path = RUNTIME_DIR / "implementation_ticket.md"
    ticket_status = cycle_state.get("implementation_ticket_status") or "skipped"
    ticket_exists = ticket_path.is_file() and ticket_status == "generated"
    qa_report = RUNTIME_DIR / "qa_report.md"
    qa_diag_path = RUNTIME_DIR / "qa_diagnostics.json"
    qa_report_exists = qa_report.is_file() or qa_diag_path.is_file()

    # Latest motion timestamps. We pull from a mix of file mtimes and
    # state-recorded timestamps so a missing file doesn't blank out a
    # field we already know.
    last_artifact_at = max(
        filter(None, [
            cycle_state.get("product_planner_at"),
            cycle_state.get("designer_critique_at"),
            cycle_state.get("designer_final_review_at"),
            cycle_state.get("pm_decision_at"),
            cycle_state.get("implementation_ticket_at"),
            cycle_state.get("claude_proposal_at"),
        ]),
        default=None,
    )
    last_required_output_at = req_last_at
    last_code_changed_at = cycle_state.get("claude_apply_at") if changed_count > 0 else None
    last_commit_at = publish_state.get("last_push_at") if publish_state.get("last_commit_hash") else None
    last_push_at = (
        publish_state.get("last_push_at")
        if publish_state.get("last_push_status") in {"ok", "succeeded"} else None
    )

    timeout_sec = float(FORWARD_PROGRESS_STAGE_TIMEOUTS.get(current_stage, 600))

    # planning_only_loop signal — count consecutive cycle_log markers.
    planning_streak = _fp_count_trailing_cycle_log_kinds(
        cycle_state,
        {"cycle_planning_only"},
    )
    no_change_streak = _fp_count_trailing_cycle_log_kinds(
        cycle_state,
        {"cycle_produced_no_code_change", "cycle_produced_docs_only"},
    )

    # ----- Status decision ----------------------------------------------
    status = "progressing"
    diagnostic_code = "healthy"
    blocking_reason: str | None = None
    next_action: str | None = None
    operator_required = False

    # 1) Pipeline orchestrator already escalated to operator? Inherit.
    if pipeline_state.get("operator_required"):
        status = "operator_required"
        diagnostic_code = pipeline_state.get("diagnostic_code") or "operator_required"
        blocking_reason = pipeline_state.get("root_cause") or "운영자 확인 필요"
        next_action = pipeline_state.get("next_action") or "operator_required"
        operator_required = True

    # 2) deploy_to_server already declared no_changes_to_deploy.
    elif (
        (cycle_state.get("status") in {"succeeded", "planning_only", "no_code_change"})
        and not _git_changed_files()
    ):
        status = "no_progress" if changed_count == 0 else "progressing"
        diagnostic_code = "no_changes_to_validate"
        blocking_reason = "changed_files=0 — 검증/배포할 변경사항 없음"
        next_action = (
            "다음 사이클을 기다리거나, operator_request 로 수동 변경 지시를 내리세요."
        )

    # 3) planning_only_loop — multiple cycles producing only artifacts.
    elif (
        planning_streak >= FORWARD_PROGRESS_PLANNING_LOOP_THRESHOLD
        and changed_count == 0
    ):
        status = "planning_only"
        diagnostic_code = "planning_only_loop"
        blocking_reason = (
            f"최근 {planning_streak}회 사이클이 기획 산출물만 만들고 코드 변경 없음."
        )
        next_action = (
            "Continuous OFF + Product Director/PM 단계로 rollback — 운영자 확인 필요."
        )
        operator_required = True

    # 4) no_change_loop — claude_apply applied but no real code change.
    elif (
        no_change_streak >= FORWARD_PROGRESS_PLANNING_LOOP_THRESHOLD
        and changed_count == 0
    ):
        status = "planning_only"
        diagnostic_code = "no_code_change_loop"
        blocking_reason = (
            f"최근 {no_change_streak}회 사이클이 코드 변경 없이 종료."
        )
        next_action = (
            "Claude 에게 실제 app/web/src 또는 control_tower/web/src 파일 수정을 재요청."
        )
        operator_required = True

    # 5) Stage stuck — required output missing past timeout.
    elif (not req_exists) and elapsed_sec > timeout_sec:
        status = "stuck"
        diagnostic_code = "current_stage_stuck"
        blocking_reason = (
            f"`{current_stage}` 가 {int(elapsed_sec)}s째 진행 중이지만 "
            f"required output ({req_label}) 가 없습니다 (timeout={int(timeout_sec)}s)."
        )
        # Stage-specific next_action — concrete and short.
        next_action = (
            "Implementation Ticket 재생성 — PM 단계로 rollback."
            if current_stage == "implementation_ticket"
            else "Claude Apply 재실행 — Implementation Ticket 기반 코드 변경 요청."
            if current_stage == "claude_apply"
            else "QA Gate 재실행 또는 Claude Repair 요청."
            if current_stage == "validation_qa"
            else "운영자 확인 — stage 가 timeout 을 초과했습니다."
        )

    # 6) Required output missing but still within timeout — blocked.
    elif not req_exists:
        # Exception: claude_apply with changed_count=0 is the
        # "no_changes_to_validate / no_code_change" case; we surface it
        # as no_progress instead of stuck.
        if current_stage == "claude_apply" and apply_status == "skipped":
            status = "blocked"
            diagnostic_code = "claude_apply_skipped"
            blocking_reason = (
                cycle_state.get("claude_apply_skipped_reason")
                or "claude_apply 가 skipped — 개발 단계 미실행"
            )
            next_action = (
                "Implementation Ticket 확인 후 claude_apply 재실행 또는 "
                "operator_request 로 수동 변경 지시."
            )
        elif current_stage == "implementation_ticket" and not ticket_exists:
            status = "blocked"
            diagnostic_code = "implementation_ticket_missing"
            blocking_reason = (
                "Implementation Ticket 의 수정 대상 파일이 비어 있어 "
                "다음 단계로 못 넘어가고 있습니다."
            )
            next_action = "PM 단계로 rollback — ticket 재생성 필요."
        elif current_stage == "validation_qa" and not qa_report_exists and changed_count == 0:
            # The "QA Gate failed but no changes" mis-classification —
            # surface it explicitly so the dashboard stops showing
            # "QA failed" red.
            status = "no_progress"
            diagnostic_code = "no_changes_to_validate"
            blocking_reason = (
                "변경 파일 0개 — 검증할 변경사항이 없어 QA 실행이 필요 없습니다."
            )
            next_action = "다음 사이클의 코드 변경을 기다리거나 operator_request 로 변경 지시."
        else:
            status = "blocked"
            diagnostic_code = "required_output_missing"
            blocking_reason = (
                f"`{current_stage}` 가 진행 중이지만 required output "
                f"({req_label}) 가 아직 없습니다."
            )
            next_action = (
                f"`{current_stage}` 산출물이 만들어질 때까지 대기 — "
                f"timeout={int(timeout_sec)}s."
            )

    # 7) heartbeat ok + factory running + same stage frozen + no required
    # output + no recent code change → no_progress.
    factory_alive_running = bool(
        cycle_state.get("status") == "running"
        and current_stage == last_seen_stage
        and elapsed_sec > min(timeout_sec, 300)
    )
    if (
        status == "progressing"
        and factory_alive_running
        and not req_exists
        and changed_count == 0
        and not last_commit_at
    ):
        status = "no_progress"
        diagnostic_code = "no_progress_despite_heartbeat"
        blocking_reason = (
            f"heartbeat 정상이지만 `{current_stage}` 에서 {int(elapsed_sec)}s 동안 "
            "산출물 / 코드 변경 / commit 모두 없음."
        )
        next_action = (
            "Pipeline Orchestrator 의 next_action 에 따라 자동 복구 또는 operator_required."
        )

    # ----- Persist + return ---------------------------------------------
    new_state = {
        "current_stage": current_stage,
        "last_stage_changed_at": last_stage_changed_at,
        "_last_stage_changed_epoch": last_stage_changed_epoch,
        "last_artifact_created_at": last_artifact_at,
        "last_required_output_created_at": last_required_output_at,
        "last_code_changed_at": last_code_changed_at,
        "last_commit_at": last_commit_at,
        "last_push_at": last_push_at,
        "status": status,
        "diagnostic_code": diagnostic_code,
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "operator_required": operator_required,
        "elapsed_sec": int(elapsed_sec),
        "timeout_sec": int(timeout_sec),
        "required_output_label": req_label,
        "required_output_exists": req_exists,
        "implementation_ticket_exists": ticket_exists,
        "claude_apply_status": apply_status,
        "changed_files_count": changed_count,
        "qa_report_exists": qa_report_exists,
        "planning_streak": planning_streak,
        "no_change_streak": no_change_streak,
    }
    _save_forward_progress_state(new_state)

    # Heartbeat-shaped output (drops internal _epoch helpers).
    return {
        "status": status,
        "current_stage": current_stage,
        "current_stage_elapsed_sec": int(elapsed_sec),
        "stage_timeout_sec": int(timeout_sec),
        "last_stage_changed_at": last_stage_changed_at,
        "last_artifact_created_at": last_artifact_at,
        "last_required_output_created_at": last_required_output_at,
        "last_code_changed_at": last_code_changed_at,
        "last_commit_at": last_commit_at,
        "last_push_at": last_push_at,
        "required_output": req_label,
        "required_output_exists": req_exists,
        "implementation_ticket_exists": ticket_exists,
        "claude_apply_status": apply_status,
        "changed_files_count": changed_count,
        "qa_report_exists": qa_report_exists,
        "blocking_reason": blocking_reason,
        "diagnostic_code": diagnostic_code,
        "next_action": next_action,
        "operator_required": operator_required,
        "planning_only_streak": planning_streak,
        "no_code_change_streak": no_change_streak,
        "stage_timeouts": dict(FORWARD_PROGRESS_STAGE_TIMEOUTS),
    }


# ---------------------------------------------------------------------------
# Agent Supervisor — meta-agent that audits the *other* agents' work.
# Runs on every watchdog tick and at the end of every cycle (via
# cycle.py). Lives in agent_supervisor.py to avoid circular imports.
# ---------------------------------------------------------------------------


def _supervisor_run() -> dict | None:
    """Best-effort wrapper around agent_supervisor.run_supervisor().
    Returns the report dict, or None when the module isn't importable
    (which only happens in degraded test environments)."""
    try:
        from . import agent_supervisor as _sup
    except ImportError as e:
        sys.stderr.write(f"[runner] agent_supervisor import failed: {e}\n")
        return None
    try:
        return _sup.run_supervisor()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[runner] agent_supervisor.run_supervisor raised: {e}\n")
        return None


def _supervisor_read_report() -> dict:
    try:
        from . import agent_supervisor as _sup
    except ImportError:
        return {}
    try:
        return _sup.read_report() or {}
    except Exception:  # noqa: BLE001
        return {}


def _build_agent_accountability_meta() -> dict:
    """Heartbeat metadata block under `local_factory.agent_accountability`.
    Reads the persisted JSON the supervisor wrote on its last tick — the
    runner doesn't run the supervisor here to keep heartbeats cheap;
    that fires from the watchdog tick + cycle.py end-of-main."""
    rep = _supervisor_read_report()
    if not rep:
        return {
            "available": False,
            "overall_status": "unknown",
            "blocking_agent": None,
            "blocking_reason": None,
            "operator_required": False,
            "agents": {},
            "implementation_ticket_exists": False,
            "meaningful_change": False,
            "changed_files": [],
            "affected_screens": [],
            "affected_flows": [],
            "qa_scenarios": [],
            "next_action": "supervisor 가 아직 한 번도 실행되지 않았습니다.",
        }
    return {
        "available": True,
        "evaluated_at": rep.get("evaluated_at"),
        "cycle_id": rep.get("cycle_id"),
        "overall_status": rep.get("overall_status"),
        "blocking_agent": rep.get("blocking_agent"),
        "blocking_reason": rep.get("blocking_reason"),
        "operator_required": bool(rep.get("operator_required")),
        "agents": rep.get("agents") or {},
        "implementation_ticket_exists": bool(rep.get("implementation_ticket_exists")),
        "meaningful_change": bool(rep.get("meaningful_change")),
        "changed_files": list(rep.get("changed_files") or [])[:30],
        "affected_screens": list(rep.get("affected_screens") or []),
        "affected_flows": list(rep.get("affected_flows") or []),
        "qa_scenarios": list(rep.get("qa_scenarios") or []),
        "commit_hash": rep.get("commit_hash"),
        "push_status": rep.get("push_status"),
        "next_action": rep.get("next_action"),
    }


# ---------------------------------------------------------------------------
# Control State aggregator wrapper.
# Single source-of-truth verdict — read by the dashboard's
# OverallStatusBar so every panel speaks one language.
# ---------------------------------------------------------------------------


def _control_state_aggregate(runner_meta: dict | None = None) -> dict | None:
    try:
        from . import control_state as _cs
    except ImportError as e:
        sys.stderr.write(f"[runner] control_state import failed: {e}\n")
        return None
    try:
        return _cs.aggregate(runner_meta)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[runner] control_state.aggregate raised: {e}\n")
        return None


def _control_state_read() -> dict:
    try:
        from . import control_state as _cs
    except ImportError:
        return {}
    try:
        return _cs.read_state() or {}
    except Exception:  # noqa: BLE001
        return {}


def _build_control_state_meta() -> dict:
    """Heartbeat metadata block under `local_factory.control_state`.
    Reads the file the watchdog last wrote — keeps heartbeats cheap."""
    rep = _control_state_read()
    if not rep:
        return {
            "available": False,
            "status": "idle",
            "summary": "control_state 가 아직 작성되지 않음",
        }
    rep["available"] = True
    return rep


def _build_operator_request_health_meta() -> dict:
    """Compose `local_factory.operator_request_health` — the dashboard's
    one-stop view of "is the operator_request loop healthy or holding a
    stale failure". Reads operator_fix_state.json + command_diagnostics
    and never claims healthy when the persisted status is in a real
    failure state.
    """
    op_state = _read_operator_fix_state() or {}
    cmd_diag = _read_command_diagnostics() or {}

    last_status = op_state.get("status") or "idle"
    last_msg = op_state.get("last_message") or ""
    last_msg_lc = last_msg.lower()

    OPERATOR_FAILURE_STATES = {
        "failed", "qa_failed", "validation_failed",
        "no_code_change_failed", "push_failed", "git_failed",
    }
    OPERATOR_HEALTHY_STATES = {
        "idle", "running", "applied", "published",
        "noop_success", "no_changes",
    }

    healthy_keywords = (
        "상태 applied", "상태 noop", "상태 published",
        "noop completed", "noop_success", "코드 변경 없이",
    )
    indicates_healthy = any(k in last_msg_lc for k in healthy_keywords)
    stale_state_mismatch = bool(
        last_status in OPERATOR_FAILURE_STATES and indicates_healthy
    )

    if last_status == "running":
        status = "degraded" if stale_state_mismatch else "unknown"
    elif last_status in OPERATOR_HEALTHY_STATES:
        status = "healthy"
    elif stale_state_mismatch:
        status = "degraded"
    elif last_status in OPERATOR_FAILURE_STATES:
        status = "broken"
    else:
        status = "unknown"

    diagnostic_code = cmd_diag.get("diagnostic_code") if (
        cmd_diag.get("last_command") == "operator_request"
    ) else None
    failed_stage = cmd_diag.get("failed_stage") if (
        cmd_diag.get("last_command") == "operator_request"
    ) else None
    suggested = cmd_diag.get("suggested_action") if (
        cmd_diag.get("last_command") == "operator_request"
    ) else None
    if stale_state_mismatch:
        diagnostic_code = "stale_state_mismatch"
        suggested = (
            "operator_fix_state.status 를 noop_success / applied 로 정규화하면 "
            "Watchdog 의 stale 실패 루프가 풀립니다."
        )

    claude_cmd = " ".join(_resolve_claude_command())

    return {
        "status": status,
        "last_status": last_status,
        "diagnostic_code": diagnostic_code,
        "failed_stage": failed_stage,
        "failed_reason": (
            cmd_diag.get("failed_reason")
            if cmd_diag.get("last_command") == "operator_request" else None
        ),
        "claude_command": claude_cmd,
        "changed_files_count": len(op_state.get("changed_files") or []),
        "stdout_tail": cmd_diag.get("stdout_tail"),
        "stderr_tail": cmd_diag.get("stderr_tail"),
        "suggested_action": suggested,
        "stale_state_mismatch": stale_state_mismatch,
        "last_checked_at": _utc_now_z(),
    }


def _build_forward_progress_meta() -> dict:
    """Heartbeat metadata block under `local_factory.forward_progress`.
    Always present so the dashboard can render the panel even before
    the first watchdog tick. Computes diagnose synchronously — cheap
    file reads only."""
    try:
        return _forward_progress_diagnose()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[runner] forward_progress build failed: {e}\n")
        return {
            "status": "no_progress",
            "current_stage": None,
            "current_stage_elapsed_sec": 0,
            "stage_timeout_sec": 0,
            "blocking_reason": f"forward_progress build failed: {e}",
            "diagnostic_code": "unknown",
            "next_action": "운영자가 runner 로그 확인 필요",
            "operator_required": True,
            "required_output": None,
            "required_output_exists": False,
            "implementation_ticket_exists": False,
            "claude_apply_status": "unknown",
            "changed_files_count": 0,
            "qa_report_exists": False,
            "stage_timeouts": dict(FORWARD_PROGRESS_STAGE_TIMEOUTS),
        }


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
                state.get("product_planner_status") in {"generated", "fallback_generated"}
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
        "command_diagnostics": _build_command_diagnostics_meta(),
        "watchdog": _build_watchdog_meta(),
        "pipeline_recovery": _build_pipeline_recovery_meta(),
        "forward_progress": _build_forward_progress_meta(),
        "agent_accountability": _build_agent_accountability_meta(),
        "operator_request_health": _build_operator_request_health_meta(),
        "control_state": _build_control_state_meta(),
        "operator_fix": _build_operator_fix_meta(),
        "cycle_effectiveness": _build_cycle_effectiveness_meta(state),
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
            "log": [],
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
        # Lifecycle event log written by _op_emit during operator_request.
        # The dashboard's SystemLog reads this via cycleEventSynth so each
        # operator_request shows operator_request_received → claude_*
        # → validation_* → commit_created → push_completed.
        "log": list(state.get("log") or [])[-OPERATOR_REQUEST_LOG_CAP:],
    }


def _build_cycle_effectiveness_meta(state: dict) -> dict:
    """Compose the heartbeat's `metadata.local_factory.cycle_effectiveness`
    block — the dashboard's source-of-truth for "did the most recent
    cycle actually change product code, or did we just spin?".

    Pulls cycle status / claude_apply detail from factory_state.json,
    the commit_hash + push status from publish_state.json, and a short
    preview of the implementation_ticket.md the cycle produced (or
    didn't produce) from disk. Together those answer "was this cycle
    real" without forcing the dashboard to cross-reference three files.
    """
    code_changed = bool(state.get("code_changed"))
    apply_status = state.get("claude_apply_status") or "skipped"
    apply_message = state.get("claude_apply_message")
    changed_files = list(state.get("claude_apply_changed_files") or [])
    no_code_change_reason = state.get("no_code_change_reason")
    failed_stage = state.get("failed_stage")
    failed_reason = state.get("failed_reason")
    suggested_action = state.get("suggested_action")
    cycle_log = list(state.get("cycle_log") or [])

    # Per-tier file-change flags. cycle.py sets these at end of main()
    # via _categorize_changed_files; default to False on older state
    # files that don't carry them yet.
    frontend_changed = bool(state.get("frontend_changed"))
    backend_changed = bool(state.get("backend_changed"))
    control_tower_changed = bool(state.get("control_tower_changed"))
    docs_only = bool(state.get("docs_only"))

    # Implementation Ticket — the per-cycle source-of-truth document.
    # We surface its existence + a small preview so the panel can render
    # "이번 사이클은 X 기능을 구현하기로 했는데 ...".
    ticket_path = state.get("implementation_ticket_path")
    if not ticket_path:
        candidate = RUNTIME_DIR / "implementation_ticket.md"
        if candidate.is_file():
            ticket_path = str(candidate)
    ticket_exists = bool(ticket_path) and Path(ticket_path).is_file()
    ticket_preview = None
    if ticket_exists:
        ticket_preview = _artifact_preview(Path(ticket_path), max_chars=480)

    ticket_status = state.get("implementation_ticket_status") or "skipped"
    ticket_target_files = list(state.get("implementation_ticket_target_files") or [])
    ticket_target_screens = list(state.get("implementation_ticket_target_screens") or [])
    ticket_feature = state.get("implementation_ticket_selected_feature")
    ticket_message = state.get("implementation_ticket_message")
    ticket_at = state.get("implementation_ticket_at")
    ticket_skipped_reason = state.get("implementation_ticket_skipped_reason")

    # Validation status rolled up from the validation-class stages so
    # the FE doesn't have to scan all stages itself. Picks the worst
    # status so a single failed build_app shows as "failed".
    validation_status = "skipped"
    for sr in state.get("stages") or []:
        if sr.get("name") not in {
            "build_app", "build_control", "syntax_check", "qa_gate", "qa_recheck",
        }:
            continue
        st = sr.get("status")
        if st == "failed":
            validation_status = "failed"
            break
        if st == "passed" and validation_status != "failed":
            validation_status = "passed"

    # Pull commit / push status from publish_state.json so the panel
    # can show "이 사이클이 만든 변경이 push 되었는가" without the FE
    # having to cross-reference two heartbeat blocks.
    publish_state = _read_publish_state() or {}
    commit_hash = publish_state.get("last_commit_hash")
    push_status = publish_state.get("last_push_status")
    push_at = publish_state.get("last_push_at")
    push_files = publish_state.get("last_pushed_files") or []

    # The publish/commit only "belongs" to this cycle if the pushed
    # file list overlaps with claude_apply_changed_files. Otherwise we
    # leave commit_hash null so the UI doesn't claim a stale commit
    # represents this cycle's output.
    if commit_hash and changed_files:
        push_set = set(push_files)
        if not push_set.intersection(changed_files):
            commit_hash = None
            push_status = None
            push_at = None

    return {
        "cycle_id": state.get("cycle"),
        "status": state.get("status"),
        "code_changed": code_changed,
        "changed_files_count": len(changed_files),
        "changed_files": changed_files[:30],
        "frontend_changed": frontend_changed,
        "backend_changed": backend_changed,
        "control_tower_changed": control_tower_changed,
        "docs_only": docs_only,
        "implementation_ticket_status": ticket_status,
        "implementation_ticket_exists": ticket_exists,
        "implementation_ticket_path": ticket_path,
        "implementation_ticket_preview": ticket_preview,
        "implementation_ticket_selected_feature": ticket_feature,
        "implementation_ticket_target_files": ticket_target_files[:20],
        "implementation_ticket_target_files_count": len(ticket_target_files),
        "implementation_ticket_target_screens": ticket_target_screens[:8],
        "implementation_ticket_message": ticket_message,
        "implementation_ticket_at": ticket_at,
        "implementation_ticket_skipped_reason": ticket_skipped_reason,
        "validation_status": validation_status,
        "commit_hash": commit_hash,
        "commit_hash_short": (commit_hash[:8] if commit_hash else None),
        "push_status": push_status,
        "push_at": push_at,
        "no_code_change_reason": no_code_change_reason,
        "last_claude_apply_status": apply_status,
        "last_claude_apply_message": apply_message,
        "failed_stage": failed_stage,
        "failed_reason": failed_reason,
        "suggested_action": suggested_action,
        "cycle_log": cycle_log[-30:],  # last 30 entries — keep payload small
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

    # Pull the latest deploy-time diagnostic blob so the UI can render
    # *why* the gate behaved the way it did (path mismatch / on-demand
    # crashed / specific build command failed) — not just status.
    diag = _read_qa_diagnostics()
    diagnostic_code = diag.get("diagnostic_code") or "unknown"
    suggested_action = diag.get("suggested_action") or _qa_diagnostic_suggested(diagnostic_code)

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
        # New: deploy-time diagnostics so the UI can show diagnostic_code
        # / failed_command / stderr_tail / suggested_action without
        # cross-referencing two files.
        "diagnostic_code": diagnostic_code,
        "suggested_action": suggested_action,
        "report_exists_before": diag.get("report_exists_before"),
        "report_exists_after": diag.get("report_exists_after"),
        "qa_required_reason": diag.get("qa_required_reason"),
        "failed_command": diag.get("failed_command"),
        "exit_code": diag.get("exit_code"),
        "stdout_tail": diag.get("stdout_tail"),
        "stderr_tail": diag.get("stderr_tail"),
        "exception_message": diag.get("exception_message"),
        "step_results": diag.get("step_results") or [],
        "decided_at": diag.get("decided_at"),
        "cycle_report_path": diag.get("cycle_report_path"),
        "stale_runner": bool(diag.get("stale_runner")),
        "changed_files": diag.get("changed_files") or [],
    }


def _build_command_diagnostics_meta() -> dict:
    """Surface the most recent dispatched-command's structured failure.
    Always present so the dashboard can decide enabled=true/false."""
    cur = _read_command_diagnostics()
    if not cur:
        return {
            "last_command": None,
            "status": "idle",
            "failed_stage": None,
            "diagnostic_code": None,
            "failed_reason": None,
            "suggested_action": None,
            "occurred_at": None,
        }
    return cur


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
    is_stale, stale_detail = _runner_is_stale_now()
    return {
        "pid": RUNNER_PID,
        "started_at": RUNNER_STARTED_AT,
        "code_mtime_at": _runner_code_mtime_iso(),
        "code_mtime_at_start_epoch": _RUNNER_CODE_MTIME_AT_START or None,
        "code_mtime_now_epoch": stale_detail.get("code_mtime_now"),
        "is_stale": is_stale,
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

    # Pile-up dedupe — when rapid clicks land multiple of the same
    # build_check / test_check / deploy_to_server in the queue, the
    # *first* to claim runs; subsequent ones within DEDUPE_WINDOW_SEC
    # short-circuit with "already_running". deploy_to_server has its
    # own command_id idempotency on top of this so a server retry of
    # the same id still gets the cached outcome.
    if _is_duplicate_recent_command(name, cid):
        prev = _LAST_COMMAND_RESULT.get(name) or {}
        prev_msg = prev.get("message") or "no detail"
        report_result(
            cid, True,
            f"already_running: {name} 가 최근 {DEDUPE_WINDOW_SEC}초 내 실행됨 — "
            f"중복 요청 무시 (cached: {prev_msg[:160]})",
        )
        return
    _mark_command_inflight(name, cid)

    # Surface the command id to handlers that want idempotency
    # (deploy_to_server uses it to dedupe the same command across
    # retries). Other handlers ignore it.
    payload["__command_id"] = cid
    sys.stderr.write(f"[runner] executing '{name}' (cmd #{cid})\n")
    try:
        ok, msg = handler(payload)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, f"handler raised: {e}"
    _record_command_result(name, cid, ok, msg)
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
    # Boot the watchdog thread regardless of FACTORY_WATCHDOG_ENABLED so
    # the dashboard can render an explicit "disabled" status. The thread
    # itself short-circuits when disabled.
    _start_watchdog_thread()

    last_heartbeat = 0.0
    while _running:
        now = time.time()
        if now - last_heartbeat > HEARTBEAT_INTERVAL_SEC:
            heartbeat()
            # Bridge API factory state → local PAUSE marker so the
            # dashboard's "Continuous OFF" actually halts the bash
            # factory loop. Throttled internally; safe to call every
            # heartbeat tick.
            _reconcile_continuous_mode()
            last_heartbeat = now
        cmd = claim_next()
        if cmd:
            _execute(cmd)
            # Loop tight — there might be more commands queued.
            continue
        time.sleep(POLL_INTERVAL_SEC)
    _WATCHDOG_STOP.set()
    sys.stderr.write("[runner] stopped.\n")


if __name__ == "__main__":
    main()
