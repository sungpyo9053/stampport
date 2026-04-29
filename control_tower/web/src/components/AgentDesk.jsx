import { motion } from "framer-motion";
import PokemonAvatar from "./PokemonAvatar.jsx";
import { getAgentPokemon } from "../constants/agentPokemon.js";

// One desk pod inside the PixelOffice. Composed entirely of solid
// rectangles so the whole thing reads as pixel-art. The wood-toned desk
// + brass lamp + paper stack lean toward "cozy office" instead of
// admin-dashboard chrome.
//
// Coordinate system: this component is positioned absolutely by the
// parent at the center-bottom of where the desk should sit. Internally
// the avatar floats above the desk surface.

const DESK_W = 116;
const DESK_H = 44;

function DeskMonitor({ accent, working }) {
  return (
    <svg
      viewBox="0 0 56 44"
      width={56}
      height={44}
      shapeRendering="crispEdges"
      style={{ display: "block" }}
    >
      {/* monitor body */}
      <rect x="0" y="0"  width="56" height="32" fill="#0b1226" />
      <rect x="2" y="2"  width="52" height="28" fill="#111c33" />
      {/* screen content tinted with agent color */}
      <rect x="4" y="4"  width={working ? 40 : 22} height="2" fill={accent} />
      <rect x="4" y="8"  width="36" height="2" fill={accent} opacity="0.55" />
      <rect x="4" y="12" width="44" height="2" fill={accent} opacity="0.55" />
      <rect x="4" y="16" width="28" height="2" fill={accent} opacity="0.55" />
      <rect x="4" y="20" width="40" height="2" fill={accent} opacity="0.55" />
      <rect x="4" y="24" width="20" height="2" fill={accent} opacity="0.55" />
      {/* monitor stand */}
      <rect x="24" y="32" width="8"  height="6" fill="#1a2540" />
      <rect x="18" y="38" width="20" height="3" fill="#1a2540" />
    </svg>
  );
}

function DeskLamp({ on }) {
  // little brass lamp on the right side. Cone glows when working.
  return (
    <svg
      viewBox="0 0 14 22"
      width={14}
      height={22}
      shapeRendering="crispEdges"
      style={{ display: "block" }}
    >
      <rect x="6"  y="0"  width="2" height="6"  fill="#caa46a" />
      <rect x="2"  y="6"  width="10" height="2" fill="#d4a843" />
      <rect x="3"  y="8"  width="8"  height="2" fill="#caa46a" />
      <rect x="6"  y="10" width="2"  height="8" fill="#1a2540" />
      <rect x="3"  y="18" width="8"  height="2" fill="#1a2540" />
      {on && (
        <>
          <rect x="0" y="10" width="14" height="6" fill="#fde68a" opacity="0.5" />
          <rect x="2" y="9"  width="10" height="2" fill="#fef3c7" opacity="0.7" />
        </>
      )}
    </svg>
  );
}

function DeskPaperStack({ accent }) {
  return (
    <svg
      viewBox="0 0 18 14"
      width={18}
      height={14}
      shapeRendering="crispEdges"
      style={{ display: "block" }}
    >
      <rect x="0" y="6"  width="18" height="8" fill="#f5e9d3" />
      <rect x="0" y="6"  width="18" height="1" fill={accent} />
      <rect x="2" y="9"  width="10" height="1" fill="#94a3b8" />
      <rect x="2" y="11" width="14" height="1" fill="#94a3b8" />
      {/* clip on top */}
      <rect x="6" y="2"  width="6"  height="4" fill="#475569" />
      <rect x="7" y="0"  width="4"  height="3" fill="#64748b" />
    </svg>
  );
}

function RoleProp({ agent, facing, working }) {
  const poke = getAgentPokemon(agent.id);
  const props = poke.props || {};
  const label = props.label;
  const emoji = props.emoji;
  if (!label && !emoji) return null;
  // Pin the chip to the side of the desk opposite the lamp so it
  // doesn't crowd the monitor.
  const sideStyle = facing === "left"
    ? { left: -36, top: 56 }
    : { right: -36, top: 56 };
  return (
    <div
      className="absolute flex items-center gap-1 px-1.5 py-0.5"
      style={{
        ...sideStyle,
        backgroundColor: "#0a1228",
        border: `1px solid ${(poke.accent || agent.color) + "66"}`,
        borderRadius: 3,
        fontFamily: "ui-monospace, monospace",
        fontSize: 9,
        fontWeight: 600,
        letterSpacing: "0.5px",
        color: poke.accent || agent.color,
        boxShadow: working ? `0 0 6px ${(poke.accent || agent.color)}55` : "none",
        whiteSpace: "nowrap",
        zIndex: 4,
      }}
    >
      {emoji && <span style={{ fontSize: 11 }}>{emoji}</span>}
      {label && <span>{label}</span>}
    </div>
  );
}

function CoffeeMug({ accent }) {
  return (
    <svg
      viewBox="0 0 12 12"
      width={12}
      height={12}
      shapeRendering="crispEdges"
      style={{ display: "block" }}
    >
      <rect x="0" y="2" width="9"  height="9" fill={accent} />
      <rect x="9" y="4" width="2"  height="5" fill={accent} />
      <rect x="0" y="2" width="9"  height="1" fill="#3a1f0e" />
      <rect x="1" y="3" width="7"  height="1" fill="#7c2d12" />
    </svg>
  );
}

export default function AgentDesk({
  agent,
  status = "idle",
  isActive = false,
  bubble,
  facing = "right",
}) {
  const working = status === "working" || isActive;
  const done = status === "done";
  const failed = status === "error" || status === "blocked";

  return (
    <div
      className="relative select-none"
      style={{ width: DESK_W + 24, height: 132 }}
    >
      {/* floor mat — subtle accent rug under the desk */}
      <div
        className="absolute"
        style={{
          left: "50%",
          bottom: -4,
          transform: "translateX(-50%)",
          width: DESK_W + 28,
          height: 18,
          background: `radial-gradient(ellipse at center, ${agent.color}33 0%, transparent 70%)`,
        }}
      />

      {/* AVATAR — Pokemon character behind the desk. Sized so the
          sprite (or fallback emoji) reads at the same footprint as
          the legacy human avatar but with more presence. */}
      <div
        className="absolute"
        style={{
          left: "50%",
          top: 6,
          transform: "translateX(-50%)",
          zIndex: 2,
        }}
      >
        <PokemonAvatar
          agentId={agent.id}
          status={status}
          isActive={isActive}
          size={56}
        />
      </div>

      {/* Role-themed prop chip — small badge floating just outside
          the desk with the desk-side emoji + label. Reinforces "the
          designer is at the palette / QA is at the checklist /
          deploy is at the box" without requiring a full sprite. */}
      <RoleProp agent={agent} facing={facing} working={working} />

      {/* DESK — wood top + side */}
      <div
        className="absolute"
        style={{
          left: "50%",
          top: 64,
          transform: "translateX(-50%)",
          width: DESK_W,
          height: DESK_H,
          zIndex: 3,
        }}
      >
        <svg
          viewBox={`0 0 ${DESK_W} ${DESK_H}`}
          width={DESK_W}
          height={DESK_H}
          shapeRendering="crispEdges"
          style={{ display: "block" }}
        >
          {/* desk top — warm wood */}
          <rect x="0"  y="0"  width={DESK_W} height="10" fill="#5c3a1f" />
          <rect x="0"  y="0"  width={DESK_W} height="2"  fill="#7a4f2a" />
          <rect x="0"  y="2"  width={DESK_W} height="1"  fill="#3d2817" />
          {/* desk front face */}
          <rect x="2"  y="10" width={DESK_W - 4} height={DESK_H - 12} fill="#3d2817" />
          {/* legs */}
          <rect x="2"  y={DESK_H - 4} width="8" height="4" fill="#1f1408" />
          <rect x={DESK_W - 10} y={DESK_H - 4} width="8" height="4" fill="#1f1408" />

          {/* drawer handle */}
          <rect x={DESK_W / 2 - 6} y="20" width="12" height="2" fill="#caa46a" />
        </svg>

        {/* desk-top items, layered over the desk surface */}
        <div
          className="absolute flex items-end gap-1.5"
          style={{
            left: "50%",
            top: -36,
            transform: "translateX(-50%)",
          }}
        >
          {facing === "left" ? (
            <>
              <DeskPaperStack accent={agent.color} />
              <DeskMonitor accent={agent.color} working={working} />
              <DeskLamp on={working} />
            </>
          ) : (
            <>
              <DeskLamp on={working} />
              <DeskMonitor accent={agent.color} working={working} />
              <DeskPaperStack accent={agent.color} />
            </>
          )}
        </div>

        {/* coffee mug pinned to a corner */}
        <div
          className="absolute"
          style={{
            left: facing === "left" ? "auto" : 4,
            right: facing === "left" ? 4 : "auto",
            top: -4,
          }}
        >
          <CoffeeMug accent={agent.color} />
        </div>

        {/* completed-stamp on desk */}
        {done && (
          <motion.div
            className="absolute"
            style={{ left: 8, top: -10 }}
            initial={{ opacity: 0, scale: 0.4, rotate: -20 }}
            animate={{ opacity: 1, scale: 1, rotate: -8 }}
            transition={{ type: "spring", stiffness: 320, damping: 20 }}
          >
            <div
              className="rounded-sm border-2 px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
              style={{
                color: "#d4a843",
                borderColor: "#d4a843",
                fontFamily: "ui-monospace, monospace",
              }}
            >
              DONE
            </div>
          </motion.div>
        )}
      </div>

      {/* NAMEPLATE — role label + mapped Pokemon (Korean + English).
          The Korean Pokemon name sits on a second line so the role
          label (PM / 기획자 / ...) stays the dominant identifier. */}
      <div
        className="absolute flex flex-col items-center whitespace-nowrap rounded-sm px-2 py-0.5"
        style={{
          left: "50%",
          bottom: -2,
          transform: "translateX(-50%)",
          backgroundColor: "#0a1228",
          color: agent.color,
          border: `1px solid ${agent.color}66`,
          boxShadow: working ? `0 0 8px ${agent.color}88` : "none",
          fontFamily: "ui-monospace, monospace",
          zIndex: 5,
        }}
      >
        <div className="flex items-center gap-1 text-[10px] font-semibold tracking-widest">
          <span
            className="inline-block h-1.5 w-1.5"
            style={{ backgroundColor: working ? agent.color : "#475569" }}
          />
          <span style={{ color: "#f5e9d3" }}>{agent.name}</span>
        </div>
        {(() => {
          const poke = getAgentPokemon(agent.id);
          if (!poke || poke.korean === "—") return null;
          return (
            <div
              className="text-[8.5px] leading-tight tracking-widest"
              style={{ color: poke.accent || agent.color }}
            >
              {poke.korean}
              <span className="opacity-60"> · </span>
              <span className="opacity-70">{poke.pokemon}</span>
            </div>
          );
        })()}
      </div>

      {/* working ring around the desk */}
      {working && !failed && (
        <motion.div
          className="pointer-events-none absolute"
          style={{
            left: "50%",
            top: 54,
            transform: "translateX(-50%)",
            width: DESK_W + 16,
            height: DESK_H + 14,
            border: `1.5px dashed ${agent.color}`,
            borderRadius: 4,
          }}
          initial={{ opacity: 0.3 }}
          animate={{ opacity: [0.3, 0.9, 0.3] }}
          transition={{ duration: 1.2, repeat: Infinity, ease: "easeInOut" }}
        />
      )}

      {/* failure ring + warning glyph for blocked/error agents */}
      {failed && (
        <>
          <motion.div
            className="pointer-events-none absolute"
            style={{
              left: "50%",
              top: 54,
              transform: "translateX(-50%)",
              width: DESK_W + 18,
              height: DESK_H + 16,
              border: "1.5px solid #f87171",
              borderRadius: 4,
            }}
            initial={{ opacity: 0.4 }}
            animate={{ opacity: [0.4, 1, 0.4] }}
            transition={{ duration: 0.9, repeat: Infinity, ease: "easeInOut" }}
          />
          <motion.div
            className="absolute"
            style={{
              left: "50%",
              top: -2,
              transform: "translateX(-50%)",
              fontFamily: "ui-monospace, monospace",
              fontSize: 12,
              fontWeight: 800,
              color: "#0a1228",
              backgroundColor: "#f87171",
              border: "1.5px solid #7f1d1d",
              padding: "0 6px",
              borderRadius: 3,
              letterSpacing: "1px",
              zIndex: 6,
            }}
            initial={{ scale: 0.6, opacity: 0 }}
            animate={{
              scale: [0.95, 1.05, 0.95],
              opacity: [0.85, 1, 0.85],
            }}
            transition={{ duration: 1.1, repeat: Infinity, ease: "easeInOut" }}
          >
            !
          </motion.div>
        </>
      )}
    </div>
  );
}

export { DESK_W, DESK_H };
