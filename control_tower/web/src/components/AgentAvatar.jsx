import { motion } from "framer-motion";

// Tiny pixel-art character. Solid rectangles only, no gradients — the
// blockiness IS the point. Drawn in a 36x44 viewBox; the SVG is rendered
// pixel-perfect via `shape-rendering="crispEdges"`. The look-driven
// fields (skin, hair, shirt, accessory) come from constants/agents.js.
//
// pose:
//   "sitting"  — torso visible, hands resting on desk (default)
//   "standing" — full body (used in the HQ board, not desks)
//
// status:
//   "working" — pulsing color aura behind head
//   "done"    — tiny ✓ stamp on desk (handled by parent)
//   else      — calm idle bob

function HairFor({ style, color }) {
  // All hair is layered on top of the head block (head fills 12-22 wide,
  // 8-18 tall in our coordinate space). Each style is just a few rects.
  switch (style) {
    case "ponytail":
      return (
        <>
          <rect x="11" y="6"  width="14" height="5" fill={color} />
          <rect x="10" y="7"  width="2"  height="5" fill={color} />
          <rect x="24" y="7"  width="2"  height="5" fill={color} />
          {/* tail */}
          <rect x="25" y="9"  width="3"  height="6" fill={color} />
        </>
      );
    case "long":
      return (
        <>
          <rect x="11" y="6"  width="14" height="5" fill={color} />
          <rect x="10" y="7"  width="2"  height="11" fill={color} />
          <rect x="24" y="7"  width="2"  height="11" fill={color} />
        </>
      );
    case "curly":
      return (
        <>
          <rect x="10" y="5"  width="16" height="3" fill={color} />
          <rect x="9"  y="6"  width="2"  height="3" fill={color} />
          <rect x="25" y="6"  width="2"  height="3" fill={color} />
          <rect x="11" y="3"  width="3"  height="3" fill={color} />
          <rect x="16" y="2"  width="4"  height="3" fill={color} />
          <rect x="22" y="3"  width="3"  height="3" fill={color} />
        </>
      );
    case "bob":
      return (
        <>
          <rect x="11" y="6"  width="14" height="5" fill={color} />
          <rect x="10" y="7"  width="2"  height="6" fill={color} />
          <rect x="24" y="7"  width="2"  height="6" fill={color} />
        </>
      );
    case "beanie":
      return (
        <>
          <rect x="11" y="5"  width="14" height="4" fill={color} />
          <rect x="13" y="3"  width="10" height="2" fill={color} />
        </>
      );
    case "side":
    case "short":
    default:
      return (
        <>
          <rect x="11" y="6"  width="14" height="5" fill={color} />
          <rect x="10" y="7"  width="2"  height="3" fill={color} />
          <rect x="24" y="7"  width="2"  height="3" fill={color} />
        </>
      );
  }
}

function AccessoryFor({ accessory, accent }) {
  switch (accessory) {
    case "glasses":
      return (
        <>
          <rect x="13" y="13" width="4" height="3" fill="#0b1120" />
          <rect x="19" y="13" width="4" height="3" fill="#0b1120" />
          <rect x="17" y="14" width="2" height="1" fill="#0b1120" />
        </>
      );
    case "tie":
      return (
        <>
          <rect x="17" y="22" width="2" height="6" fill={accent} />
          <rect x="17" y="21" width="2" height="2" fill="#facc15" />
        </>
      );
    case "headphones":
      return (
        <>
          <rect x="10" y="8"  width="2" height="6" fill="#1f2937" />
          <rect x="24" y="8"  width="2" height="6" fill="#1f2937" />
          <rect x="11" y="6"  width="14" height="2" fill="#1f2937" />
        </>
      );
    case "beret":
      return (
        <>
          <rect x="11" y="4"  width="14" height="3" fill={accent} />
          <rect x="22" y="3"  width="3" height="2" fill={accent} />
        </>
      );
    case "pen":
      return <rect x="26" y="22" width="1" height="6" fill="#facc15" />;
    case "megaphone":
      return (
        <>
          <rect x="26" y="20" width="4" height="3" fill={accent} />
          <rect x="29" y="19" width="2" height="5" fill={accent} />
        </>
      );
    default:
      return null;
  }
}

export default function AgentAvatar({
  look,
  status = "idle",
  isActive = false,
  scale = 1,
}) {
  const working = status === "working" || isActive;
  const accent = look.shirt;

  return (
    <div
      className="relative"
      style={{ width: 36 * scale, height: 44 * scale }}
    >
      {/* working aura — sits behind the character */}
      {working && (
        <motion.div
          className="absolute inset-0 rounded-full blur-md"
          style={{ backgroundColor: accent }}
          initial={{ opacity: 0.15, scale: 0.9 }}
          animate={{ opacity: [0.15, 0.55, 0.15], scale: [0.9, 1.05, 0.9] }}
          transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
        />
      )}

      <motion.svg
        viewBox="0 0 36 44"
        width={36 * scale}
        height={44 * scale}
        shapeRendering="crispEdges"
        style={{ position: "relative", display: "block" }}
        animate={
          working
            ? { y: [0, -1, 0] }
            : { y: [0, -0.5, 0] }
        }
        transition={{
          duration: working ? 0.6 : 2.4,
          repeat: Infinity,
          ease: "easeInOut",
        }}
      >
        {/* shadow */}
        <ellipse cx="18" cy="42" rx="9" ry="1.4" fill="rgba(0,0,0,0.45)" />

        {/* torso (shirt) */}
        <rect x="11" y="22" width="14" height="11" fill={look.shirt} />
        {/* shirt highlight */}
        <rect x="11" y="22" width="14" height="1" fill="rgba(255,255,255,0.18)" />
        {/* arms — resting on desk */}
        <rect x="9"  y="24" width="2"  height="7" fill={look.shirt} />
        <rect x="25" y="24" width="2"  height="7" fill={look.shirt} />
        {/* hands */}
        <rect x="8"  y="30" width="3"  height="3" fill={look.skin} />
        <rect x="25" y="30" width="3"  height="3" fill={look.skin} />

        {/* neck */}
        <rect x="16" y="20" width="4" height="2" fill={look.skin} />

        {/* head */}
        <rect x="12" y="9"  width="12" height="11" fill={look.skin} />
        {/* head shading */}
        <rect x="12" y="9"  width="1"  height="11" fill="rgba(0,0,0,0.18)" />
        <rect x="23" y="9"  width="1"  height="11" fill="rgba(0,0,0,0.18)" />

        {/* eyes */}
        <rect x="14" y="14" width="2" height="2" fill="#0b1120" />
        <rect x="20" y="14" width="2" height="2" fill="#0b1120" />
        {/* mouth */}
        <rect x="16" y="18" width="4" height="1" fill="#0b1120" />

        {/* hair on top */}
        <HairFor style={look.hairStyle} color={look.hair} />

        {/* accessory layered last so it draws on top */}
        <AccessoryFor accessory={look.accessory} accent={accent} />

        {/* working: typing motion — small bob on hands */}
        {working && (
          <motion.g
            animate={{ y: [0, -1, 0, 1, 0] }}
            transition={{ duration: 0.5, repeat: Infinity }}
          >
            <rect x="8"  y="30" width="3" height="3" fill={look.skin} />
            <rect x="25" y="30" width="3" height="3" fill={look.skin} />
          </motion.g>
        )}
      </motion.svg>
    </div>
  );
}
