// Cycle log → synthetic System Log events.
//
// The runner can't directly emit per-cycle events through the API
// event_bus (only one LOCAL_RUNNER_RESULT_REPORTED per command), so
// it instead carries an array of structured cycle markers in its
// heartbeat metadata at:
//
//   metadata.local_factory.cycle_effectiveness.cycle_log[]
//     = [{ at, cycle, kind, message, payload? }, ...]
//
// The dashboard's SystemLogPanel reads from a flat events array. To
// keep the panel as the single source of "what happened on the
// runner side", we synthesize event-shaped objects from cycle_log[]
// and merge them with the API events. The eventClassifier already
// has keyword rules for "claude apply ...", "validation ...", "QA
// gate ..." and similar phrases so the synthetic events bucket into
// the right Claude / Build / QA / Runner chips automatically.
//
// Each synthetic event gets a stable, negative id (-1, -2, ...) keyed
// off cycle + kind + at, so SystemLogPanel's `key={ev.id}` doesn't
// collide with API events (which use positive ids from the DB).

const KIND_TO_TYPE = {
  claude_apply_started:           "agent_message",
  claude_apply_changed_files:     "agent_message",
  claude_apply_no_changes:        "agent_message",
  validation_started:             "agent_message",
  validation_passed:              "agent_message",
  validation_failed:              "agent_message",
  implementation_ticket_created:  "agent_message",
  implementation_ticket_missing:  "agent_message",
  frontend_files_changed:         "agent_message",
  backend_files_changed:          "agent_message",
  control_tower_files_changed:    "agent_message",
  cycle_planning_only:            "agent_message",
  cycle_produced_code_change:     "agent_message",
  cycle_produced_docs_only:       "agent_message",
  cycle_produced_no_code_change:  "agent_message",
  cycle_failed:                   "error",
  // FE-only synthetic kinds (handoff lifecycle from AgentRouteLayer +
  // deploy_progress.history transitions).
  handoff_started:                "agent_message",
  handoff_completed:              "agent_message",
  deploy_step:                    "agent_message",
  deploy_failed:                  "error",
  deploy_completed:               "agent_message",
};

// Stable id from the entry's content. Negative so it can't collide
// with DB-assigned positive ids on api events.
function entryId(entry) {
  const seed = `${entry.cycle || 0}|${entry.kind || ""}|${entry.at || ""}`;
  let h = 0;
  for (let i = 0; i < seed.length; i++) {
    h = (h * 31 + seed.charCodeAt(i)) | 0;
  }
  // Fold to a fixed negative range so React keys stay stable across renders.
  return -1 - (Math.abs(h) % 2_000_000_000);
}

// Promote the structured kind to a human-readable Korean tag so the
// keyword classifier can match (build / qa / claude / git / push).
const KIND_TO_PHRASE = {
  claude_apply_started:           "claude apply started",
  claude_apply_changed_files:     "claude apply changed files",
  claude_apply_no_changes:        "claude apply no changes",
  validation_started:             "validation started",
  validation_passed:              "validation passed",
  validation_failed:              "validation failed",
  implementation_ticket_created:  "implementation ticket created",
  implementation_ticket_missing:  "implementation ticket missing",
  frontend_files_changed:         "frontend files changed",
  backend_files_changed:          "backend files changed",
  control_tower_files_changed:    "control_tower files changed",
  cycle_planning_only:            "cycle planning only",
  cycle_produced_code_change:     "cycle produced code change",
  cycle_produced_docs_only:       "cycle produced docs only",
  cycle_produced_no_code_change:  "cycle produced no code change",
  cycle_failed:                   "cycle failed",
  handoff_started:                "handoff started",
  handoff_completed:              "handoff completed",
  deploy_step:                    "deploy step",
  deploy_failed:                  "deploy failed",
  deploy_completed:               "deploy completed",
};

function entryToEvent(entry) {
  const phrase = KIND_TO_PHRASE[entry.kind] || entry.kind;
  const baseMsg = entry.message || phrase;
  // Prepend the canonical English phrase when the message lacks it,
  // so the eventClassifier's keyword regex still picks it up.
  const message =
    baseMsg.toLowerCase().includes(phrase.split(" ")[0])
      ? baseMsg
      : `${phrase} — ${baseMsg}`;
  return {
    id: entryId(entry),
    type: KIND_TO_TYPE[entry.kind] || "agent_message",
    message,
    payload: {
      // Mark the source so SystemLog can future-render a "synthetic"
      // badge if the operator wants to filter to API-only.
      source: "cycle_log",
      kind: entry.kind,
      cycle: entry.cycle,
      ...(entry.payload || {}),
    },
    created_at: entry.at,
    agent_id: null,
    task_id: null,
  };
}

export function synthesizeCycleEvents(runners = []) {
  const out = [];
  for (const r of runners) {
    const log = r?.metadata_json?.local_factory?.cycle_effectiveness?.cycle_log;
    if (!Array.isArray(log) || log.length === 0) continue;
    for (const entry of log) {
      if (!entry || !entry.kind) continue;
      out.push(entryToEvent(entry));
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Deploy progress → System Log events.
//
// The runner heartbeat carries a structured `deploy_progress` block at
//   metadata.local_factory.publish.deploy_progress
// with a `history` array of step transitions. We turn each transition
// into a synthetic event so the operator can scroll back through:
//   "deploy started", "git push origin main 진행 중", "deploy failed at qa_gate", ...
// without having to read the raw JSON.
// ---------------------------------------------------------------------------

function pickDeployProgress(runners = []) {
  for (const r of runners) {
    const dp = r?.metadata_json?.local_factory?.publish?.deploy_progress;
    if (dp) return dp;
  }
  return null;
}

function deployStepKind(status) {
  if (status === "completed" || status === "actions_triggered") return "deploy_completed";
  if (status === "failed") return "deploy_failed";
  return "deploy_step";
}

function deployStepMessage(entry) {
  // Each history row carries `{ at, status, current_step?, failed_reason? }`.
  // We prefer the human label the runner already wrote.
  if (entry.failed_reason) {
    return `deploy failed (${entry.status}) — ${entry.failed_reason}`;
  }
  if (entry.current_step) {
    return `deploy step · ${entry.status} · ${entry.current_step}`;
  }
  return `deploy step · ${entry.status}`;
}

function deployEntryId(attemptId, idx) {
  // Stable hash from attempt + index so a re-render of the same heartbeat
  // doesn't churn React keys.
  const seed = `deploy|${attemptId || "anon"}|${idx}`;
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) | 0;
  return -1 - (Math.abs(h) % 2_000_000_000);
}

export function synthesizeDeployEvents(runners = []) {
  const dp = pickDeployProgress(runners);
  if (!dp || !Array.isArray(dp.history)) return [];
  const attemptId = dp.attempt_id || dp.command_id || "current";
  const out = [];
  dp.history.forEach((entry, idx) => {
    if (!entry || !entry.status) return;
    const kind = deployStepKind(entry.status);
    out.push({
      id: deployEntryId(attemptId, idx),
      type: kind === "deploy_failed" ? "error" : "agent_message",
      message: deployStepMessage(entry),
      payload: {
        source: "deploy_progress",
        attempt_id: attemptId,
        status: entry.status,
        current_step: entry.current_step,
        failed_reason: entry.failed_reason,
      },
      created_at: entry.at,
      agent_id: null,
      task_id: null,
    });
  });
  return out;
}

// ---------------------------------------------------------------------------
// Watchdog log → System Log events.
//
// The runner heartbeat carries a structured `watchdog.log` array at
//   metadata.local_factory.watchdog.log
// with entries like { at, kind, message, severity, diagnostic_code }.
// Each kind maps to one of the `watchdog ...` keyword phrases the
// eventClassifier recognizes (watchdog detected issue / watchdog auto
// repair started / watchdog escalated / watchdog healthy …).
//
// Stable negative ids per (kind, at, message) so React keys don't
// collide with API events or other synthetic streams.
// ---------------------------------------------------------------------------

const WATCHDOG_KIND_TO_PHRASE = {
  watchdog_check_started:           "watchdog check started",
  watchdog_check_completed:         "watchdog check completed",
  watchdog_detected_issue:          "watchdog detected issue",
  watchdog_auto_repair_started:     "watchdog auto repair started",
  watchdog_auto_repair_step:        "watchdog auto repair step",
  watchdog_auto_repair_completed:   "watchdog auto repair completed",
  watchdog_auto_repair_skipped:     "watchdog auto repair skipped",
  watchdog_escalated:               "watchdog escalated",
  watchdog_disabled:                "watchdog disabled",
  watchdog_healthy:                 "watchdog healthy",
  // Pipeline Recovery Orchestrator entries — the watchdog mirrors them
  // into its log so they surface in the same System Log feed.
  pipeline_stage_failed:            "Stage failed",
  pipeline_rollback:                "Rollback to stage",
  pipeline_repair_started:          "Repair action started",
  pipeline_repair_completed:        "Repair action completed",
  pipeline_operator_required:       "Operator required",
  pipeline_no_changes:              "No changes to validate",
  pipeline_tick_failed:             "Pipeline tick failed",
  // Forward Progress Detector — same channel.
  forward_progress_check_started:        "Forward progress check started",
  forward_progress_blocked:              "Forward progress blocked",
  forward_progress_stuck:                "Current stage stuck",
  forward_progress_required_output_missing: "Required output missing",
  forward_progress_planning_only:        "Planning only loop detected",
  forward_progress_no_code_change:       "No code change detected",
  forward_progress_no_progress:          "No progress despite heartbeat",
  forward_progress_continuous_stopped:   "Continuous stopped due to no progress",
  forward_progress_operator_required:    "Operator required",
  forward_progress_failed:               "Forward progress check failed",
};

function watchdogEntryId(entry) {
  const seed = `wd|${entry.kind || ""}|${entry.at || ""}|${(entry.message || "").slice(0, 32)}`;
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) | 0;
  return -1 - (Math.abs(h) % 2_000_000_000);
}

export function synthesizeWatchdogEvents(runners = []) {
  const out = [];
  for (const r of runners) {
    const log = r?.metadata_json?.local_factory?.watchdog?.log;
    if (!Array.isArray(log) || log.length === 0) continue;
    for (const entry of log) {
      if (!entry || !entry.kind) continue;
      const phrase = WATCHDOG_KIND_TO_PHRASE[entry.kind] || entry.kind;
      const baseMsg = entry.message || phrase;
      const lc = baseMsg.toLowerCase();
      const phraseLc = phrase.toLowerCase();
      // Skip the prefix if the message already starts with the phrase
      // OR mentions "watchdog" (legacy entries). Pipeline kinds use
      // English phrases like "Stage failed" / "Repair action started"
      // that the eventClassifier recognizes verbatim.
      const message =
        lc.includes("watchdog") || lc.startsWith(phraseLc)
          ? baseMsg
          : `${phrase} — ${baseMsg}`;
      out.push({
        id: watchdogEntryId(entry),
        type: entry.severity === "error" ? "error" : "agent_message",
        message,
        payload: {
          source: "watchdog",
          kind: entry.kind,
          severity: entry.severity,
          diagnostic_code: entry.diagnostic_code,
          ...(entry.payload || {}),
        },
        created_at: entry.at,
        agent_id: null,
        task_id: null,
      });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Operator Request log → System Log events.
//
// runner._h_operator_request now drives a structured event log via
// _op_emit() which lands at metadata.local_factory.operator_fix.log[].
// Each entry's `kind` and `message` are crafted so the existing
// eventClassifier keyword table (claude command started/completed,
// validation started/passed/failed, commit created, git push completed,
// operator_request) buckets them into Claude / Build / Git / Error.
// ---------------------------------------------------------------------------

const OPERATOR_KIND_TO_PHRASE = {
  operator_request_received:   "operator request received",
  factory_pause_requested:     "operator request — factory pause requested",
  factory_pause_confirmed:     "operator request — factory pause confirmed",
  operator_request_blocked:    "operator request blocked",
  claude_command_started:      "Claude command started",
  claude_command_completed:    "Claude command completed",
  claude_command_failed:       "Claude command failed",
  validation_started:          "validation started",
  validation_passed:           "validation passed",
  validation_failed:           "validation failed",
  commit_created:              "commit created",
  push_completed:              "git push completed",
  git_push_failed:             "git push failed",
  operator_request_no_changes: "operator request — no code change",
};

function operatorEntryId(entry) {
  const seed = `op|${entry.kind || ""}|${entry.at || ""}|${(entry.message || "").slice(0, 32)}`;
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) | 0;
  return -1 - (Math.abs(h) % 2_000_000_000);
}

export function synthesizeOperatorRequestEvents(runners = []) {
  const out = [];
  for (const r of runners) {
    const log = r?.metadata_json?.local_factory?.operator_fix?.log;
    if (!Array.isArray(log) || log.length === 0) continue;
    for (const entry of log) {
      if (!entry || !entry.kind) continue;
      const phrase = OPERATOR_KIND_TO_PHRASE[entry.kind] || entry.kind;
      const baseMsg = entry.message || phrase;
      // Make sure the canonical English keyword (claude command started,
      // validation passed, …) appears in the message so the keyword
      // classifier picks it up regardless of the runner's Korean tail.
      const lc = baseMsg.toLowerCase();
      const phraseLc = phrase.toLowerCase();
      const hasKeyword = phraseLc
        .split(/\s+/)
        .every((tok) => lc.includes(tok));
      const message = hasKeyword ? baseMsg : `${phrase} — ${baseMsg}`;
      out.push({
        id: operatorEntryId(entry),
        type: entry.severity === "error" ? "error" : "agent_message",
        message,
        payload: {
          source: "operator_request",
          kind: entry.kind,
          severity: entry.severity,
          diagnostic_code: entry.diagnostic_code,
          ...(entry.payload || {}),
        },
        created_at: entry.at,
        agent_id: null,
        task_id: null,
      });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// In-page handoff events. AgentRouteLayer calls back when a card
// starts/finishes its trip; ControlTowerPage funnels those through this
// helper before merging into the SystemLog.
// ---------------------------------------------------------------------------

let handoffSeq = 0;

export function makeHandoffEvent({ kind, from, to, label, banner, source }) {
  // We assign a fresh negative id per call — these aren't deduplicable
  // between renders (they're event-shaped notifications, not state).
  handoffSeq -= 1;
  const phrase = KIND_TO_PHRASE[kind] || kind;
  // Demo handoffs get a "[DEMO]" prefix so the operator can tell
  // apart "AI team really moved this" from "demo loop is rotating".
  const demoTag = source === "demo" ? "[DEMO] " : "";
  const message = banner
    ? `${demoTag}${phrase} — ${banner}`
    : `${demoTag}${phrase} — ${from} → ${to} (${label})`;
  return {
    id: handoffSeq,
    type: "agent_message",
    message,
    payload: {
      source: "handoff",
      kind,
      from,
      to,
      label,
      flow_source: source,
      is_demo: source === "demo",
    },
    created_at: new Date().toISOString(),
    agent_id: null,
    task_id: null,
  };
}
