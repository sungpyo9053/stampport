import { useEffect, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { getAgentPokemon, resolveAgentAsset } from "../constants/agentPokemon.js";

// Renders the agent's mapped Pokemon as the central character at the
// desk. Loads the sprite from `/assets/agents/pokemon/<name>.png`
// (operator-supplied, see the README in that folder) and falls back to
// the emoji glyph when the asset 404s — never shows a broken-image
// icon.
//
// State indicators:
//   idle      — gentle breathing scale (or static if reduced-motion)
//   working   — sparkle ring + faster bob
//   done      — small DONE stamp pinned to the corner
//   blocked   — red warning pip + flash ring
//
// The component is fully self-contained (no external CSS); the parent
// just hands it `agentId`, `status`, `isActive`, and an optional
// `size`. Role label / nameplate stays in `AgentDesk` so we don't
// duplicate it.

const SIZE_DEFAULT = 56;

function Sparkle({ delay = 0, x = 0, y = 0, color = "#facc15" }) {
  return (
    <motion.span
      aria-hidden
      style={{
        position: "absolute",
        left: x,
        top: y,
        width: 4,
        height: 4,
        backgroundColor: color,
        borderRadius: "50%",
        boxShadow: `0 0 6px ${color}cc`,
        pointerEvents: "none",
      }}
      initial={{ opacity: 0, scale: 0.4 }}
      animate={{ opacity: [0, 1, 0], scale: [0.4, 1.2, 0.4] }}
      transition={{
        duration: 1.4,
        delay,
        repeat: Infinity,
        ease: "easeInOut",
      }}
    />
  );
}

export default function PokemonAvatar({
  agentId,
  status = "idle",
  isActive = false,
  size = SIZE_DEFAULT,
}) {
  const record = getAgentPokemon(agentId);
  const [hasAsset, setHasAsset] = useState(true);
  const reducedMotion = useReducedMotion();

  // Reset the asset-loading state if the agent id changes — keeps the
  // emoji fallback from sticking after a swap.
  useEffect(() => {
    setHasAsset(!!record.asset);
  }, [record.asset]);

  const working = status === "working" || isActive;
  const done = status === "done";
  const failed = status === "error" || status === "blocked";

  const accent = record.accent || "#facc15";
  const assetUrl = resolveAgentAsset(record.asset);

  // Bob animation — disabled under prefers-reduced-motion.
  const bob = reducedMotion
    ? { y: 0, scale: 1 }
    : working
    ? { y: [0, -2, 0], scale: [1, 1.04, 1] }
    : { y: [0, -1, 0], scale: [1, 1.01, 1] };
  const bobTransition = reducedMotion
    ? { duration: 0 }
    : {
        duration: working ? 0.8 : 2.6,
        repeat: Infinity,
        ease: "easeInOut",
      };

  return (
    <div
      className="relative select-none"
      style={{ width: size, height: size }}
      title={`${record.korean} (${record.pokemon}) — ${record.reason}`}
    >
      {/* glow / aura behind the character */}
      {(working || isActive) && !failed && (
        <motion.span
          aria-hidden
          style={{
            position: "absolute",
            inset: -6,
            backgroundColor: accent,
            borderRadius: "50%",
            filter: "blur(10px)",
            pointerEvents: "none",
          }}
          initial={{ opacity: 0.25, scale: 0.9 }}
          animate={
            reducedMotion
              ? { opacity: 0.4 }
              : { opacity: [0.25, 0.6, 0.25], scale: [0.9, 1.05, 0.9] }
          }
          transition={
            reducedMotion
              ? { duration: 0 }
              : { duration: 1.6, repeat: Infinity, ease: "easeInOut" }
          }
        />
      )}

      {failed && (
        <motion.span
          aria-hidden
          style={{
            position: "absolute",
            inset: -4,
            border: "2px solid #f87171",
            borderRadius: "50%",
            pointerEvents: "none",
          }}
          initial={{ opacity: 0.5 }}
          animate={
            reducedMotion ? { opacity: 0.8 } : { opacity: [0.5, 1, 0.5] }
          }
          transition={
            reducedMotion
              ? { duration: 0 }
              : { duration: 0.9, repeat: Infinity, ease: "easeInOut" }
          }
        />
      )}

      {/* idle/working bob wrapper */}
      <motion.div
        className="absolute inset-0 flex items-center justify-center"
        animate={bob}
        transition={bobTransition}
      >
        {hasAsset && assetUrl ? (
          <img
            src={assetUrl}
            alt={`${record.korean} (${record.pokemon})`}
            width={size}
            height={size}
            draggable={false}
            onError={() => setHasAsset(false)}
            style={{
              width: size,
              height: size,
              imageRendering: "pixelated",
              filter: failed ? "grayscale(0.4)" : "none",
              userSelect: "none",
            }}
          />
        ) : (
          // Emoji fallback — sized so the glyph fills the same footprint
          // as the sprite would. Background ring keeps the silhouette
          // readable on the dark wood floor.
          <div
            aria-label={`${record.korean} (${record.pokemon}) fallback`}
            style={{
              width: size,
              height: size,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: Math.round(size * 0.62),
              lineHeight: 1,
              borderRadius: "50%",
              backgroundColor: `${accent}22`,
              border: `1.5px solid ${accent}77`,
              boxShadow: `0 0 8px ${accent}55 inset`,
              filter: failed ? "grayscale(0.5)" : "none",
            }}
          >
            {record.fallback}
          </div>
        )}
      </motion.div>

      {/* sparkles when working */}
      {working && !failed && !reducedMotion && (
        <>
          <Sparkle x={-2} y={2} delay={0} color={accent} />
          <Sparkle x={size - 6} y={4} delay={0.4} color={accent} />
          <Sparkle x={size - 10} y={size - 12} delay={0.8} color={accent} />
        </>
      )}

      {/* DONE stamp */}
      {done && (
        <motion.div
          aria-label="done"
          style={{
            position: "absolute",
            top: -6,
            right: -10,
            padding: "1px 5px",
            border: "1.5px solid #d4a843",
            color: "#d4a843",
            backgroundColor: "#0a1228",
            fontSize: 9,
            fontFamily: "ui-monospace, monospace",
            fontWeight: 700,
            letterSpacing: "1px",
            borderRadius: 2,
            transform: "rotate(-12deg)",
          }}
          initial={{ scale: 0.4, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ type: "spring", stiffness: 320, damping: 22 }}
        >
          DONE
        </motion.div>
      )}

      {/* warning pip */}
      {failed && (
        <div
          aria-label="blocked"
          style={{
            position: "absolute",
            top: -4,
            right: -4,
            width: 16,
            height: 16,
            borderRadius: "50%",
            backgroundColor: "#f87171",
            color: "#0a1228",
            border: "1.5px solid #7f1d1d",
            fontSize: 11,
            fontWeight: 800,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: "ui-monospace, monospace",
          }}
        >
          !
        </div>
      )}
    </div>
  );
}
