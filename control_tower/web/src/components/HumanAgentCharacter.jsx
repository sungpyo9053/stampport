import { motion } from "framer-motion";

// Tiny SD human character.
//
// The SVG is laid out in a fixed 80x110 viewBox, drawn from the head down.
// `pose="sitting"` (default) hides the legs and lets the desk visually clip
// the figure at the waist. `pose="walking"` shows full legs that swing.
//
// The arms are split into two motion groups so we can subtly animate them
// for `working` (typing) and `walking` (arm swing) without re-rendering
// every prop on every tick.

const DEFAULT_LOOK = {
  skin: "#f5d0b9",
  hair: "#1f2937",
  hairStyle: "short",
  shirt: "#1d4ed8",
  pants: "#1f2937",
  accessory: "none",
};

function Hair({ style, color }) {
  switch (style) {
    case "long":
      return (
        <g fill={color}>
          <path d="M22 22 Q22 6 40 6 Q58 6 58 22 L58 36 Q58 28 50 26 L30 26 Q22 28 22 36 Z" />
          <path d="M21 22 Q21 38 26 48 L24 48 Q18 38 18 24 Z" />
          <path d="M59 22 Q59 38 54 48 L56 48 Q62 38 62 24 Z" />
        </g>
      );
    case "ponytail":
      return (
        <g fill={color}>
          <path d="M24 22 Q24 8 40 8 Q56 8 56 22 L56 28 Q52 22 40 22 Q28 22 24 28 Z" />
          <path d="M55 24 Q66 28 64 42 Q62 50 58 46 Q60 36 54 30 Z" />
        </g>
      );
    case "bob":
      return (
        <path
          d="M22 22 Q22 6 40 6 Q58 6 58 22 L58 32 Q56 28 50 28 L30 28 Q24 28 22 32 Z"
          fill={color}
        />
      );
    case "curly":
      return (
        <g fill={color}>
          <circle cx="28" cy="14" r="6" />
          <circle cx="36" cy="10" r="6" />
          <circle cx="44" cy="10" r="6" />
          <circle cx="52" cy="14" r="6" />
          <circle cx="56" cy="20" r="5" />
          <circle cx="24" cy="20" r="5" />
        </g>
      );
    case "side":
      return (
        <path
          d="M22 22 Q22 6 40 6 Q58 6 58 22 L58 26 Q52 18 36 20 Q26 22 22 28 Z"
          fill={color}
        />
      );
    case "beanie":
      return null; // covered by accessory
    case "short":
    default:
      return (
        <path
          d="M24 20 Q24 8 40 8 Q56 8 56 20 L56 24 Q50 18 40 18 Q30 18 24 24 Z"
          fill={color}
        />
      );
  }
}

function Accessory({ type, look }) {
  switch (type) {
    case "glasses":
      return (
        <g
          stroke="#0f172a"
          strokeWidth="1.2"
          fill="rgba(148,163,184,0.25)"
          strokeLinejoin="round"
        >
          <rect x="29" y="22" width="8" height="6" rx="2" />
          <rect x="43" y="22" width="8" height="6" rx="2" />
          <line x1="37" y1="25" x2="43" y2="25" />
        </g>
      );
    case "headphones":
      return (
        <g>
          <path
            d="M22 22 Q22 6 40 6 Q58 6 58 22"
            fill="none"
            stroke="#0f172a"
            strokeWidth="2.5"
            strokeLinecap="round"
          />
          <rect x="18" y="20" width="6" height="9" rx="2" fill="#0f172a" />
          <rect x="56" y="20" width="6" height="9" rx="2" fill="#0f172a" />
        </g>
      );
    case "beret":
      return (
        <g>
          <ellipse cx="40" cy="9" rx="14" ry="5" fill="#dc2626" />
          <circle cx="46" cy="6" r="2" fill="#dc2626" />
        </g>
      );
    case "beanie":
      return (
        <g>
          <path
            d="M22 18 Q22 4 40 4 Q58 4 58 18 L58 22 L22 22 Z"
            fill={look.hair || "#0f172a"}
          />
          <rect x="22" y="20" width="36" height="4" fill="#1e293b" />
        </g>
      );
    case "tie":
      return (
        <g>
          <path d="M37 36 L43 36 L41 44 L39 44 Z" fill="#dc2626" />
          <path d="M38 44 L42 44 L43 56 L40 60 L37 56 Z" fill="#b91c1c" />
        </g>
      );
    case "pen":
      return (
        <g>
          <rect
            x="64"
            y="58"
            width="3"
            height="11"
            rx="1"
            fill="#facc15"
            transform="rotate(15 65 63)"
          />
          <rect
            x="63"
            y="56"
            width="3"
            height="3"
            fill="#0f172a"
            transform="rotate(15 64 57)"
          />
        </g>
      );
    case "megaphone":
      return (
        <g>
          <path d="M58 60 L70 56 L70 70 L58 66 Z" fill="#facc15" stroke="#92400e" strokeWidth="0.8" />
          <rect x="56" y="62" width="3" height="4" fill="#92400e" />
        </g>
      );
    default:
      return null;
  }
}

const STATUS_OVERLAY = {
  done: { glyph: "✓", bg: "#10b981" },
  blocked: { glyph: "!", bg: "#f43f5e" },
  error: { glyph: "!", bg: "#dc2626" },
  waiting_approval: { glyph: "?", bg: "#a855f7" },
};

export default function HumanAgentCharacter({
  look = DEFAULT_LOOK,
  status = "idle",
  pose = "sitting",
  isActive = false,
  width = 80,
  height = 110,
  showStatusBadge = true,
}) {
  const overlay = showStatusBadge ? STATUS_OVERLAY[status] : null;

  // Body breathing animation (gentle on whole figure)
  const bodyAnim =
    pose === "walking"
      ? { y: [0, -1.5, 0] }
      : status === "working"
      ? { y: [0, -0.6, 0] }
      : { y: [0, -1.2, 0] };
  const bodyDur = pose === "walking" ? 0.34 : status === "working" ? 0.9 : 2.6;

  // Arm/typing animation when working
  const leftArmAnim =
    status === "working"
      ? { rotate: [-2, 6, -2] }
      : pose === "walking"
      ? { rotate: [-12, 14, -12] }
      : { rotate: [0, 1.5, 0] };
  const rightArmAnim =
    status === "working"
      ? { rotate: [4, -4, 4] }
      : pose === "walking"
      ? { rotate: [14, -12, 14] }
      : { rotate: [0, -1.5, 0] };
  const armDur = status === "working" ? 0.45 : pose === "walking" ? 0.34 : 3.0;

  // Leg swing for walking
  const leftLegAnim = pose === "walking" ? { rotate: [12, -12, 12] } : { rotate: 0 };
  const rightLegAnim = pose === "walking" ? { rotate: [-12, 12, -12] } : { rotate: 0 };

  return (
    <div
      style={{ width, height, position: "relative", pointerEvents: "none" }}
    >
      <motion.svg
        viewBox="0 0 80 110"
        width={width}
        height={height}
        animate={bodyAnim}
        transition={{ duration: bodyDur, repeat: Infinity, ease: "easeInOut" }}
        style={{
          overflow: "visible",
          filter: isActive
            ? "drop-shadow(0 0 8px rgba(125, 211, 252, 0.85))"
            : "drop-shadow(0 4px 4px rgba(0,0,0,0.45))",
        }}
      >
        {/* ground shadow under feet */}
        {pose === "walking" && (
          <ellipse cx="40" cy="104" rx="16" ry="2.6" fill="rgba(0,0,0,0.35)" />
        )}

        {/* legs (only walking pose) */}
        {pose === "walking" && (
          <>
            <motion.g
              animate={leftLegAnim}
              transition={{ duration: armDur, repeat: Infinity, ease: "easeInOut" }}
              style={{ transformBox: "fill-box", transformOrigin: "34px 70px" }}
            >
              <rect x="30" y="68" width="9" height="26" rx="3" fill={look.pants} />
              <ellipse cx="34.5" cy="98" rx="6" ry="3" fill="#1f2937" />
            </motion.g>
            <motion.g
              animate={rightLegAnim}
              transition={{ duration: armDur, repeat: Infinity, ease: "easeInOut" }}
              style={{ transformBox: "fill-box", transformOrigin: "46px 70px" }}
            >
              <rect x="41" y="68" width="9" height="26" rx="3" fill={look.pants} />
              <ellipse cx="45.5" cy="98" rx="6" ry="3" fill="#1f2937" />
            </motion.g>
          </>
        )}

        {/* torso (rounded shirt) */}
        <path
          d="M22 72 Q22 40 30 38 L50 38 Q58 40 58 72 Z"
          fill={look.shirt}
          stroke="rgba(15,23,42,0.4)"
          strokeWidth="0.8"
        />
        {/* neck */}
        <rect x="36" y="34" width="8" height="6" fill={look.skin} />

        {/* tie / accessory that sits on torso */}
        {(look.accessory === "tie") && (
          <Accessory type="tie" look={look} />
        )}

        {/* arms — drawn before head so hands look like they reach forward */}
        <motion.g
          animate={leftArmAnim}
          transition={{ duration: armDur, repeat: Infinity, ease: "easeInOut" }}
          style={{ transformBox: "fill-box", transformOrigin: "24px 40px" }}
        >
          <rect x="18" y="40" width="9" height="26" rx="4" fill={look.shirt} />
          <circle cx="22.5" cy="68" r="4.2" fill={look.skin} />
        </motion.g>
        <motion.g
          animate={rightArmAnim}
          transition={{ duration: armDur, repeat: Infinity, ease: "easeInOut" }}
          style={{ transformBox: "fill-box", transformOrigin: "56px 40px" }}
        >
          <rect x="53" y="40" width="9" height="26" rx="4" fill={look.shirt} />
          <circle cx="57.5" cy="68" r="4.2" fill={look.skin} />
        </motion.g>

        {/* head */}
        <ellipse cx="40" cy="22" rx="14.5" ry="15.5" fill={look.skin} />
        {/* small ear */}
        <ellipse cx="25.5" cy="24" rx="2" ry="3" fill={look.skin} />
        <ellipse cx="54.5" cy="24" rx="2" ry="3" fill={look.skin} />

        {/* hair */}
        <Hair style={look.hairStyle} color={look.hair} />

        {/* face — simple black dots */}
        <circle cx="34" cy="24" r="1.4" fill="#0f172a" />
        <circle cx="46" cy="24" r="1.4" fill="#0f172a" />
        {/* mouth — mood from status */}
        {status === "blocked" || status === "error" ? (
          <path
            d="M36 30 Q40 27 44 30"
            stroke="#0f172a"
            strokeWidth="1.1"
            fill="none"
            strokeLinecap="round"
          />
        ) : (
          <path
            d="M36 29 Q40 32 44 29"
            stroke="#0f172a"
            strokeWidth="1.1"
            fill="none"
            strokeLinecap="round"
          />
        )}
        {/* small cheek tint */}
        <circle cx="32" cy="27" r="2" fill="#fb7185" opacity="0.35" />
        <circle cx="48" cy="27" r="2" fill="#fb7185" opacity="0.35" />

        {/* head accessories (over hair) */}
        {(look.accessory === "glasses" ||
          look.accessory === "headphones" ||
          look.accessory === "beret" ||
          look.accessory === "beanie") && (
          <Accessory type={look.accessory} look={look} />
        )}

        {/* hand-held accessory */}
        {(look.accessory === "pen" || look.accessory === "megaphone") && (
          <Accessory type={look.accessory} look={look} />
        )}

        {/* working "typing dots" indicator above head */}
        {status === "working" && pose === "sitting" && (
          <g>
            {[0, 1, 2].map((i) => (
              <motion.circle
                key={i}
                cx={32 + i * 6}
                cy={3}
                r={1.6}
                fill="#fbbf24"
                animate={{ opacity: [0.2, 1, 0.2] }}
                transition={{
                  duration: 0.9,
                  repeat: Infinity,
                  delay: i * 0.18,
                }}
              />
            ))}
          </g>
        )}
      </motion.svg>

      {overlay && (
        <motion.div
          key={status}
          initial={{ scale: 0, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ type: "spring", stiffness: 360, damping: 18 }}
          style={{
            position: "absolute",
            top: -2,
            right: 6,
            width: 18,
            height: 18,
            borderRadius: 9,
            background: overlay.bg,
            color: "white",
            fontSize: 12,
            fontWeight: 700,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "0 2px 6px rgba(0,0,0,0.55)",
            border: "1.5px solid rgba(15,23,42,0.9)",
          }}
        >
          {overlay.glyph}
        </motion.div>
      )}
    </div>
  );
}
