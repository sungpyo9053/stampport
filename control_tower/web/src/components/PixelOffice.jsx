import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence } from "framer-motion";
import AgentDesk from "./AgentDesk.jsx";
import SpeechBubble from "./SpeechBubble.jsx";
import AgentCourierLayer from "./AgentCourierLayer.jsx";
import AgentPresenceLayer from "./AgentPresenceLayer.jsx";
import {
  AGENTS,
  AGENT_LIST,
  DEFAULT_BUBBLES,
  DESK_LAYOUT,
  PIXEL_OFFICE_HEIGHT,
  PIXEL_OFFICE_WIDTH,
} from "../constants/agents.js";

// Pixel-art office floor + walls. Drawn as crisp rectangles in a single
// SVG so the wood grain and brick lines stay sharp at any zoom level.
function OfficeRoom() {
  const W = PIXEL_OFFICE_WIDTH;
  const H = PIXEL_OFFICE_HEIGHT;
  const wallH = 200;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width={W}
      height={H}
      shapeRendering="crispEdges"
      className="absolute inset-0"
      preserveAspectRatio="none"
    >
      {/* WALL — deep navy */}
      <rect x="0" y="0" width={W} height={wallH} fill="#0a1228" />
      <rect x="0" y="0" width={W} height={wallH} fill="url(#wall-stripes)" />

      <defs>
        <pattern id="wall-stripes" x="0" y="0" width="40" height="40" patternUnits="userSpaceOnUse">
          <rect x="0" y="0" width="40" height="40" fill="#0a1228" />
          <rect x="0" y="0" width="40" height="1" fill="#1a2540" opacity="0.4" />
          <rect x="0" y="0" width="1"  height="40" fill="#1a2540" opacity="0.4" />
        </pattern>
        <pattern id="wood-floor" x="0" y="0" width="120" height="40" patternUnits="userSpaceOnUse">
          <rect x="0"   y="0"  width="120" height="40" fill="#5c3a1f" />
          <rect x="0"   y="0"  width="120" height="2"  fill="#3d2817" />
          <rect x="0"   y="20" width="120" height="2"  fill="#3d2817" />
          <rect x="0"   y="0"  width="2"   height="40" fill="#3d2817" />
          <rect x="60"  y="0"  width="2"   height="20" fill="#3d2817" />
          <rect x="40"  y="20" width="2"   height="20" fill="#3d2817" />
          <rect x="90"  y="20" width="2"   height="20" fill="#3d2817" />
          <rect x="14"  y="6"  width="40"  height="1"  fill="#7a4f2a" opacity="0.6" />
          <rect x="74"  y="26" width="30"  height="1"  fill="#7a4f2a" opacity="0.6" />
        </pattern>
      </defs>

      {/* FLOOR — warm wood */}
      <rect x="0" y={wallH} width={W} height={H - wallH} fill="url(#wood-floor)" />

      {/* baseboard */}
      <rect x="0" y={wallH - 4} width={W} height="4" fill="#1f1408" />
      <rect x="0" y={wallH - 6} width={W} height="2" fill="#0e4a3a" />

      {/* window 1 — left */}
      <g>
        <rect x="60"  y="30" width="180" height="120" fill="#162035" />
        <rect x="64"  y="34" width="172" height="112" fill="#1f3a5c" />
        <rect x="64"  y="34" width="172" height="2"   fill="#0a1228" />
        <rect x="148" y="34" width="2"   height="112" fill="#0a1228" />
        <rect x="64"  y="88" width="172" height="2"   fill="#0a1228" />
        <rect x="64"  y="34" width="172" height="40"  fill="#7dd3fc" opacity="0.18" />
        {/* faint stars / city lights */}
        <rect x="80"  y="50" width="2" height="2" fill="#fef3c7" />
        <rect x="120" y="60" width="2" height="2" fill="#fef3c7" />
        <rect x="180" y="46" width="2" height="2" fill="#fef3c7" />
        <rect x="208" y="68" width="2" height="2" fill="#fef3c7" />
      </g>

      {/* HQ / mission board — center wall */}
      <g>
        <rect x={W / 2 - 130} y="20" width="260" height="120" fill="#0e2818" />
        <rect x={W / 2 - 130} y="20" width="260" height="120" fill="none" stroke="#d4a843" strokeWidth="2" />
        <rect x={W / 2 - 126} y="24" width="252" height="112" fill="none" stroke="#0e4a3a" strokeWidth="1" />

        <text
          x={W / 2}
          y="58"
          textAnchor="middle"
          fontSize="20"
          fontWeight="700"
          fill="#d4a843"
          letterSpacing="6"
          fontFamily="ui-monospace, monospace"
        >
          STAMPPORT HQ
        </text>
        <text
          x={W / 2}
          y="84"
          textAnchor="middle"
          fontSize="11"
          fill="#f5e9d3"
          letterSpacing="3"
          fontFamily="ui-monospace, monospace"
        >
          오늘 다녀온 곳, 스탬포트에 도장 찍기
        </text>

        {/* mission stamps */}
        <g transform={`translate(${W / 2 - 100}, 96)`}>
          {["여권", "스탬프", "배지", "퀘스트", "킥포인트"].map((label, i) => (
            <g key={label} transform={`translate(${i * 42}, 0)`}>
              <rect x="0" y="0" width="36" height="32" fill="none" stroke="#d4a843" strokeWidth="1.5" />
              <rect x="2" y="2" width="32" height="28" fill="#0a1228" />
              <text
                x="18"
                y="20"
                textAnchor="middle"
                fontSize="9"
                fill="#d4a843"
                fontFamily="ui-monospace, monospace"
              >
                {label}
              </text>
            </g>
          ))}
        </g>
      </g>

      {/* window 2 — right */}
      <g>
        <rect x={W - 240} y="30"  width="180" height="120" fill="#162035" />
        <rect x={W - 236} y="34"  width="172" height="112" fill="#1f3a5c" />
        <rect x={W - 236} y="34"  width="172" height="2"   fill="#0a1228" />
        <rect x={W - 152} y="34"  width="2"   height="112" fill="#0a1228" />
        <rect x={W - 236} y="88"  width="172" height="2"   fill="#0a1228" />
        <rect x={W - 236} y="34"  width="172" height="40"  fill="#7dd3fc" opacity="0.18" />
        <rect x={W - 220} y="50"  width="2" height="2" fill="#fef3c7" />
        <rect x={W - 180} y="56"  width="2" height="2" fill="#fef3c7" />
        <rect x={W - 130} y="68"  width="2" height="2" fill="#fef3c7" />
      </g>

      {/* potted plant — bottom left */}
      <g transform={`translate(40, ${H - 90})`}>
        <rect x="0"  y="36" width="36" height="6" fill="rgba(0,0,0,0.5)" />
        <rect x="4"  y="20" width="28" height="20" fill="#7c2d12" />
        <rect x="4"  y="20" width="28" height="2"  fill="#9a3412" />
        <rect x="6"  y="0"  width="6"  height="22" fill="#0e4a3a" />
        <rect x="14" y="-6" width="8"  height="28" fill="#0e4a3a" />
        <rect x="22" y="2"  width="6"  height="20" fill="#16a34a" />
        <rect x="10" y="-2" width="4"  height="6"  fill="#16a34a" />
      </g>

      {/* potted plant — bottom right */}
      <g transform={`translate(${W - 76}, ${H - 90})`}>
        <rect x="0"  y="36" width="36" height="6" fill="rgba(0,0,0,0.5)" />
        <rect x="4"  y="20" width="28" height="20" fill="#7c2d12" />
        <rect x="4"  y="20" width="28" height="2"  fill="#9a3412" />
        <rect x="14" y="-4" width="8"  height="26" fill="#0e4a3a" />
        <rect x="6"  y="6"  width="4"  height="16" fill="#16a34a" />
        <rect x="24" y="6"  width="4"  height="16" fill="#16a34a" />
      </g>

      {/* ping-pong table — center back floor, between planner & designer */}
      <g transform={`translate(${W / 2 - 60}, ${wallH + 24})`}>
        <rect x="0"  y="20" width="120" height="6"  fill="rgba(0,0,0,0.4)" />
        <rect x="2"  y="0"  width="116" height="22" fill="#0e4a3a" />
        <rect x="2"  y="0"  width="116" height="2"  fill="#10593f" />
        <rect x="58" y="0"  width="4"   height="22" fill="#f5e9d3" />
        <rect x="6"  y="22" width="4"   height="14" fill="#1f1408" />
        <rect x="110" y="22" width="4"  height="14" fill="#1f1408" />
      </g>

      {/* server rack — bottom left under deploy area */}
      <g transform={`translate(60, ${H - 200})`}>
        <rect x="0"  y="0"  width="46" height="86" fill="#1a2540" />
        <rect x="0"  y="0"  width="46" height="2"  fill="#0a1228" />
        <rect x="0"  y="86" width="46" height="2"  fill="#0a1228" />
        {[10, 24, 38, 52, 66].map((y) => (
          <g key={y}>
            <rect x="3" y={y} width="40" height="10" fill="#0a1228" />
            <rect x="6" y={y + 3} width="2" height="2" fill="#22c55e" />
            <rect x="10" y={y + 3} width="2" height="2" fill="#d4a843" />
          </g>
        ))}
      </g>
    </svg>
  );
}

// Short-lived highlight that pulses on the receiving desk the instant
// a HandoffCourier arrives. Renders a colored ring + a "RECEIVED"
// badge for ~700ms (the parent removes us by clearing arrivedAt).
function ArrivalHighlight({ x, y, color, label, isDemo }) {
  return (
    <div
      className="pointer-events-none absolute"
      style={{
        left: x,
        top: y,
        transform: "translate(-50%, -50%)",
        zIndex: 12,
      }}
    >
      <style>{`
        @keyframes arrival-ring {
          0%   { transform: translate(-50%, -50%) scale(0.55); opacity: 0.95; }
          60%  { transform: translate(-50%, -50%) scale(1.25); opacity: 0.7;  }
          100% { transform: translate(-50%, -50%) scale(1.55); opacity: 0;    }
        }
        @keyframes arrival-badge {
          0%   { transform: translate(-50%, -50%) scale(0.6); opacity: 0; }
          25%  { transform: translate(-50%, -50%) scale(1.08); opacity: 1; }
          75%  { transform: translate(-50%, -50%) scale(1);    opacity: 1; }
          100% { transform: translate(-50%, -50%) scale(0.9);  opacity: 0; }
        }
      `}</style>

      {/* expanding ring */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "50%",
          width: 180,
          height: 180,
          borderRadius: "50%",
          border: `3px solid ${color}`,
          boxShadow: `0 0 24px ${color}aa, inset 0 0 18px ${color}66`,
          animation: "arrival-ring 700ms ease-out forwards",
        }}
      />

      {/* RECEIVED badge — sits dead center on the desk so the operator's
          eye snaps right onto the receiver. */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "50%",
          padding: "3px 10px 3px 7px",
          backgroundColor: color,
          color: "#0a1228",
          border: "2px solid #0a1228",
          borderRadius: 3,
          fontFamily: "ui-monospace, monospace",
          fontSize: 10,
          fontWeight: 800,
          letterSpacing: "0.18em",
          whiteSpace: "nowrap",
          boxShadow: "0 4px 10px rgba(0,0,0,0.5)",
          animation: "arrival-badge 700ms ease-out forwards",
        }}
      >
        ✓ RECEIVED · {label}
        {isDemo ? " · DEMO" : ""}
      </div>
    </div>
  );
}

// Glowing spotlight sphere for the active agent — drawn separately so we
// can position it in the same coord space as the desks.
function ActiveSpotlight({ x, y, color }) {
  return (
    <div
      className="pointer-events-none absolute"
      style={{
        left: x,
        top: y - 20,
        transform: "translate(-50%, -50%)",
        width: 240,
        height: 240,
        background: `radial-gradient(circle at center, ${color}44 0%, ${color}11 35%, transparent 70%)`,
        zIndex: 1,
      }}
    />
  );
}

export default function PixelOffice({
  agentStatuses = {},
  bubbles = {},
  activeAgentId = null,
  factory = null,
  runners = [],
  onHandoff = null,
  forceDemoHandoff = false,
}) {
  const hostRef = useRef(null);
  const [scale, setScale] = useState(1);
  const [hostWidth, setHostWidth] = useState(0);
  const [routeBanner, setRouteBanner] = useState(null);
  const [isDemoFlow, setIsDemoFlow] = useState(false);
  // {agentId, label, source, until} — non-null while the receiving
  // desk should still flash a "RECEIVED" highlight.
  const [arrivedAt, setArrivedAt] = useState(null);
  const arriveTimerRef = useRef(null);

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;

    const recompute = () => {
      const { width, height } = el.getBoundingClientRect();
      if (!width || !height) return;
      setHostWidth(width);
      const next = Math.min(
        width / PIXEL_OFFICE_WIDTH,
        height / PIXEL_OFFICE_HEIGHT,
      );
      if (Number.isFinite(next) && next > 0) {
        setScale(next);
      }
    };

    recompute();
    const ro = new ResizeObserver(recompute);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const handleBannerChange = useCallback((msg) => setRouteBanner(msg), []);
  const handleDemoChange = useCallback((flag) => setIsDemoFlow(flag), []);

  // 700ms arrival highlight — long enough to register as a "received"
  // signal without lingering past the courier's fade. Replacing an
  // earlier highlight (back-to-back handoffs) just resets the timer.
  const handleArrive = useCallback(({ agentId, label, source }) => {
    if (!agentId) return;
    if (arriveTimerRef.current) clearTimeout(arriveTimerRef.current);
    setArrivedAt({ agentId, label, source });
    arriveTimerRef.current = setTimeout(() => {
      setArrivedAt(null);
      arriveTimerRef.current = null;
    }, 700);
  }, []);

  useEffect(() => {
    return () => {
      if (arriveTimerRef.current) clearTimeout(arriveTimerRef.current);
    };
  }, []);

  // Office is "narrow" once the host viewport collapses to phone size.
  // We use the host width here, not window.innerWidth, so the layout
  // also responds when the office sits in a narrow side rail.
  const isMobile = hostWidth > 0 && hostWidth < 520;

  return (
    <div
      className="relative h-full w-full overflow-hidden"
      style={{
        backgroundColor: "#0a1228",
        border: "2px solid #0e4a3a",
        boxShadow: "0 0 40px rgba(212, 168, 67, 0.15) inset",
        borderRadius: 6,
        minHeight: 600,
      }}
    >
      {/* corner branding */}
      <div
        className="pointer-events-none absolute z-30 flex items-center gap-2 px-3 py-1.5"
        style={{
          left: 12,
          top: 12,
          backgroundColor: "#0a1228cc",
          border: "1px solid #d4a84366",
          borderRadius: 3,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <span
          className="inline-block h-2 w-2"
          style={{ backgroundColor: "#d4a843" }}
        />
        <span className="text-[10px] font-bold tracking-[0.3em] text-[#d4a843]">
          STAMPPORT · OFFICE
        </span>
        <span className="text-[9px] tracking-[0.25em] text-emerald-300/70">
          · 팀 포켓몬
        </span>
      </div>

      {/* live handoff banner — top-center, above the scaled stage so the
          text stays sharp regardless of office scale. Stays mounted
          even between cards when the URL forced the demo flow, so the
          "DEMO HANDOFF FLOW" tag is always visible while the user is
          waiting for the next walk. */}
      {(routeBanner || forceDemoHandoff) && (
        <div
          className="pointer-events-none absolute z-30 flex items-center gap-2 px-3 py-1"
          style={{
            left: "50%",
            top: 12,
            transform: "translateX(-50%)",
            maxWidth: "min(86%, 560px)",
            backgroundColor: "#0a1228e8",
            border: `1px solid ${
              isDemoFlow || forceDemoHandoff ? "#38bdf888" : "#34d39988"
            }`,
            borderRadius: 3,
            fontFamily: "ui-monospace, monospace",
            boxShadow:
              isDemoFlow || forceDemoHandoff
                ? "0 0 12px rgba(56,189,248,0.25)"
                : "0 0 12px rgba(52,211,153,0.25)",
          }}
        >
          <span
            className="inline-block h-1.5 w-1.5"
            style={{
              backgroundColor:
                isDemoFlow || forceDemoHandoff ? "#38bdf8" : "#34d399",
            }}
          />
          <span className="truncate text-[10.5px] tracking-[0.15em] text-[#f5e9d3]">
            {routeBanner ||
              (forceDemoHandoff
                ? "데모 핸드오프 흐름 — 곧 다음 작업이 전달됩니다."
                : "")}
          </span>
          {(isDemoFlow || forceDemoHandoff) && (
            <span
              className="ml-1 rounded px-1.5 py-0.5 text-[8.5px] font-bold tracking-[0.25em] text-sky-300"
              style={{
                border: "1px solid #38bdf888",
                backgroundColor: "#0a1228",
              }}
            >
              DEMO HANDOFF FLOW
            </span>
          )}
        </div>
      )}

      <div ref={hostRef} className="absolute inset-0">
        {/* fixed-coord stage, scaled to fit */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: "50%",
            width: PIXEL_OFFICE_WIDTH,
            height: PIXEL_OFFICE_HEIGHT,
            transform: `translate(-50%, -50%) scale(${scale})`,
            transformOrigin: "center center",
          }}
        >
          {/* room */}
          <OfficeRoom />

          {/* spotlight under active agent */}
          {activeAgentId && DESK_LAYOUT[activeAgentId] && (
            <ActiveSpotlight
              x={DESK_LAYOUT[activeAgentId].x}
              y={DESK_LAYOUT[activeAgentId].y}
              color={AGENTS[activeAgentId].color}
            />
          )}

          {/* desks + bubbles */}
          {AGENT_LIST.map((agent) => {
            const layout = DESK_LAYOUT[agent.id];
            if (!layout) return null;
            const status = agentStatuses[agent.id] || "idle";
            const isActive = activeAgentId === agent.id;
            const liveBubble = bubbles[agent.id];
            const message = liveBubble?.message || DEFAULT_BUBBLES[agent.id];
            const isLive = !!liveBubble;

            return (
              <div
                key={agent.id}
                className="absolute"
                style={{
                  left: layout.x,
                  top: layout.y,
                  transform: "translate(-50%, -50%)",
                  zIndex: 10,
                }}
              >
                {/* speech bubble — anchored above the avatar */}
                <div
                  className="absolute"
                  style={{
                    left: "50%",
                    top: -36,
                    transform: "translate(-50%, -100%)",
                    zIndex: 20,
                  }}
                >
                  <AnimatePresence mode="wait">
                    <SpeechBubble
                      key={`${agent.id}-${liveBubble?.id || "idle"}`}
                      agent={agent}
                      message={message}
                      idle={!isLive}
                    />
                  </AnimatePresence>
                </div>

                <AgentDesk
                  agent={agent}
                  status={status}
                  isActive={isActive}
                  facing={layout.facing}
                />
              </div>
            );
          })}

          {/* presence layer — small pip + status tag + ambient
              particles per desk. Sits between desks and couriers so
              the walking courier still draws on top of pips. */}
          <AgentPresenceLayer
            agentStatuses={agentStatuses}
            activeAgentId={activeAgentId}
          />

          {/* arrival highlight — pulses on the receiving desk for
              ~700ms when a courier completes its walk. Drawn under
              the courier so the courier itself stays the focus. */}
          {arrivedAt?.agentId && DESK_LAYOUT[arrivedAt.agentId] && (
            <ArrivalHighlight
              x={DESK_LAYOUT[arrivedAt.agentId].x}
              y={DESK_LAYOUT[arrivedAt.agentId].y}
              color={AGENTS[arrivedAt.agentId]?.color || "#34d399"}
              label={arrivedAt.label}
              isDemo={arrivedAt.source === "demo"}
            />
          )}

          {/* big walking courier — replaces the old paper-only flying
              card. AgentCourierLayer drives one HandoffCourier at a
              time with the sender's look + the artifact in hand. */}
          <AgentCourierLayer
            factory={factory}
            runners={runners}
            forceDemo={forceDemoHandoff}
            isMobile={isMobile}
            onBannerChange={handleBannerChange}
            onDemoChange={handleDemoChange}
            onHandoff={onHandoff}
            onArrive={handleArrive}
          />
        </div>
      </div>
    </div>
  );
}
