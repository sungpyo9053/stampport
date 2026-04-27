import { motion } from "framer-motion";

// Pixel-style speech balloon. Lives in flow positioning — the parent
// AgentDesk wrapper places it directly above the avatar's head. Uses
// hard-edged borders + a small triangle tail to match the pixel office.
export default function SpeechBubble({ agent, message, idle = false }) {
  return (
    <motion.div
      key={`${agent.id}-${message}`}
      initial={{ opacity: 0, y: 6, scale: 0.9 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, scale: 0.9 }}
      transition={{ type: "spring", stiffness: 300, damping: 22 }}
      className="relative max-w-[200px]"
      style={{
        backgroundColor: idle ? "#0e1a35" : "#0a1228",
        border: `1.5px solid ${agent.color}`,
        boxShadow: idle ? "none" : `0 0 12px ${agent.color}55`,
        borderRadius: 4,
        padding: "5px 8px",
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div
        className="text-[9px] font-bold uppercase tracking-widest"
        style={{ color: agent.color }}
      >
        {agent.name}
      </div>
      <div
        className="mt-0.5 text-[11px] leading-snug"
        style={{ color: idle ? "#94a3b8" : "#f5e9d3" }}
      >
        {message}
      </div>

      {/* tail — pixel triangle pointing down */}
      <div
        className="absolute"
        style={{
          left: "50%",
          bottom: -6,
          transform: "translateX(-50%)",
          width: 0,
          height: 0,
          borderLeft: "5px solid transparent",
          borderRight: "5px solid transparent",
          borderTop: `6px solid ${agent.color}`,
        }}
      />
      <div
        className="absolute"
        style={{
          left: "50%",
          bottom: -3,
          transform: "translateX(-50%)",
          width: 0,
          height: 0,
          borderLeft: "4px solid transparent",
          borderRight: "4px solid transparent",
          borderTop: `5px solid ${idle ? "#0e1a35" : "#0a1228"}`,
        }}
      />
    </motion.div>
  );
}
