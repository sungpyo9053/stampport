// autopilotPhase — single shared helper that every Auto Pilot / Agent
// Office surface reads from. Centralizing this is what kills the
// "Frontend WORKING during cycle 0" bug: deriveCurrentAgent and
// deriveAgentVisual now both compute a freshness verdict from the
// SAME phase machine, so a stale agent_accountability blob from a
// previous cycle can never paint a fresh BLOCKED · FRONTEND chip.
//
// Heartbeat shape this depends on (set by runner.py):
//   meta.local_factory.autopilot       — autopilot_state.json
//   meta.local_factory.factory_state   — factory_state.json snapshot
//   meta.local_factory.factory_smoke   — factory_smoke_state.json
//   meta.local_factory.agent_accountability — supervisor report
//   meta.local_factory.pipeline_recovery / pipeline_state — stage hints

// ---------------------------------------------------------------------------
// Phase enum
// ---------------------------------------------------------------------------
//
// idle                 — autopilot has never run / status idle
// starting             — start command queued; status running but no cycle yet
// cycle_running        — factory_smoke / cycle is mid-flight
// waiting_next_cycle   — autopilot is on but no active cycle process
// stopping             — stop requested; cycle finishing
// stopped              — autopilot torn down
// restarting           — operator clicked Restart; we're between stop and start
// failed               — last verdict failed / stop_reason set
//
// Surface label (Korean) is also returned because every panel uses
// the same wording.

export const PHASES = {
  idle:               { label: "대기",            tone: "neutral", english: "IDLE" },
  starting:           { label: "시작 중",         tone: "active",  english: "STARTING" },
  cycle_running:      { label: "사이클 실행 중",   tone: "active",  english: "CYCLE RUNNING" },
  waiting_next_cycle: { label: "다음 사이클 시작 대기 중", tone: "neutral", english: "WAITING NEXT CYCLE" },
  stopping:           { label: "정지 중",         tone: "warn",    english: "STOPPING" },
  stopped:            { label: "정지됨",          tone: "neutral", english: "STOPPED" },
  restarting:         { label: "재시작 중",       tone: "active",  english: "RESTARTING" },
  failed:             { label: "실패",            tone: "error",   english: "FAILED" },
};

// Mode marketing label.
export const MODE_BADGE = {
  safe_run:     { label: "SAFE RUN",     warn: true,  note: "배포 안 함" },
  auto_commit:  { label: "AUTO COMMIT",  warn: false, note: "commit 만" },
  auto_publish: { label: "AUTO PUBLISH", warn: false, note: "commit + push + health 활성" },
};

// ---------------------------------------------------------------------------
// Phase derivation
// ---------------------------------------------------------------------------

export function pickAutopilot(runners = []) {
  for (const r of runners) {
    const ap = r?.metadata_json?.local_factory?.autopilot;
    if (ap) return ap;
  }
  return null;
}

export function pickRunnerMeta(runners = []) {
  for (const r of runners) {
    const lf = r?.metadata_json?.local_factory;
    if (lf) return lf;
  }
  return {};
}

// "Active cycle process exists" — three independent signals must agree
// before we'll claim work is in progress. Otherwise an old factory_smoke
// timestamp can paint Frontend as WORKING long after the loop went idle.
export function hasActiveCycle(meta = {}) {
  const fs = meta.factory_state || {};
  const smoke = meta.factory_smoke || meta.smoke || {};
  const fsRunning = String(fs.status || "").toLowerCase() === "running";
  const smokeRunning = String(smoke.status || "").toLowerCase() === "running";
  const stagePresent = !!(fs.current_stage || meta?.pipeline_recovery?.current_stage);
  return fsRunning || smokeRunning || stagePresent;
}

export function derivePhase(meta = {}, opts = {}) {
  const ap = meta.autopilot || {};
  const status = String(ap.status || "idle").toLowerCase();
  const cycleCount = Number(ap.cycle_count || 0);
  const lastVerdict = String(ap.last_verdict || "").toUpperCase();
  const lastFailure = String(ap.last_failure_code || "").trim();
  const stopReason = String(ap.stop_reason || "").trim();
  const active = hasActiveCycle(meta);

  // restartInFlight is a UI-side override — set by AutoPilotPanel
  // while the operator's restart click is between stop and start.
  if (opts.restartInFlight) return "restarting";

  if (status === "failed") return "failed";
  if (status === "stopped" || status === "idle") {
    if (lastVerdict === "FAIL" || lastFailure || stopReason) return "stopped";
    if (cycleCount > 0 && lastVerdict !== "FAIL" && !lastFailure) return "stopped";
    return cycleCount > 0 ? "stopped" : "idle";
  }
  if (status === "stopping") return "stopping";

  if (status === "running") {
    // stop requested on the UI side — we render stopping until the
    // heartbeat acknowledges.
    if (opts.stopRequested) return "stopping";
    if (cycleCount === 0 && !active) return "starting";
    if (active) return "cycle_running";
    // status=running but no active cycle process — autopilot loop is
    // alive but between cycles.
    return "waiting_next_cycle";
  }
  return "idle";
}

// Helpful boolean wrappers — keeps callers from string-comparing
// phase names everywhere.
export function isRunningPhase(phase) {
  return phase === "starting" || phase === "cycle_running" || phase === "waiting_next_cycle";
}

// ---------------------------------------------------------------------------
// Stage → agent mapping
//
// EVERYTHING that says "agent X is working" should go through
// stageToWorkingAgent(meta) so the rules stay consistent across the
// office scene, the drawer, and the system log.
// ---------------------------------------------------------------------------

export const STAGE_TO_AGENT = {
  product_planning:       "planner",
  planner_proposal:       "planner",
  planner_revision:       "planner",
  designer_critique:      "designer",
  designer_final_review:  "designer",
  pm_decision:            "pm",
  implementation_ticket:  "pm",
  // claude_apply is special — refined by changed_files routing below.
  claude_apply:           null,
  validation_qa:          "qa",
  qa_gate:                "qa",
  syntax_check:           "qa",
  render_check:           "qa",
  commit:                 "deploy",
  push:                   "deploy",
  github_actions:         "deploy",
  health_check:           "deploy",
};

function routeClaudeApply(meta) {
  const fs = meta.factory_state || {};
  const aa = meta.agent_accountability || {};
  const files = []
    .concat(Array.isArray(fs.claude_apply_changed_files) ? fs.claude_apply_changed_files : [])
    .concat(Array.isArray(fs.implementation_ticket_target_files) ? fs.implementation_ticket_target_files : [])
    .concat(Array.isArray(aa.changed_files) ? aa.changed_files : [])
    .map(String);
  const hasFE = files.some((p) => p.startsWith("app/web/") || p.startsWith("control_tower/web/"));
  const hasBE = files.some((p) => p.startsWith("app/api/") || p.startsWith("control_tower/local_runner/") || p.startsWith("control_tower/api/"));
  const hasAI = files.some((p) =>
    p.includes("ai_") || p.includes("kick_point") || p.includes("recommendation") || p.includes("agents/") || p.startsWith("app/ai/"),
  );
  if (hasAI && !hasFE && !hasBE) return "ai";
  if (hasBE && !hasFE) return "backend";
  if (hasFE) return "frontend";
  // No file routing yet — implementation_ticket may have been written
  // but claude_apply hasn't started actual edits. Default to PM until
  // claude actually picks up.
  return null;
}

// Returns the single agent id that should currently be in the WORKING
// state, or null when no agent should be marked as WORKING. Crucially
// this NEVER falls through to agent_accountability.blocking_agent —
// that field is for diagnosing past failures, not driving live state.
export function stageToWorkingAgent(meta = {}, phase = null) {
  // No active cycle → nobody is working. This is the rule that fixes
  // "Frontend WORKING during cycle_count=0".
  if (phase && !isRunningPhase(phase)) return null;
  if (!hasActiveCycle(meta)) return null;

  const stageRaw =
    meta?.pipeline_recovery?.current_stage ||
    meta?.pipeline_state?.current_stage ||
    meta?.factory_state?.current_stage ||
    null;
  if (!stageRaw) return null;
  const stage = String(stageRaw).toLowerCase();
  if (stage === "claude_apply") return routeClaudeApply(meta);
  if (stage in STAGE_TO_AGENT) return STAGE_TO_AGENT[stage];
  return null;
}

// ---------------------------------------------------------------------------
// Freshness verdict for a stored artifact / accountability blob.
//
// Returns one of:
//   "current_run"        — produced this autopilot run
//   "current_cycle"      — produced this autopilot cycle
//   "previous_cycle"     — older autopilot cycle, this run
//   "stale_artifact"     — predates this autopilot run
//   "unknown"            — no timestamps available
// ---------------------------------------------------------------------------

export function freshnessOf({ artifactCycleId, artifactAt, autopilot }) {
  const ap = autopilot || {};
  const apStarted = ap.started_at ? Date.parse(ap.started_at) : null;
  const apCycle = ap.cycle_count != null ? Number(ap.cycle_count) : null;

  // When agent_accountability writes cycle_id, we can compare directly.
  if (artifactCycleId != null && apCycle != null) {
    if (Number(artifactCycleId) === apCycle) return "current_cycle";
    if (Number(artifactCycleId) < apCycle) return "previous_cycle";
    return "current_cycle";
  }

  if (artifactAt && apStarted) {
    const at = Date.parse(artifactAt);
    if (Number.isFinite(at)) {
      // Within 5 minutes of "now" feels like the current cycle
      // even when no cycle_id is recorded.
      const now = Date.now();
      if (at >= apStarted && (now - at) < 5 * 60 * 1000) return "current_cycle";
      if (at >= apStarted) return "current_run";
      return "stale_artifact";
    }
  }
  return "unknown";
}

export const FRESHNESS_LABEL = {
  current_run:    { label: "CURRENT RUN",      tone: "active",  color: "#34d399" },
  current_cycle:  { label: "CURRENT CYCLE",    tone: "active",  color: "#fbbf24" },
  previous_cycle: { label: "PREVIOUS CYCLE",   tone: "muted",   color: "#94a3b8" },
  stale_artifact: { label: "STALE ARTIFACT",   tone: "muted",   color: "#64748b" },
  unknown:        { label: "UNKNOWN",          tone: "neutral", color: "#94a3b8" },
};
