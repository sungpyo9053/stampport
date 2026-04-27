"""Sequential workflow orchestrator for the Stampport Lab MVP demo.

Runs the eight Stampport agents in pipeline order:

    PM → 기획자 → 디자이너 → 프론트엔드 → 백엔드 → AI 설계자 → QA → 배포

Each step runs through `agent_runner.run_agent`, and between steps the
orchestrator emits a `handoff` event so the dashboard can animate the baton
pass.

The planner-designer pair is treated as the conceptual core: planner
proposes new collection/reward/quest devices, designer challenges whether
those rewards are visually desirable, collectible, and shareable. Other
agents only implement what these two have aligned on.

NOTE: All user-facing strings are intentionally Korean — this is the
Stampport Korean demo build.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from .agent_runner import AgentScript, run_agent
from .event_bus import event_bus
from .models import AgentRow
from .schemas import AgentStatus, EventType
from .services.demo_service import reset_demo_state


HANDOFF_DELAY_SECONDS = 0.9


# ---------------------------------------------------------------------------
# Agent registry — used by the orchestrator AND the seed step in main.py.
# `name` and `role` are what the dashboard surfaces, so they're Korean.
# Internal `id` stays English so existing API contracts don't break.
# ---------------------------------------------------------------------------

AGENT_REGISTRY: list[dict[str, str]] = [
    {"id": "pm", "name": "PM", "role": "제품 비전과 합의 조율"},
    {"id": "planner", "name": "기획자", "role": "스탬프/뱃지/칭호/퀘스트/공유카드 신규 장치 제안"},
    {"id": "designer", "name": "디자이너", "role": "도장/뱃지/카드가 갖고 싶고 자랑하고 싶은지 반박과 개선"},
    {"id": "frontend", "name": "프론트엔드", "role": "React 화면과 모바일 우선 UI 구현"},
    {"id": "backend", "name": "백엔드", "role": "FastAPI/DB/스탬프 데이터 모델"},
    {"id": "ai_architect", "name": "AI 설계자", "role": "킥 포인트/추천/취향 클러스터링 LLM 설계"},
    {"id": "qa", "name": "QA", "role": "기능 + 수집/과시/성장/재방문 욕구 검증"},
    {"id": "deploy", "name": "배포", "role": "빌드/헬스체크/배포 후 모니터링"},
]


# Display-name lookup so handoff events can read like
# "QA가 배포에게 산출물을 전달했습니다." instead of "qa → deploy".
AGENT_DISPLAY_NAME: dict[str, str] = {
    spec["id"]: spec["name"] for spec in AGENT_REGISTRY
}


def seed_agents(db: Session) -> None:
    """Insert any missing agents in the registry, and refresh display names.

    Idempotent. Also patches existing rows so a DB seeded with a previous
    roster (e.g. an old `marketing` agent from a copied template) gets
    cleanly replaced with the Stampport Lab `deploy` agent on next boot.
    """
    legacy_ids_to_remove = {"marketing"}
    for spec in AGENT_REGISTRY:
        existing = db.get(AgentRow, spec["id"])
        if existing is None:
            db.add(
                AgentRow(
                    id=spec["id"],
                    name=spec["name"],
                    role=spec["role"],
                    status=AgentStatus.IDLE.value,
                )
            )
        else:
            existing.name = spec["name"]
            existing.role = spec["role"]
            db.add(existing)

    # Drop any legacy agents that no longer belong to the Stampport roster.
    for legacy_id in legacy_ids_to_remove:
        legacy = db.get(AgentRow, legacy_id)
        if legacy is not None:
            db.delete(legacy)

    db.commit()


# ---------------------------------------------------------------------------
# Demo workflow scripts (Korean) — Stampport Lab pipeline.
# ---------------------------------------------------------------------------

DEMO_WORKFLOW: list[AgentScript] = [
    AgentScript(
        agent_id="pm",
        task_title="스탬포트 한 사이클 합의 정리",
        task_description="기획자/디자이너 ping-pong을 작은 출하 단위 1개로 자릅니다.",
        messages=[
            "이번 사이클에 출하할 가장 작은 한 단계를 찾고 있어요.",
            "수집/과시/성장 루프 중 무엇을 한 칸 더 강하게 만들지 정하고 있어요.",
            "기획자–디자이너가 합의한 단 한 개의 장치만 통과시키고 있어요.",
        ],
        artifact_type="product_brief",
        artifact_title="스탬포트 사이클 브리프 v0.1",
        artifact_content=(
            "이번 사이클은 도장 한 번이 더 모으고 싶고 자랑하고 싶게 보이도록 "
            "하는 가장 작은 장치 하나만 출하한다."
        ),
    ),
    AgentScript(
        agent_id="planner",
        task_title="신규 보상/장치 후보 3개 제안",
        task_description="스탬프·EXP·뱃지·칭호·여권·퀘스트·공유카드 라인업에 새 장치를 제안합니다.",
        messages=[
            "사용자 동기를 한 단계 더 끌어올릴 보상 후보 3가지를 정리하고 있어요.",
            "각 후보마다 수집욕/과시욕/성장욕 자극 포인트를 서로 다르게 설계하고 있어요.",
            "디자이너에게 ‘진짜 갖고 싶은가’ 질문할 준비를 하고 있어요.",
        ],
        artifact_type="planner_proposal",
        artifact_title="신규 장치 후보 v0.1",
        artifact_content=(
            "후보1 Local Visa 뱃지, 후보2 Taste Title 진화, 후보3 Passport 빈 슬롯. "
            "각 후보의 동기 자극 포인트와 최소 출하 범위를 함께 제시한다."
        ),
    ),
    AgentScript(
        agent_id="designer",
        task_title="기획 후보 감성 검증과 개선",
        task_description="각 후보가 실제로 갖고 싶고 자랑하고 싶은지 반박합니다.",
        messages=[
            "이 뱃지가 인스타 스토리에 자랑하고 싶게 보이는지 점검하고 있어요.",
            "여권 빈 슬롯이 다음 방문 욕구를 만드는지 검증하고 있어요.",
            "최종 1개 후보를 갖고 싶은 형태로 다듬어 PM에게 넘기고 있어요.",
        ],
        artifact_type="wireframe",
        artifact_title="감성 와이어프레임 v0.1",
        artifact_content=(
            "선정 장치를 도장/뱃지/공유카드 톤으로 재해석한 와이어프레임. "
            "deep green / cream / gold / burgundy / navy 팔레트 사용."
        ),
    ),
    AgentScript(
        agent_id="frontend",
        task_title="합의된 단위 UI 구현",
        task_description="모바일 우선 React 화면에 합의된 한 가지 장치만 추가합니다.",
        messages=[
            "기존 화면을 깨지 않게 작은 컴포넌트 단위로 추가하고 있어요.",
            "390px 폭에서 깨지지 않는 카드/버튼 스타일을 확인하고 있어요.",
        ],
        artifact_type="frontend_code",
        artifact_title="프론트 골격 추가분",
        artifact_content="My Passport / Stamp Result / Share Card에 새 장치를 작은 단위로 끼워 넣는다.",
    ),
    AgentScript(
        agent_id="backend",
        task_title="스탬프/뱃지 모델 보강",
        task_description="필요 시 FastAPI 엔드포인트와 데이터 모델을 확장합니다.",
        messages=[
            "스탬프/뱃지/퀘스트 모델에 새 필드를 추가해도 되는지 검토하고 있어요.",
            "기존 응답 필드를 깨지 않는 호환 변경만 통과시키고 있어요.",
        ],
        artifact_type="api_spec",
        artifact_title="스탬프 API 확장 v0.1",
        artifact_content=(
            "POST /stamps 응답에 신규 보상 메타데이터를 옵션 필드로 추가. "
            "기존 필드는 모두 보존."
        ),
    ),
    AgentScript(
        agent_id="ai_architect",
        task_title="킥 포인트/추천 LLM 설계",
        task_description="다음 방문을 끌어내는 킥 포인트 3개 생성 흐름을 설계합니다.",
        messages=[
            "카테고리·태그·메뉴를 입력으로 받는 킥 포인트 프롬프트 초안을 정리하고 있어요.",
            "LLM 실패 시 룰 기반 fallback을 명시하고 있어요.",
        ],
        artifact_type="agent_design",
        artifact_title="킥 포인트 AI 설계 v0.1",
        artifact_content=(
            "Planner LLM은 다음 방문 후보를 생성하고, RuleEngine은 LLM 응답이 비었을 때 "
            "카테고리/태그 기반 기본 킥 포인트 3개를 보장한다."
        ),
    ),
    AgentScript(
        agent_id="qa",
        task_title="기능 + 감정 루프 검증",
        task_description="기능 동작뿐 아니라 수집/과시/성장/재방문 욕구를 함께 점검합니다.",
        messages=[
            "build / py_compile / 화면 존재 / 핵심 흐름 코드 존재를 점검하고 있어요.",
            "도장이 진짜 갖고 싶고 카드가 자랑하고 싶은지 감정 루프 체크리스트를 돌리고 있어요.",
        ],
        artifact_type="test_cases",
        artifact_title="QA 리포트 v0.1",
        artifact_content=(
            "기능 게이트 6종 + 감정 게이트 5종을 모두 통과해야 ‘배포 가능’으로 표시한다."
        ),
    ),
    AgentScript(
        agent_id="deploy",
        task_title="빌드/헬스체크/배포",
        task_description="빌드 산출물 검증과 /health 엔드포인트 확인 후 배포합니다.",
        messages=[
            "app/web 빌드와 control_tower/web 빌드 결과를 확인하고 있어요.",
            "FastAPI /health 응답을 확인하고 있어요.",
        ],
        artifact_type="deploy_log",
        artifact_title="배포 결과 v0.1",
        artifact_content=(
            "빌드 통과 + /health 200 + Stampport 화면 존재 + 핵심 흐름 코드 존재 → 배포 OK."
        ),
    ),
]


WORKFLOW_NAME = "stampport_lab_mvp_build"


def run_demo_workflow(db: Session) -> int:
    """Run the full demo workflow synchronously. Returns the number of tasks completed.

    Honors factory pause/stop between stages by calling
    `factory_service.checkpoint(...)` before each agent runs and again
    before the deploy stage at the end. Returns early (without raising)
    if the factory is asked to stop mid-flight.
    """
    from .services import factory_service
    from .services.deploy_service import run_deploy_stage

    reset_demo_state(db)

    completed = 0
    previous_agent_id: str | None = None

    for script in DEMO_WORKFLOW:
        if not factory_service.checkpoint(db, script.agent_id):
            return completed

        if previous_agent_id is not None:
            from_name = AGENT_DISPLAY_NAME.get(previous_agent_id, previous_agent_id)
            to_name = AGENT_DISPLAY_NAME.get(script.agent_id, script.agent_id)
            event_bus.emit(
                db,
                type=EventType.HANDOFF,
                message=f"{from_name}가 {to_name}에게 산출물을 전달했습니다.",
                agent_id=script.agent_id,
                payload={
                    "from_agent": previous_agent_id,
                    "to_agent": script.agent_id,
                    "from_name": from_name,
                    "to_name": to_name,
                    "workflow": WORKFLOW_NAME,
                },
            )
            # give the FE time to play the courier animation between agents
            time.sleep(HANDOFF_DELAY_SECONDS)

        run_agent(db, script)
        completed += 1
        previous_agent_id = script.agent_id

    # Deploy verification stage at the very end (simulation by default,
    # real if env says so). The deploy AGENT script above already ran;
    # this performs the actual health checks against the live URLs.
    if factory_service.checkpoint(db, "deploy"):
        run_deploy_stage(db)

    return completed
