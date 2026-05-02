#!/usr/bin/env node
// Static verification — walks the source + built bundle and asserts
// every DOM hook / keyframe / payload field that the verification
// spec calls out. Runs in milliseconds, no browser needed; the
// Playwright spec is layered on top for real render screenshots.
//
// Exit code 0 = all checks PASS, 1 = at least one FAIL.

import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const REPO_ROOT = resolve(HERE, "..", "..", "..");
const RUNTIME_DIR = resolve(REPO_ROOT, ".runtime");

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    const st = statSync(p);
    if (st.isDirectory()) {
      if (entry === "node_modules" || entry === "dist" || entry === ".git") continue;
      out.push(...walk(p));
    } else if (entry.endsWith(".jsx") || entry.endsWith(".js") || entry.endsWith(".css")) {
      out.push(p);
    }
  }
  return out;
}

const sources = walk(join(ROOT, "src"));

function loadAll(paths) {
  return paths
    .map((p) => {
      try {
        return { path: p, body: readFileSync(p, "utf8") };
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

const docs = loadAll(sources);

function searchAny(needle) {
  return docs.filter((d) => d.body.includes(needle));
}

function searchRegex(re) {
  return docs.filter((d) => re.test(d.body));
}

const REQUIRED_DOM = [
  "pixel-office-scene",
  "pixel-office-floor",
  "office-desk",
  "office-monitor",
  "pixel-agent",
  "pixel-agent-head",
  "pixel-agent-body",
  "pixel-agent-arm",
  "pixel-agent-leg",
  "pixel-agent-speech",
  "pixel-agent-nameplate",
  "agent-detail-drawer",
];

const REQUIRED_KEYFRAMES = [
  "@keyframes pixel-agent-idle",
  "@keyframes pixel-agent-working",
  "@keyframes pixel-agent-walk",
  "@keyframes pixel-agent-talk",
  "@keyframes pixel-speech-pop",
  "@keyframes pixel-office-monitor-blink",
];

const REQUIRED_BEHAVIORS = [
  // Auto Pilot start payload — auto_publish must be wired through,
  // and stop_on_hold must be sent as a literal boolean.
  ["AutoPilotPanel exposes auto_publish mode", () => {
    const panel = docs.find((d) => d.path.endsWith("AutoPilotPanel.jsx"));
    if (!panel) return false;
    return /auto_publish/.test(panel.body);
  }],
  ["start payload sets autopilot_mode", () =>
    searchAny("autopilot_mode: draft.mode").length > 0
    || searchAny("autopilot_mode:").length > 0],
  ["start payload sets stop_on_hold", () =>
    searchAny("stop_on_hold: !!draft.stopOnHold").length > 0
    || searchAny("stop_on_hold:").length > 0],
  ["start payload sets require_render_check", () =>
    searchAny("require_render_check:").length > 0],
  ["start payload sets require_api_health", () =>
    searchAny("require_api_health:").length > 0],
  ["start payload sets require_scope_consistency", () =>
    searchAny("require_scope_consistency:").length > 0],
  // Restart button + Stop UX
  ["Restart button present", () =>
    searchAny('data-testid="autopilot-restart"').length > 0],
  ["Stop label handles cycle in flight", () =>
    searchAny("cycleInFlight").length > 0],
  // Agent Office structural pieces
  ["AgentOfficeScene renders 8 distinct agent ids", () => {
    const scene = docs.find((d) =>
      d.path.endsWith("AgentOfficeScene.jsx"),
    );
    if (!scene) return false;
    const ids = ["planner", "pm", "designer", "frontend", "backend", "ai", "qa", "deploy"];
    return ids.every((id) => scene.body.includes(`id: "${id}"`));
  }],
  ["AgentCharacter component exists", () =>
    docs.some((d) => d.path.endsWith("AgentCharacter.jsx"))],
  ["AgentSpeechBubble component exists", () =>
    docs.some((d) => d.path.endsWith("AgentSpeechBubble.jsx"))],
  ["AgentDetailDrawer has 7 required sections", () => {
    const drawer = docs.find((d) =>
      d.path.endsWith("AgentDetailDrawer.jsx"),
    );
    if (!drawer) return false;
    const required = [
      "현재 역할",
      "현재 작업",
      "마지막 명령",
      "최근 로그",
      "실패 원인",
      "다음 액션",
      "관련 파일 변경",
    ];
    return required.every((label) => drawer.body.includes(label));
  }],
  ["prefers-reduced-motion guard exists", () =>
    searchAny("prefers-reduced-motion: reduce").length > 0],
];

const failures = [];
const passes = [];

for (const dom of REQUIRED_DOM) {
  const hits = searchAny(dom);
  if (hits.length === 0) {
    failures.push({ kind: "DOM", needle: dom, reason: "not found in src/" });
  } else {
    passes.push({ kind: "DOM", needle: dom, files: hits.map((h) => h.path) });
  }
}

for (const kf of REQUIRED_KEYFRAMES) {
  const hits = searchAny(kf);
  if (hits.length === 0) {
    failures.push({ kind: "KEYFRAME", needle: kf, reason: "not found in src/" });
  } else {
    passes.push({ kind: "KEYFRAME", needle: kf, files: hits.map((h) => h.path) });
  }
}

for (const [name, fn] of REQUIRED_BEHAVIORS) {
  let ok = false;
  try {
    ok = !!fn();
  } catch (e) {
    ok = false;
  }
  if (ok) passes.push({ kind: "BEHAVIOR", needle: name });
  else failures.push({ kind: "BEHAVIOR", needle: name, reason: "predicate failed" });
}

const summary = {
  ok: failures.length === 0,
  pass_count: passes.length,
  fail_count: failures.length,
  failures,
  passes: passes.map((p) => ({
    kind: p.kind,
    needle: p.needle,
    file_count: (p.files || []).length,
  })),
  generated_at: new Date().toISOString(),
};

writeFileSync(
  join(RUNTIME_DIR, "ui_agent_office_static_verification.json"),
  JSON.stringify(summary, null, 2),
  "utf8",
);

if (failures.length === 0) {
  console.log(
    `[verify-agent-office] ${passes.length} checks PASS — wrote .runtime/ui_agent_office_static_verification.json`,
  );
  process.exit(0);
} else {
  console.error(
    `[verify-agent-office] ${failures.length} checks FAILED:`,
  );
  for (const f of failures) {
    console.error(`  · [${f.kind}] ${f.needle} — ${f.reason}`);
  }
  process.exit(1);
}
