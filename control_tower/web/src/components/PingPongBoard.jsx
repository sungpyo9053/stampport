import { motion } from "framer-motion";
import { AGENTS } from "../constants/agents.js";

// Planner ↔ Designer ping-pong board. Surfaces the five-step protocol
// the docs/agent-collaboration.md describes:
//
//   1. 기획자 원안     (planner_proposal)
//   2. 디자이너 반박   (designer_critique)
//   3. 기획자 수정안   (planner_revision)
//   4. 디자이너 재평가 (designer_final_review)
//   5. PM 결정         (pm_decision)
//
// Plus a 6-axis desire scorecard panel (desire_scorecard) showing
// Collection / Share / Progression / Rarity / Revisit / Visual Desire,
// which the cycle.py shipment gate consumes.
//
// Defaults mirror the brief copy so the panel reads as a living office
// artifact even before the factory has produced anything.

const STEPS = [
  {
    key: "planner_proposal",
    agentId: "planner",
    verb: "원안",
    fallback: {
      title: "신규 장치 후보 v0.1 (원안)",
      preview:
        "후보1 Local Visa 뱃지(수집+과시), 후보2 Taste Title 진화(성장+과시), " +
        "후보3 Passport 빈 슬롯(희소+재방문). 각 후보에 핵심 루프와 디자이너 질문 포함.",
    },
  },
  {
    key: "designer_critique",
    agentId: "designer",
    verb: "반박",
    fallback: {
      title: "디자이너 감성 비판 v0.1",
      preview:
        "그냥 배지로는 약함. 여권 비자처럼 도장+발급도시+발급일자가 보여야 자랑하고 싶어진다. " +
        "빈 슬롯도 회색 사각형이 아니라 발급 대기 도장 자국으로 다시 그려야 함.",
    },
  },
  {
    key: "planner_revision",
    agentId: "planner",
    verb: "수정안",
    fallback: {
      title: "기획자 수정안 v0.1",
      preview:
        "Local Visa 뱃지 1개로 좁힘. 수집욕+과시욕+희소성을 모두 자극. " +
        "MVP: 비자 카드 + 발급일/도시 슬롯 + 공유 카드 도장 진화.",
    },
  },
  {
    key: "designer_final_review",
    agentId: "designer",
    verb: "최종 평가",
    fallback: {
      title: "디자이너 최종 평가 v0.1",
      preview:
        "Visual Desire 5 / Share 4 / Revisit 4 — ship 통과. 단 발급일 폰트는 더 두껍게.",
    },
  },
  {
    key: "pm_decision",
    agentId: "pm",
    verb: "PM 결정",
    fallback: {
      title: "PM 출하 결정 v0.1",
      preview:
        "총점 26/30, ship 결정. 출하 단위: Local Visa 뱃지 컴포넌트 + 공유 카드 도장 진화.",
    },
  },
];

const SCORE_AXES = [
  { id: "collection",    ko: "Collection",    icon: "▣" },
  { id: "share",         ko: "Share",         icon: "↗" },
  { id: "progression",   ko: "Progression",   icon: "↑" },
  { id: "rarity",        ko: "Rarity",        icon: "✦" },
  { id: "revisit",       ko: "Revisit",       icon: "↻" },
  { id: "visual_desire", ko: "Visual Desire", icon: "♥" },
];

// Default placeholder scores so the panel renders even before the
// factory has produced a real designer_final_review.
const DEFAULT_SCORES = {
  collection: 5,
  share: 4,
  progression: 4,
  rarity: 4,
  revisit: 4,
  visual_desire: 5,
};

function pickArtifact(events, type) {
  return events
    .filter(
      (ev) => ev.type === "artifact_created" && ev.payload?.artifact_type === type,
    )
    .sort((a, b) => b.id - a.id)[0];
}

function toCard(ev, fallback) {
  if (!ev) return { ...fallback, isLive: false };
  return {
    title: ev.payload?.artifact_title || ev.message || fallback.title,
    preview: ev.payload?.preview || fallback.preview,
    isLive: true,
  };
}

function StepCard({ agent, verb, card, isLatest }) {
  return (
    <div
      className="relative p-2.5"
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
        {!card.isLive && (
          <span className="text-[8.5px] tracking-widest text-slate-500">
            (기본값)
          </span>
        )}
      </div>
      <div className="mt-1 text-[12px] font-semibold text-[#f5e9d3] line-clamp-1">
        {card.title}
      </div>
      <div className="mt-1 text-[10.5px] leading-snug text-slate-400 line-clamp-3">
        {card.preview}
      </div>
    </div>
  );
}

function Pill({ children, color }) {
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest"
      style={{
        color,
        border: `1px solid ${color}66`,
        backgroundColor: "#0a1228",
      }}
    >
      {children}
    </span>
  );
}

function ScoreCell({ axis, score, threshold }) {
  const v = typeof score === "number" ? score : null;
  const fail = v !== null && v < threshold;
  const color =
    v === null   ? "#475569" :
    v >= 5       ? "#34d399" :
    v >= 4       ? "#facc15" :
    v >= 3       ? "#fb923c" :
    "#f87171";
  return (
    <div
      className="flex flex-col items-center justify-center rounded p-1.5"
      style={{
        backgroundColor: "#0a1228",
        border: `1px solid ${color}55`,
      }}
    >
      <div className="text-[9px] tracking-widest text-slate-400">
        {axis.icon} {axis.ko}
      </div>
      <div
        className="mt-0.5 text-[16px] font-bold leading-none"
        style={{ color }}
      >
        {v ?? "—"}
      </div>
      <div className="mt-0.5 text-[8.5px] tracking-widest text-slate-500">
        / 5
      </div>
      {fail && (
        <div className="mt-0.5 text-[8.5px] tracking-widest text-rose-400">
          GATE FAIL
        </div>
      )}
    </div>
  );
}

function DesireScorecard({ scoreEv, decisionEv }) {
  // Source order: live event payload → live decision payload → default.
  const liveScores =
    scoreEv?.payload?.scores ||
    decisionEv?.payload?.scores ||
    null;
  const scores = liveScores || DEFAULT_SCORES;
  const total = Object.values(scores).reduce((acc, n) => acc + (Number(n) || 0), 0);
  const isLive = !!liveScores;

  // Threshold gate (mirrors docs/agent-collaboration.md).
  const visualOk = (scores.visual_desire ?? 0) >= 4;
  const shareOk = (scores.share ?? 0) >= 4;
  const revisitOk = (scores.revisit ?? 0) >= 4;
  const totalOk = total >= 24;
  const shipReady = totalOk && visualOk && shareOk && revisitOk;

  // Per-axis re-work hints map onto the documented rules so the
  // dashboard surfaces the same prose the factory uses.
  const reworkLabels = [];
  if (!totalOk) reworkLabels.push("총점 미달");
  if (!visualOk) reworkLabels.push("디자이너 재작업");
  if (!shareOk) reworkLabels.push("공유 카드 개선");
  if (!revisitOk) reworkLabels.push("기획자 재작업");

  return (
    <div
      className="p-2.5"
      style={{
        backgroundColor: "#0a1228",
        border: "1.5px solid #d4a84355",
        borderRadius: 4,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="mb-1.5 flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
          <span>욕구 점수표</span>
          {!isLive && (
            <span className="text-[8.5px] tracking-widest text-slate-500">
              (기본값)
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] tracking-widest text-slate-300">
            총점{" "}
            <span
              className="text-[12px] font-bold"
              style={{ color: totalOk ? "#34d399" : "#f87171" }}
            >
              {total}
            </span>
            <span className="text-slate-500"> / 30</span>
          </span>
          {shipReady ? (
            <Pill color="#34d399">SHIP</Pill>
          ) : (
            <Pill color="#fb923c">HOLD</Pill>
          )}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-6">
        {SCORE_AXES.map((axis) => (
          <ScoreCell
            key={axis.id}
            axis={axis}
            score={scores[axis.id]}
            threshold={
              axis.id === "visual_desire" ? 4 :
              axis.id === "share"         ? 4 :
              axis.id === "revisit"       ? 4 :
              1   // collection / progression / rarity have no per-axis floor
            }
          />
        ))}
      </div>

      {reworkLabels.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] tracking-widest text-amber-300">
          ▸ 재작업 필요:
          {reworkLabels.map((l) => (
            <Pill key={l} color="#fbbf24">{l}</Pill>
          ))}
        </div>
      )}
    </div>
  );
}

export default function PingPongBoard({ events = [] }) {
  // Lookup each step's latest event in the event stream.
  const stepEvents = STEPS.map((step) => ({
    step,
    ev: pickArtifact(events, step.key),
  }));
  const scorecardEv = pickArtifact(events, "desire_scorecard");
  const decisionEv = stepEvents.find((s) => s.step.key === "pm_decision")?.ev;

  // "latest talker" = whichever step's event is most recent.
  const latestStep = (() => {
    let best = null;
    for (const s of stepEvents) {
      if (!s.ev) continue;
      if (!best || s.ev.id > best.ev.id) best = s;
    }
    return best?.step?.key || "planner_proposal";
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
          5단계 + 욕구 점수표
        </span>
      </div>

      {/* The 5 ping-pong steps stacked vertically. Two-column on wide
          panels, single column on narrow side rails. */}
      <div className="grid gap-2">
        {stepEvents.map(({ step, ev }) => {
          const agent = AGENTS[step.agentId];
          const card = toCard(ev, step.fallback);
          return (
            <StepCard
              key={step.key}
              agent={agent}
              verb={step.verb}
              card={card}
              isLatest={latestStep === step.key}
            />
          );
        })}
      </div>

      {/* Desire scorecard — drives the shipment gate. */}
      <DesireScorecard scoreEv={scorecardEv} decisionEv={decisionEv} />
    </section>
  );
}
