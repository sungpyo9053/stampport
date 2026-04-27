import { useEffect, useRef, useState } from "react";
import ArtifactProp from "./ArtifactProp.jsx";

// A small labeled paper / ticket that slides from one desk to another.
// Coordinates are in PixelOffice stage space (the same coord system as
// DESK_LAYOUT). The card renders an inline pixel-art artifact icon plus
// a Korean label so the operator can read what work is being handed off.
//
// Driven entirely by CSS transitions on left/top — no external animation
// library required. Respects prefers-reduced-motion by collapsing the
// motion to a quick fade in place.

const TRAVEL_MS = 1900;
const PAUSE_MS = 380;
const FADE_MS = 320;
const TOTAL_MS = TRAVEL_MS + PAUSE_MS + FADE_MS;

export default function MovingTaskCard({
  fromX,
  fromY,
  toX,
  toY,
  label,
  artifactType,
  accent = "#d4a843",
  bubble,
  reducedMotion = false,
  onDone,
}) {
  // phase:
  //   "start"   — rendered at sender, opacity 0 -> 1
  //   "travel"  — sliding to receiver
  //   "land"    — paused at receiver
  //   "fade"    — fading out
  const [phase, setPhase] = useState("start");
  const timersRef = useRef([]);

  useEffect(() => {
    const timers = timersRef.current;
    if (reducedMotion) {
      // Skip the slide; just blip in/out with a label.
      timers.push(setTimeout(() => setPhase("land"), 60));
      timers.push(setTimeout(() => setPhase("fade"), 600));
      timers.push(setTimeout(() => onDone?.(), 1000));
    } else {
      // Two RAFs so the browser commits the "start" position before the
      // transition target is applied.
      const raf1 = requestAnimationFrame(() => {
        const raf2 = requestAnimationFrame(() => setPhase("travel"));
        timers.push(() => cancelAnimationFrame(raf2));
      });
      timers.push(() => cancelAnimationFrame(raf1));
      timers.push(setTimeout(() => setPhase("land"), TRAVEL_MS + 30));
      timers.push(setTimeout(() => setPhase("fade"), TRAVEL_MS + PAUSE_MS));
      timers.push(setTimeout(() => onDone?.(), TOTAL_MS));
    }
    return () => {
      for (const t of timers) {
        if (typeof t === "function") t();
        else clearTimeout(t);
      }
      timersRef.current = [];
    };
  }, [reducedMotion, onDone]);

  const atStart = phase === "start";
  const x = atStart ? fromX : toX;
  const y = atStart ? fromY : toY;
  // tiny lift while traveling so the paper looks airborne
  const lift = phase === "travel" ? -10 : 0;
  const tilt = phase === "travel" ? -3 : phase === "land" ? 2 : 0;
  const opacity = phase === "fade" ? 0 : phase === "start" ? 0 : 1;

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        transform: "translate(-50%, -50%)",
        transition: reducedMotion
          ? `opacity ${FADE_MS}ms ease`
          : `left ${TRAVEL_MS}ms cubic-bezier(0.45, 0.05, 0.25, 1), top ${TRAVEL_MS}ms cubic-bezier(0.45, 0.05, 0.25, 1), opacity ${FADE_MS}ms ease`,
        opacity,
        zIndex: 35,
        pointerEvents: "none",
      }}
    >
      <div style={{ position: "relative" }}>
        {bubble && (
          <div
            style={{
              position: "absolute",
              left: "50%",
              top: -28,
              transform: "translateX(-50%)",
              padding: "2px 6px",
              backgroundColor: "#0a1228",
              border: `1px solid ${accent}88`,
              borderRadius: 3,
              fontSize: 9,
              color: "#f5e9d3",
              fontFamily: "ui-monospace, monospace",
              whiteSpace: "nowrap",
              letterSpacing: "0.4px",
            }}
          >
            {bubble}
          </div>
        )}

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 5,
            padding: "4px 7px 4px 5px",
            backgroundColor: "#f5e9d3",
            border: `2px solid ${accent}`,
            borderRadius: 3,
            boxShadow: `0 4px 8px rgba(0,0,0,0.5), 0 0 8px ${accent}55`,
            fontFamily: "ui-monospace, monospace",
            fontSize: 10.5,
            fontWeight: 700,
            color: "#0a1228",
            letterSpacing: "0.6px",
            whiteSpace: "nowrap",
            transform: `translateY(${lift}px) rotate(${tilt}deg)`,
            transition: reducedMotion
              ? "none"
              : `transform ${TRAVEL_MS}ms cubic-bezier(0.4, 0.0, 0.3, 1)`,
          }}
        >
          {artifactType && (
            <div
              style={{
                width: 18,
                height: 18,
                flexShrink: 0,
                display: "grid",
                placeItems: "center",
              }}
            >
              <ArtifactProp type={artifactType} size={18} />
            </div>
          )}
          <span>{label}</span>
        </div>

        {/* shadow underneath so the paper feels like it's flying */}
        <div
          aria-hidden
          style={{
            position: "absolute",
            left: "50%",
            top: "calc(100% + 2px)",
            transform: `translateX(-50%) scaleX(${
              phase === "travel" ? 0.6 : 1
            })`,
            width: 50,
            height: 5,
            background: "rgba(0,0,0,0.35)",
            borderRadius: "50%",
            filter: "blur(2px)",
            opacity: phase === "travel" ? 0.5 : 0.8,
            transition: reducedMotion
              ? "none"
              : `transform ${TRAVEL_MS}ms ease, opacity ${TRAVEL_MS}ms ease`,
          }}
        />
      </div>
    </div>
  );
}
