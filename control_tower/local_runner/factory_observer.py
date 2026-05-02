"""Stampport Factory Observer — failure relay bot.

The Observer sits one layer above the Pipeline Doctor and answers a
different operational pain:

    "운영자가 매번 Control Tower 화면을 보고, 로그를 복사해서
     ChatGPT 에 전달하고, 다시 Claude Code 용 수정 prompt 를
     받는 반복을 줄이고 싶다."

What it does:

    1. Reads every relevant on-disk runtime file
       (.runtime/control_state.json, factory_state.json,
        agent_accountability.json, pipeline_state.json,
        forward_progress_state.json, deploy_progress.json,
        factory_publish.json, auto_publish_request.json,
        qa_diagnostics.json, local_factory.log tail)
    2. Detects duplicate runner processes via `ps aux`
    3. Classifies into one of the 15 minimum diagnostic codes
       (see DIAGNOSTIC_CODES below)
    4. Writes:
         - .runtime/factory_failure_report.md (운영자 친화적 요약)
         - .runtime/claude_repair_prompt.md   (Claude 직접 적용용 prompt)
       OR for publish_required:
         - .runtime/factory_manual_review_guide.md
    5. Persists state to .runtime/factory_observer_state.json so a
       follow-up tick can compare against the previous diagnostic.

What it does NOT do (safe-mode invariants):

    - 코드 수정 / git add / git commit / git push
    - runner 프로세스 kill
    - .runtime 파일 삭제 / 덮어쓰기 (자체 출력 4종 제외)
    - Claude 자동 실행

CLI:

    python3 -m control_tower.local_runner.factory_observer --once
    python3 -m control_tower.local_runner.factory_observer --watch --interval 300
    python3 -m control_tower.local_runner.factory_observer --self-test

Stdlib-only. Does not import runner.py / cycle.py to keep the import
graph acyclic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _runtime_dir() -> Path:
    repo = Path(os.environ.get("LOCAL_RUNNER_REPO", str(Path.cwd())))
    return repo / ".runtime"


def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_log_tail(path: Path, lines: int = 300) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 256 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Diagnostic codes — exhaustive list mirrored in docs/factory-observer.md
# ---------------------------------------------------------------------------

DIAGNOSTIC_CODES: tuple[str, ...] = (
    "stale_runner",
    "duplicate_runner",
    "runner_offline",
    "implementation_ticket_missing",
    "planner_required_output_missing",
    "current_stage_stuck",
    "git_add_ignored_file",
    "git_add_failed",
    "qa_not_run",
    "qa_gate_failed",
    "claude_apply_failed_no_code_change",
    "publish_required",
    "actions_pending_timeout",
    "old_deploy_failed_stale",
    # 2026-05-02: extended diagnostics for the smoke-test driven loop.
    # See docs/factory-smoke.md — these distinguish "not really a
    # failure" cases (fresh_idle, ready_to_review, pm_hold_for_rework)
    # from real ones, and add smoke-runner specific failure codes.
    "fresh_idle",
    "bridge_pause_mismatch",
    "pm_hold_for_rework",
    "pm_scope_missing_target_files",
    "ready_to_review",
    "smoke_timeout",
    "smoke_passed",
    "smoke_failed",
    "unknown",
)


# Categories: which diagnostic codes are NOT failures.
INFO_CODES: frozenset[str] = frozenset({
    "fresh_idle",
    "smoke_passed",
})
REVIEW_CODES: frozenset[str] = frozenset({
    "publish_required",
    "ready_to_review",
})
HOLD_CODES: frozenset[str] = frozenset({
    "pm_hold_for_rework",
})


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------


def collect_state(runtime: Path | None = None) -> dict:
    """Read every runtime file we care about and return a single dict.

    Pure read-only; never modifies any file in `runtime`.
    """
    rt = runtime or _runtime_dir()
    return {
        "control_state": _read_json(rt / "control_state.json") or {},
        "factory_state": _read_json(rt / "factory_state.json") or {},
        "agent_accountability": _read_json(rt / "agent_accountability.json") or {},
        "pipeline_state": _read_json(rt / "pipeline_state.json") or {},
        "forward_progress_state": _read_json(rt / "forward_progress_state.json") or {},
        "deploy_progress": _read_json(rt / "deploy_progress.json") or {},
        "factory_publish": _read_json(rt / "factory_publish.json") or {},
        "auto_publish_request": _read_json(rt / "auto_publish_request.json") or {},
        "qa_diagnostics": _read_json(rt / "qa_diagnostics.json") or {},
        "factory_command_diagnostics": _read_json(
            rt / "factory_command_diagnostics.json",
        ) or {},
        "log_tail": _read_log_tail(rt / "local_factory.log", lines=300),
    }


# ---------------------------------------------------------------------------
# Duplicate / process probe
# ---------------------------------------------------------------------------


RUNNER_MODULE_TOKEN = "-m control_tower.local_runner.runner"

# Wrappers / utilities that may legitimately co-exist alongside the real
# Python runner — they reference the runner in their command line but
# are NOT themselves the runner. caffeinate -dimsu wraps the python -m
# call to keep the laptop awake; sh/bash/zsh wrap an exec; timeout/awk
# may appear in pipelines. None of these counts toward duplicate_runner.
_WRAPPER_BASENAMES: frozenset[str] = frozenset({
    "caffeinate",
    "sh", "bash", "zsh", "fish",
    "timeout",
    "awk", "tee", "xargs",
    "grep", "egrep", "fgrep", "rg",
    "watch",
})

_PYTHON_BASENAME_TOKENS: tuple[str, ...] = (
    "python3", "python2", "python",
    "Python",  # the macOS framework path: .../Python.app/Contents/MacOS/Python
    "pypy3", "pypy",
)


def _ps_command_field(line: str) -> str:
    """Return the COMMAND column from a `ps aux` line.

    `ps aux` columns: USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND
    The COMMAND column is the 11th field onward (joined). We split with
    `maxsplit=10` so any spaces inside COMMAND are preserved.
    """
    parts = line.split(None, 10)
    if len(parts) < 11:
        return line.strip()
    return parts[10]


def _looks_like_python(executable: str) -> bool:
    """True if the first token of the COMMAND looks like a Python interpreter.

    Matches both `python3`, `/usr/local/.../python3.11`, and the macOS
    framework path `.../Python.app/Contents/MacOS/Python` (basename `Python`).
    """
    if not executable:
        return False
    base = os.path.basename(executable)
    if base in _PYTHON_BASENAME_TOKENS:
        return True
    # Versioned binaries like python3.11
    for tok in ("python3", "python2", "python"):
        if base.startswith(tok):
            return True
    return False


def _wrapper_basename(executable: str) -> str | None:
    """If COMMAND starts with a known wrapper, return its basename, else None."""
    if not executable:
        return None
    base = os.path.basename(executable)
    return base if base in _WRAPPER_BASENAMES else None


def detect_runner_processes(
    ps_output: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return (python_runner_lines, caffeinate_wrapper_lines).

    Both lists exclude the observer's own process and the grep helper.
    Only the first list counts toward duplicate_runner — caffeinate (and
    other shell wrappers) are tracked separately so a normal
    `caffeinate -dimsu python -m control_tower.local_runner.runner`
    invocation reads as Python=1, caffeinate=1 (NOT duplicate).

    `ps_output` is injectable for tests; production calls `ps aux` once.
    """
    if ps_output is None:
        try:
            res = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=5,
            )
            ps_output = res.stdout or ""
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return [], []

    python_runners: list[str] = []
    wrappers: list[str] = []
    for line in ps_output.splitlines():
        if RUNNER_MODULE_TOKEN not in line:
            continue
        # Always exclude self / grep helpers regardless of executable.
        if "factory_observer" in line:
            continue
        if " grep " in line or line.rstrip().endswith(" grep"):
            continue

        cmd = _ps_command_field(line)
        first_token = cmd.split(None, 1)[0] if cmd else ""

        # Wrapper bucket — caffeinate / shells / timeout / etc.
        wrapper = _wrapper_basename(first_token)
        if wrapper == "caffeinate":
            wrappers.append(line.strip())
            continue
        if wrapper is not None:
            # Other wrappers (sh/bash/zsh/timeout/awk) are excluded
            # entirely — they're neither real runners nor the
            # caffeinate-style "keep alive" companion we want to surface.
            continue

        # Real runner — first token must look like a Python interpreter.
        if _looks_like_python(first_token):
            python_runners.append(line.strip())
            continue

        # Anything else that mentions the module but isn't python and
        # isn't a known wrapper (e.g. an editor showing the file) is
        # ignored.
    return python_runners, wrappers


# ---------------------------------------------------------------------------
# Diagnostic classification
# ---------------------------------------------------------------------------


IGNORED_FILE_MARKERS: tuple[str, ...] = (
    "__pycache__",
    "*.pyc",
    "ignored by one of your .gitignore files",
    "다음 경로는 .gitignore 파일 중 하나 때문에 무시합니다",
)


def _has_ignored_file_marker(*texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        for marker in IGNORED_FILE_MARKERS:
            if marker in t:
                return True
    return False


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Current-state diagnostic promotion
#
# When any of the canonical state files already carries an explicit
# diagnostic_code that matches one of our known classifications, we
# promote it to the final verdict directly. This prevents the field-
# shape fallback path from missing a clearly-set signal and falling
# through to `unknown` — the bug this section was added to fix.
#
# Priority order (highest first):
#   1. control_state.diagnostic_code        (canonical aggregator)
#   2. pipeline_state.diagnostic_code        (pipeline-level)
#   3. forward_progress_state.diagnostic_code (timing-level)
#   4. factory_command_diagnostics.diagnostic_code (last operator command)
#   5. qa_diagnostics.diagnostic_code         (qa stage)
#   6. local_factory.log pattern fallback     (handled outside promotion)
# ---------------------------------------------------------------------------


KNOWN_DIAGNOSTIC_CODES: frozenset[str] = frozenset(
    c for c in DIAGNOSTIC_CODES if c != "unknown"
)


PROMOTED_DIAGNOSTIC_META: dict[str, dict[str, Any]] = {
    "stale_runner": {
        "severity": "blocker", "category": "failure", "is_failure": True,
        "root_cause": (
            "pipeline / control_state 가 stale_runner 를 보고 — runner 부팅 이후 "
            "runner.py 가 수정됐거나 다른 runner 가 같은 .runtime 을 점유."
        ),
    },
    "duplicate_runner": {
        "severity": "blocker", "category": "failure", "is_failure": True,
        "root_cause": (
            "state 파일이 duplicate_runner 를 보고 — 같은 .runtime 을 두 runner 가 "
            "동시에 점유 중일 가능성. ps aux 로 실 프로세스도 확인 필요."
        ),
    },
    "runner_offline": {
        "severity": "blocker", "category": "failure", "is_failure": True,
        "root_cause": (
            "state 파일이 runner_offline 을 보고 — heartbeat 갱신이 멈춘 채 "
            "cycle 진행 불가."
        ),
    },
    "implementation_ticket_missing": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "PM/claude_propose 이후 implementation_ticket.md 가 없거나 "
            "target_files 를 추출하지 못해 다음 단계로 진행할 수 없음."
        ),
    },
    "planner_required_output_missing": {
        "severity": "warning", "category": "failure", "is_failure": True,
        "root_cause": (
            "Product Planner 가 품질 가드에 실패하고 fallback 도 진행되지 않음."
        ),
    },
    "current_stage_stuck": {
        "severity": "warning", "category": "failure", "is_failure": True,
        "root_cause": (
            "forward_progress 가 stuck — 현재 stage 가 timeout 초과로 진행되지 않음."
        ),
    },
    "git_add_ignored_file": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "git add 단계에서 .gitignore 에 의해 무시되는 경로 (__pycache__ / "
            "*.pyc 등) 를 add 하려다 실패. changed_files 수집 단계가 .gitignore "
            "를 필터하지 않아 발생."
        ),
    },
    "git_add_failed": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "deploy 명령의 git_add 단계가 실패. stderr 본문을 보고 .gitignore "
            "충돌 / 권한 / 잠금 / 경로 문제 중 어떤 것인지 분류 필요."
        ),
    },
    "qa_not_run": {
        "severity": "warning", "category": "failure", "is_failure": True,
        "root_cause": (
            "deploy 명령이 QA 단계까지 도달하지 못함 — branch_check / "
            "publish_blocker / secret_scan 중 하나가 먼저 실패."
        ),
    },
    "qa_gate_failed": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "QA Gate 가 실제 변경 파일에 대해 실패. qa_diagnostics.json 의 "
            "failed_command / stderr_tail 로 정확한 stage 확인 필요."
        ),
    },
    "claude_apply_failed_no_code_change": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "Implementation Ticket 은 generated 인데 claude_apply 가 0 개 파일 "
            "변경 — Ticket 의 target_files 가 모호하거나 Claude 가 변경 거부."
        ),
    },
    "actions_pending_timeout": {
        "severity": "warning", "category": "failure", "is_failure": True,
        "root_cause": (
            "git push 는 성공했지만 GitHub Actions 가 오래 pending — 워크플로우가 "
            "stuck / cancelled 됐을 수 있음."
        ),
    },
    "old_deploy_failed_stale": {
        "severity": "info", "category": "failure", "is_failure": True,
        "root_cause": (
            "deploy_progress.status 가 직전 사이클의 failed 로 남아있음 — "
            "control_state 는 이미 ready/no_changes 로 재분류했을 가능성."
        ),
    },
    "publish_required": {
        "severity": "info", "category": "review", "is_failure": False,
        "root_cause": (
            "코드 변경 + QA 통과 — commit / push 만 남은 review 상태. 실패 아님."
        ),
    },
    "fresh_idle": {
        "severity": "info", "category": "healthy", "is_failure": False,
        "root_cause": (
            "factory_state.json / cycle_id 없음 — fresh runtime. cycle 시작 전 "
            "산출물 부재는 실패가 아닙니다."
        ),
    },
    "bridge_pause_mismatch": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "desired=running 인데 runner 가 pause marker 를 작성함 — "
            "_reconcile_continuous_mode 정책 위반."
        ),
    },
    "pm_hold_for_rework": {
        "severity": "info", "category": "hold", "is_failure": False,
        "root_cause": (
            "PM 결정이 HOLD — 이번 사이클은 재작업입니다. 개발 단계 (claude_propose / "
            "implementation_ticket / claude_apply) 는 의도적으로 미실행."
        ),
    },
    "pm_scope_missing_target_files": {
        "severity": "warning", "category": "failure", "is_failure": True,
        "root_cause": (
            "PM SHIP 결정인데 PM/planner 산출물에 수정 대상 파일이 명시되지 않음 — "
            "implementation_ticket 을 만들 수 없음. PM 단계 prompt 보강 필요."
        ),
    },
    "ready_to_review": {
        "severity": "info", "category": "review", "is_failure": False,
        "root_cause": (
            "코드 변경 + QA 통과 — 자동 배포가 꺼져 있어 사람 리뷰 대기. 실패 아님."
        ),
    },
    "smoke_timeout": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "factory_smoke 가 timeout 초과로 검증을 종료함 — 단계가 stuck 이거나 "
            "cycle subprocess 가 응답 없음."
        ),
    },
    "smoke_passed": {
        "severity": "info", "category": "healthy", "is_failure": False,
        "root_cause": (
            "factory_smoke 가 PASS / READY / HOLD 중 하나로 정상 종료."
        ),
    },
    "smoke_failed": {
        "severity": "error", "category": "failure", "is_failure": True,
        "root_cause": (
            "factory_smoke 가 실패 verdict 로 종료 — factory_smoke_report.md 와 "
            "claude_repair_prompt.md 참조."
        ),
    },
}


def _promote_known_diagnostic(state: dict) -> tuple[str, str] | None:
    """Walk state files in priority order. Return (code, source_field) for
    the first source whose diagnostic_code is a known code, else None."""
    cs = state.get("control_state") or {}
    pipe = state.get("pipeline_state") or {}
    fp = state.get("forward_progress_state") or {}
    cmd = state.get("factory_command_diagnostics") or {}
    qad = state.get("qa_diagnostics") or {}

    sources: tuple[tuple[str, Any], ...] = (
        ("control_state.diagnostic_code", cs.get("diagnostic_code")),
        ("pipeline_state.diagnostic_code", pipe.get("diagnostic_code")),
        ("forward_progress_state.diagnostic_code", fp.get("diagnostic_code")),
        ("factory_command_diagnostics.diagnostic_code",
         cmd.get("diagnostic_code")),
        ("qa_diagnostics.diagnostic_code", qad.get("diagnostic_code")),
    )
    for source, raw in sources:
        if not raw:
            continue
        val = str(raw).strip()
        if val in KNOWN_DIAGNOSTIC_CODES:
            return val, source
    return None


def _build_promoted_classification(
    code: str,
    source: str,
    state: dict,
) -> dict:
    """Build the rich classification dict for a promoted code. The
    promotion tier shares one shape; per-code root_cause / severity
    come from PROMOTED_DIAGNOSTIC_META, evidence is built per-code with
    the most useful fields surfaced first."""
    meta = PROMOTED_DIAGNOSTIC_META[code]
    cs = state.get("control_state") or {}
    fs = state.get("factory_state") or {}
    pipe = state.get("pipeline_state") or {}
    fp = state.get("forward_progress_state") or {}
    cmd = state.get("factory_command_diagnostics") or {}
    qad = state.get("qa_diagnostics") or {}

    base_evidence: list[str] = [
        f"source={source}",
        f"control_state.status={cs.get('status') or '—'}",
        f"control_state.diagnostic_code={cs.get('diagnostic_code') or '—'}",
        f"pipeline_state.diagnostic_code={pipe.get('diagnostic_code') or '—'}",
        f"forward_progress.status={fp.get('status') or '—'}",
        f"agent_accountability.overall_status="
        f"{(state.get('agent_accountability') or {}).get('overall_status') or '—'}",
    ]

    # Per-code extra evidence — only the most actionable extra fields.
    extra: list[str] = []
    if code == "implementation_ticket_missing":
        extra = [
            f"pm_decision_status={fs.get('pm_decision_status') or '—'}",
            f"implementation_ticket_status="
            f"{fs.get('implementation_ticket_status') or '—'}",
            f"product_planner_status={fs.get('product_planner_status') or '—'}",
        ]
    elif code == "planner_required_output_missing":
        gates = list(fs.get("product_planner_gate_failures") or [])
        extra = [
            f"product_planner_status={fs.get('product_planner_status') or '—'}",
            f"gate_failures={len(gates)}",
        ] + [f"- {g}" for g in gates[:4]]
    elif code == "current_stage_stuck":
        extra = [
            f"current_stage={fp.get('current_stage') or '—'}",
            f"required_output={fp.get('required_output') or '—'}",
            f"required_output_exists={fp.get('required_output_exists')}",
            f"elapsed_sec={fp.get('current_stage_elapsed_sec')}",
            f"stage_timeout_sec={fp.get('stage_timeout_sec')}",
        ]
    elif code in {"git_add_ignored_file", "git_add_failed", "qa_not_run"}:
        extra = [
            f"command_diagnostics.failed_stage={cmd.get('failed_stage') or '—'}",
            f"command_diagnostics.diagnostic_code="
            f"{cmd.get('diagnostic_code') or '—'}",
            (_str(cmd.get("failed_reason")) or "")[:300],
        ]
    elif code == "qa_gate_failed":
        extra = [
            f"qa_status={fs.get('qa_status') or qad.get('qa_status') or '—'}",
            f"changed_files={len(list(fs.get('claude_apply_changed_files') or []))}",
        ]
        if qad.get("failed_command"):
            extra.append(f"failed_command={qad.get('failed_command')}")
        if qad.get("stderr_tail"):
            extra.append(f"stderr_tail={(_str(qad.get('stderr_tail')))[:200]}")
    elif code == "claude_apply_failed_no_code_change":
        extra = [
            f"claude_apply_status={fs.get('claude_apply_status') or '—'}",
            f"claude_apply_changed_files="
            f"{len(list(fs.get('claude_apply_changed_files') or []))}",
            f"implementation_ticket_status="
            f"{fs.get('implementation_ticket_status') or '—'}",
        ]
    elif code == "publish_required":
        deploy_block = cs.get("deploy") or {}
        extra = [
            f"changed_files_count="
            f"{deploy_block.get('changed_files_count') or len(list(fs.get('claude_apply_changed_files') or []))}",
            f"qa_status="
            f"{deploy_block.get('qa_status') or fs.get('qa_status') or '—'}",
            f"commit_hash={deploy_block.get('commit_hash') or '—'}",
            f"push_status={deploy_block.get('push_status') or '—'}",
        ]
    elif code == "actions_pending_timeout":
        deploy_block = cs.get("deploy") or {}
        fpub = state.get("factory_publish") or {}
        extra = [
            f"actions_status={deploy_block.get('actions_status') or '—'}",
            f"actions_run_url={deploy_block.get('actions_run_url') or '—'}",
            f"last_push_at={fpub.get('last_push_at') or '—'}",
        ]

    return {
        "diagnostic_code": code,
        "severity": meta["severity"],
        "category": meta["category"],
        "root_cause": meta["root_cause"],
        "evidence": base_evidence + extra,
        "auto_fix_possible": False,
        "is_failure": meta["is_failure"],
    }


# Bridge pause mismatch — log patterns that prove desired=running but
# pause was applied. We accept both the legacy bad message format
# ("(continuous=False, desired=running)") and the new format
# ("(desired=paused, continuous=...)" — which is fine — but
# "(desired=running, ..." with "pause applied" is NEVER fine).
_BRIDGE_PAUSE_BAD_PATTERNS: tuple[str, ...] = (
    "factory bridge · pause applied (continuous=False, desired=running)",
    "factory bridge · pause applied (desired=running",
)


def _looks_like_bridge_pause_mismatch(log_tail: str) -> bool:
    if not log_tail:
        return False
    return any(pat in log_tail for pat in _BRIDGE_PAUSE_BAD_PATTERNS)


def _bridge_mismatch_evidence(log_tail: str) -> list[str]:
    if not log_tail:
        return []
    out: list[str] = []
    for line in log_tail.splitlines()[-200:]:
        if any(pat in line for pat in _BRIDGE_PAUSE_BAD_PATTERNS):
            out.append(line.strip()[:300])
        if len(out) >= 4:
            break
    return out or ["pause applied with desired=running pattern detected"]


def _looks_like_publish_disabled_review(state: dict) -> bool:
    """Detect the 'ready_to_review (publish disabled)' shape.

    Conditions (all must hold):
      - claude_apply_status == "applied"
      - claude_apply_changed_files length > 0
      - qa_status == "passed"
      - no commit_hash on the current cycle
      - LOCAL_RUNNER_ALLOW_PUBLISH=false (or unset → defaults to false)
    """
    fs = state.get("factory_state") or {}
    cs = state.get("control_state") or {}
    fpub = state.get("factory_publish") or {}
    deploy_block = cs.get("deploy") or {}

    apply_status = _str(fs.get("claude_apply_status"))
    changed = list(fs.get("claude_apply_changed_files") or [])
    qa = _str(fs.get("qa_status"))
    deploy_commit = _str(deploy_block.get("commit_hash"))
    last_commit = _str(fpub.get("last_commit_hash"))

    allow_publish = (os.environ.get("LOCAL_RUNNER_ALLOW_PUBLISH") or "").strip().lower()
    publish_disabled = allow_publish in {"", "false", "0", "no", "off"}

    return bool(
        apply_status == "applied"
        and len(changed) > 0
        and qa == "passed"
        and not deploy_commit
        and not last_commit
        and publish_disabled
    )


def _ready_to_review_evidence(state: dict) -> list[str]:
    fs = state.get("factory_state") or {}
    cs = state.get("control_state") or {}
    deploy_block = cs.get("deploy") or {}
    changed = list(fs.get("claude_apply_changed_files") or [])
    return [
        f"changed_files_count={len(changed) or deploy_block.get('changed_files_count') or 0}",
        f"qa_status={fs.get('qa_status') or deploy_block.get('qa_status') or '—'}",
        f"claude_apply_status={fs.get('claude_apply_status') or '—'}",
        f"commit_hash={deploy_block.get('commit_hash') or '—'}",
        f"LOCAL_RUNNER_ALLOW_PUBLISH="
        f"{os.environ.get('LOCAL_RUNNER_ALLOW_PUBLISH') or '(unset → false)'}",
    ]


def classify(
    state: dict,
    runner_processes: list[str] | None = None,
    caffeinate_processes: list[str] | None = None,
) -> dict:
    """Return diagnostic classification dict.

    Shape:
        {
            "diagnostic_code": str,
            "severity": "info"|"warning"|"error"|"blocker",
            "category": "failure"|"review"|"healthy",
            "root_cause": str,
            "evidence": list[str],
            "auto_fix_possible": bool,
            "is_failure": bool,
        }

    Process counting rules (only python_runners count):
        Python runner ≥ 2                           → duplicate_runner
        Python runner = 1 (any caffeinate count)    → ok at process level
        Python runner = 0, caffeinate = 0           → runner_offline
        Python runner = 0, caffeinate ≥ 1           → broken_wrapper
                                                       (surfaced as
                                                       runner_offline with
                                                       wrapper-only note)

    Order of precedence (highest first) — earlier matches win:
        duplicate_runner    → 2+ Python runner processes (process-level)
        runner_offline      → no Python runner OR no heartbeat (process+state)
        PROMOTION PASS      → known diagnostic_code in any of:
                              control_state / pipeline_state /
                              forward_progress_state /
                              factory_command_diagnostics / qa_diagnostics
                              (in that priority order)
        git_add_ignored_file → __pycache__/.gitignore marker in logs
        git_add_failed       → command_diagnostics.failed_stage == git_add
        qa_not_run           → command_diagnostics.diagnostic_code == qa_not_run
        qa_gate_failed       → qa_status == failed with real changes
        claude_apply_failed_no_code_change
        implementation_ticket_missing  (field-shape combo)
        planner_required_output_missing
        current_stage_stuck   (forward_progress.status == "stuck")
        actions_pending_timeout
        old_deploy_failed_stale
        publish_required      → review state, NOT failure
        stale_runner          → log_tail-only fallback
        healthy / unknown
    """
    cs = state.get("control_state") or {}
    fs = state.get("factory_state") or {}
    pipe = state.get("pipeline_state") or {}
    fp = state.get("forward_progress_state") or {}
    dep = state.get("deploy_progress") or {}
    fpub = state.get("factory_publish") or {}
    cmd = state.get("factory_command_diagnostics") or {}
    qad = state.get("qa_diagnostics") or {}
    accountability = state.get("agent_accountability") or {}
    log_tail = state.get("log_tail") or ""

    if runner_processes is None and caffeinate_processes is None:
        runner_procs, caffeinate_procs = detect_runner_processes()
    else:
        runner_procs = runner_processes or []
        caffeinate_procs = caffeinate_processes or []

    # 1. duplicate_runner — only count Python runner processes. caffeinate
    # wrappers are tracked separately and do NOT count.
    if len(runner_procs) >= 2:
        return {
            "diagnostic_code": "duplicate_runner",
            "severity": "blocker",
            "category": "failure",
            "root_cause": (
                f"Python runner 프로세스가 {len(runner_procs)} 개 감지됨 — "
                "두 runner 가 같은 .runtime 파일을 동시에 쓰면 stale_runner / "
                "inconsistent state 가 반복 발생. (caffeinate wrapper 는 별도 "
                f"{len(caffeinate_procs)} 개로 정상.)"
            ),
            "evidence": (
                [f"python_runner: {p}" for p in runner_procs[:4]]
                + [f"caffeinate: {p}" for p in caffeinate_procs[:2]]
            ),
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # Read these once for use in both promotion-fallback and field-shape paths.
    pipe_code = _str(pipe.get("diagnostic_code"))
    cs_code = _str(cs.get("diagnostic_code"))
    cs_diag = _str((cs.get("pipeline") or {}).get("diagnostic_code"))

    # 2. runner_offline — Python runner 프로세스 0개 OR heartbeat 없음.
    liveness = cs.get("liveness") or {}
    no_python_runner = len(runner_procs) == 0
    heartbeat_dead = bool(cs and liveness and not liveness.get("runner_online"))
    if no_python_runner or heartbeat_dead:
        wrapper_only = no_python_runner and len(caffeinate_procs) >= 1
        if wrapper_only:
            root = (
                "Python runner 0 개인데 caffeinate wrapper 만 살아있음 — "
                "wrapper 가 시작했어야 할 python -m control_tower.local_runner.runner "
                "가 부팅 직후 죽었거나 exec 실패. (broken_wrapper)"
            )
        elif no_python_runner:
            root = (
                "Python runner 프로세스가 0 개 — runner 가 죽었거나 시작되지 않음. "
                f"(caffeinate wrapper {len(caffeinate_procs)} 개)"
            )
        else:
            root = (
                "control_state.liveness.runner_online=false — runner heartbeat 갱신이 "
                "멈춤. 프로세스는 살아있을 수 있으나 cycle 진행 불가."
            )
        return {
            "diagnostic_code": "runner_offline",
            "severity": "blocker",
            "category": "failure",
            "root_cause": root,
            "evidence": [
                f"python_runner_count={len(runner_procs)}",
                f"caffeinate_count={len(caffeinate_procs)}",
                f"liveness.runner_online={liveness.get('runner_online')}",
                f"liveness.heartbeat_at={liveness.get('heartbeat_at')}",
                f"liveness.runner_stale={liveness.get('runner_stale')}",
            ] + (
                [f"caffeinate: {caffeinate_procs[0]}"] if caffeinate_procs else []
            ),
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 2.5 bridge_pause_mismatch — runner emitted "pause applied" while
    # desired=running. Detected from the new (and the legacy bad) log
    # patterns. desired=running must NEVER produce a pause marker; the
    # log line is the canonical evidence even when the pause file was
    # later cleared.
    if _looks_like_bridge_pause_mismatch(log_tail):
        return {
            "diagnostic_code": "bridge_pause_mismatch",
            "severity": "error",
            "category": "failure",
            "root_cause": (
                "desired=running 인데 runner 가 pause marker 를 작성했습니다 — "
                "_reconcile_continuous_mode 정책 위반. continuous=False 는 "
                "'반복하지 않음' 일 뿐 'pause' 가 아닙니다."
            ),
            "evidence": _bridge_mismatch_evidence(log_tail),
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 2.6 fresh_idle — no cycle has run yet. Don't penalize the absence
    # of product_planner_report.md / implementation_ticket.md /
    # changed_files when factory_state.json is empty or has no cycle_id.
    # This is the policy that stops the observer from screaming
    # "current_stage_stuck" / "implementation_ticket_missing" on a
    # fresh checkout.
    fs_status = _str(fs.get("status"))
    fs_cycle = fs.get("cycle")
    fresh_state = (
        not fs
        or fs_cycle in (None, 0, "0", "")
        or fs_status in {"", "idle", "ready", "paused"}
    )
    cs_status_for_fresh = _str(cs.get("status"))
    cs_diag_for_fresh = _str(cs.get("diagnostic_code"))
    if (
        fresh_state
        and cs_status_for_fresh in {"", "idle", "ready"}
        and cs_diag_for_fresh in {"", "healthy"}
        and not log_tail.strip()
    ):
        return {
            "diagnostic_code": "fresh_idle",
            "severity": "info",
            "category": "healthy",
            "root_cause": (
                "factory_state.json 이 비어있거나 cycle 이 시작되지 않았습니다 — "
                "fresh runtime. cycle 시작 전 산출물 부재는 실패가 아닙니다."
            ),
            "evidence": [
                f"factory_state.cycle={fs_cycle or '—'}",
                f"factory_state.status={fs_status or '—'}",
                f"control_state.status={cs_status_for_fresh or '—'}",
                f"control_state.diagnostic_code={cs_diag_for_fresh or '—'}",
            ],
            "auto_fix_possible": False,
            "is_failure": False,
        }

    # 2.7 pm_hold_for_rework — PM 결정이 HOLD 인 정상 종료. 실패 아님.
    if fs_status == "hold_for_rework" or cs_status_for_fresh == "hold_for_rework":
        return {
            "diagnostic_code": "pm_hold_for_rework",
            "severity": "info",
            "category": "hold",
            "root_cause": (
                "PM 결정이 HOLD — 이번 사이클은 의도적으로 재작업 사이클입니다. "
                "claude_propose / implementation_ticket / claude_apply 미실행이 정상. "
                "다음 사이클의 planner 는 `.runtime/claude_rework_prompt.md` 를 입력으로 사용해야 합니다."
            ),
            "evidence": [
                f"factory_state.status={fs_status or '—'}",
                f"control_state.status={cs_status_for_fresh or '—'}",
                f"pm_decision_status={fs.get('pm_decision_status') or '—'}",
                f"pm_decision_message={fs.get('pm_decision_message') or '—'}",
                "rework_prompt=.runtime/claude_rework_prompt.md",
            ],
            "auto_fix_possible": False,
            "is_failure": False,
        }

    # 2.8 pm_scope_missing_target_files — PM SHIP 인데 target_files 없음.
    impl_ticket_status = _str(fs.get("implementation_ticket_status"))
    if impl_ticket_status == "pm_scope_missing_target_files":
        return {
            "diagnostic_code": "pm_scope_missing_target_files",
            "severity": "warning",
            "category": "failure",
            "root_cause": (
                "PM 결정에 수정 대상 파일이 명시되지 않아 implementation_ticket 을 "
                "만들 수 없음. 'implementation_ticket_missing' 와 분리된 분류 — "
                "fix 는 PM prompt 보강 (must include explicit list of files to "
                "modify under app/* or control_tower/*)."
            ),
            "evidence": [
                f"implementation_ticket_status={impl_ticket_status}",
                f"pm_decision_status={fs.get('pm_decision_status') or '—'}",
                f"pm_decision_ship_ready={fs.get('pm_decision_ship_ready')}",
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 2.9 ready_to_review — 자동 배포가 꺼진 상태에서 코드 변경 + qa 통과 + commit 없음.
    # publish_required 와 분리: ready_to_review 는 "자동 배포 비활성 (사람 리뷰
    # 대기) 으로 인한" 의도적 보류, publish_required 는 "publish 명령을
    # 기다리는" 일반 케이스. 둘 다 실패 아님.
    # 단, control_state 가 이미 publish_required 를 명시했다면 promotion 에
    # 위임 — 운영자가 publish_changes 를 부르는 즉시 release 가능한 상태이므로
    # "review 대기" 라벨로 다시 덮어쓰지 않습니다.
    # Only the EXPLICIT promotion signal (diagnostic_code) suppresses
    # ready_to_review — status=="ready_to_publish" alone is just the
    # cycle's "I produced shippable code" claim, which we want to relabel
    # as ready_to_review whenever auto-publish is disabled.
    publish_required_signal_present = (cs_diag_for_fresh == "publish_required")
    if not publish_required_signal_present and (
        fs_status == "ready_to_review"
        or cs_status_for_fresh == "ready_to_review"
        or _looks_like_publish_disabled_review(state)
    ):
        return {
            "diagnostic_code": "ready_to_review",
            "severity": "info",
            "category": "review",
            "root_cause": (
                "코드 변경 + QA 통과 + 자동 배포 비활성 — 사람 리뷰 대기. 실패 아님. "
                "LOCAL_RUNNER_ALLOW_PUBLISH=true 로 켜기 전에는 git commit/push 가 "
                "실행되지 않습니다."
            ),
            "evidence": _ready_to_review_evidence(state),
            "auto_fix_possible": False,
            "is_failure": False,
        }

    # 3. CURRENT-STATE PROMOTION — if any canonical state file already
    # carries a known diagnostic_code, promote it to the final verdict.
    # This MUST run before the field-shape fallbacks below; otherwise a
    # clearly-set signal like control_state.diagnostic_code=
    # implementation_ticket_missing can fall through to `unknown` when
    # the field-shape combo (pm_decision_status=generated +
    # implementation_ticket_status in {missing,...}) doesn't happen to
    # match. See user-reported bug 2026-04-30.
    promotion = _promote_known_diagnostic(state)
    if promotion is not None:
        promoted_code, promoted_source = promotion
        return _build_promoted_classification(promoted_code, promoted_source, state)

    # 4. git_add_ignored_file — log 또는 deploy_progress 에 ignored 마커.
    dep_text = json.dumps(dep, ensure_ascii=False) if dep else ""
    cmd_text = json.dumps(cmd, ensure_ascii=False) if cmd else ""
    if _has_ignored_file_marker(log_tail, dep_text, cmd_text):
        return {
            "diagnostic_code": "git_add_ignored_file",
            "severity": "error",
            "category": "failure",
            "root_cause": (
                "git add 단계에서 .gitignore 에 의해 무시되는 경로 (__pycache__/*.pyc 등) "
                "를 add 하려다 실패. 일반적으로 changed_files 수집 단계가 .gitignore 를 "
                "필터하지 않아 발생."
            ),
            "evidence": [
                m for m in IGNORED_FILE_MARKERS
                if m in log_tail or m in dep_text or m in cmd_text
            ][:4] or ["__pycache__ marker present"],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 5. git_add_failed — command_diagnostics 가 명시적으로 git_add 실패 보고.
    if (
        _str(cmd.get("failed_stage")) == "git_add"
        or _str(dep.get("failed_stage")) == "git_add"
    ):
        return {
            "diagnostic_code": "git_add_failed",
            "severity": "error",
            "category": "failure",
            "root_cause": (
                "deploy 명령의 git_add 단계가 실패. stderr 본문을 보고 .gitignore 충돌 / "
                "권한 / 잠금 (.git/index.lock) / 경로 문제 중 어떤 것인지 분류해야 함."
            ),
            "evidence": [
                f"command_diagnostics.failed_stage={cmd.get('failed_stage')}",
                f"command_diagnostics.diagnostic_code={cmd.get('diagnostic_code')}",
                (_str(cmd.get("failed_reason")) or "")[:400],
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 6. qa_not_run — command_diagnostics 가 명시적으로 보고.
    if _str(cmd.get("diagnostic_code")) == "qa_not_run":
        return {
            "diagnostic_code": "qa_not_run",
            "severity": "warning",
            "category": "failure",
            "root_cause": (
                "deploy 명령이 QA 단계까지 도달하지 못함 — branch_check / "
                "publish_blocker / secret_scan 중 하나가 먼저 실패."
            ),
            "evidence": [
                f"command_diagnostics.failed_stage={cmd.get('failed_stage')}",
                _str(cmd.get("suggested_action") or ""),
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 7. claude_apply_failed_no_code_change — apply 했는데 0 파일.
    apply_status = _str(fs.get("claude_apply_status"))
    apply_changed = list(fs.get("claude_apply_changed_files") or [])
    ticket_status = _str(fs.get("implementation_ticket_status"))
    if (
        apply_status in {"applied", "no_changes"}
        and len(apply_changed) == 0
        and ticket_status == "generated"
    ):
        return {
            "diagnostic_code": "claude_apply_failed_no_code_change",
            "severity": "error",
            "category": "failure",
            "root_cause": (
                "Implementation Ticket 은 generated 인데 claude_apply 가 0 개 파일 변경 — "
                "Ticket 의 target_files 가 모호하거나, Claude 가 변경 거부."
            ),
            "evidence": [
                f"claude_apply_status={apply_status}",
                f"claude_apply_changed_files=[]",
                f"implementation_ticket_status={ticket_status}",
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 8. qa_gate_failed — 실제 변경이 있는데 QA 실패.
    qa_status_fs = _str(fs.get("qa_status"))
    qa_status_qad = _str(qad.get("qa_status"))
    if qa_status_fs == "failed" and apply_changed:
        evidence = [
            f"qa_status={qa_status_fs}",
            f"changed_files={len(apply_changed)}",
        ]
        if qad.get("failed_command"):
            evidence.append(f"failed_command={qad.get('failed_command')}")
        if qad.get("stderr_tail"):
            evidence.append(f"stderr_tail={(_str(qad.get('stderr_tail')))[:200]}")
        return {
            "diagnostic_code": "qa_gate_failed",
            "severity": "error",
            "category": "failure",
            "root_cause": (
                "QA Gate 가 실제 변경 파일에 대해 실패. qa_diagnostics.json 의 "
                "failed_command / stderr_tail 로 정확한 stage 확인 필요."
            ),
            "evidence": evidence,
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 9. implementation_ticket_missing — PM decided but ticket 없음.
    if (
        _str(fs.get("pm_decision_status")) == "generated"
        and ticket_status in {"missing", "skipped", "failed", ""}
    ):
        return {
            "diagnostic_code": "implementation_ticket_missing",
            "severity": "error",
            "category": "failure",
            "root_cause": (
                "PM 결정은 generated 인데 implementation_ticket.md 가 비어있거나 "
                "skipped — fallback ticket 을 강제 생성해 cycle 을 진행시켜야 함."
            ),
            "evidence": [
                f"pm_decision_status={fs.get('pm_decision_status')}",
                f"implementation_ticket_status={ticket_status or '—'}",
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 10. planner_required_output_missing — planner gate 실패 + fallback 안 됨.
    planner_status = _str(fs.get("product_planner_status"))
    planner_failures = list(fs.get("product_planner_gate_failures") or [])
    if planner_failures and planner_status not in {"generated", "fallback_generated"}:
        return {
            "diagnostic_code": "planner_required_output_missing",
            "severity": "warning",
            "category": "failure",
            "root_cause": (
                "Product Planner 가 품질 가드에 실패하고 fallback 도 진행되지 않음."
            ),
            "evidence": [
                f"product_planner_status={planner_status or '—'}",
                f"gate_failures={len(planner_failures)}",
            ] + [f"- {f}" for f in planner_failures[:4]],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 11. current_stage_stuck — forward_progress timeout.
    fp_status = _str(fp.get("status"))
    if fp_status == "stuck":
        return {
            "diagnostic_code": "current_stage_stuck",
            "severity": "warning",
            "category": "failure",
            "root_cause": (
                f"forward_progress 가 stuck — current_stage="
                f"{fp.get('current_stage') or '—'} 가 timeout 초과로 진행되지 않음."
            ),
            "evidence": [
                f"current_stage={fp.get('current_stage')}",
                f"required_output={fp.get('required_output')}",
                f"required_output_exists={fp.get('required_output_exists')}",
                f"elapsed_sec={fp.get('current_stage_elapsed_sec')}",
                f"stage_timeout_sec={fp.get('stage_timeout_sec')}",
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 12. actions_pending_timeout — push 됐는데 GH Actions 가 너무 오래 pending.
    deploy_block = cs.get("deploy") or {}
    actions_status = _str(deploy_block.get("actions_status"))
    if actions_status in {"in_progress", "queued", "waiting", "requested", "pending"}:
        # Heuristic: if last_push_at is older than 30 min, treat as timeout.
        last_push_at = _str(fpub.get("last_push_at"))
        if _is_older_than_seconds(last_push_at, 30 * 60):
            return {
                "diagnostic_code": "actions_pending_timeout",
                "severity": "warning",
                "category": "failure",
                "root_cause": (
                    "git push 는 성공했지만 GitHub Actions 가 30 분 이상 pending — "
                    "워크플로우가 stuck / cancelled 됐을 수 있음."
                ),
                "evidence": [
                    f"actions_status={actions_status}",
                    f"actions_run_url={deploy_block.get('actions_run_url')}",
                    f"last_push_at={last_push_at}",
                ],
                "auto_fix_possible": False,
                "is_failure": True,
            }

    # 13. old_deploy_failed_stale — deploy_progress 가 failed 인데 control_state 는
    #     이미 stale 처리해서 ready / no_changes 로 분류했음. UI 가 빨간색을 잘못
    #     보여주는 케이스.
    dep_status = _str(dep.get("status"))
    cs_deploy_status = _str(deploy_block.get("status"))
    if dep_status == "failed" and cs_deploy_status in {"ready", "no_changes", "completed"}:
        return {
            "diagnostic_code": "old_deploy_failed_stale",
            "severity": "info",
            "category": "failure",
            "root_cause": (
                "deploy_progress.status 가 직전 사이클의 failed 로 남아있지만 "
                "control_state 는 이미 ready/no_changes 로 재분류함 — 화면의 붉은 표시는 stale."
            ),
            "evidence": [
                f"deploy_progress.status={dep_status}",
                f"control_state.deploy.status={cs_deploy_status}",
                f"deploy_progress.failed_stage={dep.get('failed_stage')}",
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 14. publish_required — review/publish 대기 (NOT a failure).
    cs_status = _str(cs.get("status"))
    deploy_qa_status = _str(deploy_block.get("qa_status"))
    deploy_commit = _str(deploy_block.get("commit_hash"))
    deploy_changed = int(deploy_block.get("changed_files_count") or 0)
    push_status = _str(deploy_block.get("push_status"))
    publish_required_signal = (
        cs_status == "ready_to_publish"
        or cs_code == "publish_required"
        or (
            deploy_changed > 0
            and deploy_qa_status == "passed"
            and not deploy_commit
            and push_status not in {"ok", "succeeded"}
        )
        or (
            len(apply_changed) > 0
            and qa_status_fs == "passed"
            and not _str(fpub.get("last_commit_hash"))
        )
    )
    if publish_required_signal:
        return {
            "diagnostic_code": "publish_required",
            "severity": "info",
            "category": "review",
            "root_cause": (
                "코드 변경 + QA 통과 — commit / push 만 남은 review 상태. 실패 아님."
            ),
            "evidence": [
                f"changed_files_count={deploy_changed or len(apply_changed)}",
                f"qa_status={deploy_qa_status or qa_status_fs}",
                f"commit_hash={deploy_commit or '—'}",
                f"push_status={push_status or '—'}",
            ],
            "auto_fix_possible": False,
            "is_failure": False,
        }

    # 15. stale_runner — log_tail 에서만 stale_runner 가 반복 보고되는 경우.
    # 상태 파일에 diagnostic_code 가 들어있는 케이스는 이미 promotion 이
    # 잡았으므로 여기는 순수 log-pattern 폴백.
    if any("stale_runner" in line for line in log_tail.splitlines()[-80:]):
        return {
            "diagnostic_code": "stale_runner",
            "severity": "blocker",
            "category": "failure",
            "root_cause": (
                "local_factory.log 마지막 80 줄에 stale_runner 가 반복 — runner "
                "부팅 이후 runner.py 가 수정됐거나 다른 runner 가 같은 .runtime "
                "을 점유. (state 파일에는 아직 diagnostic_code 가 미반영.)"
            ),
            "evidence": [
                "source=local_factory.log",
                f"control_state.diagnostic_code={cs_code or '—'}",
                f"pipeline_state.diagnostic_code={pipe_code or '—'}",
            ],
            "auto_fix_possible": False,
            "is_failure": True,
        }

    # 16. healthy / unknown — 마지막 폴백. unknown 은 어떤 known
    # diagnostic_code 도 없고, 어떤 log pattern 도 매칭 안 됐고, runner
    # 상태도 판단 가능했을 때만 도달함.
    if cs_status in {"completed", "running", "idle"}:
        return {
            "diagnostic_code": "healthy",
            "severity": "info",
            "category": "healthy",
            "root_cause": (
                f"control_state.status={cs_status} — Observer 개입 불요."
            ),
            "evidence": [],
            "auto_fix_possible": False,
            "is_failure": False,
        }
    return {
        "diagnostic_code": "unknown",
        "severity": "warning",
        "category": "failure",
        "root_cause": (
            "어떤 canonical state 파일에도 known diagnostic_code 가 없고 "
            "log pattern / 프로세스 시그널로도 분류 불가. 운영자 직접 확인 필요."
        ),
        "evidence": [
            "no_known_diagnostic_in: control_state / pipeline_state / "
            "forward_progress_state / factory_command_diagnostics / qa_diagnostics",
            f"control_state.status={cs_status or '—'}",
            f"control_state.diagnostic_code={cs_code or '—'}",
            f"factory_state.status={fs.get('status') or '—'}",
            f"pipeline_state.diagnostic_code={pipe_code or '—'}",
            f"forward_progress.status={fp_status or '—'}",
            f"deploy_progress.status={dep_status or '—'}",
            f"agent_accountability.overall_status={accountability.get('overall_status') or '—'}",
        ],
        "auto_fix_possible": False,
        "is_failure": True,
    }


def _is_older_than_seconds(iso_ts: str, seconds: int) -> bool:
    if not iso_ts:
        return False
    try:
        ts = datetime.strptime(iso_ts[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return False
    age = (datetime.utcnow() - ts).total_seconds()
    return age > seconds


# ---------------------------------------------------------------------------
# Manual confirmation commands per diagnostic
# ---------------------------------------------------------------------------


MANUAL_COMMANDS_BY_CODE: dict[str, list[str]] = {
    "duplicate_runner": [
        "ps aux | grep control_tower.local_runner.runner | grep -v grep",
        "# 위 결과가 2줄 이상이면 모든 runner 종료 후 1개만 재실행",
        "pkill -f control_tower.local_runner.runner",
    ],
    "stale_runner": [
        "ps aux | grep control_tower.local_runner.runner | grep -v grep",
        "git status",
        "git pull --ff-only",
        "rm -f .runtime/factory_pause.marker",
        "python3 -m control_tower.local_runner.runner",
    ],
    "runner_offline": [
        "ps aux | grep control_tower.local_runner.runner | grep -v grep",
        "tail -50 .runtime/local_factory.log",
        "python3 -m control_tower.local_runner.runner",
    ],
    "git_add_ignored_file": [
        "git status",
        "git check-ignore -v app/api/app/__pycache__",
        "cat .runtime/factory_command_diagnostics.json",
        "find . -name __pycache__ -type d | head -10",
    ],
    "git_add_failed": [
        "cat .runtime/factory_command_diagnostics.json",
        "git status",
        "ls -la .git/index.lock 2>/dev/null || echo 'no lock'",
    ],
    "qa_not_run": [
        "cat .runtime/factory_command_diagnostics.json",
        "tail -100 .runtime/local_factory.log",
    ],
    "qa_gate_failed": [
        "cat .runtime/qa_diagnostics.json | python3 -m json.tool",
        "cat .runtime/qa_report.md",
    ],
    "claude_apply_failed_no_code_change": [
        "cat .runtime/implementation_ticket.md",
        "cat .runtime/claude_apply.diff",
        "tail -100 .runtime/local_factory.log",
    ],
    "implementation_ticket_missing": [
        "cat .runtime/pm_decision.md",
        "cat .runtime/implementation_ticket.md 2>/dev/null || echo '(missing)'",
    ],
    "planner_required_output_missing": [
        "cat .runtime/product_planner_report.md 2>/dev/null || echo '(missing)'",
        "cat .runtime/factory_state.json | python3 -m json.tool | head -60",
    ],
    "current_stage_stuck": [
        "cat .runtime/forward_progress_state.json | python3 -m json.tool",
        "cat .runtime/pipeline_state.json | python3 -m json.tool",
    ],
    "actions_pending_timeout": [
        "gh run list --limit 5",
        "cat .runtime/factory_publish.json | python3 -m json.tool",
    ],
    "old_deploy_failed_stale": [
        "cat .runtime/deploy_progress.json | python3 -m json.tool",
        "cat .runtime/control_state.json | python3 -m json.tool | head -40",
    ],
    "publish_required": [
        "git status",
        "git diff --stat",
        "cat .runtime/qa_report.md | head -40",
    ],
    "unknown": [
        "cat .runtime/control_state.json | python3 -m json.tool",
        "cat .runtime/factory_state.json | python3 -m json.tool",
        "tail -100 .runtime/local_factory.log",
    ],
}


REPAIR_TARGETS_BY_CODE: dict[str, list[str]] = {
    "git_add_ignored_file": [
        "control_tower/local_runner/cycle.py (changed_files 수집/필터)",
        "control_tower/local_runner/runner.py (deploy 단계 git add 호출부)",
        ".gitignore (예상 무시 패턴 검증용; 수정 대상은 보통 아님)",
    ],
    "git_add_failed": [
        "control_tower/local_runner/runner.py (git_add 단계)",
        "control_tower/local_runner/cycle.py (publish_blocker / secret_scan)",
    ],
    "qa_not_run": [
        "control_tower/local_runner/runner.py (deploy 명령 stage 순서)",
        "control_tower/local_runner/cycle.py (branch_check / publish_blocker)",
    ],
    "qa_gate_failed": [
        "(QA 가 가리키는 실제 소스 파일 — qa_diagnostics.failed_command 의 대상)",
        "control_tower/local_runner/cycle.py (stage_qa_gate)",
    ],
    "claude_apply_failed_no_code_change": [
        "control_tower/local_runner/cycle.py (stage_implementation_ticket / stage_claude_apply)",
        ".runtime/implementation_ticket.md (target_files 명세)",
    ],
    "implementation_ticket_missing": [
        "control_tower/local_runner/cycle.py (stage_implementation_ticket fallback 경로)",
        ".runtime/pm_decision.md (수정 대상 파일이 명시돼 있는지)",
    ],
    "planner_required_output_missing": [
        "control_tower/local_runner/cycle.py (stage_product_planning fallback 경로)",
        ".runtime/product_planner_report.md (생성 여부)",
    ],
    "current_stage_stuck": [
        "control_tower/local_runner/cycle.py (해당 stage)",
        "control_tower/local_runner/agent_supervisor.py (forward_progress 평가)",
    ],
    "stale_runner": [
        "(코드 수정 없음 — runner 재시작이 우선)",
        "control_tower/local_runner/runner.py (boot stamp 비교 로직 검토)",
    ],
    "duplicate_runner": [
        "(코드 수정 없음 — 중복 프로세스 종료)",
    ],
    "runner_offline": [
        "(코드 수정 없음 — runner 재실행)",
    ],
    "actions_pending_timeout": [
        ".github/workflows/* (워크플로우 stuck 여부)",
        "control_tower/local_runner/runner.py (actions polling 로직)",
    ],
    "old_deploy_failed_stale": [
        "control_tower/local_runner/control_state.py (stale_state filter)",
        "control_tower/web/src (UI 의 deploy badge 표시 로직)",
    ],
    "unknown": [
        "(분류 추가 필요 — control_tower/local_runner/factory_observer.py 의 classify())",
    ],
}


REPAIR_REQUIREMENTS_BY_CODE: dict[str, str] = {
    "stale_runner": (
        "1. 모든 runner 프로세스 종료\n"
        "2. git pull --ff-only\n"
        "3. .runtime/factory_pause.marker 제거 (있으면)\n"
        "4. runner 1개만 재실행: `python3 -m control_tower.local_runner.runner`\n"
        "5. 같은 stale_runner 가 24시간 내 3회 이상 발생했는지 확인 — "
        "그렇다면 runner.py 의 boot stamp 비교 로직 점검."
    ),
    "duplicate_runner": (
        "1. `pkill -f control_tower.local_runner.runner` 로 모든 runner 종료\n"
        "2. `ps aux | grep control_tower.local_runner.runner | grep -v grep` 로 0개 확인\n"
        "3. runner 1개만 재실행"
    ),
    "runner_offline": (
        "1. `tail -100 .runtime/local_factory.log` 로 마지막 사망 원인 확인\n"
        "2. runner 1개 재실행: `python3 -m control_tower.local_runner.runner`\n"
        "3. 30초 후 control_state.liveness.runner_online=true 확인"
    ),
    "git_add_ignored_file": (
        "1. cycle.py / runner.py 의 changed_files 수집 단계에서 "
        "`git check-ignore` 또는 `git ls-files --others --exclude-standard` 로 "
        ".gitignore 무시 파일을 사전에 필터.\n"
        "2. claude_apply 가 새로 만든 파일 중 __pycache__/*.pyc 가 포함되지 않는지 확인.\n"
        "3. 수정 후 deploy_to_server 재시도."
    ),
    "git_add_failed": (
        "1. command_diagnostics.failed_reason 의 stderr 본문에서 정확한 실패 원인 확인.\n"
        "2. .git/index.lock 이 남아있다면 안전하게 제거 (실행 중인 git 명령 없을 때만).\n"
        "3. cycle.py 의 git_add 호출이 ignored / locked 케이스를 graceful 하게 처리하도록 보강.\n"
        "4. 재시도 후 동일 stage 에서 다시 실패하면 ticket 화."
    ),
    "qa_not_run": (
        "1. command_diagnostics.failed_stage 가 가리키는 단계 (branch_check / "
        "publish_blocker / secret_scan) 의 실패 메시지 확인.\n"
        "2. 해당 단계 통과 후 deploy_to_server 재시도.\n"
        "3. QA 가 절대 skip 되지 않도록 cycle.py 의 stage 순서 / 의존성 점검."
    ),
    "qa_gate_failed": (
        "1. .runtime/qa_diagnostics.json 의 failed_command / exit_code / stderr_tail 확인.\n"
        "2. 실패 명령이 npm run build → 변경된 .jsx/.tsx 의 syntax 오류 가능.\n"
        "3. 실패 명령이 py_compile → stderr 의 line number 로 문법/import 오류 수정.\n"
        "4. claude_apply 가 변경한 파일과 실패 파일이 일치하면 Claude 응답을 다시 검토.\n"
        "5. 수정 후 cycle 재실행."
    ),
    "claude_apply_failed_no_code_change": (
        "1. .runtime/implementation_ticket.md 의 target_files 명세가 구체적인지 확인.\n"
        "2. target_files 가 모호하면 PM 단계로 rollback 후 재실행.\n"
        "3. claude_apply.diff 를 열어 Claude 가 무엇을 보고 왜 변경하지 않았는지 분석.\n"
        "4. 같은 패턴이 3회 반복되면 continuous OFF + 운영자 직접 검토."
    ),
    "implementation_ticket_missing": (
        "1. .runtime/product_planner_report.md, .runtime/planner_revision.md, "
        ".runtime/pm_decision.md 세 파일을 스캔해 수정 대상 파일 (target_files) 을 "
        "추출 — 코드 경로 패턴 (.py / .jsx / .tsx / .ts / .js / .css / .md 중 "
        "app/ 또는 control_tower/ 하위) 을 모두 수집.\n"
        "2. target_files 가 ≥1 이면 cycle.py 의 stage_implementation_ticket 에 "
        "fallback 경로를 추가해 다음을 만족하는 implementation_ticket.md 를 "
        "직접 작성:\n"
        "   - IMPLEMENTATION_TICKET_REQUIRED_HEADINGS 의 5 개 섹션 (작업 / 수정 "
        "대상 파일 / 성공 기준 / 검증 방법 / 위험) 모두 포함\n"
        "   - target_files 섹션에 위에서 추출한 경로를 줄바꿈으로 명시\n"
        "3. ticket 생성 직후 factory_state.implementation_ticket_status = "
        "\"generated\" 로 마킹해 다음 stage (claude_apply) 가 정상 진행되도록 함.\n"
        "4. target_files 가 0 이면 fallback ticket 을 만드는 대신 PM decision "
        "프롬프트를 강화 — 'must include explicit list of files to modify under "
        "app/* or control_tower/*' 같은 강한 요구를 추가하고 PM 단계로 "
        "rollback 후 재실행.\n"
        "5. 변경 후 cycle 한 번 돌려 implementation_ticket_status=\"generated\" "
        "AND claude_apply_changed_files 길이 > 0 확인."
    ),
    "planner_required_output_missing": (
        "1. cycle.py 의 stage_product_planning 결과 / fallback 경로 점검.\n"
        "2. _persist_planner_fallback 가 모든 실패 경로에서 호출되는지 확인.\n"
        "3. fallback report 가 _validate_planner_report 의 모든 게이트를 통과하는지 단위 확인.\n"
        "4. 임시 조치: cycle 한 번 더 돌려 fallback 진입을 다시 시도."
    ),
    "current_stage_stuck": (
        "1. 어떤 stage 가 어떤 required_output 을 기다리고 있는지 확인.\n"
        "2. required_output_exists=false 라면 해당 stage 의 산출물 생성 로직 점검.\n"
        "3. stage_timeout_sec 이 너무 짧다면 cycle.py 의 timeout 정의 검토.\n"
        "4. 같은 stage 가 3회 이상 stuck 이면 운영자 직접 조치."
    ),
    "actions_pending_timeout": (
        "1. `gh run list --limit 5` 로 GH Actions 실제 상태 확인.\n"
        "2. workflow 가 stuck / cancelled 면 GitHub UI 에서 재실행.\n"
        "3. runner 의 actions polling 이 stale 한 것이면 runner.py 의 polling 로직 점검."
    ),
    "old_deploy_failed_stale": (
        "1. .runtime/deploy_progress.json 의 status=failed 가 정말 직전 사이클 결과인지 "
        "control_state.deploy.status 와 비교.\n"
        "2. control_state 가 이미 ready/no_changes 로 재분류했다면 화면의 붉은 표시는 "
        "UI 측 stale — Web UI 의 badge 결정 로직을 control_state 우선으로 변경.\n"
        "3. 또는 새 cycle 한 번 돌려 deploy_progress 자체를 갱신."
    ),
    "unknown": (
        "1. control_state.json / factory_state.json / pipeline_state.json / "
        "forward_progress_state.json 의 raw 필드를 읽고 어떤 시그널이 비어있고 어떤 "
        "시그널이 비정상인지 운영자가 직접 분류.\n"
        "2. 분류 가능한 신규 패턴이라면 factory_observer.py 의 classify() 에 새로운 "
        "diagnostic_code 추가.\n"
        "3. 분류 불가하면 운영자 직접 조치 후 사이클 재실행."
    ),
}


ACCEPTANCE_TEMPLATE_BY_CODE: dict[str, str] = {
    "stale_runner": (
        "- pipeline_state.diagnostic_code != 'stale_runner'\n"
        "- control_state.status in {running, idle, completed}"
    ),
    "duplicate_runner": (
        "- ps aux | grep runner | grep -v grep 결과 1줄 (또는 0줄)"
    ),
    "runner_offline": (
        "- control_state.liveness.runner_online == true\n"
        "- control_state.liveness.heartbeat_at 이 지난 60초 이내"
    ),
    "git_add_ignored_file": (
        "- deploy_to_server 명령이 성공\n"
        "- factory_command_diagnostics.diagnostic_code != 'qa_not_run'\n"
        "- claude_apply_changed_files 에 __pycache__/*.pyc 없음"
    ),
    "git_add_failed": (
        "- factory_command_diagnostics.failed_stage != 'git_add'\n"
        "- 다음 사이클이 deploy 단계까지 도달"
    ),
    "qa_not_run": (
        "- qa_diagnostics.qa_status in {passed, failed} (no_changes 도 OK)\n"
        "- factory_command_diagnostics.diagnostic_code != 'qa_not_run'"
    ),
    "qa_gate_failed": (
        "- qa_diagnostics.qa_status == 'passed'\n"
        "- 같은 변경 파일에 대해 cycle 재실행 시 QA Gate 통과"
    ),
    "claude_apply_failed_no_code_change": (
        "- claude_apply_changed_files 길이 > 0\n"
        "- agent_accountability.meaningful_change == true"
    ),
    "implementation_ticket_missing": (
        "- .runtime/implementation_ticket.md 가 5 개 required headings 모두 포함\n"
        "- target_files 섹션에 app/ 또는 control_tower/ 하위 경로 ≥1 명시\n"
        "- factory_state.implementation_ticket_status == 'generated'\n"
        "- 다음 cycle 에서 claude_apply_changed_files 길이 > 0\n"
        "- pipeline_state.diagnostic_code != 'implementation_ticket_missing'\n"
        "- control_state.diagnostic_code != 'implementation_ticket_missing'"
    ),
    "planner_required_output_missing": (
        "- product_planner_status in {generated, fallback_generated}\n"
        "- product_planner_gate_failures 빈 리스트"
    ),
    "current_stage_stuck": (
        "- forward_progress_state.status in {progressing, completed}\n"
        "- required_output_exists == true"
    ),
    "actions_pending_timeout": (
        "- control_state.deploy.actions_status == 'completed'\n"
        "- control_state.deploy.actions_conclusion == 'success'"
    ),
    "old_deploy_failed_stale": (
        "- deploy_progress.status != 'failed'\n"
        "- 또는 UI badge 가 control_state.deploy.status 를 따름"
    ),
    "unknown": (
        "- factory_observer 가 새 diagnostic_code 로 분류\n"
        "- 또는 운영자가 직접 분류 후 cycle 재실행"
    ),
}


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------


def _format_evidence_lines(evidence: list[str]) -> str:
    if not evidence:
        return "- (no evidence captured)"
    return "\n".join(f"- {e}" for e in evidence)


def _format_runtime_files() -> str:
    return (
        "- .runtime/control_state.json\n"
        "- .runtime/factory_state.json\n"
        "- .runtime/agent_accountability.json\n"
        "- .runtime/pipeline_state.json\n"
        "- .runtime/forward_progress_state.json\n"
        "- .runtime/deploy_progress.json\n"
        "- .runtime/factory_publish.json\n"
        "- .runtime/auto_publish_request.json\n"
        "- .runtime/qa_diagnostics.json\n"
        "- .runtime/factory_command_diagnostics.json\n"
        "- .runtime/local_factory.log (tail 300)"
    )


def build_failure_report(state: dict, classification: dict) -> str:
    cs = state.get("control_state") or {}
    fs = state.get("factory_state") or {}
    code = classification.get("diagnostic_code") or "unknown"
    severity = classification.get("severity") or "info"
    cycle_id = fs.get("cycle") or "—"
    cs_status = cs.get("status") or "—"
    summary = cs.get("summary") or "—"
    cs_diag = cs.get("diagnostic_code") or "—"
    risk = _risk_label(severity, code)

    cmds = MANUAL_COMMANDS_BY_CODE.get(code, MANUAL_COMMANDS_BY_CODE["unknown"])
    cmds_block = "\n".join(f"```\n{c}\n```" if c.startswith("#") is False
                           and not c.startswith("(") else c for c in cmds)
    # Simpler: just join as code block lines
    cmds_block = "```\n" + "\n".join(cmds) + "\n```"

    auto_fix = "예 (자동 수정 후보)" if classification.get("auto_fix_possible") else "아니오 (운영자 검토 필요)"

    procs_block = _format_process_block(classification)

    return (
        f"# Factory Failure Report\n\n"
        f"생성 시각: {_utc_now_iso()}\n"
        f"사이클 ID: {cycle_id}\n"
        f"진단 코드: **{code}**\n"
        f"심각도: {severity}\n"
        f"분류: {classification.get('category')}\n"
        f"위험도: {risk}\n"
        f"자동 수정 가능 여부: {auto_fix}\n\n"
        f"## 현재 상태 요약\n"
        f"- control_state.status: `{cs_status}`\n"
        f"- control_state.summary: {summary}\n"
        f"- control_state.diagnostic_code: `{cs_diag}`\n"
        f"- factory_state.status: `{fs.get('status') or '—'}`\n"
        f"- factory_state.claude_apply_status: `{fs.get('claude_apply_status') or '—'}`\n"
        f"- factory_state.qa_status: `{fs.get('qa_status') or '—'}`\n"
        f"- factory_state.implementation_ticket_status: `{fs.get('implementation_ticket_status') or '—'}`\n\n"
        f"## 가장 가능성 높은 root cause\n"
        f"{classification.get('root_cause') or '—'}\n\n"
        f"## 근거 로그\n"
        f"{_format_evidence_lines(classification.get('evidence') or [])}\n\n"
        f"{procs_block}"
        f"## 관련 runtime 파일\n"
        f"{_format_runtime_files()}\n\n"
        f"## 수동 확인 명령\n"
        f"{cmds_block}\n\n"
        f"## 다음 행동\n"
        f"- {cs.get('next_action') or '운영자 검토 필요'}\n"
        f"- 자동 수정 가능 여부: {auto_fix}\n"
        f"- 같은 진단이 3회 이상 반복되면 continuous OFF + 운영자 직접 조치\n"
    )


def _format_process_block(classification: dict) -> str:
    """Render the runner/caffeinate process section. classification carries
    process-shaped evidence on duplicate_runner / runner_offline; for
    other diagnostics the section is omitted."""
    code = classification.get("diagnostic_code")
    if code not in {"duplicate_runner", "runner_offline"}:
        return ""
    evidence = classification.get("evidence") or []
    runner_lines = [e for e in evidence if isinstance(e, str)
                    and e.startswith("python_runner: ")]
    wrapper_lines = [e for e in evidence if isinstance(e, str)
                     and e.startswith("caffeinate: ")]
    body = "## 프로세스 현황\n"
    body += f"- Python runner: {len(runner_lines)} 개\n"
    body += f"- caffeinate wrapper: {len(wrapper_lines)} 개\n"
    if runner_lines:
        body += "### Python runner\n"
        for line in runner_lines:
            body += f"  - `{line[len('python_runner: '):]}`\n"
    if wrapper_lines:
        body += "### caffeinate wrapper\n"
        for line in wrapper_lines:
            body += f"  - `{line[len('caffeinate: '):]}`\n"
    return body + "\n"


def build_repair_prompt(state: dict, classification: dict) -> str:
    fs = state.get("factory_state") or {}
    cs = state.get("control_state") or {}
    code = classification.get("diagnostic_code") or "unknown"
    cycle_id = fs.get("cycle") or "—"
    severity = classification.get("severity") or "info"

    targets = REPAIR_TARGETS_BY_CODE.get(code, REPAIR_TARGETS_BY_CODE["unknown"])
    requirements = REPAIR_REQUIREMENTS_BY_CODE.get(
        code, REPAIR_REQUIREMENTS_BY_CODE["unknown"],
    )
    acceptance = ACCEPTANCE_TEMPLATE_BY_CODE.get(
        code, ACCEPTANCE_TEMPLATE_BY_CODE["unknown"],
    )
    cmds = MANUAL_COMMANDS_BY_CODE.get(code, MANUAL_COMMANDS_BY_CODE["unknown"])

    log_tail = (state.get("log_tail") or "").splitlines()[-40:]
    log_block = "\n".join(log_tail) if log_tail else "(no log lines)"

    repro_lines: list[str] = [
        f"control_state.status = {cs.get('status') or '—'}",
        f"control_state.diagnostic_code = {cs.get('diagnostic_code') or '—'}",
        f"factory_state.status = {fs.get('status') or '—'}",
        f"factory_state.claude_apply_status = {fs.get('claude_apply_status') or '—'}",
        f"factory_state.qa_status = {fs.get('qa_status') or '—'}",
        f"factory_state.implementation_ticket_status = "
        f"{fs.get('implementation_ticket_status') or '—'}",
    ]

    commit_msg = _suggest_commit_message(code)

    return (
        f"# Claude Code Repair Prompt\n\n"
        f"이 prompt 는 Stampport factory_observer 가 자동 생성했습니다.\n"
        f"운영자가 Claude Code 에 그대로 붙여넣어 수정 작업을 위임하세요.\n\n"
        f"생성 시각: {_utc_now_iso()}\n"
        f"사이클 ID: {cycle_id}\n"
        f"진단 코드: **{code}**\n"
        f"심각도: {severity}\n\n"
        f"## 증상\n"
        f"{classification.get('root_cause') or '—'}\n\n"
        f"## 재현 로그 (관련 상태)\n"
        + "\n".join(f"- {l}" for l in repro_lines)
        + "\n\n"
        f"### local_factory.log tail (최근 40줄)\n"
        f"```\n{log_block}\n```\n\n"
        f"## Root cause 추정\n"
        f"{classification.get('root_cause') or '—'}\n\n"
        f"근거:\n"
        f"{_format_evidence_lines(classification.get('evidence') or [])}\n\n"
        f"## 수정 대상 파일 후보\n"
        + "\n".join(f"- {t}" for t in targets) + "\n\n"
        f"## 수정 요구사항\n"
        f"{requirements}\n\n"
        f"## Acceptance test\n"
        f"{acceptance}\n\n"
        f"## 검증 명령\n"
        f"```\npython3 -m py_compile control_tower/local_runner/runner.py\n"
        f"python3 -m py_compile control_tower/local_runner/cycle.py\n"
        f"python3 -m py_compile control_tower/local_runner/control_state.py\n"
        f"python3 -m control_tower.local_runner.factory_observer --once\n"
        f"cd control_tower/web && npm run build\n```\n\n"
        f"## 수동 확인 명령\n"
        f"```\n" + "\n".join(cmds) + "\n```\n\n"
        f"## 제약 (안전 모드)\n"
        f"- factory_observer 는 코드를 수정하거나 commit/push 하지 않습니다.\n"
        f"- 위 수정 요구사항은 운영자가 검토 후 Claude Code 에 직접 위임해야 합니다.\n"
        f"- 위험 파일 수정 금지: .env*, secrets, .runtime/, node_modules/\n"
        f"- 작업 후 cycle 한 번 돌려 control_state 가 healthy/running/completed 로 "
        f"복구되는지 확인.\n\n"
        f"## Commit message (suggestion)\n"
        f"```\n{commit_msg}\n```\n"
    )


def build_manual_review_guide(state: dict, classification: dict) -> str:
    """publish_required 케이스 전용 — 실패가 아니라 review/publish 대기."""
    fs = state.get("factory_state") or {}
    cs = state.get("control_state") or {}
    deploy = cs.get("deploy") or {}
    cycle_id = fs.get("cycle") or "—"
    changed_files = list(fs.get("claude_apply_changed_files") or [])

    files_block = (
        "\n".join(f"- {f}" for f in changed_files[:20])
        or "- (claude_apply_changed_files 비어 있음 — control_state.deploy.changed_files_count 참조)"
    )

    return (
        f"# Manual Review Guide — publish_required\n\n"
        f"이 상태는 **실패가 아닙니다**. 코드 변경 + QA 통과가 끝났고 commit/push "
        f"만 남은 상태입니다. 운영자가 변경 내용을 직접 확인한 뒤 publish 하세요.\n\n"
        f"생성 시각: {_utc_now_iso()}\n"
        f"사이클 ID: {cycle_id}\n\n"
        f"## 현재 상태\n"
        f"- control_state.status: `{cs.get('status') or '—'}`\n"
        f"- changed_files_count: {deploy.get('changed_files_count') or len(changed_files)}\n"
        f"- qa_status: `{deploy.get('qa_status') or fs.get('qa_status') or '—'}`\n"
        f"- commit_hash: `{deploy.get('commit_hash') or '—'}`\n"
        f"- push_status: `{deploy.get('push_status') or '—'}`\n\n"
        f"## 변경 파일\n"
        f"{files_block}\n\n"
        f"## 검토 체크리스트\n"
        f"- [ ] `git status` 로 현재 작업 트리 변경 내용 확인\n"
        f"- [ ] `git diff --stat` 로 파일별 변경 규모 확인\n"
        f"- [ ] `cat .runtime/qa_report.md` 로 QA 결과 확인\n"
        f"- [ ] 변경에 의도한 기능 외의 부수 변경 (.runtime/* 제외) 가 없는지 확인\n"
        f"- [ ] 변경 내용이 CLAUDE.md 의 MVP 범위 / 디자인 방향에 어긋나지 않는지 확인\n\n"
        f"## Publish 명령\n"
        f"```\n"
        f"# UI 에서 publish_changes 또는 deploy_to_server 명령 실행\n"
        f"# 또는 (마지막 수단으로 직접):\n"
        f"git status\n"
        f"git diff --stat\n"
        f"git add <변경 파일들>\n"
        f"git commit -m \"<요약>\"\n"
        f"git push origin main\n"
        f"```\n\n"
        f"## 안전 모드 원칙\n"
        f"- factory_observer 는 commit / push 를 자동 실행하지 않습니다.\n"
        f"- 운영자 검토를 거친 뒤 UI 의 publish_changes / deploy_to_server 명령을 사용하세요.\n"
    )


def _risk_label(severity: str, code: str) -> str:
    if code == "publish_required":
        return "낮음 (review 대기)"
    if severity == "blocker":
        return "높음 — cycle 진행 불가"
    if severity == "error":
        return "중간 — 같은 cycle 에서 진행 불가"
    if severity == "warning":
        return "낮음 ~ 중간 — 다음 cycle 에서 재시도 가능"
    return "낮음"


def _suggest_commit_message(code: str) -> str:
    headers = {
        "git_add_ignored_file": "Filter ignored files before git add",
        "git_add_failed": "Harden git_add stage against transient failures",
        "qa_not_run": "Ensure QA stage always runs before deploy",
        "qa_gate_failed": "Fix QA gate failure",
        "claude_apply_failed_no_code_change": "Tighten implementation ticket so claude_apply makes real changes",
        "implementation_ticket_missing": "Generate fallback implementation ticket when PM decision lacks one",
        "planner_required_output_missing": "Always run planner fallback when gate fails",
        "current_stage_stuck": "Unblock stuck pipeline stage",
        "stale_runner": "(no code change — restart runner)",
        "duplicate_runner": "(no code change — kill duplicate runner)",
        "runner_offline": "(no code change — restart runner)",
        "actions_pending_timeout": "Refresh actions polling on long-pending workflow",
        "old_deploy_failed_stale": "UI: prefer control_state.deploy over deploy_progress for badge",
        "unknown": "(diagnose-only — extend factory_observer.classify with new code)",
    }
    return headers.get(code, "Repair factory cycle blocker")


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return _runtime_dir() / "factory_observer_state.json"


def _log_path() -> Path:
    return _runtime_dir() / "factory_observer.log"


def _failure_report_path() -> Path:
    return _runtime_dir() / "factory_failure_report.md"


def _repair_prompt_path() -> Path:
    return _runtime_dir() / "claude_repair_prompt.md"


def _manual_review_path() -> Path:
    return _runtime_dir() / "factory_manual_review_guide.md"


def _append_log(line: str) -> None:
    try:
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(f"[{_utc_now_iso()}] {line}\n")
    except OSError:
        pass


def _save_text(path: Path, text: str) -> None:
    try:
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _save_state(state_dict: dict) -> None:
    try:
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        _state_path().write_text(
            json.dumps(state_dict, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError):
        pass


# ---------------------------------------------------------------------------
# Tick — single observation pass
# ---------------------------------------------------------------------------


def tick(runtime: Path | None = None) -> dict:
    """One Observer pass. Always cheap (file reads + ps aux).

    Returns the persisted state dict.
    """
    rt = runtime or _runtime_dir()
    state = collect_state(rt)
    runner_processes, caffeinate_processes = detect_runner_processes()
    classification = classify(
        state,
        runner_processes=runner_processes,
        caffeinate_processes=caffeinate_processes,
    )

    code = classification["diagnostic_code"]
    failure_report = build_failure_report(state, classification)
    _save_text(_failure_report_path(), failure_report)

    if classification["category"] == "review":
        manual = build_manual_review_guide(state, classification)
        _save_text(_manual_review_path(), manual)
        # Also write a passive repair_prompt that says "no repair needed" so
        # downstream consumers always have the file to read.
        passive = (
            f"# Claude Code Repair Prompt\n\n"
            f"진단 코드: **{code}** — 실패 아님.\n\n"
            f"이 사이클은 review/publish 대기 상태입니다. 수정 요청 없음.\n"
            f"운영자는 .runtime/factory_manual_review_guide.md 를 참고해 publish 하세요.\n"
        )
        _save_text(_repair_prompt_path(), passive)
    else:
        repair_prompt = build_repair_prompt(state, classification)
        _save_text(_repair_prompt_path(), repair_prompt)
        # Remove any stale manual review guide so the operator isn't
        # confused — but only if WE wrote it last time. We never delete
        # other files, so simply overwrite with a stub.
        if _manual_review_path().is_file():
            try:
                _manual_review_path().unlink()
            except OSError:
                pass

    persisted = {
        "updated_at": _utc_now_iso(),
        "diagnostic_code": code,
        "severity": classification.get("severity"),
        "category": classification.get("category"),
        "is_failure": bool(classification.get("is_failure")),
        "auto_fix_possible": bool(classification.get("auto_fix_possible")),
        "root_cause": classification.get("root_cause"),
        "evidence": classification.get("evidence") or [],
        "runner_process_count": len(runner_processes),
        "runner_processes": runner_processes,
        "caffeinate_process_count": len(caffeinate_processes),
        "caffeinate_processes": caffeinate_processes,
        "outputs": {
            "failure_report": str(_failure_report_path()),
            "repair_prompt": str(_repair_prompt_path()),
            "manual_review_guide": (
                str(_manual_review_path())
                if classification["category"] == "review" else None
            ),
        },
        "safe_mode": True,
    }
    _save_state(persisted)
    _append_log(
        f"observer_tick · code={code} · severity={classification.get('severity')} "
        f"· category={classification.get('category')} "
        f"· python_runner={len(runner_processes)} "
        f"· caffeinate={len(caffeinate_processes)}",
    )
    return persisted


# ---------------------------------------------------------------------------
# Self-test (acceptance fixtures)
# ---------------------------------------------------------------------------


def _empty_state() -> dict:
    return {
        "control_state": {},
        "factory_state": {},
        "agent_accountability": {},
        "pipeline_state": {},
        "forward_progress_state": {},
        "deploy_progress": {},
        "factory_publish": {},
        "auto_publish_request": {},
        "qa_diagnostics": {},
        "factory_command_diagnostics": {},
        "log_tail": "",
    }


def self_test() -> tuple[int, int, list[str]]:
    """Return (passed, total, failure_messages)."""
    failures: list[str] = []
    total = 0
    passed = 0

    # All classify() invocations below pass runner_processes=["fake-py"]
    # so the runner_offline branch (Python runner = 0) doesn't fire when
    # we're testing other diagnostics. Tests for runner_offline /
    # duplicate_runner / wrapper-only override this explicitly.
    fake_python_runner = [
        "/usr/local/bin/python3 -m control_tower.local_runner.runner",
    ]

    # A. stale_runner
    total += 1
    s = _empty_state()
    s["pipeline_state"] = {"diagnostic_code": "stale_runner",
                            "failed_stage": "implementation_ticket"}
    s["control_state"] = {"status": "operator_required",
                            "diagnostic_code": "stale_runner",
                            "liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] == "stale_runner":
        passed += 1
    else:
        failures.append(f"A: expected stale_runner, got {c['diagnostic_code']}")

    # B. git_add ignored file (__pycache__ in log)
    total += 1
    s = _empty_state()
    s["log_tail"] = (
        "[2026-04-29T21:15:32Z] runner · deploy_failed · "
        "다음 경로는 .gitignore 파일 중 하나 때문에 무시합니다:\n"
        "app/api/app/__pycache__\n"
    )
    s["control_state"] = {"status": "failed", "liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] == "git_add_ignored_file":
        passed += 1
    else:
        failures.append(f"B: expected git_add_ignored_file, got {c['diagnostic_code']}")

    # C. implementation_ticket_missing → fallback ticket creation in repair prompt
    total += 1
    s = _empty_state()
    s["factory_state"] = {
        "pm_decision_status": "generated",
        "implementation_ticket_status": "missing",
    }
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    prompt = build_repair_prompt(s, c)
    has_fallback_requirement = (
        c["diagnostic_code"] == "implementation_ticket_missing"
        and "fallback" in prompt.lower()
        and "ticket" in prompt.lower()
    )
    if has_fallback_requirement:
        passed += 1
    else:
        failures.append(
            "C: expected implementation_ticket_missing diagnostic + "
            "fallback ticket creation requirement in repair prompt"
        )

    # D. publish_required (changed=3 + qa passed + no commit)
    total += 1
    s = _empty_state()
    s["factory_state"] = {
        "claude_apply_status": "applied",
        "claude_apply_changed_files": ["a.py", "b.py", "c.py"],
        "qa_status": "passed",
        "implementation_ticket_status": "generated",
    }
    s["control_state"] = {
        "status": "ready_to_publish",
        "diagnostic_code": "publish_required",
        "deploy": {
            "changed_files_count": 3,
            "qa_status": "passed",
            "commit_hash": None,
            "push_status": None,
        },
        "liveness": {"runner_online": True},
    }
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] == "publish_required" and not c["is_failure"]:
        passed += 1
    else:
        failures.append(
            f"D: expected publish_required (not failure), got {c['diagnostic_code']} "
            f"(is_failure={c['is_failure']})"
        )

    # E. duplicate_runner (2 Python runner process lines)
    total += 1
    s = _empty_state()
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(
        s,
        runner_processes=[
            "user 12345 0.1 0.5 python3 -m control_tower.local_runner.runner",
            "user 12346 0.1 0.5 python3 -m control_tower.local_runner.runner",
        ],
        caffeinate_processes=[],
    )
    if c["diagnostic_code"] == "duplicate_runner":
        passed += 1
    else:
        failures.append(f"E: expected duplicate_runner, got {c['diagnostic_code']}")

    # F. unknown — verify raw evidence is included
    total += 1
    s = _empty_state()
    s["control_state"] = {
        "status": "blocked",
        "diagnostic_code": "weird_unmapped_code",
        "liveness": {"runner_online": True},
    }
    s["factory_state"] = {"status": "weird_state"}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    has_raw_evidence = (
        c["diagnostic_code"] == "unknown"
        and any("control_state.status=blocked" in e for e in c["evidence"])
        and any("weird_unmapped_code" in e for e in c["evidence"])
    )
    if has_raw_evidence:
        passed += 1
    else:
        failures.append(
            f"F: expected unknown with raw evidence, got {c['diagnostic_code']} "
            f"with evidence={c['evidence']}"
        )

    # G. detect_runner_processes splits Python runners from caffeinate
    # wrappers and filters self / grep.
    total += 1
    sample = (
        "sungpyo  100   0.1 0.5  100  100  s001 S    01:00 0:01 "
        "/usr/local/Cellar/python@3.11/3.11.7/Frameworks/Python.framework"
        "/Versions/3.11/Resources/Python.app/Contents/MacOS/Python "
        "-m control_tower.local_runner.runner\n"
        "sungpyo  101   0.0 0.1  100  100  s001 S    01:00 0:00 "
        "caffeinate -dimsu app/api/.venv/bin/python "
        "-m control_tower.local_runner.runner\n"
        "sungpyo  102   0.0 0.1  100  100  s001 S    01:00 0:00 "
        "grep control_tower.local_runner.runner\n"
        "sungpyo  103   0.0 0.1  100  100  s001 S    01:00 0:00 "
        "/usr/bin/python3 -m control_tower.local_runner.factory_observer --once\n"
    )
    py_procs, caff_procs = detect_runner_processes(ps_output=sample)
    if (
        len(py_procs) == 1
        and len(caff_procs) == 1
        and "Python.app/Contents/MacOS/Python" in py_procs[0]
        and "caffeinate -dimsu" in caff_procs[0]
        and not any("factory_observer" in p for p in py_procs + caff_procs)
        and not any(" grep " in p for p in py_procs + caff_procs)
    ):
        passed += 1
    else:
        failures.append(
            f"G: detect_runner_processes split failed — "
            f"py={py_procs} caffeinate={caff_procs}"
        )

    # H. Python runner 1 + caffeinate 1 → NOT duplicate_runner.
    total += 1
    s = _empty_state()
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(
        s,
        runner_processes=[
            "user 1 0 0 python3 -m control_tower.local_runner.runner",
        ],
        caffeinate_processes=[
            "user 2 0 0 caffeinate -dimsu python -m control_tower.local_runner.runner",
        ],
    )
    if c["diagnostic_code"] != "duplicate_runner":
        passed += 1
    else:
        failures.append(
            f"H: Python=1 + caffeinate=1 should NOT be duplicate_runner, got "
            f"{c['diagnostic_code']}"
        )

    # I. Python runner 2 + caffeinate 1 → duplicate_runner.
    total += 1
    s = _empty_state()
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(
        s,
        runner_processes=[
            "user 1 0 0 python3 -m control_tower.local_runner.runner",
            "user 2 0 0 python3 -m control_tower.local_runner.runner",
        ],
        caffeinate_processes=[
            "user 3 0 0 caffeinate -dimsu python -m control_tower.local_runner.runner",
        ],
    )
    if c["diagnostic_code"] == "duplicate_runner":
        passed += 1
    else:
        failures.append(
            f"I: Python=2 + caffeinate=1 should be duplicate_runner, got "
            f"{c['diagnostic_code']}"
        )

    # J. Python runner 0 + caffeinate 0 → runner_offline.
    total += 1
    s = _empty_state()
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(s, runner_processes=[], caffeinate_processes=[])
    if c["diagnostic_code"] == "runner_offline":
        passed += 1
    else:
        failures.append(
            f"J: Python=0 + caffeinate=0 should be runner_offline, got "
            f"{c['diagnostic_code']}"
        )

    # K. Python runner 0 + caffeinate 1 → runner_offline (broken_wrapper note).
    total += 1
    s = _empty_state()
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(
        s,
        runner_processes=[],
        caffeinate_processes=[
            "user 1 0 0 caffeinate -dimsu python -m control_tower.local_runner.runner",
        ],
    )
    has_broken_wrapper_note = (
        c["diagnostic_code"] == "runner_offline"
        and "broken_wrapper" in (c.get("root_cause") or "")
    )
    if has_broken_wrapper_note:
        passed += 1
    else:
        failures.append(
            f"K: Python=0 + caffeinate=1 should be runner_offline + "
            f"broken_wrapper note, got {c['diagnostic_code']} / "
            f"root={(c.get('root_cause') or '')[:80]}"
        )

    # L. ps line that mentions module but is launched by sh wrapper or
    # an editor is dropped from both lists (covers vim/sed/etc).
    total += 1
    sample = (
        "user 1 0 0 sh -c 'python -m control_tower.local_runner.runner'\n"
        "user 2 0 0 vim control_tower/local_runner/runner.py\n"
    )
    py_procs, caff_procs = detect_runner_processes(ps_output=sample)
    # vim line doesn't include "-m control_tower.local_runner.runner" so
    # it never enters the loop; sh wrapper IS matched by module token but
    # excluded as a non-caffeinate wrapper.
    if py_procs == [] and caff_procs == []:
        passed += 1
    else:
        failures.append(
            f"L: shell wrapper / editor lines should be dropped — "
            f"py={py_procs} caff={caff_procs}"
        )

    # ----- Promotion tests M–Q (regression for the unknown-misclassify bug) -----

    # M. control_state.diagnostic_code = implementation_ticket_missing
    # → observer must promote, NOT fall through to unknown.
    total += 1
    s = _empty_state()
    s["control_state"] = {
        "status": "operator_required",
        "diagnostic_code": "implementation_ticket_missing",
        "liveness": {"runner_online": True},
    }
    s["forward_progress_state"] = {"status": "operator_required"}
    s["agent_accountability"] = {"overall_status": "retry_required"}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    is_promoted = (
        c["diagnostic_code"] == "implementation_ticket_missing"
        and any("source=control_state.diagnostic_code" in e
                for e in c["evidence"])
    )
    if is_promoted:
        passed += 1
    else:
        failures.append(
            f"M: control_state.diagnostic_code=implementation_ticket_missing "
            f"should promote, got {c['diagnostic_code']} (evidence={c['evidence'][:3]})"
        )

    # N. pipeline_state.diagnostic_code only — promotion still wins.
    total += 1
    s = _empty_state()
    s["pipeline_state"] = {"diagnostic_code": "implementation_ticket_missing"}
    s["control_state"] = {"status": "blocked", "liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    is_pipeline_promoted = (
        c["diagnostic_code"] == "implementation_ticket_missing"
        and any("source=pipeline_state.diagnostic_code" in e
                for e in c["evidence"])
    )
    if is_pipeline_promoted:
        passed += 1
    else:
        failures.append(
            f"N: pipeline_state.diagnostic_code=implementation_ticket_missing "
            f"should promote (with source=pipeline_state.diagnostic_code), "
            f"got {c['diagnostic_code']}"
        )

    # O. control_state.diagnostic_code = current_stage_stuck → promote.
    total += 1
    s = _empty_state()
    s["control_state"] = {
        "status": "blocked",
        "diagnostic_code": "current_stage_stuck",
        "liveness": {"runner_online": True},
    }
    s["forward_progress_state"] = {
        "status": "stuck",
        "current_stage": "implementation_ticket",
        "required_output": ".runtime/implementation_ticket.md",
        "required_output_exists": False,
    }
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] == "current_stage_stuck":
        passed += 1
    else:
        failures.append(
            f"O: control_state.diagnostic_code=current_stage_stuck should "
            f"promote, got {c['diagnostic_code']}"
        )

    # P. Known diagnostic anywhere in the priority chain blocks `unknown`.
    # Use a code that no field-shape rule would otherwise match.
    total += 1
    s = _empty_state()
    s["qa_diagnostics"] = {"diagnostic_code": "actions_pending_timeout"}
    s["control_state"] = {
        "status": "blocked",
        "liveness": {"runner_online": True},
    }
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] != "unknown":
        passed += 1
    else:
        failures.append(
            "P: known diagnostic in qa_diagnostics should block unknown"
        )

    # Q. NO diagnostic anywhere AND no log pattern AND blocked status →
    # unknown is allowed (and only here).
    total += 1
    s = _empty_state()
    s["control_state"] = {
        "status": "blocked",
        "diagnostic_code": "weird_unmapped_xyz",
        "liveness": {"runner_online": True},
    }
    s["factory_state"] = {"status": "weird_state"}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    is_unknown_with_marker = (
        c["diagnostic_code"] == "unknown"
        and any("no_known_diagnostic_in" in e for e in c["evidence"])
    )
    if is_unknown_with_marker:
        passed += 1
    else:
        failures.append(
            f"Q: blocked + unknown diagnostic_code should fall to unknown "
            f"with no_known_diagnostic_in marker, got {c['diagnostic_code']}"
        )

    # ---- New 2026-05-02 fixtures (smoke / hold / fresh / bridge) ----

    # S. fresh runtime — empty factory_state → fresh_idle (info, healthy).
    total += 1
    s = _empty_state()
    s["control_state"] = {"liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if (
        c["diagnostic_code"] == "fresh_idle"
        and c["category"] == "healthy"
        and c["is_failure"] is False
    ):
        passed += 1
    else:
        failures.append(
            f"S: fresh runtime should be fresh_idle/healthy, got "
            f"{c['diagnostic_code']} ({c['category']}, is_failure={c['is_failure']})"
        )

    # T. PM HOLD — factory_state.status=hold_for_rework → pm_hold_for_rework.
    total += 1
    s = _empty_state()
    s["factory_state"] = {
        "status": "hold_for_rework",
        "cycle": 7,
        "pm_decision_status": "generated",
        "pm_decision_message": "HOLD (총점 19/30, rework=visual_desire,share)",
        "pm_decision_ship_ready": False,
    }
    s["control_state"] = {
        "status": "hold_for_rework",
        "liveness": {"runner_online": True},
    }
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if (
        c["diagnostic_code"] == "pm_hold_for_rework"
        and c["is_failure"] is False
        and c["category"] == "hold"
    ):
        passed += 1
    else:
        failures.append(
            f"T: pm_hold_for_rework expected (hold, not failure), got "
            f"{c['diagnostic_code']} ({c['category']}, is_failure={c['is_failure']})"
        )

    # U. PM SHIP + target_files=0 → pm_scope_missing_target_files.
    total += 1
    s = _empty_state()
    s["factory_state"] = {
        "status": "planning_only",
        "cycle": 8,
        "pm_decision_status": "generated",
        "pm_decision_ship_ready": True,
        "implementation_ticket_status": "pm_scope_missing_target_files",
    }
    s["control_state"] = {
        "status": "blocked",
        "liveness": {"runner_online": True},
    }
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] == "pm_scope_missing_target_files":
        passed += 1
    else:
        failures.append(
            f"U: pm_scope_missing_target_files expected, got {c['diagnostic_code']}"
        )

    # V. ready_to_review (publish disabled) — changed_files=3 + qa passed +
    # no commit + LOCAL_RUNNER_ALLOW_PUBLISH unset → ready_to_review.
    total += 1
    s = _empty_state()
    s["factory_state"] = {
        "status": "succeeded",
        "cycle": 9,
        "claude_apply_status": "applied",
        "claude_apply_changed_files": ["app/web/src/foo.jsx", "b.py", "c.md"],
        "qa_status": "passed",
        "implementation_ticket_status": "generated",
    }
    s["control_state"] = {
        "status": "ready_to_publish",
        "deploy": {"changed_files_count": 3, "qa_status": "passed",
                    "commit_hash": None, "push_status": None},
        "liveness": {"runner_online": True},
    }
    prev_env = os.environ.pop("LOCAL_RUNNER_ALLOW_PUBLISH", None)
    try:
        c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    finally:
        if prev_env is not None:
            os.environ["LOCAL_RUNNER_ALLOW_PUBLISH"] = prev_env
    if (
        c["diagnostic_code"] == "ready_to_review"
        and c["category"] == "review"
        and c["is_failure"] is False
    ):
        passed += 1
    else:
        failures.append(
            f"V: ready_to_review expected (review/non-failure), got "
            f"{c['diagnostic_code']} ({c['category']}, is_failure={c['is_failure']})"
        )

    # W. bridge_pause_mismatch — log shows "pause applied (... desired=running)".
    total += 1
    s = _empty_state()
    s["factory_state"] = {"status": "running", "cycle": 10}
    s["control_state"] = {"liveness": {"runner_online": True}}
    s["log_tail"] = (
        "[2026-05-02T01:00:00Z] factory bridge · pause applied "
        "(continuous=False, desired=running)\n"
    )
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] == "bridge_pause_mismatch":
        passed += 1
    else:
        failures.append(
            f"W: bridge_pause_mismatch expected, got {c['diagnostic_code']}"
        )

    # X. fresh runtime + leftover product_planner_gate_failures (empty) does
    # NOT trigger planner_required_output_missing.
    total += 1
    s = _empty_state()
    s["control_state"] = {"liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] != "planner_required_output_missing":
        passed += 1
    else:
        failures.append(
            "X: fresh runtime should not be planner_required_output_missing"
        )

    # Y. fresh runtime → not implementation_ticket_missing (pm_decision_status
    # is empty, so the field-shape rule must not fire).
    total += 1
    s = _empty_state()
    s["control_state"] = {"liveness": {"runner_online": True}}
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if c["diagnostic_code"] != "implementation_ticket_missing":
        passed += 1
    else:
        failures.append(
            "Y: fresh runtime should not be implementation_ticket_missing"
        )

    # R. publish_required promoted from state preserves category=review,
    # is_failure=False.
    total += 1
    s = _empty_state()
    s["control_state"] = {
        "status": "ready_to_publish",
        "diagnostic_code": "publish_required",
        "deploy": {
            "changed_files_count": 2,
            "qa_status": "passed",
            "commit_hash": None,
            "push_status": None,
        },
        "liveness": {"runner_online": True},
    }
    c = classify(s, runner_processes=fake_python_runner, caffeinate_processes=[])
    if (
        c["diagnostic_code"] == "publish_required"
        and c["category"] == "review"
        and c["is_failure"] is False
    ):
        passed += 1
    else:
        failures.append(
            f"R: promoted publish_required should be review/non-failure, got "
            f"code={c['diagnostic_code']} cat={c['category']} "
            f"is_failure={c['is_failure']}"
        )

    return passed, total, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_tick_summary(persisted: dict) -> None:
    code = persisted.get("diagnostic_code")
    sev = persisted.get("severity")
    cat = persisted.get("category")
    print(f"[factory_observer] code={code} severity={sev} category={cat}")
    outputs = persisted.get("outputs") or {}
    for k, v in outputs.items():
        if v:
            print(f"  - {k}: {v}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory_observer",
        description="Stampport factory failure relay bot (safe-mode).",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single observation tick and exit.")
    parser.add_argument("--watch", action="store_true",
                        help="Run in a loop with --interval seconds between ticks.")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between watch ticks (default: 300).")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in acceptance tests A–F and exit.")
    args = parser.parse_args(argv)

    if args.self_test:
        passed, total, failures = self_test()
        print(f"[factory_observer self-test] {passed}/{total} passed")
        for msg in failures:
            print(f"  FAIL · {msg}")
        return 0 if passed == total else 1

    if args.watch:
        print(f"[factory_observer] watch mode — interval={args.interval}s")
        try:
            while True:
                persisted = tick()
                _print_tick_summary(persisted)
                time.sleep(max(5, args.interval))
        except KeyboardInterrupt:
            print("[factory_observer] watch interrupted")
            return 0

    # Default to --once.
    persisted = tick()
    _print_tick_summary(persisted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
