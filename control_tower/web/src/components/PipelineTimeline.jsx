// Vertical pipeline timeline — Stampport Lab 8 stages from PM to deploy.
// Reads agent statuses + the factory's current_stage to color each stage.
//
// The order mirrors the requested factory flow:
//   PM → 기획자 → 디자이너 → 프론트엔드 → 백엔드 → AI 설계자 → QA → 배포
//
// Planner-Designer ping-pong drives the early loop, deploy is the final
// stage and runs as a real agent (no longer a separate non-agent step).

const STAGES = [
  { id: "pm",            title: "PM",           agent: "pm",          icon: "🧑‍💼" },
  { id: "planner",       title: "기획자",        agent: "planner",     icon: "📝" },
  { id: "designer",      title: "디자이너",       agent: "designer",    icon: "🎨" },
  { id: "frontend",      title: "프론트엔드",     agent: "frontend",    icon: "💻" },
  { id: "backend",       title: "백엔드",        agent: "backend",     icon: "🖥️" },
  { id: "ai_architect",  title: "AI 설계자",     agent: "ai_architect",icon: "🧠" },
  { id: "qa",            title: "QA",          agent: "qa",          icon: "🔎" },
  { id: "deploy",        title: "배포 관리자",    agent: "deploy",      icon: "🚀" },
];

const STATE_TONE = {
  passed:   { dot: "bg-emerald-500", ring: "ring-emerald-500/60", text: "text-emerald-300", label: "완료" },
  running:  { dot: "bg-amber-400 animate-pulse", ring: "ring-amber-400/60", text: "text-amber-300", label: "진행 중" },
  paused:   { dot: "bg-violet-400", ring: "ring-violet-400/60", text: "text-violet-300", label: "일시정지" },
  failed:   { dot: "bg-rose-500",   ring: "ring-rose-500/60",  text: "text-rose-300",  label: "실패" },
  waiting:  { dot: "bg-slate-700",  ring: "ring-slate-700",    text: "text-slate-500", label: "대기" },
};

function stageState(stageId, factoryStatus, currentStage, agentStatuses, deployEvent) {
  // Deploy stage may surface as an agent (deploy) AND/OR via factory deploy events.
  // We respect explicit deploy events when present.
  if (stageId === "deploy") {
    if (deployEvent?.type === "deploy_completed") return "passed";
    if (deployEvent?.type === "deploy_failed") return "failed";
    if (factoryStatus === "running" && currentStage === "deploy") return "running";
    // Fall through to the agent status if the deploy agent is wired in.
  }
  const agentStatus = agentStatuses[stageId];
  if (agentStatus === "done") return "passed";
  if (agentStatus === "error" || agentStatus === "blocked") return "failed";
  if (agentStatus === "working") {
    return factoryStatus === "paused" ? "paused" : "running";
  }
  return "waiting";
}

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
    <section className="rounded-2xl border border-slate-800 bg-slate-950/70 p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[14px] font-semibold tracking-wide text-slate-100">
          Stampport 파이프라인
        </h2>
        <span className="text-[11px] tracking-wide text-slate-500">
          {STAGES.length}단계
        </span>
      </div>

      <ol className="relative space-y-2 pl-5">
        <span className="absolute left-[7px] top-1 bottom-1 w-px bg-slate-800" />
        {STAGES.map((s, i) => {
          const state = stageState(s.id, factoryStatus, currentStage, agentStatuses, deployEvent);
          const tone = STATE_TONE[state];
          return (
            <li key={s.id} className="relative">
              <span
                className={`absolute -left-[18px] top-2.5 h-3 w-3 rounded-full ring-2 ring-offset-2 ring-offset-slate-950 ${tone.dot} ${tone.ring}`}
              />
              <div
                className={`flex items-center justify-between rounded-lg border bg-slate-900/50 px-3 py-2 ${
                  state === "running"
                    ? "border-amber-400/40"
                    : state === "passed"
                    ? "border-emerald-500/30"
                    : state === "failed"
                    ? "border-rose-500/40"
                    : "border-slate-800"
                }`}
              >
                <div className="flex items-baseline gap-2">
                  <span className="text-[11px] tabular-nums text-slate-500">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="text-[14px] font-medium text-slate-100">
                    {s.icon} {s.title}
                  </span>
                </div>
                <span className={`text-[11px] tracking-wide ${tone.text}`}>
                  {tone.label}
                </span>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
