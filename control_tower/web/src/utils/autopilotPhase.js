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

// "Active cycle process exists" — multiple signals must agree before
// we'll claim work is in progress. ANY of the following → active:
//   * autopilot.active_cycle_index is set (loop has spawned smoke)
//   * autopilot.current_stage is set (loop is mid-cycle, even if the
//     stage label hasn't reached factory_state yet)
//   * autopilot.current_cycle_started_at without a matching
//     current_cycle_finished_at (smoke subprocess blocking)
//   * factory_state.status == "running"
//   * factory_smoke.status == "running"  OR  started_at without ended_at
//   * pipeline_recovery.current_stage / factory_state.current_stage
//   * runner heartbeat reports a live process flag
//
// This is the core fix for "STUCK · 첫 cycle 미시작 while real
// designer_critique was running". Previously hasActiveCycle missed
// the active_cycle_index signal and the smoke started_at/ended_at
// pair from factory_smoke_state.
export function hasActiveCycle(meta = {}) {
  const fs = meta.factory_state || {};
  const smoke = meta.factory_smoke || meta.smoke || {};
  const ap = meta.autopilot || {};
  const pr = meta.pipeline_recovery || {};

  if (ap.active_cycle_index != null && Number(ap.active_cycle_index) > 0) return true;
  if (ap.current_stage) return true;

  if (ap.current_cycle_started_at) {
    const started = Date.parse(ap.current_cycle_started_at);
    const finished = ap.current_cycle_finished_at
      ? Date.parse(ap.current_cycle_finished_at)
      : 0;
    if (Number.isFinite(started) && (!Number.isFinite(finished) || started > finished)) {
      return true;
    }
  }

  if (String(fs.status || "").toLowerCase() === "running") return true;
  if (String(smoke.status || "").toLowerCase() === "running") return true;
  if (smoke.started_at && !smoke.ended_at) return true;

  if (fs.current_stage || pr.current_stage) return true;

  // Optional process-alive flags from the runner heartbeat (added by
  // future runner work; today they're undefined and the early-exit
  // signals above are sufficient).
  if (ap.factory_smoke_process_alive || ap.cycle_process_alive
      || ap.claude_process_alive || ap.autopilot_process_alive) {
    return true;
  }
  return false;
}

// Display-cycle resolution — UI surfaces the *active* cycle index
// when a smoke subprocess is mid-flight, otherwise the count of
// finished cycles. With this, "CYCLE 1 / 5 · 실행 중" shows the
// moment the first smoke spawns instead of staying at 0/5 until
// the smoke returns.
export function deriveDisplayCycle(meta = {}) {
  const ap = meta.autopilot || {};
  const idx = ap.active_cycle_index;
  if (idx != null && Number(idx) > 0) {
    return { number: Number(idx), active: true };
  }
  return { number: Number(ap.cycle_count || 0), active: false };
}

// stuck_before_first_cycle — the diagnostic the user asked for.
// True when:
//   autopilot_state.status == "running"
//   cycle_count == 0
//   started_at > 180s ago
//   no active cycle subprocess
// The 180s window matches the user spec. Dashboard surfaces this as a
// red diagnostic card with operator next-actions.
export function deriveStuckDiagnostic(meta = {}) {
  const ap = meta.autopilot || {};
  if (String(ap.status || "").toLowerCase() !== "running") {
    return { stuck: false };
  }
  // active_cycle_index is the strongest "loop spawned a smoke" signal —
  // when it's set we never return stuck, even if the surrounding
  // metadata is stale. This is the fix for the false-positive STUCK
  // card that fired during real product_planning / designer_critique.
  if (ap.active_cycle_index != null && Number(ap.active_cycle_index) > 0) {
    return { stuck: false };
  }
  if (ap.current_stage) return { stuck: false };
  if ((Number(ap.cycle_count) || 0) > 0) return { stuck: false };
  if (hasActiveCycle(meta)) return { stuck: false };
  const startedAt = ap.started_at ? Date.parse(ap.started_at) : null;
  if (!startedAt || !Number.isFinite(startedAt)) return { stuck: false };
  const waitSec = Math.floor((Date.now() - startedAt) / 1000);
  if (waitSec < 180) return { stuck: false, wait_sec: waitSec };
  return {
    stuck: true,
    diagnostic_code: "autopilot_stuck_before_first_cycle",
    wait_sec: waitSec,
    started_at: ap.started_at,
    next_actions: [
      "runner log 확인 (.runtime/autopilot.log)",
      "autopilot_report.md 확인",
      "Stop 후 Restart 권장",
    ],
  };
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
    if (active) return "cycle_running";
    if (cycleCount === 0) {
      // Could still be normal "cycle 1 about to spawn" OR the
      // stuck-before-first-cycle case. derivePhase doesn't tell
      // them apart; deriveStuckDiagnostic does.
      return "starting";
    }
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

  // Priority: autopilot.current_stage (set by the heartbeat thread
  // around the smoke spawn) wins because it's the freshest signal,
  // then the runner-side recovery / state stages, then factory_state.
  const stageRaw =
    meta?.autopilot?.current_stage ||
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

// Resolve the autopilot's "current cycle number" — uses
// active_cycle_index while running (since cycle_count only ticks AFTER
// a cycle finishes), otherwise cycle_count. Exported so callers /
// tests can build the same comparison.
export function resolveCurrentCycleNumber(ap) {
  if (!ap) return null;
  const status = String(ap.status || "").toLowerCase();
  const live = status === "running" || status === "starting"
    || status === "stopping" || status === "restarting";
  if (live) {
    const idx = ap.active_cycle_index != null ? Number(ap.active_cycle_index) : 0;
    if (Number.isFinite(idx) && idx > 0) return idx;
  }
  if (ap.cycle_count != null) {
    const n = Number(ap.cycle_count);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

export function freshnessOf({ artifactCycleId, artifactAt, autopilot }) {
  const ap = autopilot || {};
  const apStarted = ap.started_at ? Date.parse(ap.started_at) : null;
  const status = String(ap.status || "").toLowerCase();
  const live = status === "running" || status === "starting"
    || status === "stopping" || status === "restarting";
  const currentCycle = resolveCurrentCycleNumber(ap);
  const apActive = ap.active_cycle_index != null ? Number(ap.active_cycle_index) : null;

  // Run-boundary check first — if the artifact predates the current
  // autopilot run, it's stale regardless of cycle_id. cycle_id values
  // are NOT globally unique across runs, so a previous run can leave
  // a higher cycle_id on disk that would otherwise look "fresh".
  if (artifactAt && Number.isFinite(apStarted)) {
    const at = Date.parse(artifactAt);
    if (Number.isFinite(at) && at < apStarted) {
      return "stale_artifact";
    }
  }

  // When the artifact has a cycle_id, compare to the active cycle
  // (during a run) or the finished cycle_count (after stop).
  if (artifactCycleId != null && currentCycle != null) {
    const id = Number(artifactCycleId);
    if (id === currentCycle) return "current_cycle";
    if (id < currentCycle) return "previous_cycle";
    // id > currentCycle. Default: stale (from a previous run with
    // higher counter). Conservative exception: autopilot mid-startup
    // with no active_cycle_index yet — treat as unknown so the UI
    // doesn't aggressively hide an artifact that may legitimately be
    // ahead of the not-yet-committed index.
    if (live && (apActive == null || apActive === 0)) return "unknown";
    return "stale_artifact";
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

// ---------------------------------------------------------------------------
// Pure derivation functions for AutoPilotPanel buttons + start payload.
// Lifted out of the component so verify-autopilot-ui.mjs can run a fixture
// matrix without booting React.
// ---------------------------------------------------------------------------

// Returns the button enable matrix and the labels they should show
// for a given phase / pending-flag combination. Pure function, no
// side effects, no React.
//
// Inputs:
//   phase           — one of PHASES (idle/starting/cycle_running/...)
//   stopRequested   — true while the operator's optimistic stop is
//                     awaiting heartbeat ack
//   restartInFlight — true while the operator's restart is between
//                     stop and start
//   cycleInFlight   — hasActiveCycle(meta); used so "stop" only says
//                     "현재 cycle 종료 후 정지" when there's a cycle
//                     to wait for
//   stuck           — deriveStuckDiagnostic(meta).stuck
//
// Returns: { canStart, canStop, canRestart, formLocked, startLabel,
//            stopLabel, restartLabel }
export function deriveButtonState({
  phase,
  stopRequested = false,
  restartInFlight = false,
  cycleInFlight = false,
  stuck = false,
  busy = false,
  hasRunner = true,
}) {
  const isRunning = phase === "cycle_running" || phase === "starting" || phase === "waiting_next_cycle";
  const isStopping = phase === "stopping";
  const isLocked = isRunning || isStopping || phase === "restarting";

  const canStart   = hasRunner && !busy && !isLocked && !stopRequested && !restartInFlight;
  const canStop    = hasRunner && !busy && (isRunning || isStopping) && !stopRequested;
  const canRestart = hasRunner && !busy && !restartInFlight;

  let stopLabel = "정지";
  if (stopRequested && cycleInFlight) stopLabel = "정지 요청 중 — 현재 cycle 종료 후 정지";
  else if (stopRequested) stopLabel = "정지 요청 중...";
  else if (isStopping) stopLabel = "현재 cycle 종료 후 정지";
  else if (phase === "stopped") stopLabel = "정지됨";

  let startLabel = "Auto Pilot 시작";
  if (busy) startLabel = "처리 중...";
  else if (isRunning) startLabel = "실행 중";
  else if (isStopping) startLabel = "정지 중";
  else if (phase === "restarting") startLabel = "재시작 중";

  const restartLabel = restartInFlight ? "재시작 진행 중" : "재시작";

  return {
    canStart,
    canStop,
    canRestart,
    formLocked: isLocked,
    startLabel,
    stopLabel,
    restartLabel,
    isRunning,
    isStopping,
  };
}

// Build the start_autopilot payload from a form draft. Single source
// of truth shared by AutoPilotPanel.handleStart/handleRestart and
// the verify-autopilot-ui.mjs fixture matrix. The matrix's job is to
// prove that for every (mode, max_cycles, max_hours, checkbox combo)
// the payload round-trips bit-for-bit and never silently downgrades
// to safe_run / stop_on_hold=true.
export function buildStartPayload(draft = {}) {
  const mode = draft.mode || "safe_run";
  return {
    autopilot_enabled: true,
    autopilot_mode: mode,
    mode,                                      // alias for back-compat
    max_cycles: Number(draft.maxCycles) || 5,
    max_hours: Number(draft.maxHours) || 6,
    stop_on_hold: !!draft.stopOnHold,
    require_scope_consistency: true,
    require_render_check: !!draft.requireRender,
    require_api_health: !!draft.requireHealth,
  };
}

// Effective form values when the panel is locked. Returns the
// runtime config from autopilot_state when locked, else the operator's
// draft. Pure — fixture-testable.
export function deriveEffectiveConfig({ phase, draft, autopilot }) {
  const isLocked = phase === "cycle_running" || phase === "starting"
    || phase === "waiting_next_cycle" || phase === "stopping" || phase === "restarting";
  const ap = autopilot || {};
  return {
    mode:          isLocked ? (ap.mode || draft.mode) : draft.mode,
    maxCycles:     isLocked ? (ap.max_cycles ?? draft.maxCycles) : draft.maxCycles,
    maxHours:      isLocked ? (ap.max_hours ?? draft.maxHours) : draft.maxHours,
    stopOnHold:    isLocked
      ? (ap.stop_on_hold !== undefined ? !!ap.stop_on_hold : draft.stopOnHold)
      : draft.stopOnHold,
    requireRender: isLocked
      ? (ap.require_render_check !== undefined ? !!ap.require_render_check : draft.requireRender)
      : draft.requireRender,
    requireHealth: isLocked
      ? (ap.require_api_health !== undefined ? !!ap.require_api_health : draft.requireHealth)
      : draft.requireHealth,
    locked: isLocked,
  };
}

// ---------------------------------------------------------------------------
// Agent supervisor blob freshness — used by AgentAccountabilityPanel and
// the verify-autopilot-ui matrix. Lives here (not in the panel) so
// node-side fixtures can import it without bringing JSX along.
// ---------------------------------------------------------------------------

function _pickAccountabilityTs(aa) {
  const candidates = [aa?.evaluated_at, aa?.updated_at, aa?.created_at];
  for (const ts of candidates) {
    if (!ts) continue;
    const v = Date.parse(ts);
    if (Number.isFinite(v)) return v;
  }
  return NaN;
}

// Returns:
//   "fresh"    — same cycle as the autopilot is currently on, OR no
//                autopilot run is active and accountability is recent.
//   "stale"    — cycle_id is from a previous autopilot cycle / earlier
//                run. The accountability shouldn't drive the main UI.
//   "unknown"  — can't tell (mid-startup before active_cycle_index is
//                committed, or no cycle_id and no timestamps).
//
// `runners` accepts either the runner-list shape (looks under
// metadata_json.local_factory.autopilot) or `{ autopilot }` for tests.
export function classifyAccountabilityFreshness(aa, runners = []) {
  if (!aa) return "unknown";
  const ap = (() => {
    if (Array.isArray(runners)) {
      for (const r of runners) {
        const a = r?.metadata_json?.local_factory?.autopilot;
        if (a) return a;
      }
      return null;
    }
    return runners?.autopilot || null;
  })();
  const accCycle = aa.cycle_id != null ? Number(aa.cycle_id) : null;
  const apStatus = String(ap?.status || "").toLowerCase();
  const apIsLive = apStatus === "running" || apStatus === "starting"
    || apStatus === "stopping" || apStatus === "restarting";
  const apStartedAt = ap?.started_at ? Date.parse(ap.started_at) : NaN;
  const accAt = _pickAccountabilityTs(aa);
  const currentCycle = resolveCurrentCycleNumber(ap);
  const apActive = ap?.active_cycle_index != null ? Number(ap.active_cycle_index) : null;

  // Run-boundary check: if the supervisor blob predates the current
  // autopilot run, it's stale regardless of cycle numbers. cycle_id
  // values aren't globally unique across runs.
  if (
    apIsLive
    && Number.isFinite(apStartedAt)
    && Number.isFinite(accAt)
    && accAt < apStartedAt
  ) {
    return "stale";
  }

  if (accCycle == null) {
    return apIsLive ? "unknown" : "fresh";
  }

  // Autopilot running but no cycle has been spawned yet AND the blob
  // claims a cycle_id ≥ 1 → blob is from a prior run.
  if (apIsLive && currentCycle === 0 && accCycle >= 1) {
    return "stale";
  }

  if (currentCycle == null) {
    return "fresh";
  }

  if (accCycle === currentCycle) return "fresh";
  if (accCycle < currentCycle) return "stale";
  // accCycle > currentCycle. Default: stale (blob from prior run with
  // higher cycle counter). Conservative exception: when autopilot is
  // mid-startup with no active_cycle_index yet, the heartbeat hasn't
  // committed cycle 1's index — treat as unknown, not stale.
  if (apIsLive && (apActive == null || apActive === 0)) return "unknown";
  return "stale";
}

export const FRESHNESS_LABEL = {
  current_run:    { label: "CURRENT RUN",      tone: "active",  color: "#34d399" },
  current_cycle:  { label: "CURRENT CYCLE",    tone: "active",  color: "#fbbf24" },
  previous_cycle: { label: "PREVIOUS CYCLE",   tone: "muted",   color: "#94a3b8" },
  stale_artifact: { label: "STALE ARTIFACT",   tone: "muted",   color: "#64748b" },
  unknown:        { label: "UNKNOWN",          tone: "neutral", color: "#94a3b8" },
};
