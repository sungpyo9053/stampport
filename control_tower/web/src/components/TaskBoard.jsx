import { AnimatePresence, motion } from "framer-motion";
import { AGENTS, TASK_STATUS_LABEL } from "../constants/agents.js";

const STATUS_STYLE = {
  pending:     "border-slate-700 text-slate-400",
  in_progress: "border-amber-400/60 text-amber-300",
  completed:   "border-emerald-500/60 text-emerald-300",
  failed:      "border-rose-500/60 text-rose-300",
  cancelled:   "border-slate-600 text-slate-500",
};

const MAX_TASKS = 8;

export default function TaskBoard({ tasks }) {
  // newest 8 (server returns latest first; we re-sort ascending for the board)
  const ordered = [...tasks]
    .sort((a, b) => b.id - a.id)
    .slice(0, MAX_TASKS)
    .sort((a, b) => a.id - b.id);

  return (
    <section className="flex h-full min-h-0 flex-col rounded-2xl border border-slate-800 bg-slate-950/70">
      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">
          작업 보드
        </h2>
        <span className="text-[11px] tracking-wide text-slate-500">
          작업 {ordered.length}개
        </span>
      </div>

      <div className="scrollbar-thin flex-1 overflow-y-auto p-2">
        {ordered.length === 0 && (
          <div className="px-3 py-6 text-center text-sm text-slate-500">
            아직 작업이 없습니다
          </div>
        )}
        <ul className="space-y-1.5">
          <AnimatePresence initial={false}>
            {ordered.map((t) => {
              const style = STATUS_STYLE[t.status] || STATUS_STYLE.pending;
              const agent = t.agent_id ? AGENTS[t.agent_id] : null;
              const statusLabel = TASK_STATUS_LABEL[t.status] || t.status;
              return (
                <motion.li
                  key={t.id}
                  layout
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className={`rounded-md border bg-slate-900/60 px-2.5 py-1.5 ${style}`}
                >
                  <div className="flex items-center justify-between text-[11px] tracking-wide">
                    <span>#{t.id} · {statusLabel}</span>
                    {agent && (
                      <span style={{ color: agent.color }}>{agent.name}</span>
                    )}
                  </div>
                  <div className="mt-0.5 text-[12.5px] leading-snug text-slate-100">
                    {t.title}
                  </div>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      </div>
    </section>
  );
}
