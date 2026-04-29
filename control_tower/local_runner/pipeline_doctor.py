"""Stampport Pipeline Doctor — minimum version.

The Doctor sits one layer above the Watchdog / Pipeline Recovery
Orchestrator and answers a different question:

    "If the operator opened Control Tower right now, what's the
    smallest concrete repair-prompt I can hand to Claude (or to the
    operator) that would unblock the pipeline?"

It does NOT auto-execute Claude. It only:

    1. Reads control_state.json + factory_state.json + the tail of
       local_factory.log
    2. Classifies into one of 5 minimum diagnostic codes:
         - product_planning_contract_failed
         - implementation_ticket_missing
         - claude_apply_skipped
         - no_code_change_loop
         - qa_gate_failed
    3. Writes a Claude-ready repair prompt to
       .runtime/pipeline_doctor_repair_prompt.md
    4. Tracks repeat counts in .runtime/pipeline_doctor_state.json so
       the same diagnostic firing 3+ times escalates to operator_required

Auto-execution is gated on:

    FACTORY_DOCTOR_ENABLED              (default false)
    FACTORY_DOCTOR_ALLOW_CLAUDE_REPAIR  (default false)

This file is stdlib-only and does not import runner.py or cycle.py
to keep the import graph acyclic.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _runtime_dir() -> Path:
    repo = Path(os.environ.get("LOCAL_RUNNER_REPO", str(Path.cwd())))
    return repo / ".runtime"


def _repo_root() -> Path:
    return Path(os.environ.get("LOCAL_RUNNER_REPO", str(Path.cwd())))


def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_log_tail(path: Path, lines: int = 40) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 8192)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

DOCTOR_REPEAT_LIMIT = 3


def _doctor_enabled() -> bool:
    raw = os.environ.get("FACTORY_DOCTOR_ENABLED", "false").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _doctor_allow_claude_repair() -> bool:
    raw = os.environ.get(
        "FACTORY_DOCTOR_ALLOW_CLAUDE_REPAIR", "false",
    ).strip().lower()
    return raw in {"true", "1", "yes", "on"}


# ---------------------------------------------------------------------------
# Diagnose
# ---------------------------------------------------------------------------


def _classify(
    control_state: dict,
    cycle_state: dict,
) -> dict:
    """Pick the most specific diagnostic among the 5 minimum codes,
    plus "healthy" / "unknown" fall-throughs.
    """
    cs_status = (control_state.get("status") or "unknown").strip()
    cs_code = (control_state.get("diagnostic_code") or "").strip()
    pipeline = control_state.get("pipeline") or {}
    pipe_code = (pipeline.get("diagnostic_code") or "").strip()
    pipe_failed_stage = pipeline.get("failed_stage")
    deploy = control_state.get("deploy") or {}
    accountability = control_state.get("agent_accountability") or {}

    planner_status = (cycle_state.get("product_planner_status") or "skipped").strip()
    planner_failures = list(cycle_state.get("product_planner_gate_failures") or [])
    ticket_status = (cycle_state.get("implementation_ticket_status") or "skipped").strip()
    apply_status = (cycle_state.get("claude_apply_status") or "skipped").strip()
    apply_changed = list(cycle_state.get("claude_apply_changed_files") or [])
    qa_status = (cycle_state.get("qa_status") or "skipped").strip()

    # 1. product_planning_contract_failed — planner gate failed AND
    # we are NOT in fallback_generated (fallback is the safety net).
    if (
        planner_status not in {"generated", "fallback_generated"}
        and planner_failures
    ):
        return {
            "diagnostic_code": "product_planning_contract_failed",
            "severity": "warning",
            "root_cause": (
                f"Product Planner 가 품질 가드에 실패했고 fallback 도 진행되지 않음 "
                f"({len(planner_failures)}건의 가드 실패)."
            ),
            "evidence": planner_failures[:6],
        }

    # 2. implementation_ticket_missing — PM decided but the ticket file
    # wasn't generated (or was generated empty).
    if (
        cycle_state.get("pm_decision_status") == "generated"
        and ticket_status in {"missing", "skipped", "failed"}
    ):
        return {
            "diagnostic_code": "implementation_ticket_missing",
            "severity": "error",
            "root_cause": (
                "PM 결정은 있는데 Implementation Ticket 이 생성되지 않음 "
                f"(ticket_status={ticket_status})."
            ),
            "evidence": [
                f"pm_decision_status=generated",
                f"implementation_ticket_status={ticket_status}",
            ],
        }

    # 3. claude_apply_skipped — ticket exists but apply skipped.
    if (
        ticket_status == "generated"
        and apply_status == "skipped"
    ):
        return {
            "diagnostic_code": "claude_apply_skipped",
            "severity": "error",
            "root_cause": (
                "Implementation Ticket 은 있지만 claude_apply 가 skipped — "
                "Claude 적용 단계가 실행되지 못해 코드 변경 0개."
            ),
            "evidence": [
                "implementation_ticket_status=generated",
                "claude_apply_status=skipped",
                f"changed_files={len(apply_changed)}",
            ],
        }

    # 4. no_code_change_loop — claude ran (or planner-only) but produced
    # 0 changes for at least 2 cycles.
    if pipe_code in {"no_code_change_loop", "planning_only_loop"}:
        return {
            "diagnostic_code": "no_code_change_loop",
            "severity": "error",
            "root_cause": (
                f"코드 변경 0개로 사이클이 반복 (pipe_code={pipe_code})."
            ),
            "evidence": [f"pipeline.diagnostic_code={pipe_code}"],
        }
    if pipe_code == "claude_apply_failed_no_code_change":
        return {
            "diagnostic_code": "no_code_change_loop",
            "severity": "error",
            "root_cause": "claude_apply 가 실행됐지만 변경 파일 0개.",
            "evidence": [
                f"pipeline.diagnostic_code={pipe_code}",
                f"changed_files={len(apply_changed)}",
            ],
        }

    # 5. qa_gate_failed — QA failed (and there really were changes to
    # validate, otherwise the aggregator already reclassifies to
    # no_changes).
    if qa_status == "failed" and apply_changed:
        return {
            "diagnostic_code": "qa_gate_failed",
            "severity": "error",
            "root_cause": "QA Gate 가 실제 변경 파일에 대해 실패.",
            "evidence": [
                f"qa_status={qa_status}",
                f"changed_files={len(apply_changed)}",
            ],
        }

    # No matching diagnostic — defer to control_state's overall verdict.
    if cs_status == "completed":
        return {
            "diagnostic_code": "healthy",
            "severity": "info",
            "root_cause": "control_state=completed — Doctor intervention 불요.",
            "evidence": [],
        }
    if cs_status == "running":
        return {
            "diagnostic_code": "healthy",
            "severity": "info",
            "root_cause": "control_state=running — 정상 진행 중.",
            "evidence": [],
        }

    return {
        "diagnostic_code": "unknown",
        "severity": "info",
        "root_cause": (
            f"분류된 진단 없음 (control_state={cs_status} / "
            f"diagnostic_code={cs_code or '—'})."
        ),
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# Repair prompt
# ---------------------------------------------------------------------------

REPAIR_TEMPLATE_HEAD = """\
# Pipeline Doctor Repair Prompt

당신은 Stampport 자동화 공장의 자동 진단 결과를 받은 Claude Code 운영자입니다.
이 prompt 는 Pipeline Doctor 가 작성했고, 운영자가 직접 Claude 에 붙여 넣거나
FACTORY_DOCTOR_ALLOW_CLAUDE_REPAIR=true 로 활성화된 자동 실행 경로에서 사용합니다.

생성 시각: {generated_at}
사이클 ID: {cycle_id}
진단 코드: **{code}**
심각도: {severity}

## 원인 분석
{root_cause}

## 증거
{evidence}

## 권장 조치
"""


REPAIR_BY_CODE = {
    "product_planning_contract_failed": (
        "1. cycle.py 의 stage_product_planning 결과를 보고 fallback 이 왜 진행되지 않았는지 확인.\n"
        "2. _persist_planner_fallback 가 모든 실패 경로에서 호출되는지 점검.\n"
        "3. fallback report 가 _validate_planner_report 의 모든 게이트를 통과하는지 단위 확인.\n"
        "4. 일시 조치: cycle 을 한 번 더 돌려 fallback 진입을 다시 시도."
    ),
    "implementation_ticket_missing": (
        "1. .runtime/pm_decision.md 의 본문을 확인 — 수정 대상 파일이 명시됐는가?\n"
        "2. stage_implementation_ticket 의 _parse_target_files_from_md 가 PM 결정 파일에서 .jsx/.tsx/.py 경로를 추출했는지 점검.\n"
        "3. PM 결정에 'app/web/src/...' 등의 명시 파일이 없으면 PM 단계로 rollback 후 재실행.\n"
        "4. ticket 본문은 IMPLEMENTATION_TICKET_REQUIRED_HEADINGS 의 5개 섹션을 모두 포함해야 함."
    ),
    "claude_apply_skipped": (
        "1. FACTORY_APPLY_CLAUDE 환경변수가 false 로 설정돼 있지 않은지 확인 (default 는 ON).\n"
        "2. LOCAL_RUNNER_CLAUDE_COMMAND 가 'claude --dangerously-skip-permissions' 등 valid 명령으로 설정돼 있는지 확인.\n"
        "3. claude CLI 가 PATH 에서 발견 가능한지 `which claude` 로 확인.\n"
        "4. claude_proposal.md 가 같은 사이클에서 generated 됐는지 확인 — stale proposal 은 거부됨.\n"
        "5. risky_files 가 비어 있는지 확인 — 비어 있지 않으면 publish_blocker_resolve 단계 점검."
    ),
    "no_code_change_loop": (
        "1. claude_apply 의 prompt 와 implementation_ticket.md 를 보고 Claude 가 실제 수정 대상 파일에 접근했는지 확인.\n"
        "2. ticket 의 target_files 가 app/web/src/ 또는 app/api/ 하위인지 확인 — docs/config 만이면 의미 없음.\n"
        "3. 같은 cycle 의 claude_apply.diff 를 열어 Claude 가 어떤 파일을 보았고 왜 변경하지 않았는지 분석.\n"
        "4. 다음 cycle 에서 ticket 본문을 더 구체적으로 만들고 (FE 작업 / 수정 대상 파일 / 성공 기준) 재실행.\n"
        "5. 반복 3회 이상이면 continuous OFF + 운영자 직접 검토."
    ),
    "qa_gate_failed": (
        "1. .runtime/qa_diagnostics.json 의 failed_command / exit_code / stderr_tail 확인.\n"
        "2. 실패 명령이 npm run build 라면 변경된 .jsx/.tsx 의 syntax 오류 가능성 — Claude Repair 로 해당 파일 수정 요청.\n"
        "3. 실패 명령이 py_compile 이라면 stderr 의 line number 확인 후 수정.\n"
        "4. QA 실패가 cycle 자체 버그(qa_report 누락 / path mismatch)라면 cycle.py 의 stage_qa_gate 점검.\n"
        "5. claude_apply 가 변경한 파일과 실패 파일이 일치하면 Claude 응답을 다시 확인."
    ),
    "healthy": "조치 필요 없음 — control_state 가 정상 상태.",
    "unknown": (
        "control_state.json / factory_state.json 의 raw 데이터를 운영자가 직접 확인 후 분류 필요. "
        "Doctor 의 5개 minimum diagnostic 에 해당하지 않는 신규 케이스일 수 있음."
    ),
}


def _build_repair_prompt(diagnose: dict, cycle_state: dict, log_tail: str) -> str:
    code = diagnose.get("diagnostic_code") or "unknown"
    cycle_id = cycle_state.get("cycle") or "—"
    evidence_lines = "\n".join(f"- {e}" for e in (diagnose.get("evidence") or []))
    if not evidence_lines:
        evidence_lines = "- (no evidence captured)"

    head = REPAIR_TEMPLATE_HEAD.format(
        generated_at=_utc_now_iso(),
        cycle_id=cycle_id,
        code=code,
        severity=(diagnose.get("severity") or "info"),
        root_cause=(diagnose.get("root_cause") or "—"),
        evidence=evidence_lines,
    )
    body = REPAIR_BY_CODE.get(code, REPAIR_BY_CODE["unknown"])
    tail_block = ""
    if log_tail:
        tail_block = (
            "\n\n## local_factory.log tail (최신 40줄)\n```\n"
            + log_tail.strip() + "\n```\n"
        )
    constraints = (
        "\n\n## 제약\n"
        "- FACTORY_DOCTOR_ALLOW_CLAUDE_REPAIR=false 이면 자동 실행 금지 — 운영자 검토 후 수동 실행.\n"
        "- 같은 diagnostic_code 가 3회 반복되면 operator_required 로 격상.\n"
        "- 위험 파일 (.env*, secrets, .runtime, node_modules) 수정 금지.\n"
        "- 작업이 끝나면 cycle 을 한 번 다시 돌려 control_state 가 healthy / running 으로 복구되는지 확인.\n"
    )
    return head + body + tail_block + constraints


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

DOCTOR_STATE_FILE = ".runtime/pipeline_doctor_state.json"
DOCTOR_PROMPT_FILE = ".runtime/pipeline_doctor_repair_prompt.md"


def _state_path() -> Path:
    return _runtime_dir() / "pipeline_doctor_state.json"


def _prompt_path() -> Path:
    return _runtime_dir() / "pipeline_doctor_repair_prompt.md"


def read_state() -> dict:
    return _read_json(_state_path()) or {}


def _save_state(state: dict) -> None:
    try:
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _utc_now_iso()
        _state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError):
        pass


def _save_prompt(text: str) -> None:
    try:
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        _prompt_path().write_text(text, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def tick() -> dict:
    """One Doctor pass. Always cheap (file reads only). Returns the
    full state dict the runner can surface in heartbeat metadata under
    `local_factory.pipeline_doctor`."""
    runtime = _runtime_dir()
    control_state = _read_json(runtime / "control_state.json") or {}
    cycle_state = _read_json(runtime / "factory_state.json") or {}
    log_tail = _read_log_tail(runtime / "local_factory.log", lines=40)
    prior = read_state()

    diagnose = _classify(control_state, cycle_state)
    code = diagnose["diagnostic_code"]
    repeat_count_by_code = dict(prior.get("repeat_count_by_code") or {})

    if code in {"healthy", "unknown"}:
        # Healthy ticks decay the most-recent code's counter so a
        # transient bad cycle doesn't permanently park us at 3.
        last_code = prior.get("last_diagnostic_code")
        if last_code and last_code not in {"healthy", "unknown"}:
            repeat_count_by_code[last_code] = max(
                0, int(repeat_count_by_code.get(last_code, 0)) - 1,
            )
    else:
        repeat_count_by_code[code] = int(repeat_count_by_code.get(code, 0)) + 1

    operator_required = bool(
        code not in {"healthy", "unknown"}
        and repeat_count_by_code.get(code, 0) >= DOCTOR_REPEAT_LIMIT
    )

    prompt_text = _build_repair_prompt(diagnose, cycle_state, log_tail)
    if code != "healthy":
        _save_prompt(prompt_text)

    state = {
        "enabled": _doctor_enabled(),
        "allow_claude_repair": _doctor_allow_claude_repair(),
        "last_checked_at": _utc_now_iso(),
        "last_diagnostic_code": code,
        "severity": diagnose.get("severity"),
        "root_cause": diagnose.get("root_cause"),
        "evidence": diagnose.get("evidence") or [],
        "repeat_count_by_code": repeat_count_by_code,
        "repeat_count_for_current": repeat_count_by_code.get(code, 0),
        "operator_required": operator_required,
        "repair_prompt_path": str(_prompt_path()),
        "cycle_id": cycle_state.get("cycle"),
    }
    _save_state(state)
    return state


def read_meta() -> dict:
    """Heartbeat metadata view — same as the persisted state with a
    `available` boolean for the UI."""
    s = read_state()
    if not s:
        return {
            "available": False,
            "enabled": _doctor_enabled(),
            "allow_claude_repair": _doctor_allow_claude_repair(),
            "last_diagnostic_code": "unknown",
            "operator_required": False,
            "repair_prompt_path": str(_prompt_path()),
        }
    s["available"] = True
    return s
