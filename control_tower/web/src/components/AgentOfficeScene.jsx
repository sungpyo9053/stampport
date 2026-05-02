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

// AgentOfficeScene — pixel-office floor with absolute-positioned
// desks, monitors, and 8 walking/typing/talking AgentCharacters.
//
// The previous version treated agent_accountability.blocking_agent as
// "currently working", which painted Frontend as WORKING long after
// the loop had moved on (cycle_count=0 + stale FE failure from a
// previous run = false BLOCKED · FRONTEND chip). This rewrite reads
// from the shared autopilotPhase helper:
//
//   1. derivePhase()         — what is autopilot doing right now?
//   2. stageToWorkingAgent() — who, if anyone, owns the live stage?
//   3. freshnessOf()         — was this accountability blob produced
//                              by the current run, or is it stale?
//
// Stale failures still surface — but only as a small "previous issue"
// chip, never as a fresh BLOCKED character.

const AGENT_DEFS = [
  { id: "planner",  x: 16, y: 26, cluster: "plan" },
  { id: "pm",       x: 32, y: 22, cluster: "plan" },
  { id: "designer", x: 48, y: 28, cluster: "plan" },
  { id: "frontend", x: 22, y: 56, cluster: "build" },
  { id: "backend",  x: 42, y: 60, cluster: "build" },
  { id: "ai",       x: 62, y: 56, cluster: "build" },
  { id: "qa",       x: 70, y: 82, cluster: "ship" },
  { id: "deploy",   x: 88, y: 78, cluster: "ship" },
];

const DESK_DEFS = [
  { kind: "table",  x: 24, y: 32, w: 28, h: 6, label: "Plan Table" },
  { kind: "bench",  x: 28, y: 64, w: 38, h: 5, label: "Build Bench" },
  { kind: "gate",   x: 78, y: 86, w: 14, h: 4, label: "Ship Gate" },
];

const MONITOR_DEFS = [
  { x: 22, y: 52, color: "#38bdf8" },
  { x: 42, y: 56, color: "#34d399" },
  { x: 62, y: 52, color: "#a78bfa" },
];

const STATUS_VISUAL_FROM_ACCOUNTABILITY = {
  pass:    { kind: "passed",  ring: "#34d399" },
  fail:    { kind: "failed",  ring: "#f87171" },
  skipped: { kind: "skipped", ring: "#475569" },
};

function deriveAgentVisual(agentId, ctx) {
  const { meta, phase, workingAgentId, freshness } = ctx;
  const aa = meta?.agent_accountability || {};
  const accAgent = (aa.agents || {})[agentId];

  // 1. Active stage wins — but only when phase is actually running.
  if (workingAgentId === agentId && isRunningPhase(phase)) {
    return { kind: "running", ring: "#fbbf24" };
  }

  // 2. Fresh blocking_agent → fail/rework. Stale → drop entirely
  // (the office stage shows IDLE; the drawer still shows the prior
  // issue with a "previous cycle" pill).
  const isFresh = freshness === "current_run" || freshness === "current_cycle";
  if (aa.blocking_agent === agentId && isFresh) {
    if (aa.overall_status === "blocked" || aa.operator_required) {
      return { kind: "failed", ring: "#f87171" };
    }
    return { kind: "rework", ring: "#a78bfa" };
  }

  // 3. Per-agent accountability status from THIS cycle only.
  if (isFresh && accAgent) {
    const v = STATUS_VISUAL_FROM_ACCOUNTABILITY[accAgent.status];
    if (v) return v;
  }

  // 4. QA / Deploy passed/failed — only when fresh.
  if (isFresh && agentId === "qa") {
    const qa = meta?.qa_gate || {};
    if (qa.qa_status === "failed") return { kind: "failed", ring: "#f87171" };
    if (qa.qa_status === "passed") return { kind: "passed", ring: "#34d399" };
  }
  if (isFresh && agentId === "deploy") {
    const pub = meta?.publish || {};
    if (pub.last_push_status === "failed") return { kind: "failed", ring: "#f87171" };
    if (pub.last_push_status === "succeeded") return { kind: "passed", ring: "#facc15" };
  }

  // 5. Loop is alive but this agent isn't on the active stage → READY
  if (isRunningPhase(phase)) return { kind: "idle", ring: "#475569" };
  return { kind: "skipped", ring: "#334155" };
}

function deriveBubble(agentId, visual, meta) {
  const aa = meta?.agent_accountability || {};

  if (visual.kind === "running") {
    const map = {
      planner:  "이번 사이클 후보 3개를 뽑는 중...",
      designer: "이 배지가 진짜 갖고 싶은지 검토 중...",
      pm:       "스코프를 줄여 출하 단위로 자르는 중...",
      frontend: "Share 화면 변경 파일을 적용 중...",
      backend:  "API 엔드포인트 변경을 적용 중...",
      ai:       "Kick point 설계 갱신 중...",
      qa:       "render smoke와 scope gate 확인 중...",
      deploy:   "push 가능 여부를 기다리는 중...",
    };
    return { tone: "running", text: map[agentId] || "진행 중..." };
  }
  if (visual.kind === "failed") {
    const acc = (aa.agents || {})[agentId] || {};
    const reason =
      (acc.problems && acc.problems[0]) ||
      aa.blocking_reason ||
      "원인을 다시 확인해야 함";
    return { tone: "failed", text: String(reason).slice(0, 80) };
  }
  if (visual.kind === "rework") {
    const map = {
      planner:  "PM이 원안 재작업을 요청했어요",
      designer: "Designer가 다시 보고 싶다고 합니다",
      pm:       "스코프 좁혀서 다시 출하 결정해야 함",
    };
    return {
      tone: "rework",
      text: map[agentId] || aa.blocking_reason || "재작업 필요",
    };
  }
  if (visual.kind === "passed") {
    const map = {
      planner:  "후보 확정 ✓",
      designer: "디자인 OK",
      pm:       "출하 결정",
      frontend: "FE 변경 적용",
      backend:  "BE 변경 적용",
      ai:       "AI 설계 적용",
      qa:       "QA 통과",
      deploy:   "PUSH 완료 🚀",
    };
    return { tone: "passed", text: map[agentId] || "OK" };
  }
  return null;
}

const DEMO_BUBBLES = {
  planner:  { tone: "running", text: "다음 사이클 후보 정리 중..." },
  pm:       { tone: "running", text: "스코프 잘라서 출하 단위로 갈게요." },
  designer: { tone: "running", text: "이 배지 갖고 싶은지 한 번 더 봅시다." },
};

const PHASE_BADGE_STYLE = {
  active:  { color: "#fbbf24", border: "#fbbf2466", bg: "#1c1408" },
  warn:    { color: "#fbbf24", border: "#fbbf2466", bg: "#1c1408" },
  error:   { color: "#fecaca", border: "#f8717166", bg: "#1c0d12" },
  neutral: { color: "#cbd5e1", border: "#1e293b",   bg: "#0a1228" },
  muted:   { color: "#94a3b8", border: "#1e293b",   bg: "#0a1228" },
};

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

  const aa = meta?.agent_accountability || {};
  const hasLiveData =
    isRunningPhase(phase) ||
    (aa.available && (freshness === "current_run" || freshness === "current_cycle")) ||
    Object.keys(aa.agents || {}).length > 0;

  const computed = AGENT_DEFS.map((def) => {
    const visual = deriveAgentVisual(def.id, {
      meta,
      phase,
      workingAgentId,
      freshness,
    });
    let bubble = deriveBubble(def.id, visual, meta);
    if (!bubble && !hasLiveData && DEMO_BUBBLES[def.id]) {
      bubble = DEMO_BUBBLES[def.id];
    }
    return { def, visual, bubble };
  });

  // Stage label — driven by phase + workingAgentId. Stale BLOCKED is
  // shown as "STALE · <agent>" so the operator knows it's a previous
  // issue, not a current one.
  const stageLabel = (() => {
    if (phase === "starting") return "STARTING · 사이클 준비 중";
    if (phase === "cycle_running" && workingAgentId) {
      return `WORKING · ${workingAgentId.toUpperCase()}`;
    }
    if (phase === "cycle_running") return "WORKING";
    if (phase === "waiting_next_cycle") return "다음 사이클 시작 대기 중";
    if (phase === "stopping") return "STOPPING · 현재 사이클 종료 후 정지";
    if (phase === "restarting") return "RESTARTING";
    if (phase === "failed") return "FAILED";
    if (phase === "stopped") return "STOPPED";
    if (aa.blocking_agent &&
        (freshness === "current_run" || freshness === "current_cycle")) {
      return `BLOCKED · ${aa.blocking_agent.toUpperCase()}`;
    }
    if (aa.blocking_agent) {
      // Stale — surface gently without a red chip.
      return `IDLE · 이전 cycle ${aa.blocking_agent.toUpperCase()} 미해결`;
    }
    return "IDLE";
  })();
  const stageBadge =
    phase === "failed" ? PHASE_BADGE_STYLE.error
    : isRunningPhase(phase) ? PHASE_BADGE_STYLE.active
    : phase === "stopping" || phase === "restarting" ? PHASE_BADGE_STYLE.warn
    : PHASE_BADGE_STYLE.muted;

  return (
    <section
      className={
        "pixel-office-scene agent-office-stage" +
        (drawerOpen ? " is-drawer-open" : "") +
        (isRunningPhase(phase) ? " is-running" : "")
      }
      data-testid="pixel-office-scene"
      data-phase={phase}
    >
      {/* Header */}
      <header className="pixel-office-header">
        <span className="pixel-office-header-dot" aria-hidden />
        <span className="pixel-office-header-title">AGENT OFFICE</span>
        <span className="pixel-office-header-sub">8 에이전트 · 실시간</span>
        <span
          className="pixel-office-header-stage"
          style={{
            color: stageBadge.color,
            borderColor: stageBadge.border,
            backgroundColor: stageBadge.bg,
          }}
          data-testid="pixel-office-stage-label"
        >
          {stageLabel}
        </span>
      </header>

      <div
        className="pixel-office-floor"
        data-testid="pixel-office-floor"
      >
        <div className="pixel-office-floor-tiles" aria-hidden />
        <div className="pixel-office-floor-vignette" aria-hidden />

        <div className="pixel-office-wall" aria-hidden>
          <div className="pixel-office-window" />
          <div className="pixel-office-window" />
          <div className="pixel-office-window" />
        </div>

        <div className="pixel-office-whiteboard" aria-hidden style={{ left: "5%", top: "10%" }}>
          <div className="pixel-office-whiteboard-line" />
          <div className="pixel-office-whiteboard-line" />
          <div className="pixel-office-whiteboard-line short" />
        </div>

        {DESK_DEFS.map((d, i) => (
          <div
            key={i}
            className={`office-desk office-desk-${d.kind}`}
            data-testid={`office-desk-${d.kind}`}
            style={{
              left: `${d.x}%`,
              top: `${d.y}%`,
              width: `${d.w}%`,
              height: `${d.h}%`,
            }}
            aria-label={d.label}
          >
            <span className="office-desk-top" />
            <span className="office-desk-leg office-desk-leg-l" />
            <span className="office-desk-leg office-desk-leg-r" />
          </div>
        ))}

        {MONITOR_DEFS.map((m, i) => (
          <div
            key={i}
            className="office-monitor"
            data-testid={`office-monitor-${i}`}
            style={{
              left: `${m.x}%`,
              top: `${m.y}%`,
              "--monitor-color": m.color,
            }}
            aria-hidden
          >
            <span className="office-monitor-frame">
              <span className="office-monitor-screen" />
            </span>
            <span className="office-monitor-stand" />
          </div>
        ))}

        <svg
          className="pixel-office-route"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          aria-hidden
        >
          <defs>
            <linearGradient id="pixel-office-route-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%"   stopColor="#7dd3fc" stopOpacity="0.6" />
              <stop offset="50%"  stopColor="#d4a843" stopOpacity="0.4" />
              <stop offset="100%" stopColor="#facc15" stopOpacity="0.6" />
            </linearGradient>
          </defs>
          <path
            d="M 32 26 C 38 38, 28 50, 42 60 C 56 70, 60 78, 80 82"
            stroke="url(#pixel-office-route-grad)"
            strokeWidth="0.9"
            fill="none"
            strokeDasharray="2 1.5"
            className="pixel-office-route-line"
          />
        </svg>

        {/* Speech bubbles — positioned just above each agent */}
        {computed
          .filter(({ bubble }) => !!bubble)
          .map(({ def, bubble }) => (
            <div
              key={`bubble-${def.id}`}
              className="pixel-office-bubble-anchor"
              style={{
                left: `${def.x}%`,
                top: `${Math.max(0, def.y - 14)}%`,
              }}
              data-testid={`pixel-office-bubble-${def.id}`}
            >
              <AgentSpeechBubble tone={bubble.tone} text={bubble.text} />
            </div>
          ))}

        {/* Agents */}
        {computed.map(({ def, visual, bubble }) => (
          <div
            key={def.id}
            className="pixel-agent-anchor"
            style={{ left: `${def.x}%`, top: `${def.y}%` }}
          >
            <AgentCharacter
              agentId={def.id}
              state={visual.kind}
              bubble={bubble}
              isCurrent={workingAgentId === def.id}
              isSelected={selectedAgentId === def.id}
              onClick={onAgentClick}
            />
          </div>
        ))}
      </div>

      <footer className="pixel-office-legend">
        <span className="pixel-office-legend-chip pixel-office-legend-plan">
          PLAN · PM ↔ Planner ↔ Designer
        </span>
        <span className="pixel-office-legend-chip pixel-office-legend-build">
          BUILD · FE / BE / AI
        </span>
        <span className="pixel-office-legend-chip pixel-office-legend-ship">
          SHIP · QA → Deploy
        </span>
      </footer>
    </section>
  );
}
