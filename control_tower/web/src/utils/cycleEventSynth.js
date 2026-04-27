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
