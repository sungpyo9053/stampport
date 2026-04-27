import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import HandoffCourier from "./HandoffCourier.jsx";
import { AGENTS, DESK_LAYOUT } from "../constants/agents.js";
import {
  STAGE_HANDOFFS,
  PINGPONG_HANDOFFS,
  PINGPONG_ORDER,
  DEMO_FLOW,
  DEMO_INTERVAL_MS,
  bannerFor,
  pickPingPongMeta,
} from "../utils/handoffRoutes.js";

// Drives the visible "agents physically hand work to each other" layer
// inside PixelOffice. One HandoffCourier walks across the office at a
// time, sourced from (in priority order):
//
//   1. live ping-pong heartbeat (planner ↔ designer ↔ pm)
//   2. factory.current_stage transitions
//   3. demo flow — runs when nothing's live, OR when forceDemo=true
//      (set by URL ?handoffDemo=1 so the operator can verify the
//      animation regardless of factory state)
//
// The big visible thing is the courier itself; the small per-desk
// presence pips are intentionally a *secondary* signal handled by
// AgentPresenceLayer.

export default function AgentCourierLayer({
  factory,
  runners = [],
  forceDemo = false,
  isMobile = false,
  onBannerChange,
  onDemoChange,
  onHandoff,
  onArrive,
}) {
  const [activeCard, setActiveCard] = useState(null);
  const queueRef = useRef([]);
  const lastStageRef = useRef(null);
  const seenPPRef = useRef(new Set());
  const demoIdxRef = useRef(0);
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

  // Drain queue: when nothing is in flight, promote the head and
  // notify the page (banner + handoff_started log entry).
  useEffect(() => {
    if (activeCard) return;
    if (queueRef.current.length === 0) return;
    const next = queueRef.current.shift();
    setActiveCard(next);
    onBannerChange?.(bannerFor(next));
    onDemoChange?.(next.source === "demo");
    onHandoff?.({
      kind: "handoff_started",
      from: next.from,
      to: next.to,
      label: next.label,
      artifactType: next.artifactType,
      banner: bannerFor(next),
      source: next.source,
    });
  }, [activeCard, onBannerChange, onDemoChange, onHandoff]);

  // factory.current_stage → handoff
  const currentStage = factory?.current_stage || null;
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

  // Ping-pong heartbeat → handoff
  const pp = pickPingPongMeta(runners);
  const ppKey = useMemo(() => {
    if (!pp) return "";
    return PINGPONG_ORDER.map(
      (k) =>
        `${k}:${pp[`${k}_status`] || (pp[`${k}_exists`] ? "generated" : "")}`,
    ).join("|");
  }, [pp]);
  useEffect(() => {
    if (!pp) return;
    for (const step of PINGPONG_ORDER) {
      const generated =
        pp[`${step}_status`] === "generated" ||
        (step === "planner_proposal" && pp.planner_proposal_exists);
      if (generated && !seenPPRef.current.has(step)) {
        seenPPRef.current.add(step);
        const ho = PINGPONG_HANDOFFS[step];
        if (ho) enqueue(ho, "live");
      }
    }
  }, [ppKey, pp, enqueue]);
  // Reset ping-pong tracking when a new cycle restarts.
  useEffect(() => {
    if (!pp) {
      seenPPRef.current.clear();
      return;
    }
    const allEmpty = PINGPONG_ORDER.every((k) => {
      const status = pp[`${k}_status`];
      return !status || status === "running";
    });
    if (allEmpty) seenPPRef.current.clear();
  }, [ppKey, pp]);

  // Demo loop — fires when forceDemo (URL flag) OR when nothing live.
  // We track *both* whether the queue is empty AND whether the active
  // card came from a live source, so a demo handoff in flight never
  // blocks the next live handoff from being queued.
  const factoryStatus = factory?.status || "idle";
  const isLive =
    factoryStatus === "running" ||
    !!pp ||
    (queueRef.current.length > 0 &&
      queueRef.current[0]?.source === "live");
  const runDemo = forceDemo || !isLive;

  useEffect(() => {
    if (!runDemo) return;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      // Avoid stacking demo cards in the queue — wait for the active
      // courier to finish before pushing the next one.
      if (queueRef.current.some((c) => c.source === "demo")) return;
      const step = DEMO_FLOW[demoIdxRef.current % DEMO_FLOW.length];
      demoIdxRef.current += 1;
      enqueue(step, "demo");
    };
    const initial = setTimeout(tick, 800);
    const interval = setInterval(tick, DEMO_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearTimeout(initial);
      clearInterval(interval);
    };
  }, [runDemo, enqueue]);

  // Arrival → fire the desk-highlight callback so PixelOffice can
  // pulse the receiving agent's desk for ~700ms.
  const handleArrive = useCallback(() => {
    if (!activeCard) return;
    onArrive?.({
      agentId: activeCard.to,
      label: activeCard.label,
      source: activeCard.source,
    });
  }, [activeCard, onArrive]);

  // Card finished → log handoff_completed and clear the slot so the
  // queue can advance.
  const handleDone = useCallback(() => {
    if (activeCard) {
      onHandoff?.({
        kind: "handoff_completed",
        from: activeCard.from,
        to: activeCard.to,
        label: activeCard.label,
        artifactType: activeCard.artifactType,
        banner: bannerFor(activeCard),
        source: activeCard.source,
      });
    }
    setActiveCard(null);
    onBannerChange?.(null);
  }, [activeCard, onBannerChange, onHandoff]);

  if (!activeCard) return null;
  const fromLayout = DESK_LAYOUT[activeCard.from];
  const toLayout = DESK_LAYOUT[activeCard.to];
  if (!fromLayout || !toLayout) return null;

  const fromAgent = AGENTS[activeCard.from];
  const toAgent = AGENTS[activeCard.to];
  // Aim slightly above the desk center so the courier's feet land
  // around the chair rather than the floor.
  const yOffset = -28;

  return (
    <HandoffCourier
      key={activeCard.key}
      fromX={fromLayout.x}
      fromY={fromLayout.y + yOffset}
      toX={toLayout.x}
      toY={toLayout.y + yOffset}
      fromAgent={fromAgent}
      toAgent={toAgent}
      label={activeCard.label}
      artifactType={activeCard.artifactType}
      bubble={isMobile ? null : activeCard.bubble}
      isMobile={isMobile}
      reducedMotion={reducedMotion}
      onArrive={handleArrive}
      onDone={handleDone}
    />
  );
}
