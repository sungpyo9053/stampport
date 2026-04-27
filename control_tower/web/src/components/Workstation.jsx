import { motion, AnimatePresence } from "framer-motion";
import HumanAgentCharacter from "./HumanAgentCharacter.jsx";
import ArtifactProp from "./ArtifactProp.jsx";
import { STATUS_META } from "../constants/agents.js";

// One desk pod for a single agent. Positioned absolutely at the agent's
// (x, y) inside the office's fixed coord system. The desk renders BEHIND
// the character's lower body so the figure looks like it's sitting at it,
// while the head and hands are clearly visible above the desk surface.
//
// Internal local-coord layout (origin = workstation center = agent.x,y):
//   chair  : behind, centered at (0, +6)
//   character: centered head-up at (0, -32)  (renders 80x110 box)
//   desk    : in front of waist, centered at (0, +44), 140x60
//   monitor : on desk surface
//   artifact (done state): on desk to the side
//   nameplate: below desk

const DESK_W = 150;
const DESK_H = 60;

function Chair({ color = "#1e293b" }) {
  // simple SD office chair, centered on (0,0)
  return (
    <svg
      viewBox="-30 -30 60 60"
      width={60}
      height={60}
      style={{ overflow: "visible" }}
    >
      {/* backrest */}
      <rect x="-14" y="-22" width="28" height="22" rx="6" fill={color} stroke="#0f172a" strokeWidth="1" />
      {/* seat */}
      <rect x="-16" y="-2" width="32" height="8" rx="3" fill={color} stroke="#0f172a" strokeWidth="1" />
      {/* stem */}
      <rect x="-1.5" y="6" width="3" height="10" fill="#0f172a" />
      {/* base */}
      <ellipse cx="0" cy="18" rx="14" ry="4" fill="#0f172a" />
    </svg>
  );
}

function Desk({ accentColor, status }) {
  // desk surface viewed slightly from above. monitor sits on top.
  return (
    <svg
      viewBox={`0 0 ${DESK_W} ${DESK_H}`}
      width={DESK_W}
      height={DESK_H}
      style={{ overflow: "visible" }}
    >
      {/* drop shadow under desk */}
      <ellipse cx={DESK_W / 2} cy={DESK_H + 4} rx={DESK_W / 2 - 6} ry="4" fill="rgba(0,0,0,0.45)" />

      {/* desk side (front face) */}
      <rect x="2" y="22" width={DESK_W - 4} height={DESK_H - 22} rx="4" fill="#1f2a44" stroke="#475569" strokeWidth="1" />
      {/* desk top */}
      <rect x="0" y="14" width={DESK_W} height="14" rx="3" fill="#3a4a6a" stroke="#64748b" strokeWidth="1.2" />
      {/* desk top highlight */}
      <rect x="2" y="15" width={DESK_W - 4} height="2" rx="1" fill="rgba(255,255,255,0.06)" />

      {/* monitor (back plate sitting on desk top) */}
      <g transform={`translate(${DESK_W / 2 - 28}, -16)`}>
        <rect x="0" y="0" width="56" height="28" rx="3" fill="#0b1626" stroke="#334155" strokeWidth="1" />
        <rect x="2" y="2" width="52" height="22" rx="1.5" fill="#0f1a2e" />
        {/* fake screen content tinted with agent color */}
        <rect x="5" y="5" width={status === "working" ? 38 : 24} height="2" fill={accentColor} opacity="0.95" />
        <rect x="5" y="9" width="34" height="1.5" fill={accentColor} opacity="0.6" />
        <rect x="5" y="12" width="42" height="1.5" fill={accentColor} opacity="0.6" />
        <rect x="5" y="15" width="28" height="1.5" fill={accentColor} opacity="0.6" />
        {/* working glow */}
        {status === "working" && (
          <motion.rect
            x="2" y="2" width="52" height="22" rx="1.5"
            fill={accentColor}
            initial={{ opacity: 0.05 }}
            animate={{ opacity: [0.05, 0.18, 0.05] }}
            transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
          />
        )}
        {/* monitor stand */}
        <rect x="24" y="28" width="8" height="4" fill="#1e293b" />
        <rect x="18" y="32" width="20" height="2" rx="1" fill="#1e293b" />
      </g>

      {/* keyboard */}
      <rect x={DESK_W / 2 - 30} y="18" width="60" height="6" rx="1.5" fill="#0b1626" stroke="#334155" />
      {/* mouse */}
      <ellipse cx={DESK_W / 2 + 38} cy="21" rx="4" ry="2.5" fill="#1e293b" stroke="#334155" />
      {/* coffee mug */}
      <g transform={`translate(${10}, 14)`}>
        <rect x="0" y="0" width="8" height="9" rx="1.5" fill="#0e7490" />
        <path d="M8 2 q3 2 0 5" fill="none" stroke="#0e7490" strokeWidth="1.4" />
        <ellipse cx="4" cy="0.5" rx="3.5" ry="1.2" fill="#7c2d12" />
      </g>
    </svg>
  );
}

export default function Workstation({ agent, status, isActive }) {
  const meta = STATUS_META[status] || STATUS_META.idle;
  const isDone = status === "done";

  return (
    <div
      className="absolute"
      style={{
        left: agent.x,
        top: agent.y,
        transform: "translate(-50%, -50%)",
        width: DESK_W + 20,
        height: 200,
      }}
    >
      {/* CHAIR — behind everything */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: 84,
          transform: "translate(-50%, 0)",
          zIndex: 1,
        }}
      >
        <Chair />
      </div>

      {/* CHARACTER — sitting, head & hands above desk */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: -8,
          transform: "translate(-50%, 0)",
          zIndex: 2,
        }}
      >
        <HumanAgentCharacter
          look={agent.look}
          status={status}
          pose="sitting"
          isActive={isActive}
        />
      </div>

      {/* DESK — in front of character's waist/lower body */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: 88,
          transform: "translate(-50%, 0)",
          zIndex: 3,
        }}
      >
        <Desk accentColor={agent.color} status={status} />
      </div>

      {/* DONE-STATE ARTIFACT on desk */}
      <AnimatePresence>
        {isDone && (
          <motion.div
            key="artifact-on-desk"
            initial={{ opacity: 0, scale: 0.4, y: 6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.4 }}
            transition={{ type: "spring", stiffness: 320, damping: 22 }}
            style={{
              position: "absolute",
              left: "50%",
              top: 92,
              transform: "translate(-90%, 0)",
              zIndex: 4,
            }}
          >
            <ArtifactProp type={agent.artifactType} size={32} />
          </motion.div>
        )}
      </AnimatePresence>

      {/* NAMEPLATE under desk */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: 152,
          transform: "translate(-50%, 0)",
          zIndex: 5,
        }}
        className="flex items-center gap-1.5 whitespace-nowrap rounded-full bg-slate-950/90 px-2.5 py-0.5 text-[11px] uppercase tracking-wider text-slate-100 ring-1 ring-slate-700/70 backdrop-blur"
      >
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: agent.color }}
        />
        <span>{agent.name}</span>
        <span className={`text-[10px] ${meta.text}`}>{meta.label}</span>
      </div>
    </div>
  );
}
