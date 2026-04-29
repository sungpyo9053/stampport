"""Stampport Agent Supervisor (Product Director / 공장장).

A meta-agent that audits the *other* agents' outputs at the end of every
factory cycle. Its job is to refuse to call a cycle "succeeded" when the
work that landed is just artifacts — no concrete user-facing change, no
implementation ticket, no real code edit, no QA tied to the change.

The supervisor reads on-disk state (factory_state.json, the artifact
files cycle.py writes, factory_publish.json) and produces:

    .runtime/agent_accountability.json   structured verdict
    .runtime/agent_accountability_report.md   human-readable report

Both runner.py (heartbeat metadata + watchdog dispatch) and cycle.py
(final status decision) consume the JSON.

Design note: this module is deliberately stdlib-only and free of any
import on runner.py or cycle.py to avoid circular imports. It receives
all state via file reads and exposes a small functional API.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths — recomputed each call so tests using LOCAL_RUNNER_REPO override
# pick up the right runtime directory without an import cycle.
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


def _read_text(path: Path, *, max_chars: int = 12_000) -> str:
    if not path.is_file():
        return ""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(body) > max_chars:
        return body[:max_chars]
    return body


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The Stampport-specific user-screen vocabulary the planner/designer must
# eventually point at. Used to detect "did the artifact name a real
# screen" without requiring a Figma-style file path.
STAMPPORT_SCREENS = (
    "Landing", "Login", "StampForm", "StampResult",
    "MyPassport", "Badges", "Titles", "Quests",
    "ShareCard", "Passport",
)

# User-desire keywords that planner/designer must address.
USER_DESIRE_KEYWORDS = (
    "수집욕", "과시욕", "성장욕", "희소성", "재방문",
    "수집", "공유", "자랑", "성장", "희소",
)

# Hard signals that the planner is just renaming badges instead of
# proposing a feature.
PLANNER_GENERIC_RED_FLAGS = (
    "단순 배지", "스탬프 이름만", "도장 이름만",
    "general improvement", "more polish",
)

# Designer red flags — abstract critique only.
DESIGNER_RED_FLAGS = (
    "더 예쁘게", "사용자 경험을 개선", "감각적으로",
    "전반적으로", "전체적으로",
)

# Files where a "code change" must land for the cycle to count as
# user-impacting. Anything else is docs/config/scripts noise from the
# supervisor's perspective.
USER_FACING_PATH_PREFIXES = (
    "app/web/src/",
    "control_tower/web/src/",
)
SERVER_CODE_PATH_PREFIXES = (
    "app/api/",
    "control_tower/api/",
)
DOCS_ONLY_PATH_PREFIXES = (
    "docs/",
    "config/",
)

# Implementation Ticket required headings.
IMPLEMENTATION_TICKET_REQUIRED_HEADINGS = (
    "## 선택한 기능",
    "## 수정 대상 파일",
    "## FE 작업",
    "## QA 시나리오",
    "## 성공 기준",
)


# ---------------------------------------------------------------------------
# Small helpers — score-into-status, keyword density, evidence builders.
# ---------------------------------------------------------------------------


def _bucket(score: int, *, pass_at: int = 60) -> str:
    if score >= pass_at:
        return "pass"
    return "fail"


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    h = haystack.lower()
    return any(n.lower() in h for n in needles)


def _count_any(haystack: str, needles: tuple[str, ...]) -> int:
    h = haystack.lower()
    return sum(1 for n in needles if n.lower() in h)


def _empty_agent_row(name: str) -> dict:
    return {
        "name": name,
        "status": "skipped",
        "score": 0,
        "problems": [],
        "evidence": [],
        "required_retry": False,
        "retry_prompt": "",
    }


# ---------------------------------------------------------------------------
# Per-agent evaluators
# ---------------------------------------------------------------------------


def evaluate_planner(state: dict) -> dict:
    """Score the planner output. PASS requires a concrete, novel feature
    proposal that names target screens/files, success criteria, and at
    least two user-desire signals."""
    runtime = _runtime_dir()
    row = _empty_agent_row("planner")

    body = (
        _read_text(runtime / "planner_proposal.md")
        or _read_text(runtime / "product_planner_report.md")
        or _read_text(runtime / "planner_revision.md")
    )

    planner_status = (state or {}).get("product_planner_status") or "skipped"
    if not body and planner_status != "generated":
        row["status"] = "skipped"
        row["problems"].append("planner_proposal.md / product_planner_report.md 모두 없음")
        return row

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    # 1) User problem statement
    if _has_any(body, ("사용자 문제", "사용자가", "다시 열", "리텐션", "반복 방문")):
        score += 15
        evidence.append("사용자 문제 / 재방문 동기 언급")
    else:
        problems.append("사용자가 다시 앱을 열 이유 / 사용자 문제 명시 부족")

    # 2) Novelty vs existing
    if _has_any(body, ("기존", "차별", "새로운", "novel", "기존과 다른")):
        score += 15
        evidence.append("기존 기능과의 차별성 언급")
    else:
        problems.append("기존 기능과 어떻게 다른지 설명 없음")

    # 3) Screen targets named
    screen_hits = [s for s in STAMPPORT_SCREENS if s.lower() in body.lower()]
    if screen_hits:
        score += 15
        evidence.append(f"수정 대상 화면 명시: {', '.join(screen_hits[:5])}")
    else:
        problems.append("수정 대상 화면 (StampForm / MyPassport / ShareCard 등) 명시 없음")

    # 4) File targets named
    if re.search(r"app/web/src/|control_tower/web/src/|app/api/", body):
        score += 15
        evidence.append("예상 수정 파일 경로 명시")
    else:
        problems.append("예상 수정 파일 명시 없음")

    # 5) User-desire signals — at least 2.
    desire_hits = _count_any(body, USER_DESIRE_KEYWORDS)
    if desire_hits >= 2:
        score += 15
        evidence.append(f"사용자 욕구 신호 {desire_hits}개")
    else:
        problems.append(
            f"수집욕/과시욕/성장욕/희소성/재방문 중 최소 2개 이상 필요 (현재 {desire_hits}개)"
        )

    # 6) Success criteria
    if _has_any(body, ("성공 기준", "성공 조건", "지표", "metric", "KPI")):
        score += 10
        evidence.append("성공 기준 언급")
    else:
        problems.append("성공 기준 (지표 / KPI / 성공 조건) 명시 없음")

    # 7) MVP scope mention
    if _has_any(body, ("MVP", "이번 사이클", "범위")):
        score += 10
        evidence.append("MVP / 사이클 범위 언급")
    else:
        problems.append("이번 사이클 MVP 범위 명시 없음")

    # 8) Designer review questions (planner explicitly asks designer)
    if _has_any(body, ("디자이너에게", "디자이너 검토", "designer")):
        score += 5
        evidence.append("디자이너 검토 요청 포함")

    # 9) Red flags (rename-only)
    if _has_any(body, PLANNER_GENERIC_RED_FLAGS):
        score = max(0, score - 30)
        problems.append("배지/스탬프 이름만 추가하는 패턴 감지 — 실제 행동 변화 부족")

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "기존 Local Visa/배지 아이디어 반복은 reject입니다. "
            "이번에는 사용자가 앱을 다시 열 이유가 있는 새로운 기능을 제안하세요. "
            "반드시 사용자 문제, 기존 기능과의 차별성, 수정 대상 화면 (StampForm / "
            "MyPassport / ShareCard 등), 예상 수정 파일, 수집욕/과시욕/성장욕/희소성/"
            "재방문 중 최소 2개 자극, 이번 사이클 MVP 범위, 성공 기준을 포함하세요."
        )
    return row


def evaluate_designer(state: dict) -> dict:
    runtime = _runtime_dir()
    row = _empty_agent_row("designer")

    body = (
        _read_text(runtime / "designer_critique.md")
        + "\n\n"
        + _read_text(runtime / "designer_final_review.md")
    ).strip()

    if not body:
        row["status"] = "skipped"
        row["problems"].append("designer_critique.md / designer_final_review.md 없음")
        return row

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    screen_hits = [s for s in STAMPPORT_SCREENS if s.lower() in body.lower()]
    if screen_hits:
        score += 25
        evidence.append(f"화면 단위 지시: {', '.join(screen_hits[:5])}")
    else:
        problems.append("실제 화면(StampForm/MyPassport/ShareCard 등) 단위 지시 없음")

    ui_directive_hits = _count_any(
        body,
        ("색상", "color", "레이아웃", "layout", "카드", "card", "버튼", "button",
         "아이콘", "icon", "문구", "copy", "타이포", "여백", "spacing"),
    )
    if ui_directive_hits >= 3:
        score += 25
        evidence.append(f"UI 지시 키워드 {ui_directive_hits}개")
    elif ui_directive_hits >= 1:
        score += 10
        evidence.append(f"UI 지시 키워드 {ui_directive_hits}개 (부족)")
        problems.append("색상/레이아웃/카드/버튼/아이콘/문구 등 UI 지시가 더 필요")
    else:
        problems.append("색상/레이아웃/카드/버튼/아이콘/문구 단위 지시 없음")

    desire_hits = _count_any(body, USER_DESIRE_KEYWORDS)
    if desire_hits >= 2:
        score += 20
        evidence.append(f"사용자 욕구 자극 설명 {desire_hits}개")
    else:
        problems.append("수집욕/과시욕/성장욕 자극 설명 부족")

    if _has_any(body, ("Figma", "와이어프레임", "wireframe", "mock")):
        score += 10
        evidence.append("Figma/와이어프레임 형식 시도")

    # File path references
    if re.search(r"app/web/src/|control_tower/web/src/", body):
        score += 10
        evidence.append("FE 구현 파일 경로까지 명시")

    # Red flags
    if _has_any(body, DESIGNER_RED_FLAGS):
        score = max(0, score - 30)
        problems.append("추상 비평 감지 (`더 예쁘게` / `전반적으로` 등)")

    if len(body.strip()) < 200:
        score = max(0, score - 20)
        problems.append("디자인 산출물 길이가 너무 짧음")

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "추상적인 비평만 있어 reject입니다. StampForm, StampResult, MyPassport, "
            "ShareCard 중 어떤 화면을 어떻게 바꿀지 카드 구조, 버튼 상태, 색상, "
            "문구, 아이콘 기준으로 다시 제안하세요. 사용자 수집욕/과시욕 자극 방법과 "
            "FE 구현 파일 경로까지 포함하세요."
        )
    return row


def evaluate_pm(state: dict) -> dict:
    runtime = _runtime_dir()
    row = _empty_agent_row("pm")

    pm_body = _read_text(runtime / "pm_decision.md")
    ticket_path = runtime / "implementation_ticket.md"
    ticket_body = _read_text(ticket_path)
    ticket_status = (state or {}).get("implementation_ticket_status") or "skipped"
    target_files = list((state or {}).get("implementation_ticket_target_files") or [])

    if not pm_body and not ticket_body and ticket_status == "skipped":
        row["status"] = "skipped"
        row["problems"].append("pm_decision.md / implementation_ticket.md 모두 없음")
        return row

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    # 1) Implementation Ticket present
    if ticket_path.is_file() and ticket_status == "generated":
        score += 30
        evidence.append("implementation_ticket.md status=generated")
    elif ticket_status == "missing":
        problems.append("Implementation Ticket의 수정 대상 파일이 비어 있어 missing 처리됨")
    else:
        problems.append("implementation_ticket.md 부재 또는 status≠generated")

    # 2) Target files non-empty
    if target_files:
        score += 25
        evidence.append(f"수정 대상 파일 {len(target_files)}개 명시")
    else:
        problems.append("수정 대상 파일 (target_files) 비어 있음")

    # 3) Required headings present
    if ticket_body:
        present = [h for h in IMPLEMENTATION_TICKET_REQUIRED_HEADINGS if h in ticket_body]
        if len(present) >= 4:
            score += 20
            evidence.append(f"필수 섹션 {len(present)}/{len(IMPLEMENTATION_TICKET_REQUIRED_HEADINGS)} 충족")
        else:
            problems.append(
                f"필수 섹션 부족 ({len(present)}/{len(IMPLEMENTATION_TICKET_REQUIRED_HEADINGS)}) — "
                "선택한 기능 / 수정 대상 파일 / FE 작업 / QA 시나리오 / 성공 기준 모두 필요"
            )
        # 4) Per-role assignments
        if _has_any(ticket_body, ("FE 작업", "프론트")) and _has_any(
            ticket_body, ("BE 작업", "백엔드", "API")
        ):
            score += 15
            evidence.append("FE/BE 작업 분해 확인")
        else:
            problems.append("FE/BE 담당자별 작업 분해 부족")

        # 5) QA scenarios
        if _has_any(ticket_body, ("QA 시나리오", "QA scenario", "manual QA")):
            score += 10
            evidence.append("QA 시나리오 포함")
        else:
            problems.append("QA 시나리오 명시 없음")

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "PM 결정은 구현 티켓이 아닙니다. 선택 기능, 수정 대상 파일 (app/web/src/ "
            "혹은 control_tower/web/src/), FE/BE/QA 작업, 제외 범위, QA 시나리오, "
            "성공 기준을 포함한 Implementation Ticket (.runtime/implementation_ticket.md)을 "
            "생성하세요."
        )
    return row


def _classify_changed_files(changed: list[str]) -> dict:
    fe = [f for f in changed if any(f.startswith(p) for p in USER_FACING_PATH_PREFIXES)]
    be = [f for f in changed if any(f.startswith(p) for p in SERVER_CODE_PATH_PREFIXES)]
    docs = [f for f in changed if any(f.startswith(p) for p in DOCS_ONLY_PATH_PREFIXES)]
    other = [f for f in changed if f not in fe and f not in be and f not in docs]
    return {"frontend": fe, "backend": be, "docs": docs, "other": other}


def evaluate_frontend(state: dict) -> dict:
    row = _empty_agent_row("frontend")
    changed = list((state or {}).get("claude_apply_changed_files") or [])
    cats = _classify_changed_files(changed)
    docs_only_cycle = bool((state or {}).get("docs_only"))

    if not changed:
        row["status"] = "skipped"
        row["problems"].append("claude_apply_changed_files 비어 있음 — FE 변경 없음")
        return row

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    if cats["frontend"]:
        score += 50
        evidence.append(f"FE 파일 변경 {len(cats['frontend'])}개")
    else:
        problems.append("app/web/src/ 또는 control_tower/web/src/ 변경 없음")

    if docs_only_cycle:
        score = max(0, score - 30)
        problems.append("docs/config 만 변경 — 사용자 영향 없음")

    # affected_screens / affected_flows hints (best-effort: filename match)
    screen_files = [
        f for f in cats["frontend"]
        if any(s in f for s in STAMPPORT_SCREENS) or "/screens/" in f or "/pages/" in f
    ]
    if screen_files:
        score += 30
        evidence.append(f"화면 파일 {len(screen_files)}개 변경: {', '.join(screen_files[:3])}")
    elif cats["frontend"]:
        problems.append("FE 변경이 components 일부에만 머물고 화면 단위 영향 불명확")

    # Build status — propagated by validation stage flags.
    val_status = (state or {}).get("validation_status") or "skipped"
    if val_status == "passed":
        score += 20
        evidence.append("validation passed (build_app/build_control)")
    elif val_status == "failed":
        problems.append("build/syntax validation 실패")
        score = max(0, score - 30)

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "이번 변경은 실제 화면에 연결되지 않았습니다. app/web/src/screens 또는 "
            "components 에서 사용자가 볼 수 있는 화면 변화를 만들고, npm run build "
            "결과를 남기세요. affected_screens / affected_flows 를 cycle_log에 표시할 "
            "수 있도록 구체 화면을 수정 대상으로 하세요."
        )
    return row


def evaluate_backend(state: dict) -> dict:
    row = _empty_agent_row("backend")
    changed = list((state or {}).get("claude_apply_changed_files") or [])
    cats = _classify_changed_files(changed)

    if not cats["backend"] and not cats["frontend"]:
        row["status"] = "skipped"
        row["problems"].append("BE/FE 변경 모두 없음")
        return row

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    if cats["backend"]:
        score += 70
        evidence.append(f"BE 파일 변경 {len(cats['backend'])}개")
        if any("schema" in f or "models" in f for f in cats["backend"]):
            score += 10
            evidence.append("schema/models 변경 감지")
        if any("router" in f or "endpoint" in f or "/api/" in f for f in cats["backend"]):
            score += 10
            evidence.append("API endpoint 변경 감지")
    elif cats["frontend"]:
        # FE-only cycle is acceptable when ticket didn't ask for BE work.
        ticket_text = _read_text(_runtime_dir() / "implementation_ticket.md")
        if _has_any(ticket_text, ("BE 작업", "API", "백엔드", "schema")):
            problems.append("Implementation Ticket이 BE 작업을 요구했지만 BE 변경 없음")
        else:
            row["status"] = "skipped"
            row["problems"].append("Implementation Ticket이 BE 작업을 명시하지 않아 skipped")
            return row

    val_status = (state or {}).get("validation_status") or "skipped"
    if val_status == "passed":
        score += 10

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "백엔드 산출물이 실제 API/schema/storage 변경으로 이어지지 않았습니다. "
            "FE가 사용할 수 있는 데이터 계약과 검증 가능한 코드 변경을 만드세요."
        )
    return row


def evaluate_ai(state: dict) -> dict:
    row = _empty_agent_row("ai")
    changed = list((state or {}).get("claude_apply_changed_files") or [])
    ticket_text = _read_text(_runtime_dir() / "implementation_ticket.md")
    proposal_text = _read_text(_runtime_dir() / "claude_proposal.md")

    asked_ai = _has_any(
        ticket_text + "\n" + proposal_text,
        ("추천", "AI/룰", "rule", "score", "kick_point", "킥포인트", "개인화"),
    )
    if not asked_ai:
        row["status"] = "skipped"
        row["problems"].append("이번 ticket은 AI/룰 작업을 요구하지 않음 — skipped")
        return row

    score = 30  # seed: ticket asked for it
    problems: list[str] = []
    evidence: list[str] = []

    rule_files = [
        f for f in changed
        if "score" in f or "rule" in f or "kick" in f or "recommend" in f
    ]
    if rule_files:
        score += 50
        evidence.append(f"룰/점수 관련 파일 {len(rule_files)}개 변경: {', '.join(rule_files[:3])}")
    else:
        problems.append("ticket이 AI/룰 작업을 요구했지만 score/rule/kick/recommend 파일 변경 없음")

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "AI/룰 산출물이 실제 코드 변경으로 이어지지 않았습니다. score/rule/kick "
            "포인트/추천 로직을 실제 app/api 또는 app/web 코드에 반영하세요."
        )
    return row


def evaluate_qa(state: dict) -> dict:
    runtime = _runtime_dir()
    row = _empty_agent_row("qa")
    changed = list((state or {}).get("claude_apply_changed_files") or [])
    qa_status = (state or {}).get("qa_status") or "skipped"
    qa_report = runtime / "qa_report.md"
    qa_diag = runtime / "qa_diagnostics.json"

    # No changes → QA isn't required for this cycle.
    if not changed:
        row["status"] = "skipped"
        row["problems"].append("changed_files=0 — 검증할 변경사항이 없어 QA 생략")
        return row

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    if qa_status == "passed":
        score += 60
        evidence.append("qa_status=passed")
    elif qa_status == "failed":
        problems.append("qa_status=failed — QA Gate 실패")
    else:
        problems.append(f"qa_status={qa_status} (passed가 아님)")

    if qa_report.is_file():
        body = _read_text(qa_report)
        score += 20
        evidence.append("qa_report.md 존재")
        if any(f.split("/")[-1] in body for f in changed[:20]):
            score += 15
            evidence.append("qa_report에 변경 파일명 언급")
        else:
            problems.append("qa_report가 generic — 변경 파일 기준 검증 부족")
    elif qa_diag.is_file():
        score += 5
        evidence.append("qa_diagnostics.json 존재 (qa_report.md 없음)")
        problems.append("qa_report.md가 없어 변경 기준 QA 어려움")
    else:
        problems.append("qa_report.md / qa_diagnostics.json 모두 없음")

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "QA report가 없거나 generic 합니다. 이번 사이클의 변경 파일 기준으로 "
            "수동 QA 시나리오, 성공 조건, 실패 조건, build 결과를 qa_report.md에 "
            "기록하세요."
        )
    return row


def evaluate_deploy(state: dict, publish_state: dict) -> dict:
    row = _empty_agent_row("deploy")
    changed = list((state or {}).get("claude_apply_changed_files") or [])

    if not changed:
        row["status"] = "skipped"
        row["problems"].append("changed_files=0 — 배포할 것이 없어 deploy 생략")
        return row

    commit_hash = (publish_state or {}).get("last_commit_hash")
    push_status = (publish_state or {}).get("last_push_status") or ""
    deploy_progress = (publish_state or {}).get("deploy_progress") or {}

    score = 0
    problems: list[str] = []
    evidence: list[str] = []

    if commit_hash:
        score += 40
        evidence.append(f"commit hash {commit_hash[:8]}")
    else:
        problems.append("publish_state.last_commit_hash 없음")

    if push_status in {"ok", "succeeded"}:
        score += 40
        evidence.append("git push succeeded")
    else:
        problems.append(f"push_status={push_status or 'none'} (succeeded 아님)")

    dp_status = deploy_progress.get("status")
    if dp_status in {"actions_triggered", "completed"}:
        score += 20
        evidence.append(f"deploy_progress.status={dp_status}")
    elif dp_status == "failed":
        problems.append("deploy_progress.status=failed")

    score = min(100, max(0, score))
    row["score"] = score
    row["evidence"] = evidence
    row["problems"] = problems
    row["status"] = _bucket(score, pass_at=60)
    if row["status"] == "fail":
        row["required_retry"] = True
        row["retry_prompt"] = (
            "배포 단계가 완료되지 않았습니다. commit hash, push_status, deploy_progress "
            "를 확인하고 누락된 단계 (publish_changes / GitHub Actions 트리거)를 "
            "처리하세요."
        )
    return row


# ---------------------------------------------------------------------------
# Meaningful change classifier
# ---------------------------------------------------------------------------


def evaluate_meaningful_change(state: dict) -> dict:
    """Returns {meaningful_change, changed_files, affected_screens,
    affected_flows, evidence}. Hard-codes the policy: a change is
    meaningful iff at least one product-code path was edited (FE or BE)
    AND the cycle isn't docs_only."""
    changed = list((state or {}).get("claude_apply_changed_files") or [])
    cats = _classify_changed_files(changed)
    docs_only = bool((state or {}).get("docs_only"))

    affected_screens = sorted({
        s for f in cats["frontend"] for s in STAMPPORT_SCREENS
        if s.lower() in f.lower()
    })
    affected_flows: list[str] = []
    if any("/screens/" in f or "/pages/" in f for f in cats["frontend"]):
        affected_flows.append("user-screen-render")
    if cats["backend"]:
        affected_flows.append("api-data-contract")
    if any("kick" in f.lower() or "score" in f.lower() for f in changed):
        affected_flows.append("rule-or-score-engine")

    meaningful = bool(
        (cats["frontend"] or cats["backend"]) and not docs_only
    )

    evidence: list[str] = []
    if cats["frontend"]:
        evidence.append(f"FE 변경 {len(cats['frontend'])}개")
    if cats["backend"]:
        evidence.append(f"BE 변경 {len(cats['backend'])}개")
    if cats["docs"]:
        evidence.append(f"docs 변경 {len(cats['docs'])}개")
    if not changed:
        evidence.append("변경 파일 없음")

    return {
        "meaningful_change": meaningful,
        "changed_files": changed[:30],
        "affected_screens": affected_screens,
        "affected_flows": affected_flows,
        "evidence": evidence,
        "docs_only": docs_only,
    }


# ---------------------------------------------------------------------------
# Top-level run + report writers
# ---------------------------------------------------------------------------


def _decide_overall(agents: dict, mc: dict, state: dict) -> tuple[str, str | None, str | None, bool]:
    """Returns (overall_status, blocking_agent, blocking_reason, operator_required)."""
    # blocking_agent — first agent in pipeline order whose required_retry=true.
    pipeline_order = ("planner", "designer", "pm", "frontend", "backend", "ai", "qa", "deploy")
    blocking = None
    for name in pipeline_order:
        a = agents.get(name) or {}
        if a.get("required_retry"):
            blocking = name
            break

    operator_required = False
    if blocking:
        return "retry_required", blocking, agents[blocking]["problems"][0] if agents[blocking].get("problems") else None, operator_required

    if not mc["meaningful_change"]:
        # Distinguish planning_only (artifacts produced but no code) vs
        # plain blocked (no artifacts at all).
        if any((agents.get(n) or {}).get("status") == "pass" for n in ("planner", "designer", "pm")):
            return "planning_only", "frontend", "산출물은 있으나 의미 있는 코드 변경이 없음", operator_required
        return "blocked", None, "산출물도, 코드 변경도 없음", operator_required

    return "pass", None, None, operator_required


def _next_action(overall: str, blocking: str | None) -> str:
    if overall == "pass":
        return "모든 에이전트 산출물이 기준 통과 + 실제 제품 변경 발생 → 사이클 succeeded"
    if overall == "retry_required" and blocking:
        return f"`{blocking}` 에이전트에게 retry_prompt 전달 후 재실행"
    if overall == "planning_only":
        return "산출물만 있고 코드 변경 없음 — Continuous OFF + FE/Implementation Ticket 재요청"
    if overall == "blocked":
        return "산출물도 코드 변경도 없음 — 운영자 확인 필요"
    return "운영자 확인 필요"


def _write_report_md(report: dict) -> None:
    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Agent Accountability Report",
        "",
        f"- 사이클: #{report.get('cycle_id') or '—'}",
        f"- 종합 상태: **{report.get('overall_status')}**",
        f"- blocking_agent: {report.get('blocking_agent') or '—'}",
        f"- blocking_reason: {report.get('blocking_reason') or '—'}",
        f"- meaningful_change: {report.get('meaningful_change')}",
        f"- implementation_ticket_exists: {report.get('implementation_ticket_exists')}",
        f"- changed_files_count: {len(report.get('changed_files') or [])}",
        f"- next_action: {report.get('next_action') or '—'}",
        "",
        "## 에이전트별 평가",
        "",
    ]
    for name, row in (report.get("agents") or {}).items():
        lines.append(f"### {name}")
        lines.append(f"- status: {row.get('status')} (score={row.get('score')})")
        if row.get("problems"):
            lines.append("- problems:")
            for p in row["problems"]:
                lines.append(f"  - {p}")
        if row.get("evidence"):
            lines.append("- evidence:")
            for e in row["evidence"]:
                lines.append(f"  - {e}")
        if row.get("retry_prompt"):
            lines.append(f"- retry_prompt: {row['retry_prompt']}")
        lines.append("")

    if report.get("affected_screens"):
        lines.append(f"## 영향 화면\n- {', '.join(report['affected_screens'])}\n")
    if report.get("affected_flows"):
        lines.append(f"## 영향 플로우\n- {', '.join(report['affected_flows'])}\n")
    if report.get("qa_scenarios"):
        lines.append(f"## QA 시나리오\n- {', '.join(report['qa_scenarios'])}\n")

    try:
        (runtime / "agent_accountability_report.md").write_text(
            "\n".join(lines), encoding="utf-8",
        )
    except OSError:
        pass


def run_supervisor() -> dict:
    """Read on-disk state, evaluate every agent, persist
    agent_accountability.json + .md, and return the report dict."""
    runtime = _runtime_dir()
    state = _read_json(runtime / "factory_state.json") or {}
    publish_state = _read_json(runtime / "factory_publish.json") or {}

    agents = {
        "planner":   evaluate_planner(state),
        "designer":  evaluate_designer(state),
        "pm":        evaluate_pm(state),
        "frontend":  evaluate_frontend(state),
        "backend":   evaluate_backend(state),
        "ai":        evaluate_ai(state),
        "qa":        evaluate_qa(state),
        "deploy":    evaluate_deploy(state, publish_state),
    }
    mc = evaluate_meaningful_change(state)
    overall, blocking, reason, op_req = _decide_overall(agents, mc, state)

    ticket_path = runtime / "implementation_ticket.md"
    qa_scenarios: list[str] = []
    if ticket_path.is_file():
        body = _read_text(ticket_path)
        m = re.search(r"## QA 시나리오\s*\n(.+?)(?:\n##|\Z)", body, re.DOTALL)
        if m:
            qa_scenarios = [
                line.lstrip("-* ").strip()
                for line in m.group(1).strip().splitlines()
                if line.strip()
            ][:8]

    report = {
        "cycle_id": state.get("cycle"),
        "evaluated_at": _utc_now_iso(),
        "overall_status": overall,
        "blocking_agent": blocking,
        "blocking_reason": reason,
        "operator_required": op_req,
        "agents": agents,
        "implementation_ticket_exists": (
            ticket_path.is_file()
            and (state.get("implementation_ticket_status") == "generated")
        ),
        "meaningful_change": mc["meaningful_change"],
        "changed_files": mc["changed_files"],
        "affected_screens": mc["affected_screens"],
        "affected_flows": mc["affected_flows"],
        "qa_scenarios": qa_scenarios,
        "commit_hash": (publish_state or {}).get("last_commit_hash"),
        "push_status": (publish_state or {}).get("last_push_status"),
        "next_action": _next_action(overall, blocking),
    }

    runtime.mkdir(parents=True, exist_ok=True)
    try:
        (runtime / "agent_accountability.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError):
        pass
    _write_report_md(report)
    return report


def read_report() -> dict:
    """Convenience reader for runner.py heartbeat builders."""
    runtime = _runtime_dir()
    obj = _read_json(runtime / "agent_accountability.json")
    return obj or {}
