import { useEffect, useRef, useState } from "react";
import HumanAgentCharacter from "./HumanAgentCharacter.jsx";
import ArtifactProp from "./ArtifactProp.jsx";
import { getAgentPokemon } from "../constants/agentPokemon.js";

// Big, visible courier that walks one handoff between desks.
//
// We render a full-body HumanAgentCharacter (≥44×60 desktop, ≥32×44
// mobile) carrying an ArtifactProp + label badge. The walk is driven
// entirely by CSS transitions on the absolute (left, top) coordinates
// so we don't pull in a physics engine or another animation library.
//
// Phases:
//   spawn   — placed at sender, opacity 0→1
//   travel  — slides to receiver  (≈1.9s, eased)
//   land    — held at receiver while the desk highlight fires (≈600ms)
//   fade    — opacity 1→0, then onDone()
//
// Reduced motion: skip the slide; the courier blips into place at the
// receiver and the desk still receives the highlight, so the operator
// still sees "who handed work to whom" without motion.

const TRAVEL_MS_DEFAULT = 1900;
const PAUSE_MS = 650;
const FADE_MS = 380;

export default function HandoffCourier({
  fromX,
  fromY,
  toX,
  toY,
  fromAgent,
  toAgent,
  label,
  artifactType,
  bubble,
  isMobile = false,
  reducedMotion = false,
  onArrive,
  onDone,
}) {
  const [phase, setPhase] = useState("spawn");
  const timersRef = useRef([]);

  const TRAVEL_MS = reducedMotion ? 320 : TRAVEL_MS_DEFAULT;

  useEffect(() => {
    const timers = timersRef.current;
    // Two RAFs so the browser commits the "spawn" position before the
    // transition target lands — without this the courier teleports.
    const r1 = requestAnimationFrame(() => {
      const r2 = requestAnimationFrame(() => setPhase("travel"));
      timers.push(() => cancelAnimationFrame(r2));
    });
    timers.push(() => cancelAnimationFrame(r1));
    timers.push(
      setTimeout(() => {
        setPhase("land");
        onArrive?.();
      }, TRAVEL_MS + 30),
    );
    timers.push(
      setTimeout(() => setPhase("fade"), TRAVEL_MS + PAUSE_MS),
    );
    timers.push(
      setTimeout(() => onDone?.(), TRAVEL_MS + PAUSE_MS + FADE_MS),
    );
    return () => {
      for (const t of timers) {
        if (typeof t === "function") t();
        else clearTimeout(t);
      }
      timersRef.current = [];
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const atSpawn = phase === "spawn";
  const x = atSpawn ? fromX : toX;
  const y = atSpawn ? fromY : toY;
  const opacity = phase === "spawn" ? 0 : phase === "fade" ? 0 : 1;

  // Visible courier body — clearly larger than a status pip.
  const charW = isMobile ? 32 : 44;
  const charH = isMobile ? 44 : 60;
  const artifactSize = isMobile ? 20 : 26;

  const accent = fromAgent?.color || "#d4a843";

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        transform: "translate(-50%, -55%)",
        transition: reducedMotion
          ? `opacity ${FADE_MS}ms ease`
          : `left ${TRAVEL_MS}ms cubic-bezier(0.45, 0.05, 0.25, 1), ` +
            `top ${TRAVEL_MS}ms cubic-bezier(0.45, 0.05, 0.25, 1), ` +
            `opacity ${FADE_MS}ms ease`,
        opacity,
        zIndex: 38,
        pointerEvents: "none",
      }}
    >
      <div style={{ position: "relative", width: charW, height: charH }}>
        {/* Walking-pose human courier — uses the sender's "look" so
            it visibly reads as "the planner is delivering this", not
            a generic mailman. */}
        <div
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            width: charW,
            height: charH,
          }}
        >
          <HumanAgentCharacter
            look={fromAgent?.look}
            status="working"
            pose="walking"
            width={charW}
            height={charH}
            showStatusBadge={false}
          />
        </div>

        {/* Optional speech bubble above the courier — we use this for
            the designer's "이건 갖고 싶어 보이지 않아요" critique on
            the way back to the planner. */}
        {bubble && (
          <div
            style={{
              position: "absolute",
              left: "50%",
              top: -22,
              transform: "translateX(-50%)",
              padding: "2px 6px",
              backgroundColor: "#0a1228",
              border: `1px solid ${accent}88`,
              borderRadius: 3,
              color: "#f5e9d3",
              fontSize: 9,
              fontFamily: "ui-monospace, monospace",
              whiteSpace: "nowrap",
              letterSpacing: "0.4px",
              boxShadow: `0 2px 6px rgba(0,0,0,0.5)`,
            }}
          >
            {bubble}
          </div>
        )}

        {/* Document + label badge held at chest height, in front of
            the body. The artifact icon is rendered inline so the
            paper looks "in hand". */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: charH * 0.42,
            transform: "translate(-50%, 0)",
            display: "flex",
            alignItems: "center",
            gap: 4,
            padding: isMobile ? "2px 5px 2px 4px" : "3px 6px 3px 4px",
            backgroundColor: "#f5e9d3",
            border: `2px solid ${accent}`,
            borderRadius: 3,
            boxShadow: `0 4px 8px rgba(0,0,0,0.55), 0 0 8px ${accent}55`,
            fontFamily: "ui-monospace, monospace",
            fontSize: isMobile ? 9 : 10.5,
            fontWeight: 700,
            color: "#0a1228",
            letterSpacing: "0.4px",
            whiteSpace: "nowrap",
          }}
        >
          {artifactType && (
            <div
              style={{
                width: artifactSize,
                height: artifactSize,
                flexShrink: 0,
              }}
            >
              <ArtifactProp type={artifactType} size={artifactSize} />
            </div>
          )}
          <span>{label}</span>
          {/* From → to Pokemon glyph row — even with sprite assets
              missing, the emoji fallbacks make the lineage of the
              handoff visible at a glance. */}
          {(fromAgent?.id || toAgent?.id) && (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 2,
                marginLeft: 4,
                paddingLeft: 4,
                borderLeft: `1px solid ${accent}66`,
                fontSize: isMobile ? 10 : 12,
              }}
              aria-label="handoff lineage"
            >
              <span title={getAgentPokemon(fromAgent?.id).korean}>
                {getAgentPokemon(fromAgent?.id).fallback}
              </span>
              <span style={{ opacity: 0.6 }}>→</span>
              <span title={getAgentPokemon(toAgent?.id).korean}>
                {getAgentPokemon(toAgent?.id).fallback}
              </span>
            </span>
          )}
        </div>

        {/* Footstep shadow — squashes while traveling so the courier
            looks airborne mid-stride. */}
        <div
          aria-hidden
          style={{
            position: "absolute",
            left: "50%",
            top: "100%",
            transform: `translateX(-50%) scaleX(${
              phase === "travel" ? 0.55 : 1
            })`,
            width: charW * 0.7,
            height: 5,
            background: "rgba(0,0,0,0.42)",
            borderRadius: "50%",
            filter: "blur(2px)",
            opacity: phase === "fade" ? 0 : 0.7,
            transition: reducedMotion
              ? `opacity ${FADE_MS}ms ease`
              : `transform ${TRAVEL_MS}ms ease, opacity ${FADE_MS}ms ease`,
          }}
        />
      </div>
    </div>
  );
}
