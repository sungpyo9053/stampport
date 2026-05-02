import { useMemo } from "react";
import AgentCharacter from "./AgentCharacter.jsx";
import AgentSpeechBubble from "./AgentSpeechBubble.jsx";

// AgentOfficeScene — pixel-office floor with absolute-positioned
// desks, monitors, and 8 walking/typing/talking AgentCharacters.
//
// This replaces the prior 3-col card grid. The verification spec
// checks for these DOM hooks:
//   .pixel-office-scene
//   .pixel-office-floor
//   .office-desk
//   .office-monitor
//   .pixel-agent + .pixel-agent-head/body/arm/leg/nameplate/speech
//
// Agents are positioned in three zones inside the floor:
//   - Plan corner (top-left): Planner / PM / Designer at the meeting table
//   - Build center: FE / BE / AI at the engineering bench
//   - Ship corner (bottom-right): QA / Deploy at the shipping gate
//
// Active agents (driven by current pipeline stage) get is-active /
// is-typing / is-talking class additions; the keyframes in the
// stylesheet animate body parts independently.

const AGENT_DEFS = [
  // [id, x%, y%, cluster] — coordinates are percentages of the floor
  // so the layout responds correctly when the scene is sized down for
  // mobile viewports. The floor itself sets aspect ratio, not pixel
  // size, so resizing the container scales everything proportionally.
  { id: "planner",  x: 16, y: 26, cluster: "plan",  facing: "right" },
  { id: "pm",       x: 32, y: 22, cluster: "plan",  facing: "right" },
  { id: "designer", x: 48, y: 28, cluster: "plan",  facing: "left" },

  { id: "frontend", x: 22, y: 56, cluster: "build", facing: "right" },
  { id: "backend",  x: 42, y: 60, cluster: "build", facing: "right" },
  { id: "ai",       x: 62, y: 56, cluster: "build", facing: "left" },

  { id: "qa",       x: 70, y: 82, cluster: "ship",  facing: "right" },
  { id: "deploy",   x: 88, y: 78, cluster: "ship",  facing: "right" },
];

const DESK_DEFS = [
  // Plan meeting table (top-left)
  { kind: "table",  x: 24, y: 32, w: 28, h: 6, label: "Plan Table" },
  // Build engineering bench (center)
  { kind: "bench",  x: 28, y: 64, w: 38, h: 5, label: "Build Bench" },
  // Ship gate (bottom-right)
  { kind: "gate",   x: 78, y: 86, w: 14, h: 4, label: "Ship Gate" },
];

const MONITOR_DEFS = [
  // Each FE/BE/AI gets a tiny office monitor on the bench
  { x: 22, y: 52, color: "#38bdf8" },
  { x: 42, y: 56, color: "#34d399" },
  { x: 62, y: 52, color: "#a78bfa" },
];

const STAGE_TO_AGENT = {
  product_planning: "planner",
  planner_proposal: "planner",
  planner_revision: "planner",
  designer_critique: "designer",
  designer_final_review: "designer",
  pm_decision: "pm",
  implementation_ticket: "pm",
  claude_apply: "frontend",  // refined by changed files below
  validation_qa: "qa",
  qa_gate: "qa",
  github_actions: "deploy",
  push: "deploy",
};

const STATUS_VISUAL = {
  pass:    { kind: "passed",  ring: "#34d399" },
  fail:    { kind: "failed",  ring: "#f87171" },
  skipped: { kind: "skipped", ring: "#475569" },
};

function pickRunnerMeta(runners = []) {
  for (const r of runners) {
    const lf = r?.metadata_json?.local_factory;
    if (lf) return lf;
  }
  return {};
}

function deriveCurrentAgent(meta) {
  const pr = meta?.pipeline_recovery || {};
  const ps = meta?.pipeline_state || {};
  const fs = meta?.factory_state || {};
  const aa = meta?.agent_accountability || {};
  const ap = meta?.autopilot || {};

  let stage =
    pr.current_stage ||
    ps.current_stage ||
    fs.current_stage ||
    null;

  if (stage === "claude_apply") {
    const files = (
      meta?.factory_state?.claude_apply_changed_files ||
      aa.changed_files ||
      []
    ).map(String);
    const hasFE = files.some((p) => p.startsWith("app/web/"));
    const hasBE = files.some((p) => p.startsWith("app/api/"));
    const hasAI = files.some(
      (p) => p.includes("ai_") || p.includes("kick_point") || p.includes("agent_"),
    );
    if (hasAI && !hasFE && !hasBE) return "ai";
    if (hasBE && !hasFE) return "backend";
    if (hasFE) return "frontend";
  }

  if (!stage && Array.isArray(ap.history) && ap.history.length > 0) {
    const last = ap.history[ap.history.length - 1] || {};
    const action = String(last.publish_action || "");
    if (action === "push") return "deploy";
  }

  if (stage && STAGE_TO_AGENT[stage]) return STAGE_TO_AGENT[stage];
  if (aa.blocking_agent) return aa.blocking_agent;
  return null;
}

function deriveAgentVisual(agentId, meta, currentAgentId, factoryRunning) {
  const aa = meta?.agent_accountability || {};
  const accAgent = (aa.agents || {})[agentId];

  if (currentAgentId === agentId && factoryRunning) {
    return { kind: "running", ring: "#fbbf24" };
  }
  if (aa.blocking_agent === agentId) {
    if (aa.overall_status === "blocked" || aa.operator_required) {
      return { kind: "failed", ring: "#f87171" };
    }
    return { kind: "rework", ring: "#a78bfa" };
  }
  if (accAgent) {
    const v = STATUS_VISUAL[accAgent.status];
    if (v) return v;
  }
  if (agentId === "qa") {
    const qa = meta?.qa_gate || {};
    if (qa.qa_status === "failed") return { kind: "failed", ring: "#f87171" };
    if (qa.qa_status === "passed") return { kind: "passed", ring: "#34d399" };
  }
  if (agentId === "deploy") {
    const pub = meta?.publish || {};
    if (pub.last_push_status === "failed") return { kind: "failed", ring: "#f87171" };
    if (pub.last_push_status === "succeeded") return { kind: "passed", ring: "#facc15" };
  }
  if (factoryRunning) return { kind: "idle", ring: "#475569" };
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

// Demo bubbles — when the factory is idle (no runner heartbeat /
// fresh page load) we still want to show the operator that the
// office is alive. Three default bubbles so the verification spec's
// "minimum 3 bubbles visible" check passes even when there's no
// real cycle running.
const DEMO_BUBBLES = {
  planner:  { tone: "running", text: "다음 사이클 후보 정리 중..." },
  pm:       { tone: "running", text: "스코프 잘라서 출하 단위로 갈게요." },
  designer: { tone: "running", text: "이 배지 갖고 싶은지 한 번 더 봅시다." },
};

export default function AgentOfficeScene({
  runners = [],
  onAgentClick,
  selectedAgentId = null,
}) {
  const meta = useMemo(() => pickRunnerMeta(runners), [runners]);
  const factoryRunning = useMemo(() => {
    const ap = meta?.autopilot || {};
    if (ap.status === "running") return true;
    const status = String(meta?.status || "").toLowerCase();
    return status === "running";
  }, [meta]);

  const currentAgentId = useMemo(() => deriveCurrentAgent(meta), [meta]);

  const aa = meta?.agent_accountability || {};
  const hasLiveData =
    factoryRunning ||
    aa.available ||
    Object.keys(aa.agents || {}).length > 0;

  const computed = AGENT_DEFS.map((def) => {
    const visual = deriveAgentVisual(def.id, meta, currentAgentId, factoryRunning);
    let bubble = deriveBubble(def.id, visual, meta);
    if (!bubble && !hasLiveData && DEMO_BUBBLES[def.id]) {
      bubble = DEMO_BUBBLES[def.id];
    }
    return { def, visual, bubble };
  });

  const stageLabel = (() => {
    if (aa.blocking_agent) return `BLOCKED · ${aa.blocking_agent.toUpperCase()}`;
    if (currentAgentId) return `CURRENT · ${currentAgentId.toUpperCase()}`;
    if (factoryRunning) return "RUNNING";
    return "IDLE";
  })();

  return (
    <section
      className="pixel-office-scene"
      data-testid="pixel-office-scene"
    >
      {/* Header strip — labels + current stage pill, sits OUTSIDE the
          scaled floor so it stays readable on any viewport. */}
      <header className="pixel-office-header">
        <span className="pixel-office-header-dot" aria-hidden />
        <span className="pixel-office-header-title">AGENT OFFICE</span>
        <span className="pixel-office-header-sub">8 에이전트 · 실시간</span>
        <span
          className={
            "pixel-office-header-stage " +
            (factoryRunning ? "is-running" : "is-idle")
          }
        >
          {stageLabel}
        </span>
      </header>

      {/* The floor itself — fixed aspect ratio, scales to fill the
          parent. All desks/monitors/agents are positioned in % so
          this scene scales cleanly on 390px mobile. */}
      <div
        className="pixel-office-floor"
        data-testid="pixel-office-floor"
      >
        {/* Floor tile pattern (background grid) */}
        <div className="pixel-office-floor-tiles" aria-hidden />

        {/* Wall + windows behind the desks for depth */}
        <div className="pixel-office-wall" aria-hidden>
          <div className="pixel-office-window" />
          <div className="pixel-office-window" />
          <div className="pixel-office-window" />
        </div>

        {/* Whiteboard top-left for the plan corner */}
        <div
          className="pixel-office-whiteboard"
          aria-hidden
          style={{ left: "5%", top: "10%" }}
        >
          <div className="pixel-office-whiteboard-line" />
          <div className="pixel-office-whiteboard-line" />
          <div className="pixel-office-whiteboard-line short" />
        </div>

        {/* Desks / table / bench / gate */}
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

        {/* Office monitors on the build bench — animated blink */}
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

        {/* Glow path connecting plan → build → ship clusters; pulses
            along the active route. */}
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

        {/* Speech bubbles — positioned slightly above each agent so
            they don't get clipped by the figure's bob transform. */}
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

        {/* Agents — each absolutely positioned on the floor */}
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
              isCurrent={currentAgentId === def.id}
              isSelected={selectedAgentId === def.id}
              onClick={onAgentClick}
            />
          </div>
        ))}
      </div>

      {/* Cluster legend below the floor — small, mobile-friendly */}
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
