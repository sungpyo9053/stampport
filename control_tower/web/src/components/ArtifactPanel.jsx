import { AnimatePresence, motion } from "framer-motion";
import { AGENTS, ARTIFACT_TYPE_LABEL } from "../constants/agents.js";

/**
 * The backend doesn't expose /artifacts yet, so we derive the artifact list
 * from `artifact_created` events. Each one carries the title/type/preview
 * in its payload, which is exactly what we need here.
 */
const MAX_ARTIFACTS = 8;

export default function ArtifactPanel({ events }) {
  // dedupe by artifact_id (in case of any duplicate events) and keep latest 8
  const seen = new Set();
  const artifacts = events
    .filter((ev) => ev.type === "artifact_created")
    .sort((a, b) => b.id - a.id)
    .filter((ev) => {
      const key = ev.payload?.artifact_id ?? `e${ev.id}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, MAX_ARTIFACTS);

  return (
    <section className="flex h-full min-h-0 flex-col rounded-2xl border border-slate-800 bg-slate-950/70">
      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">
          산출물
        </h2>
        <span className="text-[11px] tracking-wide text-slate-500">
          산출물 {artifacts.length}개
        </span>
      </div>

      <div className="scrollbar-thin flex-1 overflow-y-auto p-2">
        {artifacts.length === 0 && (
          <div className="px-3 py-6 text-center text-sm text-slate-500">
            아직 산출물이 없습니다
          </div>
        )}

        <ul className="space-y-1.5">
          <AnimatePresence initial={false}>
            {artifacts.map((ev) => {
              const agent = ev.agent_id ? AGENTS[ev.agent_id] : null;
              const title = ev.payload?.artifact_title || ev.message;
              const rawType = ev.payload?.artifact_type || "artifact";
              const typeLabel = ARTIFACT_TYPE_LABEL[rawType] || rawType;
              const preview = ev.payload?.preview;
              return (
                <motion.li
                  key={ev.id}
                  layout
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="rounded-md border border-slate-800/70 bg-slate-900/60 px-2.5 py-1.5"
                >
                  <div className="flex items-center justify-between text-[11px] tracking-wide text-slate-400">
                    <span className="text-emerald-300">📦 {typeLabel}</span>
                    {agent && <span style={{ color: agent.color }}>{agent.name}</span>}
                  </div>
                  <div className="mt-0.5 text-[13px] font-medium text-slate-100">
                    {title}
                  </div>
                  {preview && (
                    <div className="mt-1 line-clamp-2 text-[11.5px] leading-snug text-slate-400">
                      {preview}
                    </div>
                  )}
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      </div>
    </section>
  );
}
