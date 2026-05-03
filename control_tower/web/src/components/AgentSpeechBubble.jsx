import { memo } from "react";

// AgentSpeechBubble — comic-style bubble that hovers above an agent
// in the pixel office floor. Rendered by AgentOfficeScene at floor
// coordinates so it's not jittered by the character's bob/walk
// transforms.
//
// The verification spec checks for `pixel-agent-speech` so the class
// list below leads with that exact name.

const TONE_PRESET = {
  running: { color: "#fde68a", border: "#fbbf2466", bg: "#1c1408", glow: "#fbbf2455" },
  rework:  { color: "#ddd6fe", border: "#a78bfa66", bg: "#180f25", glow: "#a78bfa55" },
  failed:  { color: "#fecaca", border: "#f8717166", bg: "#1c0d12", glow: "#f8717155" },
  passed:  { color: "#bbf7d0", border: "#34d39966", bg: "#0c1f1a", glow: "#34d39955" },
  info:    { color: "#cbd5e1", border: "#1e293b",  bg: "#0a1228", glow: "#1e293b55" },
};

function AgentSpeechBubble({ tone = "info", text, side = "top", small = false }) {
  if (!text) return null;
  const preset = TONE_PRESET[tone] || TONE_PRESET.info;
  // Accept the new compact side props too — bubble-top / bubble-left
  // / bubble-right come straight from the redesigned scene's per-slot
  // index, while the old "left"/"right"/"top" strings are kept for
  // legacy callers.
  const sideKey =
    side === "bubble-left" || side === "left"   ? "left"
    : side === "bubble-right" || side === "right" ? "right"
    : "top";

  return (
    <div
      className={
        "pixel-agent-speech agent-speech-bubble " +
        `bubble-${sideKey} ` +
        (sideKey === "left"  ? "pixel-agent-speech-left agent-speech-bubble-left "
        : sideKey === "right" ? "pixel-agent-speech-right agent-speech-bubble-right "
        :                       "pixel-agent-speech-top agent-speech-bubble-top ") +
        (small ? "pixel-agent-speech-small agent-speech-bubble-small" : "")
      }
      role="status"
      aria-live="polite"
      style={{
        color: preset.color,
        borderColor: preset.border,
        backgroundColor: preset.bg,
        boxShadow: `0 0 12px ${preset.glow}`,
      }}
      data-testid="pixel-agent-speech"
      data-tone={tone}
    >
      <span className="pixel-agent-speech-text">{text}</span>
      {tone === "running" && (
        <span className="pixel-agent-speech-typing" aria-hidden>
          <span />
          <span />
          <span />
        </span>
      )}
      <span
        className="pixel-agent-speech-tail"
        style={{ borderTopColor: preset.bg, filter: `drop-shadow(0 0 2px ${preset.border})` }}
        aria-hidden
      />
    </div>
  );
}

export default memo(AgentSpeechBubble);
