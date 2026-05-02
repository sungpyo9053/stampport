import { memo } from "react";

// AgentCharacter — pure-CSS pixel character with real body parts in
// the DOM. The verification spec calls for class names with the
// `pixel-agent-*` prefix so the grep regression test can confirm
// each part is actually rendered (head / body / arm / leg / speech /
// nameplate). Animations live in styles/index.css under the matching
// `pixel-agent-*` keyframe names.
//
// State classes (driven by `state` and `bubble` props):
//   is-active   → the agent the factory is currently progressing
//   is-talking  → has a non-null bubble; head wobbles + bubble pop
//   is-typing   → arms tap on the keyboard / clipboard
//   is-walking  → legs step (Deploy walking to the gate, etc.)
//   is-passed   → gold stamp animation
//   is-hold     → purple rework wobble
//   is-failed   → red shake
//   is-skipped  → low opacity, no animation
//
// Role-specific tools live as a small DOM node inside the figure so
// each role visually differs without importing an icon library:
//   PM        → clipboard
//   Planner   → triangle ruler
//   Designer  → palette dot row
//   Frontend  → tiny monitor frame
//   Backend   → wrench bar
//   AI        → chip pattern
//   QA        → magnifier loop
//   Deploy    → rocket fin

const ROLE_PRESETS = {
  pm: {
    label: "PM",
    role: "프로덕트 매니저",
    skin: "#fcd9b6",
    hair: "#1f2937",
    shirt: "#d4a843",
    pants: "#1c2540",
    accent: "#d4a843",
    tool: "clipboard",
  },
  planner: {
    label: "Planner",
    role: "기획자",
    skin: "#f9d4b8",
    hair: "#0f172a",
    shirt: "#7dd3fc",
    pants: "#0e1a35",
    accent: "#7dd3fc",
    tool: "ruler",
  },
  designer: {
    label: "Designer",
    role: "디자이너",
    skin: "#fdd4c2",
    hair: "#5b21b6",
    shirt: "#f472b6",
    pants: "#312e81",
    accent: "#f472b6",
    tool: "palette",
  },
  frontend: {
    label: "Frontend",
    role: "FE 엔지니어",
    skin: "#fcd9b6",
    hair: "#0f172a",
    shirt: "#38bdf8",
    pants: "#0a1228",
    accent: "#38bdf8",
    tool: "monitor",
  },
  backend: {
    label: "Backend",
    role: "BE 엔지니어",
    skin: "#fbd2a4",
    hair: "#1f2937",
    shirt: "#34d399",
    pants: "#0a1228",
    accent: "#34d399",
    tool: "wrench",
  },
  ai: {
    label: "AI",
    role: "AI Architect",
    skin: "#f9d4b8",
    hair: "#1e1b4b",
    shirt: "#a78bfa",
    pants: "#1e1b4b",
    accent: "#a78bfa",
    tool: "chip",
  },
  qa: {
    label: "QA",
    role: "QA 엔지니어",
    skin: "#fcd9b6",
    hair: "#1f2937",
    shirt: "#fb923c",
    pants: "#1c2540",
    accent: "#fb923c",
    tool: "magnifier",
  },
  deploy: {
    label: "Deploy",
    role: "배포 담당",
    skin: "#fbd2a4",
    hair: "#0f172a",
    shirt: "#facc15",
    pants: "#0e1a35",
    accent: "#facc15",
    tool: "rocket",
  },
};

function Tool({ kind, color }) {
  switch (kind) {
    case "clipboard":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-clipboard" aria-hidden>
          <span className="pixel-agent-tool-paper">
            <span className="pixel-agent-tool-line" />
            <span className="pixel-agent-tool-line" />
            <span className="pixel-agent-tool-line" />
          </span>
          <span className="pixel-agent-tool-clip" style={{ backgroundColor: color }} />
        </span>
      );
    case "ruler":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-ruler" aria-hidden>
          <span className="pixel-agent-tool-triangle" style={{ borderColor: color }} />
        </span>
      );
    case "palette":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-palette" aria-hidden>
          <span className="pixel-agent-tool-palette-base" />
          <span className="pixel-agent-tool-palette-dot" style={{ backgroundColor: "#f87171" }} />
          <span className="pixel-agent-tool-palette-dot" style={{ backgroundColor: "#34d399" }} />
          <span className="pixel-agent-tool-palette-dot" style={{ backgroundColor: "#7dd3fc" }} />
          <span className="pixel-agent-tool-palette-dot" style={{ backgroundColor: "#facc15" }} />
        </span>
      );
    case "monitor":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-monitor" aria-hidden>
          <span className="pixel-agent-tool-monitor-frame" style={{ borderColor: color }}>
            <span className="pixel-agent-tool-monitor-screen" />
          </span>
          <span className="pixel-agent-tool-monitor-stand" style={{ backgroundColor: color }} />
        </span>
      );
    case "wrench":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-wrench" aria-hidden>
          <span className="pixel-agent-tool-wrench-handle" style={{ backgroundColor: color }} />
          <span className="pixel-agent-tool-wrench-head" style={{ borderColor: color }} />
        </span>
      );
    case "chip":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-chip" aria-hidden>
          <span className="pixel-agent-tool-chip-body" style={{ backgroundColor: color }}>
            <span className="pixel-agent-tool-chip-core" />
          </span>
          <span className="pixel-agent-tool-chip-pin" />
          <span className="pixel-agent-tool-chip-pin" />
          <span className="pixel-agent-tool-chip-pin" />
          <span className="pixel-agent-tool-chip-pin" />
        </span>
      );
    case "magnifier":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-magnifier" aria-hidden>
          <span className="pixel-agent-tool-magnifier-ring" style={{ borderColor: color }} />
          <span className="pixel-agent-tool-magnifier-handle" style={{ backgroundColor: color }} />
        </span>
      );
    case "rocket":
      return (
        <span className="pixel-agent-tool pixel-agent-tool-rocket" aria-hidden>
          <span className="pixel-agent-tool-rocket-body" style={{ backgroundColor: color }} />
          <span className="pixel-agent-tool-rocket-fin" />
          <span className="pixel-agent-tool-rocket-flame" />
        </span>
      );
    default:
      return null;
  }
}

function AgentCharacter({
  agentId,
  state,                    // visual kind
  bubble,                   // { tone, text } | null  (for sr-only text only)
  isCurrent = false,
  isSelected = false,
  onClick,
}) {
  const preset = ROLE_PRESETS[agentId] || ROLE_PRESETS.pm;

  const stateClass = (() => {
    const classes = ["pixel-agent", `pixel-agent-${agentId}`];
    if (state === "running") classes.push("is-active", "is-typing");
    if (state === "walking") classes.push("is-walking");
    if (state === "passed") classes.push("is-passed");
    if (state === "rework") classes.push("is-hold");
    if (state === "failed") classes.push("is-failed");
    if (state === "skipped") classes.push("is-skipped");
    if (bubble) classes.push("is-talking");
    if (isCurrent) classes.push("is-current");
    if (isSelected) classes.push("is-selected");
    return classes.join(" ");
  })();

  const handleActivate = (e) => {
    e?.stopPropagation?.();
    onClick && onClick(agentId);
  };

  return (
    <button
      type="button"
      onClick={handleActivate}
      className={stateClass}
      data-agent-id={agentId}
      data-agent-state={state}
      data-testid={`pixel-agent-${agentId}`}
      aria-label={`${preset.label} 캐릭터 — ${state}`}
      style={{
        "--agent-skin":   preset.skin,
        "--agent-hair":   preset.hair,
        "--agent-shirt":  preset.shirt,
        "--agent-pants":  preset.pants,
        "--agent-accent": preset.accent,
      }}
    >
      <span className="pixel-agent-shadow" aria-hidden />

      {isSelected && (
        <span
          className="pixel-agent-select-ring"
          aria-hidden
          style={{ "--agent-ring": preset.accent }}
        />
      )}

      <span className="pixel-agent-figure" aria-hidden>
        <span className="pixel-agent-head">
          <span className="pixel-agent-hair" />
          <span className="pixel-agent-eye pixel-agent-eye-l" />
          <span className="pixel-agent-eye pixel-agent-eye-r" />
          <span className="pixel-agent-mouth" />
        </span>
        <span className="pixel-agent-body">
          <span className="pixel-agent-arm pixel-agent-arm-l" />
          <span className="pixel-agent-arm pixel-agent-arm-r" />
          <Tool kind={preset.tool} color={preset.accent} />
        </span>
        <span className="pixel-agent-legs">
          <span className="pixel-agent-leg pixel-agent-leg-l" />
          <span className="pixel-agent-leg pixel-agent-leg-r" />
        </span>
      </span>

      {/* Nameplate — verified by grep regression test (pixel-agent-nameplate). */}
      <span
        className="pixel-agent-nameplate"
        style={{
          color: preset.accent,
          borderColor: `${preset.accent}66`,
        }}
      >
        {preset.label}
      </span>

      {state === "passed" && (
        <span className="pixel-agent-stamp" aria-hidden>
          ✓
        </span>
      )}
      {state === "failed" && (
        <span className="pixel-agent-alert" aria-hidden>
          !
        </span>
      )}

      {bubble && (
        <span className="sr-only" data-testid={`pixel-agent-bubble-text-${agentId}`}>
          {bubble.text}
        </span>
      )}
    </button>
  );
}

export default memo(AgentCharacter);
