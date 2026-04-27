// Mini pipeline chip — a thin horizontal strip of 8 stage dots above the
// pixel office. Pipeline status is *secondary* information here; the
// office stage itself shows where work is happening. This file used to
// host a full vertical 8-step list and that is intentionally gone.

const STAGES = [
  { id: "pm",            title: "PM",         icon: "🧑‍💼" },
  { id: "planner",       title: "기획",       icon: "📝" },
  { id: "designer",      title: "디자인",     icon: "🎨" },
  { id: "frontend",      title: "FE",         icon: "💻" },
  { id: "backend",       title: "BE",         icon: "🖥️" },
  { id: "ai_architect",  title: "AI",         icon: "🧠" },
  { id: "qa",            title: "QA",         icon: "🔎" },
  { id: "deploy",        title: "배포",       icon: "🚀" },
];

function stageState(stageId, factoryStatus, currentStage, agentStatuses, deployEvent) {
  if (stageId === "deploy") {
    if (deployEvent?.type === "deploy_completed") return "passed";
    if (deployEvent?.type === "deploy_failed") return "failed";
    if (factoryStatus === "running" && currentStage === "deploy") return "running";
  }
  const agentStatus = agentStatuses[stageId];
  if (agentStatus === "done") return "passed";
  if (agentStatus === "error" || agentStatus === "blocked") return "failed";
  if (agentStatus === "working") {
    return factoryStatus === "paused" ? "paused" : "running";
  }
  if (currentStage === stageId && factoryStatus === "running") return "running";
  return "waiting";
}

const TONE = {
  passed:  { dot: "#34d399", text: "#34d399" },
  running: { dot: "#fbbf24", text: "#fbbf24" },
  paused:  { dot: "#a78bfa", text: "#a78bfa" },
  failed:  { dot: "#f87171", text: "#f87171" },
  waiting: { dot: "#1a2540", text: "#475569" },
};

export default function PipelineTimeline({
  factory,
  agentStatuses = {},
  factoryEvents = [],
}) {
  const factoryStatus = factory?.status || "idle";
  const currentStage = factory?.current_stage;
  const deployEvent = factoryEvents.find(
    (e) => e.type === "deploy_completed" || e.type === "deploy_failed",
  );

  return (
    <div
      className="flex w-full flex-wrap items-center gap-2 px-3 py-2"
      style={{
        backgroundColor: "#0a1228",
        border: "1px solid #0e4a3a",
        borderRadius: 4,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <span className="text-[9px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
        PIPELINE
      </span>

      <div className="flex flex-1 flex-wrap items-center gap-1.5">
        {STAGES.map((s, i) => {
          const state = stageState(
            s.id,
            factoryStatus,
            currentStage,
            agentStatuses,
            deployEvent,
          );
          const tone = TONE[state];
          const isLast = i === STAGES.length - 1;

          return (
            <div key={s.id} className="flex items-center gap-1.5">
              <div
                className="flex items-center gap-1 px-1.5 py-0.5"
                style={{
                  backgroundColor:
                    state === "running" ? `${tone.dot}22` : "transparent",
                  border: `1px solid ${state === "running" ? tone.dot : "#0e4a3a"}`,
                  borderRadius: 2,
                }}
              >
                <span
                  className="inline-block h-1.5 w-1.5"
                  style={{
                    backgroundColor: tone.dot,
                    animation:
                      state === "running"
                        ? "pulse 1.2s infinite ease-in-out"
                        : "none",
                  }}
                />
                <span
                  className="text-[10px] font-bold tracking-wider"
                  style={{ color: tone.text }}
                >
                  {s.title}
                </span>
              </div>
              {!isLast && (
                <span className="text-[10px] text-[#0e4a3a]">·</span>
              )}
            </div>
          );
        })}
      </div>

      <span className="text-[9px] tracking-wider text-slate-500">
        {STAGES.length}단계
      </span>
    </div>
  );
}
