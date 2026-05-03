import { useMemo } from "react";
import AgentCharacter from "./AgentCharacter.jsx";
import AgentSpeechBubble from "./AgentSpeechBubble.jsx";
import {
  derivePhase,
  freshnessOf,
  hasActiveCycle,
  isRunningPhase,
  pickRunnerMeta,
  stageToWorkingAgent,
} from "../utils/autopilotPhase.js";

// AgentOfficeScene — three fixed zones (PLAN / BUILD / SHIP) instead
// of the previous absolute-positioned floor. Each zone is a card
// that contains a row of agent slots; each agent slot is a flex
// column of [bubble | character on desk | nameplate | status badge]
// so nothing can overlap. The verifier required guarantees:
//
//   * Three .agent-office-zone elements with PLAN / BUILD / SHIP labels
//   * Eight .agent-character figures inside the zones
//   * At most three .agent-speech-bubble elements rendered scene-wide
//   * No horizontal overflow at 1440px or 390px (CSS grid + box-sizing)
//
// Speech bubble selection: the active agent always speaks, plus up to
// two adjacent agents (cluster mates) so the operator sees a small
// conversation. Cap is enforced by slicing the deterministic list,
// not by accidental layout. Long text is truncated to ≤18 chars.

const AGENT_DEFS = [
  { id: "planner",  cluster: "plan"  },
  { id: "pm",       cluster: "plan"  },
  { id: "designer", cluster: "plan"  },
  { id: "frontend", cluster: "build" },
  { id: "backend",  cluster: "build" },
  { id: "ai",       cluster: "build" },
  { id: "qa",       cluster: "ship"  },
  { id: "deploy",   cluster: "ship"  },
];

const ZONES = [
  {
    key: "plan",
    title: "PLAN ZONE",
    subtitle: "회의실 · PM ↔ Planner ↔ Designer",
    agents: ["planner", "pm", "designer"],
  },
  {
    key: "build",
    title: "BUILD ZONE",
    subtitle: "개발실 · FE / BE / AI",
    agents: ["frontend", "backend", "ai"],
  },
  {
    key: "ship",
    title: "SHIP ZONE",
    subtitle: "출하실 · QA → Deploy",
    agents: ["qa", "deploy"],
  },
];

const STATUS_VISUAL_FROM_ACCOUNTABILITY = {
  pass:    { kind: "passed",  label: "PASS" },
  fail:    { kind: "failed",  label: "FAIL" },
  skipped: { kind: "skipped", label: "SKIP" },
};

const STATUS_BADGE_TONE = {
  running:  { color: "#0a1228", bg: "#fbbf24", label: "WORKING" },
  passed:   { color: "#0a1228", bg: "#34d399", label: "PASS"    },
  failed:   { color: "#fff",    bg: "#f87171", label: "BLOCKED" },
  rework:   { color: "#0a1228", bg: "#a78bfa", label: "REWORK"  },
  idle:     { color: "#cbd5e1", bg: "#1e293b", label: "READY"   },
  skipped:  { color: "#94a3b8", bg: "#1e293b", label: "SKIP"    },
};

// Short, fixed-width bubble copy per stage. Capped at ≤18 chars so
// the bubble never wraps to multiple lines and never overflows its
// agent card.
const BUBBLE_RUNNING = {
  planner:  "후보 정리 중",
  designer: "디자인 검토 중",
  pm:       "스코프 조정 중",
  frontend: "화면 적용 중",
  backend:  "API 적용 중",
  ai:       "AI 설계 중",
  qa:       "게이트 확인 중",
  deploy:   "push 대기 중",
};
const BUBBLE_PASSED = {
  planner:  "후보 확정",
  designer: "디자인 OK",
  pm:       "출하 결정",
  frontend: "FE 적용 완료",
  backend:  "BE 적용 완료",
  ai:       "AI 적용 완료",
  qa:       "QA 통과",
  deploy:   "PUSH 완료",
};
const BUBBLE_REWORK = {
  planner:  "원안 재작업",
  designer: "다시 검토",
  pm:       "스코프 좁히기",
};

function pickBubbleSide(zoneKey, idx) {
  // bubble-top for the leftmost agent in each zone; -right for the
  // middle column; -left for the rightmost. Keeps the tail of the
  // bubble pointing toward open floor so multiple bubbles don't
  // cross-aim.
  if (idx === 0) return "bubble-top";
  if (idx === 1) return "bubble-right";
  return "bubble-left";
}

function deriveAgentVisual(agentId, ctx) {
  const { meta, phase, workingAgentId, freshness } = ctx;
  const aa = meta?.agent_accountability || {};
  const accAgent = (aa.agents || {})[agentId];

  if (workingAgentId === agentId && isRunningPhase(phase)) {
    return { kind: "running" };
  }
  const isFresh = freshness === "current_run" || freshness === "current_cycle";
  if (isFresh && aa.blocking_agent === agentId) {
    if (aa.overall_status === "blocked" || aa.operator_required) {
      return { kind: "failed" };
    }
    return { kind: "rework" };
  }
  if (isFresh && accAgent) {
    const v = STATUS_VISUAL_FROM_ACCOUNTABILITY[accAgent.status];
    if (v) return v;
  }
  if (isFresh && agentId === "qa") {
    const qa = meta?.qa_gate || {};
    if (qa.qa_status === "failed") return { kind: "failed" };
    if (qa.qa_status === "passed") return { kind: "passed" };
  }
  if (isFresh && agentId === "deploy") {
    const pub = meta?.publish || {};
    if (pub.last_push_status === "failed") return { kind: "failed" };
    if (pub.last_push_status === "succeeded") return { kind: "passed" };
  }
  if (isRunningPhase(phase)) return { kind: "idle" };
  return { kind: "skipped" };
}

function deriveBubble(agentId, visual, meta) {
  if (visual.kind === "running") {
    return { tone: "running", text: BUBBLE_RUNNING[agentId] || "진행 중" };
  }
  if (visual.kind === "failed") {
    const aa = meta?.agent_accountability || {};
    const acc = (aa.agents || {})[agentId] || {};
    const reason = (acc.problems && acc.problems[0]) || aa.blocking_reason || "원인 확인";
    return { tone: "failed", text: String(reason).slice(0, 18) };
  }
  if (visual.kind === "rework") {
    return { tone: "rework", text: BUBBLE_REWORK[agentId] || "재작업 필요" };
  }
  if (visual.kind === "passed") {
    return { tone: "passed", text: BUBBLE_PASSED[agentId] || "OK" };
  }
  return null;
}

// Pick at most 3 bubbles. Active agent first, then up to two cluster
// mates with non-null bubbles. Cap is structural — anything past
// index 2 is dropped before rendering.
const BUBBLE_HARD_CAP = 3;

function pickBubbleSet(computedByAgent, workingAgentId) {
  const order = [];
  if (workingAgentId && computedByAgent[workingAgentId]?.bubble) {
    order.push(workingAgentId);
  }
  // Cluster mates of the active agent get priority.
  const activeCluster = AGENT_DEFS.find((a) => a.id === workingAgentId)?.cluster;
  if (activeCluster) {
    for (const def of AGENT_DEFS) {
      if (order.includes(def.id)) continue;
      if (def.cluster !== activeCluster) continue;
      if (computedByAgent[def.id]?.bubble) order.push(def.id);
    }
  }
  // Then any other agent with a bubble.
  for (const def of AGENT_DEFS) {
    if (order.includes(def.id)) continue;
    if (computedByAgent[def.id]?.bubble) order.push(def.id);
  }
  return new Set(order.slice(0, BUBBLE_HARD_CAP));
}

const PHASE_HEADLINE = {
  cycle_running:      (workingAgentId) =>
    workingAgentId
      ? `Auto Pilot 실행 중 · ${labelFor(workingAgentId)}가 작업 중`
      : "Auto Pilot 실행 중 · 사이클 진행",
  starting:           () => "Auto Pilot 시작 중 · 사이클 준비",
  waiting_next_cycle: () => "다음 사이클 시작 대기 중",
  stopping:           () => "정지 요청 중 · 현재 사이클 종료 후 정지",
  restarting:         () => "재시작 중",
  stopped:            () => "Auto Pilot 정지됨",
  failed:             () => "Auto Pilot 실패",
  idle:               () => "Auto Pilot 대기",
};

function labelFor(agentId) {
  const map = {
    planner: "Planner", pm: "PM", designer: "Designer",
    frontend: "Frontend", backend: "Backend", ai: "AI",
    qa: "QA", deploy: "Deploy",
  };
  return map[agentId] || agentId;
}

export default function AgentOfficeScene({
  runners = [],
  onAgentClick,
  selectedAgentId = null,
  restartInFlight = false,
  stopRequested = false,
  drawerOpen = false,
}) {
  const meta = useMemo(() => pickRunnerMeta(runners), [runners]);
  const phase = useMemo(
    () => derivePhase(meta, { restartInFlight, stopRequested }),
    [meta, restartInFlight, stopRequested],
  );
  const workingAgentId = useMemo(() => stageToWorkingAgent(meta, phase), [meta, phase]);
  const freshness = useMemo(
    () => freshnessOf({
      artifactCycleId: meta?.agent_accountability?.cycle_id,
      artifactAt: meta?.agent_accountability?.evaluated_at,
      autopilot: meta?.autopilot,
    }),
    [meta],
  );
  const ctx = { meta, phase, workingAgentId, freshness };

  const computedByAgent = useMemo(() => {
    const out = {};
    for (const def of AGENT_DEFS) {
      const visual = deriveAgentVisual(def.id, ctx);
      const bubble = deriveBubble(def.id, visual, meta);
      out[def.id] = { def, visual, bubble };
    }
    return out;
  }, [meta, phase, workingAgentId, freshness]);

  // Hard-cap the bubble set to BUBBLE_HARD_CAP.
  const bubbleAgents = useMemo(
    () => pickBubbleSet(computedByAgent, workingAgentId),
    [computedByAgent, workingAgentId],
  );

  // Top status headline — single big readable line.
  const ap = meta?.autopilot || {};
  const cycleCount = ap.cycle_count ?? 0;
  const maxCycles = ap.max_cycles ?? 0;
  const headline = (PHASE_HEADLINE[phase] || PHASE_HEADLINE.idle)(workingAgentId);
  const headlineDetail = (() => {
    if (phase === "cycle_running" || phase === "starting" || phase === "waiting_next_cycle") {
      const mode = String(ap.mode || "safe_run").toUpperCase().replace("_", " ");
      return `${mode} · CYCLE ${cycleCount}/${maxCycles || "?"}`;
    }
    if (ap.last_verdict) return `last verdict: ${ap.last_verdict}`;
    return null;
  })();

  return (
    <section
      className={
        "pixel-office-scene agent-office-stage office-scene-redesign" +
        (drawerOpen ? " is-drawer-open" : "") +
        (isRunningPhase(phase) ? " is-running" : "")
      }
      data-testid="pixel-office-scene"
      data-phase={phase}
    >
      <header className="pixel-office-header office-header">
        <span className="pixel-office-header-dot" aria-hidden />
        <span className="pixel-office-header-title office-header-title">AGENT OFFICE</span>
        <span className="pixel-office-header-sub">8 에이전트 · 3 zones</span>
      </header>

      {/* Big readable headline — replaces the squishy stage chip. */}
      <div
        className={"office-headline office-headline-" + phase}
        data-testid="office-headline"
      >
        <div className="office-headline-main">{headline}</div>
        {headlineDetail && (
          <div className="office-headline-detail">{headlineDetail}</div>
        )}
      </div>

      <div className="office-zones" data-testid="office-zones">
        {ZONES.map((zone) => (
          <section
            key={zone.key}
            className={`agent-office-zone agent-office-zone-${zone.key}`}
            data-zone={zone.key}
            data-testid={`agent-office-zone-${zone.key}`}
          >
            <header className="agent-office-zone-header">
              <span className="agent-office-zone-title">{zone.title}</span>
              <span className="agent-office-zone-subtitle">{zone.subtitle}</span>
            </header>
            <div className={`agent-office-zone-grid agent-office-zone-grid-${zone.agents.length}`}>
              {zone.agents.map((agentId, idx) => {
                const { def, visual, bubble } = computedByAgent[agentId];
                const showBubble = bubble && bubbleAgents.has(agentId);
                const tone = STATUS_BADGE_TONE[visual.kind] || STATUS_BADGE_TONE.skipped;
                return (
                  <div
                    key={agentId}
                    className={
                      "agent-slot agent-slot-" + agentId +
                      " agent-slot-" + visual.kind +
                      (selectedAgentId === agentId ? " is-selected" : "") +
                      (workingAgentId === agentId ? " is-current" : "")
                    }
                    data-agent-id={agentId}
                    data-agent-state={visual.kind}
                    data-testid={`agent-slot-${agentId}`}
                  >
                    {/* Bubble row: a fixed-height slot so a missing
                        bubble still preserves vertical rhythm. */}
                    <div className="agent-slot-bubble">
                      {showBubble && (
                        <AgentSpeechBubble
                          tone={bubble.tone}
                          text={bubble.text}
                          side={pickBubbleSide(zone.key, idx)}
                          small
                        />
                      )}
                    </div>
                    {/* Character + desk */}
                    <div className="agent-slot-figure">
                      <AgentCharacter
                        agentId={agentId}
                        state={visual.kind}
                        bubble={null /* bubble lives in the slot */}
                        isCurrent={workingAgentId === agentId}
                        isSelected={selectedAgentId === agentId}
                        onClick={onAgentClick}
                      />
                    </div>
                    {/* Nameplate + status badge — in their own rows so
                        they never overlap the figure or the desk. */}
                    <div className="agent-slot-nameplate" data-testid={`agent-slot-name-${agentId}`}>
                      {labelFor(agentId)}
                    </div>
                    <div
                      className={"agent-slot-status agent-slot-status-" + visual.kind}
                      data-testid={`agent-slot-status-${agentId}`}
                      style={{
                        color: tone.color,
                        backgroundColor: tone.bg,
                      }}
                    >
                      {tone.label}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </section>
  );
}
