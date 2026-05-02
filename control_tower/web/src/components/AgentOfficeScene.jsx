import { useMemo } from "react";

// AgentOfficeScene — Instagram-Reels-style "AI agent office".
//
// Reads from runners[].metadata_json.local_factory.{agent_accountability,
// pipeline_recovery, factory_state, autopilot, ping_pong, qa_gate,
// publish}. Renders 8 agent characters arranged as three clusters:
//
//   1. Plan ring   — PM ↔ Planner ↔ Designer (ping-pong)
//   2. Build ring  — FE · BE · AI Architect
//   3. Ship ring   — QA → Deploy
//
// Click an agent → onAgentClick(agentId). The drawer / detail panel is
// rendered by the parent so this scene stays purely presentational.

const AGENT_DEFS = [
  // Plan ring — close together on top.
  {
    id: "pm",
    label: "PM",
    role: "프로덕트 매니저",
    emoji: "🧭",
    accent: "#d4a843",
    cluster: "plan",
  },
  {
    id: "planner",
    label: "Planner",
    role: "기획자",
    emoji: "📐",
    accent: "#7dd3fc",
    cluster: "plan",
  },
  {
    id: "designer",
    label: "Designer",
    role: "디자이너",
    emoji: "🎨",
    accent: "#f472b6",
    cluster: "plan",
  },
  // Build ring — pipeline middle row.
  {
    id: "frontend",
    label: "Frontend",
    role: "FE 엔지니어",
    emoji: "💻",
    accent: "#38bdf8",
    cluster: "build",
  },
  {
    id: "backend",
    label: "Backend",
    role: "BE 엔지니어",
    emoji: "🛠️",
    accent: "#34d399",
    cluster: "build",
  },
  {
    id: "ai",
    label: "AI",
    role: "AI Architect",
    emoji: "🧠",
    accent: "#a78bfa",
    cluster: "build",
  },
  // Ship ring — bottom row, output side.
  {
    id: "qa",
    label: "QA",
    role: "QA 엔지니어",
    emoji: "🔍",
    accent: "#fb923c",
    cluster: "ship",
  },
  {
    id: "deploy",
    label: "Deploy",
    role: "배포 담당",
    emoji: "🚀",
    accent: "#facc15",
    cluster: "ship",
  },
];

// Map factory pipeline stages → agent ids. The "current stage" picks
// which agent is animated as RUNNING when factory is mid-cycle.
const STAGE_TO_AGENT = {
  product_planning: "planner",
  planner_proposal: "planner",
  planner_revision: "planner",
  designer_critique: "designer",
  designer_final_review: "designer",
  pm_decision: "pm",
  implementation_ticket: "pm",
  claude_apply: "frontend", // refined below by changed-file routing
  validation_qa: "qa",
  qa_gate: "qa",
  github_actions: "deploy",
  push: "deploy",
};

// Map pure agent_accountability statuses → visual state.
const STATUS_VISUAL = {
  pass:    { kind: "passed",  ring: "#34d399", label: "PASS"   },
  fail:    { kind: "failed",  ring: "#f87171", label: "RETRY"  },
  skipped: { kind: "skipped", ring: "#475569", label: "SKIP"   },
};

const VISUAL_BG = {
  running: "#0a1228",
  passed:  "#0c1f1a",
  failed:  "#1c0d12",
  rework:  "#180f25",
  skipped: "#0a1228",
  waiting: "#0a1228",
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

  // 1. Pipeline recovery / state currently knows the stuck or active stage.
  let stage =
    pr.current_stage ||
    ps.current_stage ||
    fs.current_stage ||
    null;

  // 2. claude_apply changed files → route between FE/BE/AI by path.
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

  // 3. Autopilot history may give a more current "what just happened".
  if (!stage && Array.isArray(ap.history) && ap.history.length > 0) {
    const last = ap.history[ap.history.length - 1] || {};
    const action = String(last.publish_action || "");
    if (action === "push") return "deploy";
  }

  if (stage && STAGE_TO_AGENT[stage]) return STAGE_TO_AGENT[stage];

  // 4. Fall back to blocking agent if accountability said someone failed.
  if (aa.blocking_agent) return aa.blocking_agent;

  return null;
}

function deriveAgentVisual(agentId, meta, currentAgentId, factoryRunning) {
  const aa = meta?.agent_accountability || {};
  const fs = meta?.factory_state || {};
  const accAgent = (aa.agents || {})[agentId];

  // Active = factory currently working this agent's stage.
  if (currentAgentId === agentId && factoryRunning) {
    return { kind: "running", ring: "#fbbf24", label: "WORKING" };
  }

  // Blocking agent overrides accountability for visual prominence.
  if (aa.blocking_agent === agentId) {
    if (aa.overall_status === "blocked" || aa.operator_required) {
      return { kind: "failed", ring: "#f87171", label: "BLOCKED" };
    }
    return { kind: "rework", ring: "#a78bfa", label: "REWORK" };
  }

  if (accAgent) {
    const v = STATUS_VISUAL[accAgent.status];
    if (v) return v;
  }

  // QA gate-specific signal.
  if (agentId === "qa") {
    const qa = meta?.qa_gate || {};
    if (qa.qa_status === "failed") {
      return { kind: "failed", ring: "#f87171", label: "QA FAIL" };
    }
    if (qa.qa_status === "passed") {
      return { kind: "passed", ring: "#34d399", label: "PASS" };
    }
  }

  // Deploy-specific signal.
  if (agentId === "deploy") {
    const pub = meta?.publish || {};
    if (pub.last_push_status === "failed") {
      return { kind: "failed", ring: "#f87171", label: "PUSH FAIL" };
    }
    if (pub.last_push_status === "succeeded") {
      return { kind: "passed", ring: "#facc15", label: "SHIPPED" };
    }
  }

  if (factoryRunning) {
    return { kind: "waiting", ring: "#475569", label: "대기" };
  }
  return { kind: "skipped", ring: "#334155", label: "IDLE" };
}

function deriveBubble(agentId, visual, meta, currentAgentId) {
  const aa = meta?.agent_accountability || {};
  const fs = meta?.factory_state || {};

  // Bubble texts driven by visual kind + pipeline stage.
  if (visual.kind === "running") {
    const map = {
      planner:  "이번 사이클 후보 3개를 뽑는 중...",
      designer: "이 배지가 진짜 갖고 싶은지 검토 중...",
      pm:       "스코프를 줄여 출하 단위로 자르는 중...",
      frontend: "Share 화면 변경 파일을 적용 중...",
      backend:  "API 엔드포인트 변경을 적용 중...",
      ai:       "Kick point / 에이전트 설계 갱신 중...",
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
      planner:  "후보 확정",
      designer: "디자인 OK",
      pm:       "출하 결정",
      frontend: "FE 변경 적용",
      backend:  "BE 변경 적용",
      ai:       "AI 설계 적용",
      qa:       "QA 통과",
      deploy:   "PUSH 완료",
    };
    return { tone: "passed", text: map[agentId] || "OK" };
  }

  return null;
}

function AgentBubble({ tone, text }) {
  const styles = {
    running: { color: "#fde68a", border: "#fbbf2466", bg: "#0a1228" },
    failed:  { color: "#fecaca", border: "#f8717166", bg: "#1c0d12" },
    rework:  { color: "#ddd6fe", border: "#a78bfa66", bg: "#180f25" },
    passed:  { color: "#bbf7d0", border: "#34d39966", bg: "#0c1f1a" },
  }[tone] || { color: "#cbd5e1", border: "#1e293b", bg: "#0a1228" };

  return (
    <div
      className="agent-office-bubble relative max-w-[200px] rounded-2xl px-2.5 py-1.5 text-[10.5px] leading-snug"
      style={{
        backgroundColor: styles.bg,
        border: `1px solid ${styles.border}`,
        color: styles.color,
        boxShadow: `0 0 12px ${styles.border}`,
      }}
    >
      {text}
      {tone === "running" && (
        <span className="agent-office-typing ml-1 inline-flex gap-0.5 align-middle">
          <span />
          <span />
          <span />
        </span>
      )}
    </div>
  );
}

function AgentCard({ def, visual, bubble, onClick, isCurrent }) {
  const { kind, ring, label } = visual;
  const bg = VISUAL_BG[kind] || "#0a1228";
  const dim = kind === "skipped" || kind === "waiting";

  return (
    <button
      type="button"
      onClick={() => onClick && onClick(def.id)}
      className={
        "agent-office-card group relative flex w-full flex-col items-center gap-1.5 rounded-2xl px-2 py-2 text-center transition " +
        (kind === "running" ? "agent-office-bounce " : "") +
        (dim ? "opacity-60 " : "")
      }
      data-testid={`agent-card-${def.id}`}
      data-agent-id={def.id}
      style={{
        backgroundColor: bg,
        border: `1px solid ${ring}55`,
        boxShadow: isCurrent ? `0 0 16px ${ring}aa` : `0 0 6px ${ring}22`,
        cursor: "pointer",
      }}
    >
      {/* Avatar with gradient ring (Instagram story style) */}
      <span
        className="agent-office-avatar relative grid h-12 w-12 place-items-center rounded-full text-2xl"
        style={{
          background: `conic-gradient(from 0deg, ${ring}, ${def.accent}, ${ring})`,
          padding: 2,
          boxShadow:
            kind === "running" ? `0 0 14px ${ring}` : `0 0 6px ${ring}55`,
        }}
      >
        <span
          className="grid h-full w-full place-items-center rounded-full"
          style={{
            backgroundColor: "#0a1228",
            color: def.accent,
          }}
        >
          {def.emoji}
        </span>
        {kind === "passed" && (
          <span
            className="agent-office-stamp absolute -right-1 -bottom-1 grid h-5 w-5 place-items-center rounded-full text-[10px] font-bold"
            style={{
              backgroundColor: "#facc15",
              color: "#0a1228",
              border: "1px solid #facc15",
            }}
            aria-hidden
          >
            ✓
          </span>
        )}
        {kind === "failed" && (
          <span
            className="agent-office-alert absolute -right-1 -bottom-1 grid h-5 w-5 place-items-center rounded-full text-[10px] font-bold"
            style={{
              backgroundColor: "#f87171",
              color: "#0a1228",
              border: "1px solid #f87171",
            }}
            aria-hidden
          >
            !
          </span>
        )}
      </span>

      <span className="text-[10.5px] font-bold tracking-widest text-slate-100">
        {def.label}
      </span>
      <span className="text-[8.5px] tracking-widest text-slate-500">
        {def.role}
      </span>
      <span
        className="rounded-full px-1.5 py-[1px] text-[8.5px] font-bold tracking-widest"
        style={{
          color: ring,
          border: `1px solid ${ring}66`,
          backgroundColor: "#050912",
        }}
      >
        {label}
      </span>

      {bubble && (
        <div className="absolute left-1/2 top-[-6px] z-10 -translate-x-1/2 -translate-y-full">
          <AgentBubble tone={bubble.tone} text={bubble.text} />
        </div>
      )}
    </button>
  );
}

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

  const currentAgentId = useMemo(
    () => deriveCurrentAgent(meta),
    [meta],
  );

  const cards = AGENT_DEFS.map((def) => {
    const visual = deriveAgentVisual(
      def.id,
      meta,
      currentAgentId,
      factoryRunning,
    );
    const bubble = deriveBubble(def.id, visual, meta, currentAgentId);
    return { def, visual, bubble };
  });

  const planRing = cards.filter((c) => c.def.cluster === "plan");
  const buildRing = cards.filter((c) => c.def.cluster === "build");
  const shipRing = cards.filter((c) => c.def.cluster === "ship");

  const stageLabel = (() => {
    const aa = meta?.agent_accountability || {};
    if (aa.blocking_agent) return `BLOCKED · ${aa.blocking_agent.toUpperCase()}`;
    if (currentAgentId) return `CURRENT · ${currentAgentId.toUpperCase()}`;
    if (factoryRunning) return "RUNNING";
    return "IDLE";
  })();

  return (
    <section
      className="agent-office-scene relative flex flex-col gap-3 overflow-hidden rounded-2xl p-3 sm:p-4"
      data-testid="agent-office-scene"
      style={{
        background:
          "radial-gradient(ellipse at top, #15264a 0%, #0a1228 70%, #050912 100%)",
        border: "1.5px solid #0e4a3a",
        fontFamily: "ui-monospace, monospace",
      }}
    >
      {/* Header */}
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ backgroundColor: "#34d399" }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.35em] text-emerald-300">
            AGENT OFFICE
          </span>
          <span className="text-[9px] tracking-widest text-slate-500">
            8 에이전트 · 실시간
          </span>
        </div>
        <span
          className="rounded-full px-2 py-0.5 text-[9px] font-bold tracking-[0.25em]"
          style={{
            color: factoryRunning ? "#fbbf24" : "#94a3b8",
            border: `1px solid ${factoryRunning ? "#fbbf2466" : "#1e293b"}`,
            backgroundColor: "#0a1228",
          }}
        >
          {stageLabel}
        </span>
      </header>

      {/* Plan ring — 3 agents in a horizontal row, ping-pong path. */}
      <div className="agent-office-ring agent-office-ring-plan">
        <div className="mb-1 flex items-center gap-2 text-[9px] tracking-widest text-slate-500">
          <span className="h-px flex-1" style={{ backgroundColor: "#1e293b" }} />
          <span>PLAN · PM ↔ Planner ↔ Designer</span>
          <span className="h-px flex-1" style={{ backgroundColor: "#1e293b" }} />
        </div>
        <div className="grid grid-cols-3 gap-2 pt-7">
          {planRing.map(({ def, visual, bubble }) => (
            <AgentCard
              key={def.id}
              def={def}
              visual={visual}
              bubble={bubble}
              onClick={onAgentClick}
              isCurrent={
                selectedAgentId === def.id || currentAgentId === def.id
              }
            />
          ))}
        </div>
      </div>

      {/* Build ring — 3 agents */}
      <div className="agent-office-ring agent-office-ring-build">
        <div className="mb-1 flex items-center gap-2 text-[9px] tracking-widest text-slate-500">
          <span className="h-px flex-1" style={{ backgroundColor: "#1e293b" }} />
          <span>BUILD · FE / BE / AI</span>
          <span className="h-px flex-1" style={{ backgroundColor: "#1e293b" }} />
        </div>
        <div className="grid grid-cols-3 gap-2 pt-7">
          {buildRing.map(({ def, visual, bubble }) => (
            <AgentCard
              key={def.id}
              def={def}
              visual={visual}
              bubble={bubble}
              onClick={onAgentClick}
              isCurrent={
                selectedAgentId === def.id || currentAgentId === def.id
              }
            />
          ))}
        </div>
      </div>

      {/* Ship ring — 2 agents centered */}
      <div className="agent-office-ring agent-office-ring-ship">
        <div className="mb-1 flex items-center gap-2 text-[9px] tracking-widest text-slate-500">
          <span className="h-px flex-1" style={{ backgroundColor: "#1e293b" }} />
          <span>SHIP · QA → Deploy</span>
          <span className="h-px flex-1" style={{ backgroundColor: "#1e293b" }} />
        </div>
        <div className="mx-auto grid w-full max-w-md grid-cols-2 gap-2 pt-7">
          {shipRing.map(({ def, visual, bubble }) => (
            <AgentCard
              key={def.id}
              def={def}
              visual={visual}
              bubble={bubble}
              onClick={onAgentClick}
              isCurrent={
                selectedAgentId === def.id || currentAgentId === def.id
              }
            />
          ))}
        </div>
      </div>
    </section>
  );
}
