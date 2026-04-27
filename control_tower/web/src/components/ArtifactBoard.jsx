import { AnimatePresence, motion } from "framer-motion";
import {
  AGENTS,
  ARTIFACT_TYPE_LABEL,
} from "../constants/agents.js";

// Cork-board style cycle outputs. Drawn as a grid of pinned cards rather
// than a vertical list/log. Each artifact_created event becomes a small
// note pinned to the board with an agent-colored thumbtack.
//
// We dedupe by artifact_id and surface up to 6 of the most recent
// artifacts — the goal is "what did the team produce this cycle", not a
// comprehensive log.

const MAX_CARDS = 6;

const ROLE_LABEL = {
  product_brief:    "기획",
  planner_proposal: "기획",
  designer_critique:"디자인",
  wireframe:        "디자인",
  api_spec:         "백엔드",
  frontend_code:    "프론트",
  agent_design:     "AI",
  test_cases:       "QA",
  deploy_log:       "배포",
};

function Pin({ color }) {
  return (
    <div
      className="absolute"
      style={{
        left: "50%",
        top: -6,
        transform: "translateX(-50%)",
        width: 14,
        height: 14,
        backgroundColor: color,
        borderRadius: 999,
        boxShadow: "inset -2px -2px 0 rgba(0,0,0,0.35), 0 1px 1px rgba(0,0,0,0.5)",
        zIndex: 2,
      }}
    />
  );
}

export default function ArtifactBoard({ events = [], factory = null }) {
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
    .slice(0, MAX_CARDS);

  const deployStatus = factory?.status || "idle";

  return (
    <section
      className="flex flex-col p-3"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #0e4a3a",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: "#d4a843" }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            CYCLE BOARD · 산출물
          </span>
        </div>
        <span
          className="rounded-sm px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
          style={{
            color: "#0e4a3a",
            backgroundColor: "#d4a843",
          }}
        >
          {artifacts.length} / {MAX_CARDS}
        </span>
      </div>

      {/* corkboard */}
      <div
        className="relative grid flex-1 grid-cols-2 gap-3 p-3 sm:grid-cols-3"
        style={{
          backgroundColor: "#3d2817",
          backgroundImage:
            "radial-gradient(circle at 25% 25%, rgba(212,168,67,0.06) 0%, transparent 30%), radial-gradient(circle at 75% 60%, rgba(212,168,67,0.04) 0%, transparent 35%)",
          border: "2px solid #1f1408",
          borderRadius: 4,
          minHeight: 220,
        }}
      >
        {artifacts.length === 0 && (
          <div className="col-span-full flex items-center justify-center py-8 text-center text-[12px] text-[#f5e9d3]/60">
            아직 산출물이 없습니다 — 시작 버튼을 누르면 사이클이 돌아갑니다.
          </div>
        )}

        <AnimatePresence initial={false}>
          {artifacts.map((ev, i) => {
            const agent = ev.agent_id ? AGENTS[ev.agent_id] : null;
            const title = ev.payload?.artifact_title || ev.message;
            const rawType = ev.payload?.artifact_type || "artifact";
            const typeLabel = ARTIFACT_TYPE_LABEL[rawType] || rawType;
            const role = ROLE_LABEL[rawType] || "산출";
            const preview = ev.payload?.preview;
            const tilt = ((i % 3) - 1) * 1.5; // -1.5°, 0°, 1.5°

            return (
              <motion.div
                key={ev.id}
                layout
                initial={{ opacity: 0, y: 8, scale: 0.92 }}
                animate={{ opacity: 1, y: 0, scale: 1, rotate: tilt }}
                exit={{ opacity: 0, scale: 0.85 }}
                transition={{ type: "spring", stiffness: 260, damping: 22 }}
                className="relative pt-3"
              >
                <Pin color={agent?.color || "#d4a843"} />
                <div
                  className="p-2.5"
                  style={{
                    backgroundColor: "#f5e9d3",
                    color: "#1f1408",
                    boxShadow: "2px 3px 0 rgba(0,0,0,0.35)",
                    borderRadius: 2,
                  }}
                >
                  <div className="flex items-center justify-between text-[9px] font-bold uppercase tracking-widest">
                    <span style={{ color: agent?.color || "#1f1408" }}>
                      {role}
                    </span>
                    <span className="text-[#1f1408]/60">{typeLabel}</span>
                  </div>
                  <div className="mt-1 text-[12px] font-bold leading-tight">
                    {title}
                  </div>
                  {preview && (
                    <div className="mt-1 line-clamp-3 text-[10.5px] leading-snug text-[#3d2817]">
                      {preview}
                    </div>
                  )}
                  {agent && (
                    <div className="mt-1.5 text-[9px] font-bold uppercase tracking-widest text-[#3d2817]/70">
                      by {agent.name}
                    </div>
                  )}
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>

      {/* footer — deploy status as a tiny boarding-pass strip */}
      <div
        className="mt-2 flex items-center justify-between px-2 py-1.5 text-[10px] tracking-widest"
        style={{
          backgroundColor: "#0a1228",
          border: "1px solid #0e4a3a",
          borderRadius: 3,
          color: "#f5e9d3",
        }}
      >
        <span className="font-bold text-[#d4a843]">DEPLOY</span>
        <span>{factory?.current_stage || "—"}</span>
        <span className={
          deployStatus === "running" ? "text-amber-300" :
          deployStatus === "completed" ? "text-emerald-300" :
          deployStatus === "failed" ? "text-rose-300" :
          deployStatus === "paused" ? "text-violet-300" :
          "text-slate-400"
        }>
          {deployStatus.toUpperCase()}
        </span>
      </div>
    </section>
  );
}
