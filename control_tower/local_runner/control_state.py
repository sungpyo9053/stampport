"""Stampport Control State Aggregator.

Single source of truth for "is the factory healthy / is it making
progress / can the operator go home". Reads every relevant on-disk
state file (factory_state.json, factory_publish.json, qa_diagnostics
.json, factory_command_diagnostics.json, operator_fix_state.json,
factory_watchdog.json, pipeline_state.json, forward_progress_state.json,
agent_accountability.json), applies a hard-coded contradiction-resolver,
and writes a single normalized verdict to .runtime/control_state.json.

Why this exists:

    The Watchdog, Pipeline Recovery, Forward Progress, Agent
    Supervisor, and Deploy panels each compute their own "status" from
    their own file. When one says HEALTHY and another says STUCK, the
    dashboard tells the operator two different stories. The aggregator
    closes that gap by picking the strictest verdict and forcing every
    downstream consumer to read from it.

Hard rules (mirror spec section 12 "금지되는 조합"):

    1. If `agent_accountability.overall_status` ∈ {failed, retry_required,
       planning_only, blocked} → control_state.status is at least `blocked`.
    2. If `forward_progress.status` ∈ {stuck, planning_only, no_progress,
       operator_required} → at least `blocked`.
    3. If `pipeline_recovery.failed_stage` is set AND
       `pipeline_recovery.diagnostic_code` not in healthy-set → at least
       `blocked`.
    4. If `deploy_progress.status == failed` AND `changed_files == 0` →
       deploy is reclassified to `no_changes`, NOT failed; the failed
       row is treated as stale and does not propagate.
    5. If `qa_status == failed` AND `changed_files == 0` → qa is
       reclassified to `no_changes`, NOT failed.
    6. If `operator_fix_state.status` is in failure-set BUT
       `last_message` indicates applied/no-op → treated as stale, does
       not propagate.
    7. If retry budget exceeded on any stage (pipeline_state's
       retry_count_by_stage[stage] >= max_retry) → operator_required.
    8. If supervisor `meaningful_change=false` → completed status is
       refused; falls to `blocked` or `planning_only`.

Output shape mirrors spec section 1 verbatim so the UI can render
field-for-field.

Stdlib-only (no import on runner.py / cycle.py) so both modules can
import it without a circular dependency.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


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


# ---------------------------------------------------------------------------
# State sets
# ---------------------------------------------------------------------------

OPERATOR_FAILURE_STATES: frozenset[str] = frozenset({
    "failed",
    "qa_failed",
    "validation_failed",
    "no_code_change_failed",
    "push_failed",
    "git_failed",
})

OPERATOR_HEALTHY_STATES: frozenset[str] = frozenset({
    "idle", "running",
    "applied", "published",
    "noop_success", "no_changes",
})

# Markers in operator_fix_state.last_message that indicate the request
# actually succeeded as a no-op even when the status row is in a failure
# state. Same heuristic the watchdog stale_state_mismatch detector uses.
OPERATOR_HEALTHY_MARKERS: tuple[str, ...] = (
    "상태 applied",
    "상태 noop",
    "상태 published",
    "noop completed",
    "noop_success",
    "코드 변경 없이",
    "[normalize] prior=",
    "[smoke-clear] prior=",
)

# Pipeline diagnostic codes that should NOT count as "failed" at the
# top level — they're informational ("no changes to validate") or
# self-healing.
PIPELINE_HEALTHY_CODES: frozenset[str] = frozenset({
    "healthy",
    "no_changes_to_deploy",
    "no_changes_to_validate",
    "qa_report_missing_before_run",
})

# Diagnostic codes that must always block the cycle's completed verdict
# even when the supervisor or factory.status would otherwise accept it.
# `claude_apply_failed_no_code_change` is the canonical "ran but
# produced 0 files" signal — never let that float to completed.
PIPELINE_HARD_BLOCK_CODES: frozenset[str] = frozenset({
    "claude_apply_failed_no_code_change",
    "claude_apply_skipped",
    "implementation_ticket_missing",
    "implementation_ticket_invalid",
})

# Forward Progress statuses that DO bubble up as "blocked" at the
# control level.
FP_BLOCKED_STATUSES: frozenset[str] = frozenset({
    "stuck",
    "planning_only",
    "no_progress",
    "operator_required",
    "blocked",
})


# ---------------------------------------------------------------------------
# Sub-block computations
# ---------------------------------------------------------------------------


def _compute_liveness(runner_meta: dict | None) -> dict:
    """Liveness lives in the runner heartbeat metadata. The aggregator
    receives that block (or None when called outside the runner context
    — e.g. from cycle.py) and returns a stable shape."""
    if not runner_meta:
        return {
            "runner_online": False,
            "heartbeat_at": None,
            "runner_stale": False,
        }
    return {
        "runner_online": True,
        "heartbeat_at": runner_meta.get("started_at"),
        "runner_stale": bool(runner_meta.get("is_stale")),
    }


def _operator_indicates_healthy(op_state: dict) -> bool:
    msg = (op_state.get("last_message") or "").lower()
    return any(k in msg for k in (m.lower() for m in OPERATOR_HEALTHY_MARKERS))


def _compute_execution_kernel(
    op_state: dict,
    cmd_diag: dict,
) -> dict:
    """Operator request loop is the "execution kernel" — sits above the
    factory cycle and represents user-driven work. Its health is
    independent of cycle health."""
    op_status = (op_state.get("status") or "idle").strip()
    indicates_healthy = _operator_indicates_healthy(op_state)
    in_failure = op_status in OPERATOR_FAILURE_STATES

    if op_status in OPERATOR_HEALTHY_STATES:
        kernel_status = "healthy"
    elif in_failure and indicates_healthy:
        kernel_status = "degraded"  # stale state, will be cleared by watchdog
    elif in_failure:
        kernel_status = "broken"
    elif op_status == "running":
        kernel_status = "healthy"
    else:
        kernel_status = "healthy" if op_status == "idle" else "degraded"

    return {
        "status": kernel_status,
        "operator_request_status": op_status,
        "claude_started": op_status not in {"idle", ""},
        "last_failed_stage": (
            cmd_diag.get("failed_stage")
            if cmd_diag.get("last_command") == "operator_request" else None
        ),
        "last_error": (
            cmd_diag.get("failed_reason")
            if cmd_diag.get("last_command") == "operator_request" else None
        ),
        "stale_state_mismatch": bool(in_failure and indicates_healthy),
    }


def _compute_pipeline(
    pipeline_state: dict,
    fp_state: dict,
) -> dict:
    """Combine pipeline_recovery (stage classification) and
    forward_progress (time/output dimension). FP wins when its status
    is more severe than pipeline's."""
    fp_status = (fp_state.get("status") or "progressing").strip()
    pipe_code = (pipeline_state.get("diagnostic_code") or "healthy").strip()
    pipe_failed_stage = pipeline_state.get("failed_stage")

    # Pipeline-side severity.
    pipe_blocked = bool(
        pipe_failed_stage
        and pipe_code not in PIPELINE_HEALTHY_CODES
    )
    if pipe_code in PIPELINE_HARD_BLOCK_CODES:
        pipe_blocked = True
    fp_blocked = fp_status in FP_BLOCKED_STATUSES

    if fp_status == "stuck":
        status = "stuck"
    elif fp_status == "planning_only":
        status = "planning_only"
    elif fp_status == "no_progress":
        status = "no_progress"
    elif fp_status == "operator_required":
        status = "operator_required"
    elif fp_blocked or pipe_blocked:
        status = "blocked"
    elif fp_status == "progressing":
        status = "progressing"
    else:
        status = "idle"

    return {
        "status": status,
        "current_stage": (
            pipeline_state.get("current_stage")
            or fp_state.get("current_stage")
        ),
        "last_success_stage": pipeline_state.get("last_success_stage"),
        "failed_stage": pipe_failed_stage,
        "diagnostic_code": pipe_code if pipe_code != "healthy" else (
            fp_state.get("diagnostic_code") or "healthy"
        ),
        "required_output": fp_state.get("required_output"),
        "required_output_exists": bool(fp_state.get("required_output_exists")),
        "elapsed_sec": int(fp_state.get("current_stage_elapsed_sec") or 0),
        "stage_timeout_sec": int(fp_state.get("stage_timeout_sec") or 0),
    }


def _compute_agent_accountability(supervisor_report: dict) -> dict:
    """Just normalize the supervisor's verdict into the aggregator's
    shape. Spec wants the supervisor's blocking_agent to surface up."""
    overall = (supervisor_report.get("overall_status") or "skipped").strip()
    if overall == "pass":
        status = "pass"
    elif overall in {"retry_required", "planning_only", "blocked", "failed"}:
        status = "blocked"
    else:
        status = "skipped"
    return {
        "status": status,
        "blocking_agent": supervisor_report.get("blocking_agent"),
        "blocking_reason": supervisor_report.get("blocking_reason"),
        "meaningful_change": bool(supervisor_report.get("meaningful_change")),
        "implementation_ticket_exists": bool(
            supervisor_report.get("implementation_ticket_exists")
        ),
    }


def _compute_deploy(
    cycle_state: dict,
    publish_state: dict,
    cmd_diag: dict,
) -> dict:
    """Spec rule: changed_files=0 means deploy is `no_changes`, NOT
    failed. We override stale failed deploy_progress rows when there's
    nothing to ship."""
    changed_files = list(cycle_state.get("claude_apply_changed_files") or [])
    changed_count = len(changed_files)
    qa_status_raw = (cycle_state.get("qa_status") or "skipped").strip()
    dp = publish_state.get("deploy_progress") or {}
    dp_status = (dp.get("status") or "idle").strip()
    failed_stage = dp.get("failed_stage")

    # Reclassify QA when nothing changed.
    if changed_count == 0 and qa_status_raw in {"failed", "skipped"}:
        qa_status = "no_changes"
    else:
        qa_status = qa_status_raw

    # Reclassify deploy when nothing changed.
    if changed_count == 0:
        if dp_status in {"failed", "completed", "actions_triggered"}:
            deploy_status = "no_changes"
        elif dp_status == "idle":
            deploy_status = "idle"
        else:
            deploy_status = "no_changes"
        failed_stage_clean = None
    elif dp_status == "failed":
        deploy_status = "failed"
        failed_stage_clean = failed_stage
    elif dp_status == "completed":
        deploy_status = "completed"
        failed_stage_clean = None
    elif dp_status in {"deploying", "validating", "command_received",
                       "actions_triggered"}:
        deploy_status = "deploying"
        failed_stage_clean = None
    else:
        deploy_status = "ready" if changed_count > 0 else "idle"
        failed_stage_clean = None

    # If command_diagnostics says no_changes_to_deploy, that's the
    # canonical no-changes signal even if dp_status was stale.
    if (
        cmd_diag.get("last_command") == "deploy_to_server"
        and cmd_diag.get("diagnostic_code") == "no_changes_to_deploy"
    ):
        deploy_status = "no_changes"
        failed_stage_clean = None
        if qa_status == "failed":
            qa_status = "no_changes"

    return {
        "status": deploy_status,
        "changed_files_count": changed_count,
        "qa_status": qa_status,
        "last_failed_stage": failed_stage_clean,
        "commit_hash": (publish_state or {}).get("last_commit_hash"),
        "push_status": (publish_state or {}).get("last_push_status"),
    }


# ---------------------------------------------------------------------------
# Top-level resolver
# ---------------------------------------------------------------------------


HEALTHY_OVERALL_RESOLUTIONS: tuple[str, ...] = (
    "running", "completed", "idle",
)


def _resolve_overall(
    factory_status: str,
    pipeline_block: dict,
    accountability_block: dict,
    deploy_block: dict,
    kernel_block: dict,
) -> tuple[str, str | None, str | None, str | None]:
    """Pick the strictest verdict across all sub-blocks. Returns
    (status, summary, diagnostic_code, blocking_reason)."""
    pipe_status = pipeline_block.get("status")
    pipe_code = pipeline_block.get("diagnostic_code")
    acc_status = accountability_block.get("status")
    acc_blocking = accountability_block.get("blocking_agent")
    acc_meaningful = accountability_block.get("meaningful_change")
    acc_ticket = accountability_block.get("implementation_ticket_exists")
    kernel_status = kernel_block.get("status")
    deploy_status = deploy_block.get("status")
    changed = deploy_block.get("changed_files_count", 0)

    # Hardest signals first — operator_required wins over everything.
    if pipe_status == "operator_required":
        return (
            "operator_required",
            f"pipeline operator_required ({pipe_code})",
            pipe_code,
            pipeline_block.get("required_output") or "operator action needed",
        )
    if kernel_status == "broken":
        return (
            "operator_required",
            "operator_request loop in real failure state",
            "operator_request_failed_repeatedly",
            kernel_block.get("last_error"),
        )

    # Stuck / no_progress / planning_only block the cycle.
    if pipe_status in {"stuck", "no_progress", "planning_only"}:
        return (
            "blocked",
            f"pipeline {pipe_status} — {pipe_code}",
            pipe_code,
            pipeline_block.get("required_output")
            or "stage timeout / required output missing",
        )

    # Supervisor blocked dominates over factory.status==running.
    if acc_status == "blocked":
        return (
            "blocked",
            f"agent accountability blocked by {acc_blocking or 'unknown'}",
            "agent_accountability_blocked",
            accountability_block.get("blocking_reason"),
        )

    # Pipeline blocked.
    if pipe_status == "blocked":
        return (
            "blocked",
            f"pipeline blocked at {pipeline_block.get('failed_stage') or 'unknown'}",
            pipe_code,
            pipeline_block.get("required_output")
            or pipeline_block.get("failed_stage"),
        )

    # Deploy is in real failed state with code change present.
    if deploy_status == "failed":
        return (
            "failed",
            f"deploy failed at {deploy_block.get('last_failed_stage') or 'unknown'}",
            "deploy_failed",
            deploy_block.get("last_failed_stage"),
        )

    # Meaningful change gate — supervisor said pass but no real change.
    if acc_status == "pass" and not acc_meaningful and changed == 0:
        # Healthy idle — nothing to do, no failure either.
        return ("idle", "no work in progress", "healthy", None)

    # Factory itself reports a terminal classification.
    if factory_status in {"docs_only", "no_code_change", "planning_only"}:
        return (
            "blocked",
            f"factory cycle ended as {factory_status}",
            f"cycle_{factory_status}",
            "no meaningful code change this cycle",
        )

    if factory_status in {"failed"}:
        return (
            "failed",
            "factory cycle failed",
            "cycle_failed",
            None,
        )

    if factory_status == "running":
        # Genuine running — but only if pipeline is progressing.
        if pipe_status in {"progressing", "idle"}:
            return ("running", "factory cycle running", "healthy", None)
        # If we're here pipe_status was already handled above.
        return ("blocked", "factory running but pipeline not progressing", "no_forward_motion", None)

    if factory_status in {"succeeded", "completed"}:
        # Only call it completed if accountability + meaningful change agree.
        if acc_status == "pass" and acc_meaningful and acc_ticket:
            return ("completed", "factory cycle completed with meaningful change", "healthy", None)
        return (
            "blocked",
            "cycle says succeeded but supervisor / meaningful_change disagree",
            "supervisor_disagreed_with_succeeded",
            "supervisor or meaningful_change_gate refused completion",
        )

    return ("idle", "factory idle", "healthy", None)


def _suggest_next_action(status: str, diagnostic: str | None,
                         pipe: dict, acc: dict, kernel: dict) -> str:
    if status == "completed":
        return "조치 필요 없음"
    if status == "running":
        return "사이클이 진행 중입니다 — 다음 stage 결과 대기"
    if status == "operator_required":
        if kernel.get("status") == "broken":
            return "operator_request 가 실제 실패 상태 — 운영자가 직접 검토 후 재시도"
        return "재시도 한도 초과 — 운영자 직접 조치 필요"
    if status == "failed":
        return "publish/deploy 단계 실패 — failed_stage 의 stderr 확인 후 재배포"
    if status == "blocked":
        if acc.get("blocking_agent"):
            return (
                f"`{acc['blocking_agent']}` 에이전트 산출물 보강 후 재실행 — "
                f"{acc.get('blocking_reason') or 'supervisor retry_prompt 참조'}"
            )
        if pipe.get("required_output") and not pipe.get("required_output_exists"):
            return (
                f"`{pipe.get('current_stage')}` 의 required output "
                f"({pipe['required_output']}) 가 만들어질 때까지 대기 — "
                f"timeout={pipe.get('stage_timeout_sec')}s"
            )
        if diagnostic and diagnostic.startswith("cycle_"):
            return "이번 사이클은 코드 변경 없이 종료 — Continuous OFF + 운영자 검토"
        return "blocking_reason 확인 후 해당 단계 재실행"
    return "상태 확인 필요"


def aggregate(runner_meta: dict | None = None) -> dict:
    """Read every state file, compute the unified verdict, persist it
    to .runtime/control_state.json, and return the dict."""
    runtime = _runtime_dir()
    cycle_state = _read_json(runtime / "factory_state.json") or {}
    publish_state = _read_json(runtime / "factory_publish.json") or {}
    cmd_diag = _read_json(runtime / "factory_command_diagnostics.json") or {}
    op_state = _read_json(runtime / "operator_fix_state.json") or {}
    pipeline_state = _read_json(runtime / "pipeline_state.json") or {}
    fp_state = _read_json(runtime / "forward_progress_state.json") or {}
    supervisor_report = _read_json(runtime / "agent_accountability.json") or {}

    # Direct aggregator-level diagnostic for the "claude_apply applied
    # but produced 0 files" case. The pipeline orchestrator usually
    # writes this to pipeline_state.json, but on the very first tick
    # of a fresh runtime that file may not exist yet — we still need
    # to refuse `completed` immediately. Mirror the diagnostic into
    # pipeline_state so the rest of the pipeline-block computation
    # picks it up.
    apply_status = (cycle_state.get("claude_apply_status") or "").strip()
    apply_changed_count = len(cycle_state.get("claude_apply_changed_files") or [])
    if (
        apply_status in {"applied", "no_changes"}
        and apply_changed_count == 0
        and (cycle_state.get("implementation_ticket_status") in {"generated"})
        and not pipeline_state.get("diagnostic_code")
    ):
        pipeline_state = {
            **pipeline_state,
            "current_stage": "claude_apply",
            "failed_stage": "claude_apply",
            "diagnostic_code": "claude_apply_failed_no_code_change",
            "severity": "error",
            "root_cause": (
                "claude_apply 가 실행됐지만 변경 파일 0개 — completed 거부."
            ),
            "evidence": [f"claude_apply_status={apply_status}",
                         "claude_apply_changed_files=[]"],
        }

    liveness = _compute_liveness(runner_meta)
    kernel = _compute_execution_kernel(op_state, cmd_diag)
    pipeline = _compute_pipeline(pipeline_state, fp_state)
    accountability = _compute_agent_accountability(supervisor_report)
    deploy = _compute_deploy(cycle_state, publish_state, cmd_diag)

    factory_status = (cycle_state.get("status") or "idle").strip()
    overall_status, summary, diag_code, blocking_reason = _resolve_overall(
        factory_status,
        pipeline,
        accountability,
        deploy,
        kernel,
    )

    # Failed_stage at top level — pull from the most-specific source.
    top_failed_stage = (
        pipeline.get("failed_stage")
        or deploy.get("last_failed_stage")
        or kernel.get("last_failed_stage")
    )

    next_action = _suggest_next_action(
        overall_status, diag_code, pipeline, accountability, kernel,
    )

    # Continuous-stop signal — anything past "running" should not auto-
    # restart the cycle. The watchdog reads this to drive its
    # pause_factory action.
    should_stop_continuous = overall_status in {
        "blocked", "failed", "operator_required",
    }

    state = {
        "status": overall_status,
        "summary": summary,
        "diagnostic_code": diag_code,
        "failed_stage": top_failed_stage,
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "should_stop_continuous": should_stop_continuous,
        "updated_at": _utc_now_iso(),
        "factory_status_raw": factory_status,
        "liveness": liveness,
        "execution_kernel": kernel,
        "pipeline": pipeline,
        "agent_accountability": accountability,
        "deploy": deploy,
    }

    try:
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "control_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError):
        pass
    return state


def read_state() -> dict:
    return _read_json(_runtime_dir() / "control_state.json") or {}
