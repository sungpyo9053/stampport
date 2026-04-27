import { useEffect, useRef, useState } from "react";
import { AnimatePresence } from "framer-motion";
import OfficeBackground from "./OfficeBackground.jsx";
import Workstation from "./Workstation.jsx";
import SpeechBubble from "./SpeechBubble.jsx";
import HandoffCourier from "./HandoffCourier.jsx";
import {
  AGENT_LIST,
  AGENTS,
  OFFICE_HEIGHT,
  OFFICE_WIDTH,
  getAgent,
} from "../constants/agents.js";

/**
 * Miniature office diorama.
 *
 * Internal coords are a fixed 760x620 stage; the host div is uniformly
 * CSS-scaled to fit whatever space the page hands us. Every absolute
 * child (background, workstations, speech bubbles, handoff couriers)
 * shares the same coordinate space, so handing off from PM (120,130)
 * to Designer (120,320) just means walking that delta on screen.
 */
export default function AgentOffice({
  agentStatuses,
  bubbles,
  handoffs,
  onHandoffDone,
  activeAgentId,
}) {
  const hostRef = useRef(null);
  const [scale, setScale] = useState(1);

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;

    const recompute = () => {
      const { width, height } = el.getBoundingClientRect();
      if (!width || !height) return;
      const next = Math.min(width / OFFICE_WIDTH, height / OFFICE_HEIGHT);
      if (Number.isFinite(next) && next > 0) {
        setScale(next);
      }
    };

    recompute();
    const ro = new ResizeObserver(recompute);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const renderable = AGENT_LIST;
  const fallbackVisible = renderable.length !== 8;

  return (
    <div className="relative h-full w-full min-h-[650px] overflow-hidden rounded-2xl border-2 border-slate-700/80 bg-gradient-to-br from-slate-800 via-slate-900 to-slate-950 shadow-[0_0_60px_rgba(56,189,248,0.2)]">
      {/* debug label */}
      <div className="pointer-events-none absolute left-4 top-4 z-40 flex items-center gap-2 rounded-md bg-slate-950/80 px-2.5 py-1 text-[11px] tracking-wide text-sky-300 ring-1 ring-sky-400/40 backdrop-blur">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />
        스탬포트 제작소 · AI 오피스 시뮬레이션
      </div>

      {/* scale host fills the wrapper */}
      <div ref={hostRef} className="absolute inset-0">
        {/* fixed-coordinate stage, scaled to fit */}
        <div
          className="absolute"
          style={{
            left: "50%",
            top: "50%",
            width: OFFICE_WIDTH,
            height: OFFICE_HEIGHT,
            transform: `translate(-50%, -50%) scale(${scale})`,
            transformOrigin: "center center",
          }}
        >
          {/* layer 0: room */}
          <OfficeBackground />

          {/* layer 1: workstations (each a self-contained desk + character pod) */}
          {renderable.map((agent) => (
            <Workstation
              key={agent.id}
              agent={agent}
              status={agentStatuses[agent.id] || "idle"}
              isActive={activeAgentId === agent.id}
            />
          ))}

          {/* layer 2: speech bubbles above heads */}
          <AnimatePresence>
            {Object.entries(bubbles).map(([agentId, bubble]) => {
              const agent = getAgent(agentId);
              if (!agent) return null;
              return (
                <SpeechBubble
                  key={`${agentId}-${bubble.id}`}
                  agent={agent}
                  message={bubble.message}
                />
              );
            })}
          </AnimatePresence>

          {/* layer 3: little couriers walking artifacts between desks */}
          <AnimatePresence>
            {handoffs.map((h) => {
              const from = AGENTS[h.from];
              const to = AGENTS[h.to];
              if (!from || !to) return null;
              return (
                <HandoffCourier
                  key={h.id}
                  from={{ x: from.x, y: from.y }}
                  to={{ x: to.x, y: to.y }}
                  fromAgent={from}
                  artifactType={from.artifactType}
                  onDone={() => onHandoffDone(h.id)}
                />
              );
            })}
          </AnimatePresence>
        </div>
      </div>

      {fallbackVisible && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-slate-950/85 text-center text-sm text-rose-300">
          ⚠ 캐릭터를 그릴 수 없습니다 — <code className="mx-1 rounded bg-slate-800 px-1">src/constants/agents.js</code> 를 확인해주세요.
        </div>
      )}
    </div>
  );
}
