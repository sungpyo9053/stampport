import { motion } from "framer-motion";

function Stat({ label, value, accent }) {
  return (
    <div className="flex items-baseline gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-1.5">
      <span className="text-[10px] tracking-wide text-slate-400">{label}</span>
      <span className={`text-base font-semibold ${accent || "text-slate-100"}`}>
        {value}
      </span>
    </div>
  );
}

export default function HeaderStatusBar({
  agents,
  tasks,
  events,
  onRunDemo,
  isRunningDemo,
}) {
  const totalAgents = agents.length;
  const activeAgents = agents.filter((a) => a.status === "working").length;
  const completedTasks = tasks.filter((t) => t.status === "completed").length;
  const totalTasks = tasks.length;
  const eventCount = events.length;
  const currentAgent = agents.find((a) => a.status === "working");
  const progress = totalTasks
    ? Math.round((completedTasks / totalTasks) * 100)
    : 0;

  return (
    <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 bg-slate-950/85 px-6 py-3 backdrop-blur">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-sky-500/15 text-lg ring-1 ring-sky-400/40">
          🛰️
        </div>
        <div>
          <div className="text-sm font-semibold tracking-wide text-slate-100">
            Stampport Lab · 스탬포트 제작소
          </div>
          <div className="text-[11px] tracking-wide text-slate-400">
            AI Agent Studio · 기획자–디자이너 ping-pong 관제
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Stat label="진행률" value={`${progress}%`} accent="text-sky-300" />
        <Stat
          label="작업 중"
          value={`${activeAgents}/${totalAgents}`}
          accent={activeAgents ? "text-amber-300" : "text-slate-100"}
        />
        <Stat
          label="작업"
          value={`${completedTasks}/${totalTasks}`}
          accent="text-emerald-300"
        />
        <Stat label="이벤트" value={eventCount} accent="text-purple-300" />
        <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-1.5">
          <span className="text-[10px] tracking-wide text-slate-400">현재</span>
          {currentAgent ? (
            <span className="flex items-center gap-1 text-sm text-amber-200">
              <motion.span
                className="h-1.5 w-1.5 rounded-full bg-amber-300"
                animate={{ opacity: [0.3, 1, 0.3] }}
                transition={{ duration: 1, repeat: Infinity }}
              />
              {currentAgent.name}
            </span>
          ) : (
            <span className="text-sm text-slate-400">—</span>
          )}
        </div>

        <button
          onClick={onRunDemo}
          disabled={isRunningDemo}
          className={`ml-1 rounded-lg px-3 py-1.5 text-sm font-medium tracking-wide transition ${
            isRunningDemo
              ? "cursor-not-allowed bg-slate-800 text-slate-500"
              : "bg-sky-500 text-slate-950 hover:bg-sky-400 active:scale-[0.98]"
          }`}
        >
          {isRunningDemo ? "실행 중..." : "▶ 데모 실행"}
        </button>
      </div>
    </header>
  );
}
