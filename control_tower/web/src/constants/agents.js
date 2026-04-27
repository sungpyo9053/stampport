export const OFFICE_WIDTH = 760;
export const OFFICE_HEIGHT = 620;

// Pixel-office stage. Bigger than the legacy AgentOffice diorama because
// the new spatial layout puts every desk in view at once.
export const PIXEL_OFFICE_WIDTH = 1080;
export const PIXEL_OFFICE_HEIGHT = 760;

// Desk grid for the pixel office. 3 columns × 3 rows; the bottom-right
// cell is left empty so the Stampport HQ board can live there.
//   Row 1 (Planner Lab):  PM | Planner | Designer  (planner ↔ designer ping-pong)
//   Row 2 (Build Floor):  Frontend | Backend | AI Architect
//   Row 3 (Ship Floor):   QA | Deploy | (HQ board)
export const DESK_LAYOUT = {
  pm:           { x: 200, y: 240, facing: "right" },
  planner:      { x: 540, y: 240, facing: "right" },
  designer:     { x: 880, y: 240, facing: "left"  },
  frontend:     { x: 200, y: 460, facing: "right" },
  backend:      { x: 540, y: 460, facing: "right" },
  ai_architect: { x: 880, y: 460, facing: "left"  },
  qa:           { x: 200, y: 660, facing: "right" },
  deploy:       { x: 540, y: 660, facing: "right" },
};

// Default ambient lines shown in each agent's speech bubble when no
// live `agent_message` event has arrived for them yet. Keeps the office
// feeling alive even on a fresh page load.
export const DEFAULT_BUBBLES = {
  pm:           "이번 사이클 스코프를 정리 중이에요.",
  planner:      "이번 사이클 후보 3개를 뽑고 있어요.",
  designer:     "이 배지가 진짜 갖고 싶어 보이는지 검토 중이에요.",
  frontend:     "스탬프 획득 카드 UI를 구현 중이에요.",
  backend:      "stamp/badge 스키마를 다듬는 중이에요.",
  ai_architect: "킥 포인트 추천 룰을 설계 중이에요.",
  qa:           "수집욕/과시욕/성장욕 게이트를 검증 중이에요.",
  deploy:       "다음 빌드를 기다리고 있어요.",
};

// Stampport Lab agent roster.
//
// `look` describes how to draw the small SD human:
//   skin / hair / hairStyle / shirt / pants / accessory
// `artifactType` is the small prop the agent produces and hands off.
//
// Layout intent: planner (기획자) and designer (디자이너) sit in the
// top row together because their ping-pong drives the entire factory.
// The user-facing labels live with the design system in:
//   docs/agent-collaboration.md
//   config/domain_profiles/stampport.json
export const AGENTS = {
  pm: {
    id: "pm",
    name: "PM",
    fullName: "PM 에이전트",
    role: "제품 비전과 합의 조율",
    emoji: "🧑‍💼",
    color: "#38bdf8",
    x: 120,
    y: 130,
    artifactType: "brief",
    artifactLabel: "제품 브리프",
    look: {
      skin: "#f5d0b9",
      hair: "#1f2937",
      hairStyle: "side",
      shirt: "#1d4ed8",
      pants: "#0f172a",
      accessory: "tie",
    },
  },
  planner: {
    id: "planner",
    name: "기획자",
    fullName: "기획자 에이전트",
    role: "스탬프/뱃지/칭호/퀘스트/공유카드 신규 장치 제안",
    emoji: "📝",
    color: "#a78bfa",
    x: 360,
    y: 130,
    artifactType: "schedule",
    artifactLabel: "신규 장치 후보",
    look: {
      skin: "#f0c8a8",
      hair: "#7c2d12",
      hairStyle: "ponytail",
      shirt: "#7c3aed",
      pants: "#1e293b",
      accessory: "pen",
    },
  },
  designer: {
    id: "designer",
    name: "디자이너",
    fullName: "디자이너 에이전트",
    role: "도장/뱃지/카드가 갖고 싶고 자랑하고 싶은지 반박과 개선",
    emoji: "🎨",
    color: "#fb7185",
    x: 600,
    y: 130,
    artifactType: "wireframe",
    artifactLabel: "감성 와이어프레임",
    look: {
      skin: "#f5d0b9",
      hair: "#92400e",
      hairStyle: "long",
      shirt: "#fb7185",
      pants: "#1f2937",
      accessory: "beret",
    },
  },
  frontend: {
    id: "frontend",
    name: "프론트엔드",
    fullName: "프론트엔드 에이전트",
    role: "React 화면과 모바일 우선 UI 구현",
    emoji: "💻",
    color: "#facc15",
    x: 120,
    y: 320,
    artifactType: "uimock",
    artifactLabel: "프론트 골격",
    look: {
      skin: "#f1c79a",
      hair: "#facc15",
      hairStyle: "curly",
      shirt: "#ca8a04",
      pants: "#1e293b",
      accessory: "headphones",
    },
  },
  backend: {
    id: "backend",
    name: "백엔드",
    fullName: "백엔드 에이전트",
    role: "FastAPI/DB/스탬프 데이터 모델",
    emoji: "🖥️",
    color: "#34d399",
    x: 360,
    y: 320,
    artifactType: "apispec",
    artifactLabel: "API 명세",
    look: {
      skin: "#deb088",
      hair: "#1f2937",
      hairStyle: "short",
      shirt: "#047857",
      pants: "#1e293b",
      accessory: "glasses",
    },
  },
  ai_architect: {
    id: "ai_architect",
    name: "AI 설계자",
    fullName: "AI 설계자 에이전트",
    role: "킥 포인트/추천/취향 클러스터링 LLM 설계",
    emoji: "🧠",
    color: "#f472b6",
    x: 600,
    y: 320,
    artifactType: "diagram",
    artifactLabel: "AI 설계서",
    look: {
      skin: "#e8b48c",
      hair: "#0f172a",
      hairStyle: "beanie",
      shirt: "#be185d",
      pants: "#1e293b",
      accessory: "beanie",
    },
  },
  qa: {
    id: "qa",
    name: "QA",
    fullName: "QA 에이전트",
    role: "기능 + 수집/과시/성장/재방문 욕구 검증",
    emoji: "🔎",
    color: "#fb923c",
    x: 360,
    y: 500,
    artifactType: "checklist",
    artifactLabel: "QA 리포트",
    look: {
      skin: "#f0c8a8",
      hair: "#0f172a",
      hairStyle: "bob",
      shirt: "#ea580c",
      pants: "#0f172a",
      accessory: "glasses",
    },
  },
  deploy: {
    id: "deploy",
    name: "배포",
    fullName: "배포 관리자 에이전트",
    role: "빌드/헬스체크/배포 후 모니터링",
    emoji: "🚀",
    color: "#22d3ee",
    x: 600,
    y: 500,
    artifactType: "deploy_log",
    artifactLabel: "배포 결과",
    look: {
      skin: "#e8b48c",
      hair: "#fbbf24",
      hairStyle: "side",
      shirt: "#0891b2",
      pants: "#1e293b",
      accessory: "megaphone",
    },
  },
};

export const AGENT_LIST = Object.values(AGENTS);

export const STATUS_META = {
  idle: { label: "대기", color: "bg-slate-600", text: "text-slate-300", ring: "ring-slate-600" },
  working: { label: "작업 중", color: "bg-amber-500", text: "text-amber-300", ring: "ring-amber-400" },
  done: { label: "완료", color: "bg-emerald-500", text: "text-emerald-300", ring: "ring-emerald-400" },
  waiting_approval: { label: "승인 대기", color: "bg-purple-500", text: "text-purple-300", ring: "ring-purple-400" },
  blocked: { label: "막힘", color: "bg-rose-500", text: "text-rose-300", ring: "ring-rose-400" },
  error: { label: "오류", color: "bg-rose-600", text: "text-rose-300", ring: "ring-rose-500" },
};

// Korean-localized labels for backend task statuses (TaskBoard).
export const TASK_STATUS_LABEL = {
  pending: "대기",
  in_progress: "진행 중",
  completed: "완료",
  failed: "실패",
  cancelled: "취소",
};

// Korean-localized labels for backend artifact types (ArtifactPanel).
export const ARTIFACT_TYPE_LABEL = {
  product_brief: "제품 브리프",
  planner_proposal: "신규 장치 후보",
  designer_critique: "감성 비판/개선",
  wireframe: "감성 와이어프레임",
  api_spec: "API 명세",
  frontend_code: "프론트 골격",
  agent_design: "AI 설계서",
  test_cases: "QA 리포트",
  deploy_log: "배포 결과",
};

export function getAgent(agentId) {
  return AGENTS[agentId] || null;
}
