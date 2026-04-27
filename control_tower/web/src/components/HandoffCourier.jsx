import { motion } from "framer-motion";
import HumanAgentCharacter from "./HumanAgentCharacter.jsx";
import ArtifactProp from "./ArtifactProp.jsx";

// A tiny "courier" copy of the sender that walks across the office floor
// to the receiver's seat with the artifact in hand, pauses briefly to
// hand it over, then disappears. No documents fly through the air.

const COURIER_W = 60;
const COURIER_H = 86;

const WALK_DURATION = 1.6;          // seconds spent walking
const HANDOFF_PAUSE = 0.45;          // seconds stopped at receiver's desk
const FADE_DURATION = 0.35;          // seconds fading out at the end
const TOTAL_DURATION = WALK_DURATION + HANDOFF_PAUSE + FADE_DURATION;

export default function HandoffCourier({ from, to, fromAgent, artifactType, onDone }) {
  // From/to are agent-center coordinates. We aim the courier's feet a bit
  // below center (where the chair would sit), and start them just to the
  // side of the sender so it reads like they "step out from their desk".
  const startX = from.x + 14;
  const startY = from.y + 10;
  const endX = to.x - 18;
  const endY = to.y + 10;

  // keyframe times
  const walkT = WALK_DURATION / TOTAL_DURATION;       // when walking ends
  const pauseT = (WALK_DURATION + HANDOFF_PAUSE) / TOTAL_DURATION;

  return (
    <motion.div
      className="absolute"
      style={{
        left: 0,
        top: 0,
        width: COURIER_W,
        height: COURIER_H,
        zIndex: 35,
        pointerEvents: "none",
      }}
      initial={{
        x: startX - COURIER_W / 2,
        y: startY - COURIER_H / 2,
        opacity: 0,
        scale: 0.85,
      }}
      animate={{
        x: [
          startX - COURIER_W / 2,
          endX - COURIER_W / 2,
          endX - COURIER_W / 2,
          endX - COURIER_W / 2,
        ],
        y: [
          startY - COURIER_H / 2,
          endY - COURIER_H / 2,
          endY - COURIER_H / 2,
          endY - COURIER_H / 2,
        ],
        opacity: [0, 1, 1, 0],
        scale: [0.85, 1, 1, 0.92],
      }}
      transition={{
        duration: TOTAL_DURATION,
        times: [0, walkT, pauseT, 1],
        ease: ["easeOut", "linear", "easeIn"],
      }}
      onAnimationComplete={onDone}
    >
      <div style={{ position: "relative", width: COURIER_W, height: COURIER_H }}>
        {/* The little person, in walking pose */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: 0,
            transform: "translateX(-50%)",
          }}
        >
          <HumanAgentCharacter
            look={fromAgent?.look}
            status="working"
            pose="walking"
            width={COURIER_W}
            height={COURIER_H}
            showStatusBadge={false}
          />
        </div>

        {/* Artifact held in front of the courier's chest */}
        <motion.div
          style={{
            position: "absolute",
            left: "50%",
            top: 32,
            transform: "translateX(-50%)",
          }}
          animate={{ y: [0, -1.5, 0] }}
          transition={{
            duration: 0.34,
            repeat: Infinity,
            ease: "easeInOut",
          }}
        >
          <ArtifactProp type={artifactType} size={28} />
        </motion.div>

        {/* "Delivering…" label so the user can read what's happening */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: -14,
            transform: "translateX(-50%)",
            fontSize: 9,
            color: "#e2e8f0",
            background: "rgba(15,23,42,0.85)",
            padding: "1px 6px",
            borderRadius: 4,
            whiteSpace: "nowrap",
            fontFamily: "ui-monospace, Menlo, monospace",
            letterSpacing: "0.5px",
            border: "1px solid rgba(125,211,252,0.4)",
          }}
        >
          전달 중
        </div>
      </div>
    </motion.div>
  );
}
