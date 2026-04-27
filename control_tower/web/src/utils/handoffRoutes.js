// Handoff route definitions — the single source of truth for "who
// hands what to whom" inside the Pixel Office.
//
// Three sources fan into the same shape:
//   STAGE_HANDOFFS    — factory.current_stage transitions
//   PINGPONG_HANDOFFS — planner ↔ designer 5-step protocol
//   DEMO_FLOW         — fallback loop when nothing live is happening,
//                       and the route the ?handoffDemo=1 mode forces
//
// AgentCourierLayer reads these and pushes one handoff to its queue
// at a time so the office never plays multiple courier walks at once.

import { AGENTS } from "../constants/agents.js";

// factory.current_stage transitions → handoff. Keys are
// "<from_stage>>" + "<to_stage>" so a flip from "planner" → "designer"
// resolves a single lookup.
export const STAGE_HANDOFFS = {
  "pm>planner":           { from: "pm",          to: "planner",      label: "기획 요청",   artifactType: "brief" },
  "planner>designer":     { from: "planner",     to: "designer",     label: "기획안",       artifactType: "schedule" },
  "designer>frontend":    { from: "designer",    to: "frontend",     label: "FE 작업",      artifactType: "wireframe" },
  "frontend>backend":     { from: "frontend",    to: "backend",      label: "API 연동",     artifactType: "uimock" },
  "backend>ai_architect": { from: "backend",     to: "ai_architect", label: "데이터/룰",    artifactType: "apispec" },
  "ai_architect>qa":      { from: "ai_architect",to: "qa",           label: "QA 체크",      artifactType: "diagram" },
  "qa>deploy":            { from: "qa",          to: "deploy",       label: "배포 패키지",  artifactType: "checklist" },
};

// Ping-pong steps → handoff. Triggered the first time the runner
// heartbeat reports a step's status flipping to "generated".
export const PINGPONG_HANDOFFS = {
  planner_proposal:       { from: "planner",  to: "designer", label: "기획안",        artifactType: "schedule" },
  designer_critique:      { from: "designer", to: "planner",  label: "디자인 반박",   artifactType: "wireframe", bubble: "이건 갖고 싶어 보이지 않아요" },
  planner_revision:       { from: "planner",  to: "designer", label: "기획자 수정안", artifactType: "schedule" },
  designer_final_review:  { from: "designer", to: "pm",       label: "최종 평가",     artifactType: "wireframe" },
  pm_decision:            { from: "pm",       to: "frontend", label: "PM 결정",       artifactType: "brief" },
};

export const PINGPONG_ORDER = [
  "planner_proposal",
  "designer_critique",
  "planner_revision",
  "designer_final_review",
  "pm_decision",
];

// Fallback / forced demo. Hits 9 routes covering every required edge:
// PM → 기획자, 기획자 → 디자이너, 디자이너 → 기획자, 기획자 → PM,
// PM → FE, PM → BE, PM → AI, FE/BE/AI → QA, QA → 배포.
export const DEMO_FLOW = [
  { from: "pm",           to: "planner",      label: "기획 요청",      artifactType: "brief",      banner: "PM이 기획자에게 새 기능 요청을 전달 중" },
  { from: "planner",      to: "designer",     label: "기획안",         artifactType: "schedule",   banner: "기획자가 디자이너에게 기획안을 전달 중" },
  { from: "designer",     to: "planner",      label: "디자인 반박",    artifactType: "wireframe",  bubble: "이건 갖고 싶어 보이지 않아요", banner: "디자이너가 기획자에게 반박 노트를 돌려보내는 중" },
  { from: "planner",      to: "pm",           label: "합의안",         artifactType: "schedule",   banner: "기획자가 PM에게 합의안을 전달 중" },
  { from: "pm",           to: "frontend",     label: "FE 작업",        artifactType: "uimock",     banner: "PM이 프론트엔드에 작업 티켓을 분배 중" },
  { from: "pm",           to: "backend",      label: "API 작업",       artifactType: "apispec",    banner: "PM이 백엔드에 API 티켓을 분배 중" },
  { from: "pm",           to: "ai_architect", label: "AI 룰",          artifactType: "diagram",    banner: "PM이 AI 설계자에게 룰 티켓을 전달 중" },
  { from: "ai_architect", to: "qa",           label: "QA 체크",        artifactType: "checklist",  banner: "AI 설계자가 QA에게 체크리스트를 전달 중" },
  { from: "qa",           to: "deploy",       label: "배포 패키지",    artifactType: "brief",      banner: "QA가 배포 담당에게 검증 결과를 전달 중" },
];

export const DEMO_INTERVAL_MS = 9500;

// Banner text — uses the handoff's pre-set banner when present, or
// falls back to "<fromName>가 <toName>에게 <label>를 전달 중".
export function bannerFor(handoff) {
  if (!handoff) return null;
  if (handoff.banner) return handoff.banner;
  const fromAgent = AGENTS[handoff.from];
  const toAgent = AGENTS[handoff.to];
  if (!fromAgent || !toAgent) return null;
  return `${fromAgent.name}가 ${toAgent.name}에게 ${handoff.label}를 전달 중`;
}

// Pick the most-online runner that's actually advertising ping-pong.
// Online > busy > offline; returns the ping_pong block or null.
export function pickPingPongMeta(runners = []) {
  const ranked = [...runners].sort((a, b) => {
    const order = (s) => (s === "online" ? 0 : s === "busy" ? 1 : 2);
    return order(a?.status) - order(b?.status);
  });
  for (const r of ranked) {
    const pp = r?.metadata_json?.local_factory?.ping_pong;
    if (pp && pp.enabled) return pp;
  }
  return null;
}
