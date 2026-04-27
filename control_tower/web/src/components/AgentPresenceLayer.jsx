import { useEffect, useState } from "react";
import { AGENT_LIST, DESK_LAYOUT } from "../constants/agents.js";

// Subtle "agents are alive" overlay that sits inside PixelOffice's
// scaled stage. Each desk gets:
//
//   - a faint floating dot near the agent's head that pulses while
//     idle and intensifies while active,
//   - a status pip beside the desk (idle → soft blue, active → agent
//     accent, done → emerald, error → rose),
//   - the active agent gets a "WORKING" / "DONE" / "BLOCKED" tag
//     pinned just above the desk so a glance tells the operator who
//     is on point right now.
//
// Drawn with CSS keyframes so we don't pull a motion library; respects
// prefers-reduced-motion by switching off animations and just showing
// the pip+tag.

const STATUS_TONE = {
  idle:             { color: "#94a3b8", label: "IDLE",     dim: 0.55 },
  working:          { color: null,      label: "WORKING",  dim: 1.0 },
  done:             { color: "#34d399", label: "DONE",     dim: 0.95 },
  error:            { color: "#f87171", label: "BLOCKED",  dim: 1.0 },
  blocked:          { color: "#f87171", label: "BLOCKED",  dim: 1.0 },
  waiting_approval: { color: "#a78bfa", label: "REVIEW",   dim: 0.95 },
};

function PresencePip({ color, working, failed, reducedMotion }) {
  const animation = reducedMotion
    ? "none"
    : failed
    ? "presence-warn 0.9s ease-in-out infinite"
    : working
    ? "presence-pulse 1.1s ease-in-out infinite"
    : "presence-soft 3.2s ease-in-out infinite";
  return (
    <span
      aria-hidden="true"
      style={{
        display: "inline-block",
        width: 8,
        height: 8,
        borderRadius: "50%",
        backgroundColor: color,
        boxShadow: `0 0 6px ${color}cc`,
        animation,
      }}
    />
  );
}

function StatusTag({ status, color, reducedMotion }) {
  const tone = STATUS_TONE[status] || STATUS_TONE.idle;
  const txt = tone.label;
  const accent = tone.color || color;
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1.5px 6px",
        backgroundColor: "#0a1228cc",
        border: `1px solid ${accent}88`,
        borderRadius: 3,
        fontSize: 8.5,
        fontWeight: 700,
        letterSpacing: "1.5px",
        color: accent,
        fontFamily: "ui-monospace, monospace",
        opacity: tone.dim,
        animation:
          reducedMotion || status !== "working"
            ? "none"
            : "presence-fade 2.4s ease-in-out infinite",
      }}
    >
      <span>·</span>
      <span>{txt}</span>
    </div>
  );
}

// Floating ambient particle near each desk — drifts upward slowly so
// the room feels lived-in even when nothing's working. Rendered only
// when motion is allowed.
function FloatingParticle({ color, delay, drift, reducedMotion }) {
  if (reducedMotion) return null;
  return (
    <span
      aria-hidden="true"
      style={{
        position: "absolute",
        left: `${50 + drift}%`,
        top: 0,
        width: 3,
        height: 3,
        borderRadius: "50%",
        backgroundColor: color,
        opacity: 0.55,
        boxShadow: `0 0 4px ${color}`,
        animation: `presence-float 5.4s ease-in-out ${delay}s infinite`,
      }}
    />
  );
}

export default function AgentPresenceLayer({ agentStatuses = {}, activeAgentId = null }) {
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const handler = (e) => setReducedMotion(e.matches);
    if (mq.addEventListener) mq.addEventListener("change", handler);
    else mq.addListener(handler);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener("change", handler);
      else mq.removeListener(handler);
    };
  }, []);

  return (
    <>
      {/* keyframes scoped to this layer so we don't pollute global CSS. */}
      <style>{`
        @keyframes presence-pulse {
          0%, 100% { transform: scale(1);   opacity: 0.95; }
          50%      { transform: scale(1.7); opacity: 0.4;  }
        }
        @keyframes presence-soft {
          0%, 100% { transform: scale(1);   opacity: 0.45; }
          50%      { transform: scale(1.25); opacity: 0.85; }
        }
        @keyframes presence-warn {
          0%, 100% { transform: scale(1);   opacity: 1;    }
          50%      { transform: scale(1.4); opacity: 0.4;  }
        }
        @keyframes presence-fade {
          0%, 100% { opacity: 0.7; }
          50%      { opacity: 1;   }
        }
        @keyframes presence-float {
          0%   { transform: translate(0, 0)    scale(1);    opacity: 0.0; }
          15%  { opacity: 0.55; }
          50%  { transform: translate(-3px, -22px) scale(0.95); opacity: 0.45; }
          85%  { opacity: 0.2; }
          100% { transform: translate(2px, -42px) scale(0.7);  opacity: 0;   }
        }
      `}</style>

      {AGENT_LIST.map((agent) => {
        const layout = DESK_LAYOUT[agent.id];
        if (!layout) return null;
        const status = agentStatuses[agent.id] || "idle";
        const isActive = activeAgentId === agent.id;
        const working = status === "working" || isActive;
        const failed = status === "error" || status === "blocked";
        const tone = STATUS_TONE[status] || STATUS_TONE.idle;
        const pipColor = failed
          ? "#f87171"
          : working
          ? agent.color
          : tone.color || "#94a3b8";

        return (
          <div
            key={`presence-${agent.id}`}
            className="absolute pointer-events-none"
            style={{
              left: layout.x,
              top: layout.y,
              transform: "translate(-50%, -50%)",
              zIndex: 11,
            }}
          >
            {/* status tag — pinned above the speech bubble area */}
            <div
              style={{
                position: "absolute",
                left: "50%",
                top: -78,
                transform: "translateX(-50%)",
                whiteSpace: "nowrap",
              }}
            >
              <StatusTag
                status={status}
                color={agent.color}
                reducedMotion={reducedMotion}
              />
            </div>

            {/* presence pip — small floating dot near the agent's head */}
            <div
              style={{
                position: "absolute",
                left: "50%",
                top: -22,
                transform: "translateX(-50%)",
              }}
            >
              <PresencePip
                color={pipColor}
                working={working}
                failed={failed}
                reducedMotion={reducedMotion}
              />
            </div>

            {/* a couple of ambient particles — only when working, so
                idle desks stay calm and busy desks feel alive. */}
            {working && !failed && (
              <div
                style={{
                  position: "absolute",
                  left: 0,
                  top: -10,
                  width: 80,
                  height: 60,
                  pointerEvents: "none",
                }}
              >
                <FloatingParticle
                  color={agent.color}
                  delay={0}
                  drift={-12}
                  reducedMotion={reducedMotion}
                />
                <FloatingParticle
                  color={agent.color}
                  delay={1.6}
                  drift={6}
                  reducedMotion={reducedMotion}
                />
                <FloatingParticle
                  color={agent.color}
                  delay={3.2}
                  drift={-2}
                  reducedMotion={reducedMotion}
                />
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}
