import { AnimatePresence, motion } from "framer-motion";
import { AGENTS } from "../constants/agents.js";

const TYPE_STYLE = {
  task_created:    { dot: "bg-sky-400",     label: "작업" },
  agent_started:   { dot: "bg-amber-400",   label: "시작" },
  agent_message:   { dot: "bg-slate-400",   label: "메시지" },
  artifact_created:{ dot: "bg-emerald-400", label: "산출물" },
  task_completed:  { dot: "bg-emerald-500", label: "완료" },
  handoff:         { dot: "bg-purple-400",  label: "전달" },
  approval_requested: { dot: "bg-purple-300", label: "승인 요청" },
  approval_granted:{ dot: "bg-emerald-300", label: "승인" },
  approval_rejected:{ dot: "bg-rose-400",   label: "반려" },
  error:           { dot: "bg-rose-500",    label: "오류" },
};

function fmtTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour12: false });
  } catch {
    return iso;
  }
}

const MAX_EVENTS = 40;

export default function EventFeed({ events }) {
  // newest first, cap at MAX_EVENTS
  const ordered = [...events]
    .sort((a, b) => b.id - a.id)
    .slice(0, MAX_EVENTS);

  return (
    <aside className="flex h-full flex-col rounded-2xl border border-slate-800 bg-slate-950/70">
      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-sky-400" />
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">
            이벤트 로그
          </h2>
        </div>
        <span className="text-[11px] tracking-wide text-slate-500">
          실시간 · 1초 갱신
        </span>
      </div>

      <div className="scrollbar-thin flex-1 overflow-y-auto px-2 py-2">
        {ordered.length === 0 && (
          <div className="px-3 py-6 text-center text-sm text-slate-500">
            아직 이벤트가 없습니다 — 상단의{" "}
            <span className="text-slate-300">▶ 데모 실행</span>을 눌러보세요.
          </div>
        )}

        <ul className="space-y-1.5">
          <AnimatePresence initial={false}>
            {ordered.map((ev) => {
              const style = TYPE_STYLE[ev.type] || { dot: "bg-slate-500", label: ev.type };
              const agent = ev.agent_id ? AGENTS[ev.agent_id] : null;
              return (
                <motion.li
                  key={ev.id}
                  layout
                  initial={{ opacity: 0, y: -6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.18 }}
                  className="rounded-md border border-slate-800/70 bg-slate-900/60 px-2.5 py-1.5"
                >
                  <div className="flex items-center justify-between text-[11px] text-slate-500">
                    <div className="flex items-center gap-1.5">
                      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
                      <span className="tracking-wide">{style.label}</span>
                      {agent && (
                        <span className="text-slate-400">
                          · <span style={{ color: agent.color }}>{agent.name}</span>
                        </span>
                      )}
                    </div>
                    <span>{fmtTime(ev.created_at)}</span>
                  </div>
                  <div className="mt-0.5 text-[12.5px] leading-snug text-slate-200">
                    {ev.message}
                  </div>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      </div>
    </aside>
  );
}
