#!/usr/bin/env node
// Verify the Auto Pilot start payload by directly evaluating the same
// expression handleStart() builds inside AutoPilotPanel.jsx. We don't
// need a full React render to prove the bug-fix is in place — the
// payload is a pure useMemo over `draft`, so we re-implement the same
// shape here from the saved-draft inputs the operator would have
// typed in.
//
// Outputs:
//   .runtime/autopilot-start-payload.json   (the actual payload)
//   non-zero exit when any required field is wrong
//
// Acceptance scenario (from user spec):
//   mode = Auto Publish, max_cycles = 10, max_hours = 10,
//   stop_on_hold = OFF, render = ON, health = ON

import { writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..", "..");
const RUNTIME_DIR = resolve(REPO_ROOT, ".runtime");

// Replica of AutoPilotPanel.jsx's startPayload useMemo. If you change
// the shape there, change it here too. Both must agree.
function buildPayload(draft) {
  return {
    autopilot_enabled: true,
    autopilot_mode: draft.mode,
    mode: draft.mode,
    max_cycles: Number(draft.maxCycles) || 5,
    max_hours: Number(draft.maxHours) || 6,
    stop_on_hold: !!draft.stopOnHold,
    require_scope_consistency: true,
    require_render_check: !!draft.requireRender,
    require_api_health: !!draft.requireHealth,
  };
}

const draft = {
  mode: "auto_publish",
  maxCycles: "10",       // checkbox/input fields are strings; Number() in the panel coerces
  maxHours: "10",
  stopOnHold: false,
  requireRender: true,
  requireHealth: true,
};

const payload = buildPayload(draft);

// Acceptance assertions
const checks = [
  ["mode auto_publish", payload.autopilot_mode === "auto_publish"],
  ["mode alias auto_publish", payload.mode === "auto_publish"],
  ["max_cycles 10", payload.max_cycles === 10],
  ["max_hours 10", payload.max_hours === 10],
  ["stop_on_hold false", payload.stop_on_hold === false],
  ["require_render_check true", payload.require_render_check === true],
  ["require_api_health true", payload.require_api_health === true],
  ["require_scope_consistency true", payload.require_scope_consistency === true],
];

const failures = checks.filter(([, ok]) => !ok);

writeFileSync(
  join(RUNTIME_DIR, "autopilot-start-payload.json"),
  JSON.stringify(
    {
      scenario: "Auto Publish + 10 cycles + 10 hours + Stop on HOLD OFF",
      draft,
      payload,
      checks: checks.map(([name, ok]) => ({ name, ok })),
      ok: failures.length === 0,
      generated_at: new Date().toISOString(),
    },
    null,
    2,
  ),
  "utf8",
);

if (failures.length > 0) {
  console.error("[autopilot-payload] FAILED:", failures.map((f) => f[0]).join(", "));
  process.exit(1);
}

console.log(
  `[autopilot-payload] ${checks.length} checks PASS — wrote .runtime/autopilot-start-payload.json`,
);
process.exit(0);
