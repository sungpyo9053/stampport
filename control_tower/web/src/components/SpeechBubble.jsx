import { motion } from "framer-motion";

// Floats above the agent's head. The character's head sits roughly at
// (agent.x, agent.y - 86) inside the office stage, so we anchor the
// bubble's tail near agent.y - 95.
export default function SpeechBubble({ agent, message }) {
  return (
    <motion.div
      key={`${agent.id}-${message}`}
      initial={{ opacity: 0, y: 8, scale: 0.85 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6, scale: 0.85 }}
      transition={{ type: "spring", stiffness: 320, damping: 22 }}
      className="absolute z-30"
      style={{
        left: agent.x,
        top: agent.y - 95,
        transform: "translate(-50%, -100%)",
      }}
    >
      <div className="relative max-w-[220px] rounded-xl border border-slate-700 bg-slate-900/95 px-3 py-2 text-[12px] leading-snug text-slate-100 shadow-lg backdrop-blur">
        <div className="absolute -bottom-1 left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 border-b border-r border-slate-700 bg-slate-900/95" />
        <div
          className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider"
          style={{ color: agent.color }}
        >
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ backgroundColor: agent.color }}
          />
          <span>{agent.name}</span>
        </div>
        <div className="mt-1 text-slate-200">{message}</div>
      </div>
    </motion.div>
  );
}
