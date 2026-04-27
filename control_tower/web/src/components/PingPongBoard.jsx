import { motion } from "framer-motion";
import { AGENTS } from "../constants/agents.js";

// Planner ↔ Designer ping-pong board. Pulls the latest planner_proposal
// and designer_critique artifact_created events out of the event stream,
// then shows the PM brief (or a placeholder) as the current cycle's
// decision. Designed as a side panel — NOT a row in a table.
//
// Default copy mirrors the example in the brief so the panel reads as a
// living office artifact even before the factory has produced anything.

const DEFAULT_PROPOSAL = {
  title: "성수동 고수 배지 추가 제안",
  preview:
    "성수동에서 카페 3곳을 5번 이상 다녀온 사람에게 ‘성수동 고수’ 배지를 부여합니다.",
};
const DEFAULT_CRITIQUE = {
  title: "그냥 배지로는 약합니다",
  preview:
    "단순 배지는 자랑하고 싶지 않다. 여권 비자처럼 보여야 한다 — 도장 + 발급 도시 + 발급일자.",
};
const DEFAULT_DECISION = {
  title: "이번 사이클 결정",
  preview:
    "Local Visa Badge + 공유 카드 개선으로 범위를 제한합니다. (PM)",
};

function pickArtifact(events, type) {
  return events
    .filter(
      (ev) => ev.type === "artifact_created" && ev.payload?.artifact_type === type,
    )
    .sort((a, b) => b.id - a.id)[0];
}

function toCard(ev, fallback) {
  if (!ev) return fallback;
  return {
    title: ev.payload?.artifact_title || ev.message || fallback.title,
    preview: ev.payload?.preview || fallback.preview,
  };
}

function Slot({ agent, kind, card, isLatest }) {
  // kind: "proposal" | "critique" | "decision"
  const verb =
    kind === "proposal"
      ? "제안"
      : kind === "critique"
      ? "반박 / 개선"
      : "결정";

  return (
    <div
      className="relative p-3"
      style={{
        backgroundColor: "#0a1228",
        border: `1.5px solid ${agent.color}55`,
        borderRadius: 4,
        boxShadow: isLatest ? `0 0 14px ${agent.color}33` : "none",
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div
        className="flex items-center justify-between text-[9px] font-bold uppercase tracking-[0.3em]"
        style={{ color: agent.color }}
      >
        <span>
          {agent.name} · {verb}
        </span>
        {isLatest && (
          <motion.span
            className="inline-block h-1.5 w-1.5"
            style={{ backgroundColor: agent.color }}
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
        )}
      </div>
      <div className="mt-1.5 text-[12.5px] font-semibold text-[#f5e9d3]">
        {card.title}
      </div>
      <div className="mt-1 text-[11px] leading-snug text-slate-400 line-clamp-3">
        {card.preview}
      </div>
    </div>
  );
}

export default function PingPongBoard({ events = [] }) {
  const planner = AGENTS.planner;
  const designer = AGENTS.designer;
  const pm = AGENTS.pm;

  const proposalEv = pickArtifact(events, "planner_proposal");
  const critiqueEv = pickArtifact(events, "designer_critique");
  const briefEv = pickArtifact(events, "product_brief");

  const proposal = toCard(proposalEv, DEFAULT_PROPOSAL);
  const critique = toCard(critiqueEv, DEFAULT_CRITIQUE);
  const decision = toCard(briefEv, DEFAULT_DECISION);

  // Whichever side spoke last is "latest" — use this to drive the
  // active glow + ping-pong arrow direction.
  const lastTalker = (() => {
    if (!proposalEv && !critiqueEv) return "planner";
    if (!critiqueEv) return "planner";
    if (!proposalEv) return "designer";
    return proposalEv.id > critiqueEv.id ? "planner" : "designer";
  })();

  return (
    <section
      className="flex flex-col gap-2 p-3"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #0e4a3a",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: "#d4a843" }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            기획자 ↔ 디자이너 PING-PONG
          </span>
        </div>
        <span className="text-[10px] tracking-wider text-slate-500">
          이번 사이클
        </span>
      </div>

      {/* PLANNER vs DESIGNER row */}
      <div className="relative grid grid-cols-2 gap-2">
        <Slot
          agent={planner}
          kind="proposal"
          card={proposal}
          isLatest={lastTalker === "planner"}
        />
        <Slot
          agent={designer}
          kind="critique"
          card={critique}
          isLatest={lastTalker === "designer"}
        />

        {/* center arrow strip */}
        <motion.div
          className="pointer-events-none absolute"
          style={{
            left: "50%",
            top: "50%",
            transform: "translate(-50%, -50%)",
            color: "#d4a843",
            fontSize: 14,
            fontWeight: 700,
            letterSpacing: 2,
            backgroundColor: "#0a1228",
            border: "1.5px solid #d4a843",
            borderRadius: 3,
            padding: "2px 6px",
            fontFamily: "ui-monospace, monospace",
          }}
          animate={{
            x: lastTalker === "planner" ? [-2, 4, -2] : [2, -4, 2],
          }}
          transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
        >
          {lastTalker === "planner" ? "→" : "←"}
        </motion.div>
      </div>

      {/* PM decision row */}
      <div className="relative">
        <div
          className="absolute left-1/2 -top-1 h-2 w-px"
          style={{ backgroundColor: "#d4a843" }}
        />
        <Slot agent={pm} kind="decision" card={decision} isLatest={!!briefEv} />
      </div>
    </section>
  );
}
