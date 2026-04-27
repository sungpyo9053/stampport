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

// Pick the first runner whose heartbeat carries a live ping-pong
// block. We prefer "online" over "busy" so we don't echo a runner
// that's mid-publish, but we accept either: a busy runner is still
// the one driving the cycle.
function pickPingPongMeta(runners = []) {
  const ranked = [...runners].sort((a, b) => {
    const order = (s) => (s === "online" ? 0 : s === "busy" ? 1 : 2);
    return order(a?.status) - order(b?.status);
  });
  for (const r of ranked) {
    const pp = r?.metadata_json?.local_factory?.ping_pong;
    if (pp && pp.enabled) return pp;
  }
  return null;
}

function toCard({ pp, ev, stepKey, fallback }) {
  // Source of truth precedence:
  //   1. live ping-pong block in heartbeat metadata (real cycle)
  //   2. demo workflow's artifact_created event
  //   3. hard-coded fallback copy so the panel stays alive on a
  //      fresh page load.
  if (pp) {
    const previewKey = `${stepKey}_preview`;
    const existsKey  = `${stepKey}_exists`;
    if (pp[existsKey] && pp[previewKey]) {
      // Title pulls from the cycle artifacts when available; we don't
      // store a title server-side so reuse the fallback title which
      // already names the step.
      const selected = pp.planner_revision_selected_feature;
      const title =
        stepKey === "planner_revision" && selected
          ? `기획자 수정안 — ${selected}`
          : stepKey === "pm_decision" && pp.pm_decision_message
          ? `PM 결정 — ${pp.pm_decision_message}`
          : fallback.title;
      return {
        title,
        preview: pp[previewKey],
        isLive: true,
        source: "cycle",
      };
    }
  }
  if (ev) {
    return {
      title: ev.payload?.artifact_title || ev.message || fallback.title,
      preview: ev.payload?.preview || fallback.preview,
      isLive: true,
      source: "demo",
    };
  }
  return { ...fallback, isLive: false, source: "fallback" };
}

// Map cycle-stage statuses (from heartbeat metadata.local_factory.ping_pong)
// into a small label + color the StepCard renders next to the agent name.
const STATUS_TAG = {
  generated: { text: "완료",  color: "#34d399" },
  running:   { text: "실행",  color: "#fbbf24" },
  failed:    { text: "실패",  color: "#f87171" },
  skipped:   { text: "스킵",  color: "#94a3b8" },
};

function StepCard({ agent, verb, card, status, isLatest }) {
  const tag = STATUS_TAG[status] || null;
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
        className="flex items-center justify-between gap-2 text-[9px] font-bold uppercase tracking-[0.3em]"
        style={{ color: agent.color }}
      >
        <span>
          {agent.name} · {verb}
        </span>
        <div className="flex items-center gap-1.5">
          {tag && card.source === "cycle" && (
            <span
              className="rounded px-1.5 py-0.5 text-[8.5px]"
              style={{
                color: tag.color,
                border: `1px solid ${tag.color}66`,
                backgroundColor: "#0a1228",
              }}
            >
              {tag.text}
            </span>
          )}
          {isLatest && (
            <motion.span
              className="inline-block h-1.5 w-1.5"
              style={{ backgroundColor: agent.color }}
              animate={{ opacity: [0.3, 1, 0.3] }}
              transition={{ duration: 1.2, repeat: Infinity }}
            />
          )}
          {card.source === "demo" && (
            <span className="text-[8.5px] tracking-widest text-sky-400">
              데모
            </span>
          )}
          {card.source === "fallback" && (
            <span className="text-[8.5px] tracking-widest text-slate-500">
              (기본값)
            </span>
          )}
        </div>
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

// Map a runner-side rework axis id back to the human prose the
// docs/agent-collaboration.md gate uses. Both the helper here and
// the local recompute below produce the same labels so the panel
// reads consistently regardless of which source is authoritative.
const REWORK_LABEL = {
  visual_desire:   "디자이너 재작업",
  share:           "공유 카드 개선 필요",
  revisit:         "기획자 재작업",
  total_below_24:  "총점 미달",
  no_score:        "점수 미수신",
};

function DesireScorecard({ pp, scoreEv, decisionEv }) {
  // Source order:
  //   1. live runner heartbeat block (cycle.py output, authoritative)
  //   2. demo's desire_scorecard event payload
  //   3. demo's pm_decision payload (fallback for older demo runs)
  //   4. hard-coded defaults so the panel renders on a fresh page.
  const liveCard = pp?.desire_scorecard || null;
  const eventScores =
    scoreEv?.payload?.scores ||
    decisionEv?.payload?.scores ||
    null;

  let scores, total, shipReady, reworkAxes, source;
  if (liveCard && Object.keys(liveCard.scores || {}).length > 0) {
    scores = liveCard.scores;
    total = liveCard.total || Object.values(scores).reduce((a, n) => a + (Number(n) || 0), 0);
    shipReady = !!liveCard.ship_ready;
    reworkAxes = liveCard.rework || [];
    source = "cycle";
  } else if (eventScores) {
    scores = eventScores;
    total = Object.values(scores).reduce((a, n) => a + (Number(n) || 0), 0);
    // The demo event payloads don't pre-compute the gate, so we
    // recompute it here using the same thresholds the factory uses.
    const v = scores.visual_desire ?? 0;
    const s = scores.share ?? 0;
    const r = scores.revisit ?? 0;
    const axes = [];
    if (v < 4) axes.push("visual_desire");
    if (s <= 3) axes.push("share");
    if (r <= 3) axes.push("revisit");
    if (total < 24) axes.push("total_below_24");
    shipReady = total >= 24 && axes.length === 0;
    reworkAxes = axes;
    source = "demo";
  } else {
    scores = DEFAULT_SCORES;
    total = Object.values(scores).reduce((a, n) => a + (Number(n) || 0), 0);
    shipReady = true;
    reworkAxes = [];
    source = "fallback";
  }

  const reworkLabels = reworkAxes.map((id) => REWORK_LABEL[id] || id);

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
          {source === "fallback" && (
            <span className="text-[8.5px] tracking-widest text-slate-500">
              (기본값)
            </span>
          )}
          {source === "demo" && (
            <span className="text-[8.5px] tracking-widest text-sky-400">
              데모
            </span>
          )}
          {source === "cycle" && (
            <span className="text-[8.5px] tracking-widest text-emerald-300">
              CYCLE LIVE
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] tracking-widest text-slate-300">
            총점{" "}
            <span
              className="text-[12px] font-bold"
              style={{ color: total >= 24 ? "#34d399" : "#f87171" }}
            >
              {total}
            </span>
            <span className="text-slate-500"> / 30</span>
          </span>
          {shipReady ? (
            <Pill color="#34d399">출하 가능</Pill>
          ) : (
            <Pill color="#fb923c">재작업 필요</Pill>
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

export default function PingPongBoard({ events = [], runners = [] }) {
  // Live cycle data wins over demo/fallback. The block is null when
  // no runner heartbeat carries `ping_pong.enabled = true`, in which
  // case the panel falls through to artifact_created events.
  const pp = pickPingPongMeta(runners);

  // Lookup each step's latest event in the event stream.
  const stepEvents = STEPS.map((step) => ({
    step,
    ev: pickArtifact(events, step.key),
  }));
  const scorecardEv = pickArtifact(events, "desire_scorecard");
  const decisionEv = stepEvents.find((s) => s.step.key === "pm_decision")?.ev;

  // Per-step status derived from runner metadata so the StepCard can
  // render a status pill ("완료" / "실행" / "실패" / "스킵"). The
  // planner_proposal step doesn't have its own runner-side status
  // field — it inherits the upstream product_planner status.
  const stepStatusOf = (key) => {
    if (!pp) return null;
    if (key === "planner_proposal") {
      return pp.planner_proposal_exists ? "generated" : null;
    }
    return pp[`${key}_status`] || null;
  };

  // "latest talker" — prefer the cycle data (newest stage in pp), fall
  // back to events. We pick whichever step has the most recent
  // generated_at timestamp (stage statuses are generated/skipped/failed).
  const latestStep = (() => {
    if (pp) {
      const order = [
        "pm_decision", "designer_final_review", "planner_revision",
        "designer_critique", "planner_proposal",
      ];
      for (const k of order) {
        if (k === "planner_proposal" && pp.planner_proposal_exists) return k;
        if (pp[`${k}_status`] === "generated") return k;
      }
    }
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
        <span className="flex items-center gap-1.5 text-[10px] tracking-wider text-slate-500">
          {pp ? (
            <span className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-emerald-300"
              style={{ border: "1px solid #34d39955", backgroundColor: "#0a1228" }}>
              CYCLE LIVE
            </span>
          ) : (
            <span className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-sky-400"
              style={{ border: "1px solid #38bdf855", backgroundColor: "#0a1228" }}>
              데모 / 기본값
            </span>
          )}
          <span>5단계 + 욕구 점수표</span>
        </span>
      </div>

      {/* The 5 ping-pong steps stacked vertically. Two-column on wide
          panels, single column on narrow side rails. */}
      <div className="grid gap-2">
        {stepEvents.map(({ step, ev }) => {
          const agent = AGENTS[step.agentId];
          const card = toCard({
            pp,
            ev,
            stepKey: step.key,
            fallback: step.fallback,
          });
          return (
            <StepCard
              key={step.key}
              agent={agent}
              verb={step.verb}
              card={card}
              status={stepStatusOf(step.key)}
              isLatest={latestStep === step.key}
            />
          );
        })}
      </div>

      {/* Desire scorecard — drives the shipment gate. */}
      <DesireScorecard pp={pp} scoreEv={scorecardEv} decisionEv={decisionEv} />
    </section>
  );
}
