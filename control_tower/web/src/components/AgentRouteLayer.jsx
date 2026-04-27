import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import MovingTaskCard from "./MovingTaskCard.jsx";
import { AGENTS, DESK_LAYOUT } from "../constants/agents.js";

// Drives the "agents physically hand work to each other" layer that sits
// on top of the desks inside PixelOffice. Each handoff is rendered as a
// MovingTaskCard sliding from sender desk → receiver desk in stage
// coordinates. One card animates at a time so the office doesn't get
// busy.
//
// Sources, in priority order:
//   1. Live ping-pong block on a runner heartbeat (planner ↔ designer
//      five-step protocol). Newly-generated steps push a card.
//   2. factory.current_stage transitions from the backend pipeline.
//   3. Demo flow that loops on a slow cadence whenever the factory is
//      idle and no ping-pong is live, so the office never feels static.
//
// The current banner ("기획자가 디자이너에게 ... 전달 중") and a
// "DEMO FLOW" flag are reported up to PixelOffice via callbacks so the
// banner sits *outside* the scaled stage (it's text and would smear at
// the office scale otherwise).

// Stage-pipeline handoffs (factory.current_stage transitions).
const STAGE_HANDOFFS = {
  "pm>planner":           { from: "pm",          to: "planner",      label: "기획 요청",   artifactType: "brief" },
  "planner>designer":     { from: "planner",     to: "designer",     label: "기획안",       artifactType: "schedule" },
  "designer>frontend":    { from: "designer",    to: "frontend",     label: "FE 작업",      artifactType: "wireframe" },
  "frontend>backend":     { from: "frontend",    to: "backend",      label: "API 연동",     artifactType: "uimock" },
  "backend>ai_architect": { from: "backend",     to: "ai_architect", label: "데이터/룰",    artifactType: "apispec" },
  "ai_architect>qa":      { from: "ai_architect",to: "qa",           label: "QA 체크",      artifactType: "diagram" },
  "qa>deploy":            { from: "qa",          to: "deploy",       label: "배포 패키지",  artifactType: "checklist" },
};

// Ping-pong → handoff. We trigger a card the first time we see a step
// flip from "not generated" to "generated" in the runner heartbeat.
const PINGPONG_HANDOFFS = {
  planner_proposal:       { from: "planner",  to: "designer", label: "기획안",        artifactType: "schedule" },
  designer_critique:      { from: "designer", to: "planner",  label: "디자인 반박",   artifactType: "wireframe", bubble: "이건 갖고 싶어 보이지 않아요" },
  planner_revision:       { from: "planner",  to: "designer", label: "기획자 수정안", artifactType: "schedule" },
  designer_final_review:  { from: "designer", to: "pm",       label: "최종 평가",     artifactType: "wireframe" },
  pm_decision:            { from: "pm",       to: "frontend", label: "PM 결정",       artifactType: "brief" },
};

// Demo flow — runs whenever the factory is idle so the office has visible
// motion even on a fresh page load. Cycles forever; one step every
// DEMO_INTERVAL_MS.
const DEMO_FLOW = [
  { from: "planner",  to: "designer",     label: "기획안",        artifactType: "schedule",  banner: "기획자가 디자이너에게 새 기능 후보를 전달 중" },
  { from: "designer", to: "planner",      label: "디자인 반박",    artifactType: "wireframe", bubble: "이건 갖고 싶어 보이지 않아요", banner: "디자이너가 기획자에게 반박 노트를 돌려보내는 중" },
  { from: "planner",  to: "pm",           label: "합의안",         artifactType: "schedule",  banner: "기획자가 PM에게 합의안을 전달 중" },
  { from: "pm",       to: "frontend",     label: "FE 작업",        artifactType: "uimock",    banner: "PM이 프론트엔드에 작업 티켓을 분배 중" },
  { from: "pm",       to: "backend",      label: "API 작업",       artifactType: "apispec",   banner: "PM이 백엔드에 API 티켓을 분배 중" },
  { from: "pm",       to: "ai_architect", label: "AI 룰",          artifactType: "diagram",   banner: "PM이 AI 설계자에게 룰 티켓을 전달 중" },
  { from: "frontend", to: "qa",           label: "QA 체크",        artifactType: "checklist", banner: "프론트엔드가 QA에게 체크리스트를 전달 중" },
  { from: "backend",  to: "qa",           label: "QA 체크",        artifactType: "checklist", banner: "백엔드가 QA에게 체크리스트를 전달 중" },
  { from: "qa",       to: "deploy",       label: "배포 패키지",     artifactType: "brief",     banner: "QA가 배포 담당에게 배포 패키지를 전달 중" },
];

const DEMO_INTERVAL_MS = 9500;
const PINGPONG_ORDER = [
  "planner_proposal",
  "designer_critique",
  "planner_revision",
  "designer_final_review",
  "pm_decision",
];

function pickPingPongMeta(runners = []) {
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

function bannerFor(handoff) {
  if (handoff.banner) return handoff.banner;
  const fromAgent = AGENTS[handoff.from];
  const toAgent = AGENTS[handoff.to];
  if (!fromAgent || !toAgent) return null;
  return `${fromAgent.name}가 ${toAgent.name}에게 ${handoff.label}를 전달 중`;
}

export default function AgentRouteLayer({
  factory,
  runners = [],
  onBannerChange,
  onDemoChange,
  isMobile = false,
}) {
  const [activeCard, setActiveCard] = useState(null);
  const queueRef = useRef([]);
  const lastStageRef = useRef(null);
  const seenPingPongRef = useRef(new Set());
  const demoIndexRef = useRef(0);
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const handler = (e) => setReducedMotion(e.matches);
    if (mq.addEventListener) mq.addEventListener("change", handler);
    else mq.addListener(handler);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener("change", handler);
      else mq.removeListener(handler);
    };
  }, []);

  const enqueue = useCallback((handoff, source) => {
    queueRef.current.push({
      ...handoff,
      source,
      key: `${source}-${handoff.from}-${handoff.to}-${Date.now()}-${Math.random()
        .toString(36)
        .slice(2, 6)}`,
    });
  }, []);

  // Drain queue: whenever no card is active and queue is non-empty,
  // promote the head of the queue to the active card.
  useEffect(() => {
    if (activeCard) return;
    if (queueRef.current.length === 0) return;
    const next = queueRef.current.shift();
    setActiveCard(next);
    onBannerChange?.(bannerFor(next));
    onDemoChange?.(next.source === "demo");
  }, [activeCard, onBannerChange, onDemoChange]);

  // Watch factory.current_stage for forward transitions.
  const currentStage = factory?.current_stage || null;
  const factoryStatus = factory?.status || "idle";
  useEffect(() => {
    if (!currentStage) {
      lastStageRef.current = null;
      return;
    }
    const prev = lastStageRef.current;
    lastStageRef.current = currentStage;
    if (!prev || prev === currentStage) return;
    const handoff = STAGE_HANDOFFS[`${prev}>${currentStage}`];
    if (handoff) enqueue(handoff, "live");
  }, [currentStage, enqueue]);

  // Watch ping-pong heartbeat for newly-generated steps.
  const pingPongMeta = pickPingPongMeta(runners);
  const pingPongKey = useMemo(() => {
    if (!pingPongMeta) return "";
    return PINGPONG_ORDER.map(
      (k) =>
        `${k}:${pingPongMeta[`${k}_status`] || (pingPongMeta[`${k}_exists`] ? "generated" : "")}`,
    ).join("|");
  }, [pingPongMeta]);

  useEffect(() => {
    if (!pingPongMeta) return;
    for (const step of PINGPONG_ORDER) {
      const generated =
        pingPongMeta[`${step}_status`] === "generated" ||
        (step === "planner_proposal" && pingPongMeta.planner_proposal_exists);
      if (generated && !seenPingPongRef.current.has(step)) {
        seenPingPongRef.current.add(step);
        const handoff = PINGPONG_HANDOFFS[step];
        if (handoff) enqueue(handoff, "live");
      }
    }
  }, [pingPongKey, pingPongMeta, enqueue]);

  // Reset ping-pong tracking when a new cycle restarts (all stages flip
  // back to empty / running).
  useEffect(() => {
    if (!pingPongMeta) {
      seenPingPongRef.current.clear();
      return;
    }
    const allEmpty = PINGPONG_ORDER.every((k) => {
      const status = pingPongMeta[`${k}_status`];
      return !status || status === "running";
    });
    if (allEmpty) seenPingPongRef.current.clear();
  }, [pingPongKey, pingPongMeta]);

  // Demo loop — only when nothing live is happening.
  const isLive =
    factoryStatus === "running" ||
    !!pingPongMeta ||
    queueRef.current.length > 0;

  useEffect(() => {
    if (isLive) return;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      const step = DEMO_FLOW[demoIndexRef.current % DEMO_FLOW.length];
      demoIndexRef.current += 1;
      enqueue(step, "demo");
    };
    // kick off after a short delay so the office settles
    const initial = setTimeout(tick, 1200);
    const interval = setInterval(tick, DEMO_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearTimeout(initial);
      clearInterval(interval);
    };
  }, [isLive, enqueue]);

  // Card finished — clear active so the queue can advance.
  const handleDone = useCallback(() => {
    setActiveCard(null);
    onBannerChange?.(null);
  }, [onBannerChange]);

  // Mobile: skip animations entirely if the user opted into reduced
  // motion AND we're on a narrow viewport. Otherwise we still play, just
  // with the same shorter card.
  const shouldSimplify = reducedMotion;

  if (!activeCard) return null;

  const fromLayout = DESK_LAYOUT[activeCard.from];
  const toLayout = DESK_LAYOUT[activeCard.to];
  if (!fromLayout || !toLayout) return null;

  const fromAgent = AGENTS[activeCard.from];
  // Aim slightly above the desk center (chest height of the receiver)
  // so the card looks like it lands in their hands, not the floor.
  const yOffset = -22;

  return (
    <MovingTaskCard
      key={activeCard.key}
      fromX={fromLayout.x}
      fromY={fromLayout.y + yOffset}
      toX={toLayout.x}
      toY={toLayout.y + yOffset}
      label={activeCard.label}
      artifactType={activeCard.artifactType}
      accent={fromAgent?.color || "#d4a843"}
      bubble={isMobile ? null : activeCard.bubble}
      reducedMotion={shouldSimplify}
      onDone={handleDone}
    />
  );
}
